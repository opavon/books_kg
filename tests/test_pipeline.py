import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.enrich.wikidata_enrich import (
    DUE_AUTHORS_QUERY,
    DUE_BOOKS_QUERY,
    DUE_SUMMARIES_QUERY,
    JsonHttpClient,
    OpenLibraryClient,
    RECONCILE_AUTHOR_MATCH_QUERY,
    RECONCILE_BOOK_MATCH_QUERY,
    WikidataClient,
    build_argument_parser,
    claim_entity_ids,
    confirm_author_candidate,
    confirm_book_candidate,
    filter_review_rows,
    labels_for_properties,
    load_review_report,
    normalize_text,
    parse_key_qid,
    prompt_author_candidate,
    prompt_book_candidate,
    prompt_openlibrary_candidate,
    run_enrichment,
    year_from_claims,
    write_author,
    write_book,
    write_review_report,
)
from src.ingest.load_books import (
    EXISTING_QUERY,
    UPSERT_QUERY,
    load_rows,
    resolve_source_csv,
    upsert_batch,
)


class FakeResult(list):
    def consume(self):
        return None


class FakeDate:
    def __init__(self, value):
        self.value = value

    def iso_format(self):
        return self.value


class InMemoryTransaction:
    def __init__(self):
        self.records = {}

    def run(self, query, **parameters):
        if query == EXISTING_QUERY:
            return FakeResult(
                [
                    {
                        "book_id": book_id,
                        "title": record["title"],
                        "date": FakeDate(record["date"]),
                        "score": record["score"],
                        "title_read": record.get("title_read"),
                        "language_read": record.get("language_read"),
                        "authors": record["authors"],
                    }
                    for book_id in parameters["book_ids"]
                    if (record := self.records.get(book_id)) is not None
                ]
            )
        if query == UPSERT_QUERY:
            for row in parameters["rows"]:
                previous = self.records.get(row["book_id"], {})
                self.records[row["book_id"]] = {
                    "title": row["title"],
                    "date": row["date"],
                    "score": row["score"],
                    "title_read": row["title_read"],
                    "language_read": row["language_read"],
                    "authors": set(previous.get("authors", set())) | set(row["authors"]),
                }
            return FakeResult()
        raise AssertionError(f"Unexpected query: {query}")


class FakeWikidataClient(WikidataClient):
    def __init__(self, results, entities):
        super().__init__(None)
        self.results = results
        self.entity_data = entities
        self.search_calls = []
        self.entity_calls = []

    def search(self, label):
        self.search_calls.append(label)
        return self.results

    def entities(self, entity_ids):
        self.entity_calls.append(list(entity_ids))
        return self.entity_data


class FakeOpenLibraryHttp:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        return next(self.responses)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.payload


class RecordingTransaction:
    def __init__(self):
        self.calls = []

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        return FakeResult()


class AuthorEnrichmentSession:
    def __init__(self, author):
        self.author = author
        self.transaction = RecordingTransaction()

    def run(self, query, **parameters):
        if "MATCH (b:Book)" in query:
            return FakeResult()
        if "MATCH (a:Author)" in query:
            return FakeResult([self.author])
        raise AssertionError(f"Unexpected query: {query}")

    def execute_write(self, callback, *arguments):
        result = callback(self.transaction, *arguments)
        query, parameters = self.transaction.calls[-1]
        if "MATCH (a:Author {name: $name})" in query and parameters["wikidata_id"]:
            self.author["wikidata_id"] = parameters["wikidata_id"]
        return result


class BookEnrichmentSession:
    def __init__(self, book):
        self.book = book
        self.transaction = RecordingTransaction()

    def run(self, query, **parameters):
        if "MATCH (b:Book)" in query:
            return FakeResult([self.book])
        if "MATCH (a:Author)" in query:
            return FakeResult()
        raise AssertionError(f"Unexpected query: {query}")

    def execute_write(self, callback, *arguments):
        result = callback(self.transaction, *arguments)
        record = arguments[0]
        if isinstance(record, dict) and record.get("wikidata_id"):
            self.book["wikidata_id"] = record["wikidata_id"]
        return result


class EnrichmentSession:
    def __init__(self):
        self.transaction = RecordingTransaction()

    def run(self, query, **parameters):
        if "MATCH (b:Book)" in query:
            return FakeResult(
                [
                    {
                        "book_id": "0001",
                        "title": "A Novel",
                        "wikidata_id": None,
                        "summary": None,
                        "authors": [{"name": "An Author", "wikidata_id": None}],
                    }
                ]
            )
        if "MATCH (a:Author)" in query:
            return FakeResult()
        raise AssertionError(f"Unexpected query: {query}")

    def execute_write(self, callback, *arguments):
        return callback(self.transaction, *arguments)


class TargetedEnrichmentSession:
    def __init__(self, authors=None, books=None, summaries=None):
        self.authors = authors or []
        self.books = books or []
        self.summaries = summaries or []
        self.transaction = RecordingTransaction()
        self.query_calls = []

    def run(self, query, **parameters):
        self.query_calls.append((query, parameters))
        if query == DUE_AUTHORS_QUERY:
            return FakeResult(self.authors)
        if query == DUE_BOOKS_QUERY:
            return FakeResult(self.books)
        if query == DUE_SUMMARIES_QUERY:
            return FakeResult(self.summaries)
        raise AssertionError(f"Unexpected query: {query}")

    def execute_write(self, callback, *arguments):
        return callback(self.transaction, *arguments)


class MatchedBookWikidataClient(FakeWikidataClient):
    def resolve(self, label, entity_type, known_id=None, related_author_ids=None):
        return "matched", "Q_BOOK", self.entity_data["Q_BOOK"], []


class PipelineTests(unittest.TestCase):
    def test_source_csv_path_supports_relative_absolute_and_missing_values(self):
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(
            resolve_source_csv("data/raw/books.csv"),
            project_root / "data" / "raw" / "books.csv",
        )

        absolute_path = Path(__file__).resolve()
        self.assertEqual(resolve_source_csv(str(absolute_path)), absolute_path)

        with self.assertRaisesRegex(ValueError, "BOOKS_CSV_PATH"):
            resolve_source_csv("")

    def parse_csv(self, content):
        temporary = tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8", delete=False)
        try:
            temporary.write(content)
            temporary.close()
            return load_rows(Path(temporary.name))
        finally:
            Path(temporary.name).unlink(missing_ok=True)

    def test_csv_preserves_id_decimals_and_splits_authors(self):
        rows = self.parse_csv(
            "book_id,title,title_read,language_read,author,year_month,score\n"
            "0007,Example,Read title,en,A / B,2024_02,8.5\n"
        )
        self.assertEqual(rows[0]["book_id"], "0007")
        self.assertEqual(rows[0]["score"], 8.5)
        self.assertEqual(rows[0]["title_read"], "Read title")
        self.assertEqual(rows[0]["language_read"], "en")
        self.assertEqual(rows[0]["authors"], ["A", "B"])
        self.assertEqual(rows[0]["date"], "2024-02-01")

    def test_csv_trims_and_requires_read_title_and_language(self):
        rows = self.parse_csv(
            "book_id,title,title_read,language_read,author,year_month,score\n"
            "1,Canonical,  Read title  , en ,Author,2024_01,8\n"
        )
        self.assertEqual(rows[0]["title_read"], "Read title")
        self.assertEqual(rows[0]["language_read"], "en")

        with self.assertRaisesRegex(ValueError, "title_read is blank"):
            self.parse_csv(
                "book_id,title,title_read,language_read,author,year_month,score\n"
                "1,Canonical, ,en,Author,2024_01,8\n"
            )
        with self.assertRaisesRegex(ValueError, "language_read is blank"):
            self.parse_csv(
                "book_id,title,title_read,language_read,author,year_month,score\n"
                "1,Canonical,Read title, ,Author,2024_01,8\n"
            )

    def test_csv_reports_duplicate_ids_bad_dates_and_scores(self):
        with self.assertRaisesRegex(ValueError, "line 3: duplicate book_id"):
            self.parse_csv(
                "book_id,title,title_read,language_read,author,year_month,score\n"
                "001,One,One,en,A,2024_01,8\n"
                "001,Two,Two,en,B,2024_13,nan\n"
            )

    def test_csv_rejects_empty_author_segments(self):
        with self.assertRaisesRegex(ValueError, "nonblank names"):
            self.parse_csv("book_id,title,title_read,language_read,author,year_month,score\n1,Title,Title,en,A//B,2024_01,8\n")

    def test_csv_rejects_float_overflow(self):
        with self.assertRaisesRegex(ValueError, "finite number"):
            self.parse_csv("book_id,title,title_read,language_read,author,year_month,score\n1,Title,Title,en,A,2024_01,1e10000\n")

    def test_csv_requires_all_columns(self):
        with self.assertRaisesRegex(ValueError, "missing required columns: score"):
            self.parse_csv("book_id,title,title_read,language_read,author,year_month\n1,Title,Title,en,A,2024_01\n")

        with self.assertRaisesRegex(ValueError, "missing required columns: language_read"):
            self.parse_csv("book_id,title,title_read,author,year_month,score\n1,Title,Title,A,2024_01,8\n")

    def test_csv_rejects_duplicate_headers(self):
        with self.assertRaisesRegex(ValueError, "duplicate column headers"):
            self.parse_csv("book_id,title,title_read,language_read,author,year_month,score,score\n")

    def test_review_report_round_trip_and_filters(self):
        rows = [
            {"type": "author", "key": "Author A", "label": "Author A", "status": "needs_review", "reason": "", "candidates": '[{"id":"Q1"}]'},
            {"type": "book", "key": "0001", "label": "Book A", "status": "no_candidate", "reason": "", "candidates": "[]"},
            {"type": "book_summary", "key": "0001", "label": "Book A", "status": "ambiguous", "reason": "", "candidates": '[{"id":"/works/OL1W"}]'},
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "review.csv"
            write_review_report(report_path, rows)
            loaded = load_review_report(report_path)

        self.assertEqual(loaded[0]["candidates"], [{"id": "Q1"}])
        selected = filter_review_rows(
            loaded,
            entity_types=["book_summary"],
            summary_statuses=["ambiguous"],
            book_ids=["0001"],
        )
        self.assertEqual([(row["type"], row["key"]) for row in selected], [("book_summary", "0001")])
        selected = filter_review_rows(loaded, statuses=["no_candidate"], entity_types=["book"])
        self.assertEqual([(row["type"], row["status"]) for row in selected], [("book", "no_candidate")])
        selected = filter_review_rows(loaded, author_names=["Author A"])
        self.assertEqual([(row["type"], row["key"]) for row in selected], [("author", "Author A")])
        selected = filter_review_rows(loaded, book_ids=["0001"])
        self.assertEqual({row["type"] for row in selected}, {"book", "book_summary"})
        selected = filter_review_rows(loaded, author_names=["Author A"], book_ids=["0001"])
        self.assertEqual(
            {(row["type"], row["key"]) for row in selected},
            {("author", "Author A"), ("book", "0001"), ("book_summary", "0001")},
        )

    def test_cli_exposes_targeted_review_operations(self):
        args = build_argument_parser().parse_args(
            [
                "--interactive",
                "--from-report", "review.csv",
                "--entity-type", "book_summary",
                "--summary-status", "ambiguous",
                "--book-id", "0001",
                "--refresh-summaries",
            ]
        )
        self.assertTrue(args.interactive)
        self.assertEqual(args.from_report, Path("review.csv"))
        self.assertEqual(args.entity_type, ["book_summary"])
        self.assertEqual(args.summary_status, ["ambiguous"])
        self.assertEqual(args.book_ids, ["0001"])
        self.assertTrue(args.refresh_summaries)
        self.assertEqual(parse_key_qid(["Author A=Q123"], "--manual-author-qid"), {"Author A": "Q123"})
        with self.assertRaisesRegex(ValueError, "KEY=QID"):
            parse_key_qid(["0001=not-a-qid"], "--manual-book-qid")

    def test_selected_status_is_passed_to_only_selected_entity_queue(self):
        author = {"name": "Author", "wikidata_id": None}
        client = FakeWikidataClient([], {})
        session = TargetedEnrichmentSession(authors=[author])

        run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            entity_types={"author"},
            author_names=["Author"],
            statuses=["no_candidate"],
        )

        self.assertEqual(len(session.query_calls), 1)
        query, parameters = session.query_calls[0]
        self.assertEqual(query, DUE_AUTHORS_QUERY)
        self.assertEqual(parameters["author_names"], ["Author"])
        self.assertEqual(parameters["statuses"], ["no_candidate"])
        self.assertTrue(parameters["force_selected"])

    def test_author_target_does_not_force_all_summaries_in_mixed_queue(self):
        session = TargetedEnrichmentSession(
            authors=[{"name": "Author", "wikidata_id": None}],
            summaries=[{"book_id": "0001", "title": "Book", "authors": ["Author"]}],
        )

        run_enrichment(
            session,
            FakeWikidataClient([], {}),
            OpenLibraryClient(FakeOpenLibraryHttp([])),
            refresh=False,
            stale_days=180,
            limit=None,
            entity_types={"author", "book_summary"},
            author_names=["Author"],
        )

        summary_call = next(call for call in session.query_calls if call[0] == DUE_SUMMARIES_QUERY)
        self.assertFalse(summary_call[1]["force_selected"])
        self.assertIsNone(summary_call[1]["summary_book_ids"])

    def test_no_description_selection_is_stored_as_matched_with_work_id(self):
        open_library = OpenLibraryClient(
            FakeOpenLibraryHttp(
                [
                    {"docs": [{"key": "/works/OL1W", "title": "Title", "author_name": ["Author"]}]},
                    {},
                ]
            )
        )
        session = TargetedEnrichmentSession(
            summaries=[{"book_id": "0001", "title": "Title", "authors": ["Author"], "openlibrary_id": None}]
        )
        prior_row = {
            "type": "book_summary",
            "key": "0001",
            "label": "Title",
            "status": "ambiguous",
            "reason": "",
            "candidates": [{"id": "/works/OL1W", "label": "Title", "description": "Author", "matched": True}],
        }

        counts, reviews = run_enrichment(
            session,
            FakeWikidataClient([], {}),
            open_library,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: "1",
            entity_types={"book_summary"},
            book_ids=["0001"],
            report_rows=[prior_row],
        )

        _, parameters = session.transaction.calls[0]
        self.assertEqual(parameters["openlibrary_id"], "/works/OL1W")
        self.assertIsNone(parameters["summary"])
        self.assertEqual(parameters["status"], "matched")
        self.assertNotIn("previous_openlibrary_id", parameters)
        self.assertEqual(counts["summary_matched"], 1)
        self.assertEqual(reviews, [])
        self.assertIn("b.openlibrary_status = 'error'", DUE_SUMMARIES_QUERY)
        self.assertIn(
            "OR b.openlibrary_status = 'error'\n        OR (b.summary IS NULL AND b.openlibrary_id IS NULL)",
            DUE_SUMMARIES_QUERY,
        )

        session.summaries[0].update(
            openlibrary_id="/works/OL1W",
            openlibrary_status="matched",
        )
        next_counts, next_reviews = run_enrichment(
            session,
            FakeWikidataClient([], {}),
            open_library,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: self.fail("a selected work without a description must not prompt again"),
            entity_types={"book_summary"},
            book_ids=["0001"],
        )
        self.assertEqual(next_counts["summaries_checked"], 0)
        self.assertEqual(next_reviews, [])

    def test_csv_rejects_malformed_quoted_fields_with_line_number(self):
        malformed_examples = (
            '1,"A "quoted" title",A title,en,Author,2024_01,8\n',
            '0148,"Lovesickness in the Middle Ages: The "Viaticum" and Its Commentaries",'
            '"Lovesickness in the Middle Ages: The "Viaticum" and Its Commentaries",'
            'en,Author,2012_10,6\n',
        )
        for malformed_row in malformed_examples:
            with self.subTest(row=malformed_row[:20]), self.assertRaisesRegex(ValueError, "Malformed CSV near line 2"):
                self.parse_csv(
                    'book_id,title,title_read,language_read,author,year_month,score\n'
                    + malformed_row
                )

    def test_http_client_applies_random_interval_and_positive_jitter(self):
        current_time = [0.0]
        sleeps = []
        random_values = iter([5.0, 0.42])

        def fake_sleep(seconds):
            sleeps.append(seconds)
            current_time[0] += seconds

        client = JsonHttpClient(
            "test-agent",
            opener=lambda request, timeout: FakeResponse(b"{}"),
            uniform=lambda lower, upper: next(random_values),
            monotonic=lambda: current_time[0],
            sleeper=fake_sleep,
        )
        client.get_json("https://example.invalid/first")
        client.get_json("https://example.invalid/second")
        self.assertEqual(sleeps, [5.42])

    def test_http_client_rejects_invalid_interval_and_jitter_bounds(self):
        with self.assertRaisesRegex(ValueError, "request interval bounds"):
            JsonHttpClient("test-agent", request_interval_min=10, request_interval_max=5)
        with self.assertRaisesRegex(ValueError, "jitter bounds"):
            JsonHttpClient("test-agent", jitter_min=2, jitter_max=1)

    def test_reimport_is_idempotent_and_detects_updates(self):
        transaction = InMemoryTransaction()
        row = {
            "book_id": "0001",
            "title": "Title",
            "title_read": "Title as read",
            "language_read": "en",
            "authors": ["A", "B"],
            "date": "2024-01-01",
            "score": 8.5,
        }
        self.assertEqual(upsert_batch(transaction, [row]), {"inserted": 1, "updated": 0, "unchanged": 0})
        self.assertEqual(upsert_batch(transaction, [row]), {"inserted": 0, "updated": 0, "unchanged": 1})
        changed = {**row, "score": 9.0}
        self.assertEqual(upsert_batch(transaction, [changed]), {"inserted": 0, "updated": 1, "unchanged": 0})
        changed_title_read = {**changed, "title_read": "Alternate read title"}
        self.assertEqual(
            upsert_batch(transaction, [changed_title_read]),
            {"inserted": 0, "updated": 1, "unchanged": 0},
        )
        changed_language_read = {**changed_title_read, "language_read": "fr"}
        self.assertEqual(
            upsert_batch(transaction, [changed_language_read]),
            {"inserted": 0, "updated": 1, "unchanged": 0},
        )
        self.assertEqual(
            upsert_batch(transaction, [changed_language_read]),
            {"inserted": 0, "updated": 0, "unchanged": 1},
        )
        stored = transaction.records["0001"]
        self.assertEqual(stored["title"], "Title")
        self.assertEqual(stored["title_read"], "Alternate read title")
        self.assertEqual(stored["language_read"], "fr")
        self.assertIn("MERGE (b:Book {book_id: row.book_id})", UPSERT_QUERY)
        self.assertIn("MERGE (r:Read {book_id: row.book_id})", UPSERT_QUERY)

    def test_reimport_backfills_read_fields_on_existing_records(self):
        transaction = InMemoryTransaction()
        transaction.records["0001"] = {
            "title": "Canonical title",
            "date": "2024-01-01",
            "score": 8.5,
            "authors": {"Author"},
        }
        row = {
            "book_id": "0001",
            "title": "Canonical title",
            "title_read": "Title as read",
            "language_read": "en",
            "authors": ["Author"],
            "date": "2024-01-01",
            "score": 8.5,
        }

        self.assertEqual(
            upsert_batch(transaction, [row]),
            {"inserted": 0, "updated": 1, "unchanged": 0},
        )
        self.assertEqual(transaction.records["0001"]["title_read"], "Title as read")
        self.assertEqual(transaction.records["0001"]["language_read"], "en")

    def test_wikidata_accepts_one_exact_human_only(self):
        person = {
            "labels": {"en": {"value": "Ada Lovelace"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q1"}], {"Q1": person})
        status, entity_id, entity, _ = client.resolve("Ada Lovelace", "author")
        self.assertEqual((status, entity_id), ("matched", "Q1"))
        self.assertIs(entity, person)

    def test_wikidata_reports_ambiguous_exact_candidates(self):
        person = {
            "labels": {"en": {"value": "Alex Smith"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q1"}, {"id": "Q2"}], {"Q1": person, "Q2": person})
        status, entity_id, _, _ = client.resolve("Alex Smith", "author")
        self.assertEqual((status, entity_id), ("needs_review", None))

    def test_wikidata_routes_single_nonexact_human_to_review(self):
        person = {
            "labels": {"en": {"value": "Christopher Paolini"}},
            "descriptions": {"en": {"value": "author, novelist"}},
            "aliases": {"en": [{"value": "Christopher James Paolini"}]},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
        }
        non_person = {"labels": {"en": {"value": "Christopher Paolini"}}, "claims": {}}
        client = FakeWikidataClient([{"id": "Q1"}, {"id": "Q2"}], {"Q1": person, "Q2": non_person})

        status, entity_id, _, candidates = client.resolve("Christopher James Paolini", "author")

        self.assertEqual((status, entity_id), ("needs_review", None))
        self.assertEqual([candidate["id"] for candidate in candidates], ["Q1"])
        self.assertEqual(candidates[0]["aliases"], ["Christopher James Paolini"])

    def test_wikidata_reports_no_candidate_when_no_human_is_returned(self):
        non_person = {"labels": {"en": {"value": "Ada Lovelace"}}, "claims": {}}
        client = FakeWikidataClient([{"id": "Q1"}], {"Q1": non_person})

        status, entity_id, _, candidates = client.resolve("Ada Lovelace", "author")

        self.assertEqual((status, entity_id, candidates), ("no_candidate", None, []))

    def test_wikidata_ignores_nonhuman_results_when_exact_human_exists(self):
        person = {
            "labels": {"en": {"value": "Ada Lovelace"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q1"}, {"id": "Q2"}], {"Q1": person, "Q2": {"claims": {}}})

        status, entity_id, _, candidates = client.resolve("Ada Lovelace", "author")

        self.assertEqual((status, entity_id), ("matched", "Q1"))
        self.assertEqual([candidate["id"] for candidate in candidates], ["Q1"])

    def test_interactive_author_selection_is_validated_and_persisted(self):
        person = {
            "labels": {"en": {"value": "Christopher Paolini"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q1"}], {"Q1": person})
        author = {"name": "Christopher James Paolini", "wikidata_id": None}
        session = AuthorEnrichmentSession(author)

        counts, reviews = run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: "1",
        )

        author_update = session.transaction.calls[0][1]
        self.assertEqual(counts["matched"], 1)
        self.assertEqual(reviews, [])
        self.assertEqual(author_update["wikidata_id"], "Q1")
        self.assertEqual(author_update["status"], "matched")

        accepted = confirm_author_candidate("Q1", [{"id": "Q1"}], client)
        skipped = confirm_author_candidate(None, [{"id": "Q1"}], client)
        invalid = confirm_author_candidate("Q_OUTSIDE_LIST", [{"id": "Q1"}], client)
        self.assertEqual(accepted, ("Q1", person))
        self.assertIsNone(skipped)
        self.assertIsNone(invalid)
        self.assertEqual(author["wikidata_id"], "Q1")

        next_counts, next_reviews = run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: self.fail("a stored author ID should not prompt"),
        )
        self.assertEqual(next_counts["matched"], 1)
        self.assertEqual(next_reviews, [])
        self.assertEqual(client.search_calls, ["Christopher James Paolini"])

    def test_interactive_author_skip_keeps_candidate_unresolved(self):
        person = {
            "labels": {"en": {"value": "Christopher Paolini"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q1"}], {"Q1": person})
        session = AuthorEnrichmentSession({"name": "Christopher James Paolini", "wikidata_id": None})

        counts, reviews = run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: "",
        )

        author_update = session.transaction.calls[0][1]
        self.assertEqual(counts["unresolved"], 1)
        self.assertEqual(author_update["wikidata_id"], None)
        self.assertEqual(author_update["status"], "needs_review")
        self.assertEqual(reviews[0]["status"], "needs_review")

    def test_author_prompt_rejects_out_of_range_choice(self):
        selected = prompt_author_candidate("Author", [{"id": "Q1", "label": "Candidate"}], lambda _: "2")
        self.assertIsNone(selected)

    def test_wikidata_book_candidate_must_not_conflict_with_known_author(self):
        book = {
            "labels": {"en": {"value": "A Novel"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
                "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q_WRONG_AUTHOR"}}}}],
            },
        }
        client = FakeWikidataClient([{"id": "Q_BOOK"}], {"Q_BOOK": book})
        status, entity_id, _, _ = client.resolve(
            "A Novel", "book", related_author_ids=["Q_EXPECTED_AUTHOR"]
        )
        self.assertEqual((status, entity_id), ("no_candidate", None))

    def test_wikidata_accepts_one_exact_book_with_matching_author(self):
        book = {
            "labels": {"en": {"value": "A Novel"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
                "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q_AUTHOR"}}}}],
            },
        }
        client = FakeWikidataClient([{"id": "Q_BOOK"}], {"Q_BOOK": book})

        status, entity_id, entity, _ = client.resolve(
            "A Novel", "book", related_author_ids=["Q_AUTHOR"]
        )

        self.assertEqual((status, entity_id), ("matched", "Q_BOOK"))
        self.assertIs(entity, book)

    def test_wikidata_routes_single_nonexact_suitable_book_to_review(self):
        book = {
            "labels": {"en": {"value": "English Title"}},
            "descriptions": {"en": {"value": "novel by an author"}},
            "aliases": {"en": [{"value": "Local Title"}]},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
                "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q_AUTHOR"}}}}],
            },
        }
        client = FakeWikidataClient([{"id": "Q_BOOK"}], {"Q_BOOK": book})

        status, entity_id, _, candidates = client.resolve(
            "Local Title", "book", related_author_ids=["Q_AUTHOR"]
        )

        self.assertEqual((status, entity_id), ("needs_review", None))
        self.assertEqual(candidates[0]["id"], "Q_BOOK")
        self.assertEqual(candidates[0]["author_ids"], ["Q_AUTHOR"])
        self.assertEqual(candidates[0]["aliases"], ["Local Title"])

    def test_wikidata_routes_multiple_exact_books_to_review(self):
        book = {
            "labels": {"en": {"value": "A Novel"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q1"}, {"id": "Q2"}], {"Q1": book, "Q2": book})

        status, entity_id, _, candidates = client.resolve("A Novel", "book")

        self.assertEqual((status, entity_id), ("needs_review", None))
        self.assertEqual([candidate["id"] for candidate in candidates], ["Q1", "Q2"])

    def test_wikidata_excludes_nonbooks_from_book_candidates(self):
        nonbook = {
            "labels": {"en": {"value": "A Novel"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q1"}], {"Q1": nonbook})

        status, entity_id, _, candidates = client.resolve("A Novel", "book")

        self.assertEqual((status, entity_id, candidates), ("no_candidate", None, []))

    def test_wikidata_does_not_accept_broad_book_claims_without_book_type(self):
        unrelated_item = {
            "labels": {"en": {"value": "Kotlin"}},
            "claims": {
                "P1476": [{"mainsnak": {"datavalue": {"value": {"text": "Kôt"}}}}],
                "P577": [{"mainsnak": {"datavalue": {"value": {"time": "+2011-01-01T00:00:00Z"}}}}],
            },
        }
        client = FakeWikidataClient([{"id": "Q3816639"}], {"Q3816639": unrelated_item})

        status, entity_id, _, candidates = client.resolve("Kôt", "book")

        self.assertEqual((status, entity_id, candidates), ("no_candidate", None, []))

    def test_stored_wikidata_id_does_not_bypass_entity_type_validation(self):
        unrelated_item = {
            "labels": {"en": {"value": "Kotlin"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q6256"}}}}]},
        }
        client = FakeWikidataClient([], {"Q3816639": unrelated_item})

        status, entity_id, entity, candidates = client.resolve("Kôt", "book", known_id="Q3816639")

        self.assertEqual((status, entity_id, entity), ("error", "Q3816639", None))
        self.assertIn("type/author validation", candidates[0]["description"])

    def test_manual_wikidata_ids_are_type_checked(self):
        human = {
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
        }
        book = {
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}]},
        }
        client = FakeWikidataClient([], {"Q1": human, "Q2": book})

        self.assertIs(client.validate_manual_id("Q1", "author"), human)
        self.assertIs(client.validate_manual_id("Q2", "book"), book)
        with self.assertRaisesRegex(ValueError, "does not validate"):
            client.validate_manual_id("Q2", "author")
        with self.assertRaisesRegex(ValueError, "Invalid Wikidata QID"):
            client.validate_manual_id("not-a-qid", "author")

        authored_book = {
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
                "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q_AUTHOR"}}}}],
            },
        }
        client.entity_data["Q3"] = authored_book
        self.assertIs(client.validate_manual_id("Q3", "book", ["Q_AUTHOR"]), authored_book)
        with self.assertRaisesRegex(ValueError, "does not validate"):
            client.validate_manual_id("Q3", "book", ["Q_DIFFERENT"])

    def test_refresh_reuses_stored_wikidata_id_without_rematching(self):
        person = {
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
        }
        client = FakeWikidataClient([], {"Q_OLD": person})
        author = {"name": "Author", "wikidata_id": "Q_OLD"}
        session = AuthorEnrichmentSession(author)

        counts, reviews = run_enrichment(
            session,
            client,
            None,
            refresh=True,
            stale_days=180,
            limit=None,
            entity_types={"author"},
        )

        self.assertEqual(counts["matched"], 1)
        self.assertEqual(reviews, [])
        self.assertEqual(client.search_calls, [])
        self.assertEqual(author["wikidata_id"], "Q_OLD")

    def test_report_candidates_are_used_for_interactive_second_pass(self):
        person = {
            "labels": {"en": {"value": "Author"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
        }
        client = FakeWikidataClient([], {"Q1": person})
        author = {"name": "Author", "wikidata_id": None}
        session = AuthorEnrichmentSession(author)
        prior_row = {
            "type": "author",
            "key": "Author",
            "label": "Author",
            "status": "needs_review",
            "reason": "",
            "candidates": [{"id": "Q1", "label": "Author", "description": "writer"}],
        }

        counts, reviews = run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: "1",
            entity_types={"author"},
            author_names=["Author"],
            report_rows=[prior_row],
        )

        self.assertEqual(counts["matched"], 1)
        self.assertEqual(reviews, [])
        self.assertEqual(client.search_calls, [])
        self.assertEqual(author["wikidata_id"], "Q1")

    def test_manual_author_qid_is_saved_without_candidate_search(self):
        person = {
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
        }
        client = FakeWikidataClient([], {"Q1": person})
        author = {"name": "Manual Author", "wikidata_id": None}
        session = AuthorEnrichmentSession(author)

        counts, reviews = run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            entity_types={"author"},
            author_names=["Manual Author"],
            manual_author_qids={"Manual Author": "Q1"},
        )

        self.assertEqual(counts["matched"], 1)
        self.assertEqual(reviews, [])
        self.assertEqual(client.search_calls, [])
        self.assertEqual(author["wikidata_id"], "Q1")

    def test_manual_qid_for_missing_target_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "Book target\(s\) not found: 0001"):
            run_enrichment(
                TargetedEnrichmentSession(),
                FakeWikidataClient([], {}),
                None,
                refresh=False,
                stale_days=180,
                limit=None,
                entity_types={"book"},
                book_ids=["0001"],
                manual_book_qids={"0001": "Q1"},
            )

    def test_rematch_skip_keeps_the_existing_book_match(self):
        entity = {
            "labels": {"en": {"value": "English Title"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q_NEW"}], {"Q_NEW": entity})
        book = {
            "book_id": "0001",
            "title": "English Title",
            "wikidata_id": "Q_OLD",
            "summary": "Existing summary",
            "authors": [],
        }
        session = BookEnrichmentSession(book)

        counts, reviews = run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: "",
            entity_types={"book"},
            book_ids=["0001"],
            rematch_books={"0001"},
        )

        self.assertEqual(counts["unresolved"], 1)
        self.assertEqual(book["wikidata_id"], "Q_OLD")
        self.assertEqual(session.transaction.calls, [])
        self.assertEqual(reviews[0]["reason"], "Existing match was retained.")

    def test_interactive_book_rematch_reconciles_then_saves_replacement(self):
        replacement = {
            "labels": {"en": {"value": "English Title"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q_NEW"}], {"Q_NEW": replacement})
        book = {
            "book_id": "0001",
            "title": "English Title",
            "wikidata_id": "Q_OLD",
            "summary": "Existing summary",
            "authors": [],
        }
        session = BookEnrichmentSession(book)

        counts, reviews = run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: "1",
            entity_types={"book"},
            book_ids=["0001"],
            rematch_books={"0001"},
        )

        self.assertEqual(session.transaction.calls[0][0], RECONCILE_BOOK_MATCH_QUERY)
        self.assertEqual(client.search_calls, ["English Title"])
        self.assertEqual(book["wikidata_id"], "Q_NEW")
        self.assertEqual(counts["matched"], 1)
        self.assertEqual(reviews, [])

    def test_replacing_matches_runs_relationship_reconciliation_first(self):
        author_tx = RecordingTransaction()
        write_author(author_tx, "Author", "matched", "Q_NEW", [], [], "Q_OLD")
        self.assertEqual(author_tx.calls[0][0], RECONCILE_AUTHOR_MATCH_QUERY)
        self.assertIn("DELETE r", author_tx.calls[0][0])
        self.assertNotIn("DELETE a", author_tx.calls[0][0])

        book_tx = RecordingTransaction()
        write_book(
            book_tx,
            {
                "book_id": "0001",
                "status": "matched",
                "wikidata_id": "Q_NEW",
                "openlibrary_id": None,
                "publication_year": None,
                "publication_year_source": None,
                "summary": None,
                "summary_source": None,
                "previous_wikidata_id": "Q_OLD",
                "genres": [],
            },
        )
        self.assertEqual(book_tx.calls[0][0], RECONCILE_BOOK_MATCH_QUERY)
        self.assertIn("DELETE r", book_tx.calls[0][0])
        self.assertNotIn("DELETE b", book_tx.calls[0][0])

    def test_wikidata_excludes_books_with_only_conflicting_authors(self):
        book = {
            "labels": {"en": {"value": "A Novel"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
                "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q_WRONG"}}}}],
            },
        }
        client = FakeWikidataClient([{"id": "Q1"}], {"Q1": book})

        status, entity_id, _, candidates = client.resolve(
            "A Novel", "book", related_author_ids=["Q_EXPECTED"]
        )

        self.assertEqual((status, entity_id, candidates), ("no_candidate", None, []))

    def test_manual_book_selection_is_persisted_and_reused(self):
        book_entity = {
            "labels": {"en": {"value": "English Title"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
                "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q_AUTHOR"}}}}],
            },
        }
        client = FakeWikidataClient([{"id": "Q_BOOK"}], {"Q_BOOK": book_entity})
        book = {
            "book_id": "0001",
            "title": "Local Title",
            "wikidata_id": None,
            "summary": "Already present",
            "authors": [{"name": "Author", "wikidata_id": "Q_AUTHOR"}],
        }
        session = BookEnrichmentSession(book)

        counts, reviews = run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: "1",
        )

        record = next(
            parameters
            for query, parameters in session.transaction.calls
            if "MATCH (b:Book {book_id: $book_id})" in query
         )
        self.assertEqual(counts["matched"], 1)
        self.assertEqual(reviews, [])
        self.assertEqual(record["wikidata_id"], "Q_BOOK")
        self.assertEqual(record["status"], "matched")
        self.assertEqual(book["wikidata_id"], "Q_BOOK")
        self.assertEqual(
            confirm_book_candidate("Q_BOOK", [{"id": "Q_BOOK"}], client, ["Q_AUTHOR"]),
            ("Q_BOOK", book_entity),
        )
        self.assertIsNone(confirm_book_candidate("Q_OUTSIDE", [{"id": "Q_BOOK"}], client, ["Q_AUTHOR"]))

        next_counts, next_reviews = run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: self.fail("a stored book ID should not prompt"),
        )
        self.assertEqual(next_counts["matched"], 1)
        self.assertEqual(next_reviews, [])
        self.assertEqual(client.search_calls, ["Local Title"])

    def test_manual_book_selection_rejects_conflicting_author_claims(self):
        conflicting_book = {
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
                "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q_WRONG"}}}}],
            },
        }
        client = FakeWikidataClient([], {"Q_BOOK": conflicting_book})

        accepted = confirm_book_candidate("Q_BOOK", [{"id": "Q_BOOK"}], client, ["Q_EXPECTED"])

        self.assertIsNone(accepted)

    def test_interactive_book_skip_keeps_candidate_unresolved(self):
        book_entity = {
            "labels": {"en": {"value": "English Title"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q_BOOK"}], {"Q_BOOK": book_entity})
        session = BookEnrichmentSession(
            {
                "book_id": "0001",
                "title": "Local Title",
                "wikidata_id": None,
                "summary": "Already present",
                "authors": [],
            }
        )

        counts, reviews = run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: "",
        )

        record = next(
            parameters
            for query, parameters in session.transaction.calls
            if "MATCH (b:Book {book_id: $book_id})" in query
         )
        self.assertEqual(counts["unresolved"], 1)
        self.assertIsNone(record["wikidata_id"])
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(reviews[-1]["status"], "needs_review")

    def test_book_prompt_rejects_out_of_range_choice(self):
        selected = prompt_book_candidate("A Novel", [{"id": "Q1", "label": "Candidate"}], lambda _: "2")
        self.assertIsNone(selected)

    def test_wikidata_genre_claim_alone_does_not_identify_a_book(self):
        item = {
            "labels": {"en": {"value": "A Novel"}},
            "claims": {"P136": [{"mainsnak": {"datavalue": {"value": {"id": "Q_GENRE"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q_ITEM"}], {"Q_ITEM": item})
        status, entity_id, _, _ = client.resolve("A Novel", "book")
        self.assertEqual((status, entity_id), ("no_candidate", None))

    def test_wikidata_helpers_extract_claims_and_year(self):
        entity = {
            "claims": {
                "P136": [{"mainsnak": {"datavalue": {"value": {"id": "Q123"}}}}],
                "P577": [{"mainsnak": {"datavalue": {"value": {"time": "+2017-06-01T00:00:00Z"}}}}],
            }
        }
        self.assertEqual(claim_entity_ids(entity, "P136"), ["Q123"])
        self.assertEqual(year_from_claims(entity), 2017)
        self.assertEqual(normalize_text("  ÉXAMPLE   Title "), "éxample title")

    def test_author_fact_labels_batch_and_deduplicate_entity_requests(self):
        entity = {
            "claims": {
                "P21": [
                    {"mainsnak": {"datavalue": {"value": {"id": "Q1"}}}},
                    {"mainsnak": {"datavalue": {"value": {"id": "Q2"}}}},
                ],
                "P27": [
                    {"mainsnak": {"datavalue": {"value": {"id": "Q2"}}}},
                    {"mainsnak": {"datavalue": {"value": {"id": "Q3"}}}},
                ],
            }
        }
        client = FakeWikidataClient(
            [],
            {
                entity_id: {"labels": {"en": {"value": label}}}
                for entity_id, label in (("Q1", "Gender A"), ("Q2", "Shared"), ("Q3", "Country C"))
            },
        )

        labels = labels_for_properties(client, entity, ["P21", "P27"])

        self.assertEqual(client.entity_calls, [["Q1", "Q2", "Q3"]])
        self.assertEqual(labels["P21"], [{"id": "Q1", "name": "Gender A"}, {"id": "Q2", "name": "Shared"}])
        self.assertEqual(labels["P27"], [{"id": "Q2", "name": "Shared"}, {"id": "Q3", "name": "Country C"}])

        empty_labels = labels_for_properties(client, {}, ["P21", "P27"])
        self.assertEqual(empty_labels, {"P21": [], "P27": []})
        self.assertEqual(client.entity_calls, [["Q1", "Q2", "Q3"]])

    def test_author_fact_label_requests_are_chunked_without_dropping_ids(self):
        entity_ids = [f"Q{index}" for index in range(51)]
        entity = {
            "claims": {
                "P21": [
                    {"mainsnak": {"datavalue": {"value": {"id": entity_id}}}}
                    for entity_id in entity_ids
                ]
            }
        }
        client = FakeWikidataClient(
            [],
            {
                entity_id: {"labels": {"en": {"value": entity_id}}}
                for entity_id in entity_ids
            },
        )

        labels = labels_for_properties(client, entity, ["P21"])

        self.assertEqual([len(batch) for batch in client.entity_calls], [50, 1])
        self.assertEqual(client.entity_calls[0] + client.entity_calls[1], entity_ids)
        self.assertEqual(len(labels["P21"]), 51)

    def test_author_enrichment_fetches_gender_and_country_labels_together(self):
        author_entity = {
            "labels": {"en": {"value": "Author"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
                "P21": [{"mainsnak": {"datavalue": {"value": {"id": "Q_GENDER"}}}}],
                "P27": [{"mainsnak": {"datavalue": {"value": {"id": "Q_COUNTRY"}}}}],
            },
        }
        client = FakeWikidataClient(
            [{"id": "Q_AUTHOR"}],
            {
                "Q_AUTHOR": author_entity,
                "Q_GENDER": {"labels": {"en": {"value": "Gender"}}},
                "Q_COUNTRY": {"labels": {"en": {"value": "Country"}}},
            },
        )
        session = AuthorEnrichmentSession({"name": "Author", "wikidata_id": None})

        run_enrichment(
            session,
            client,
            None,
            refresh=False,
            stale_days=180,
            limit=None,
            entity_types={"author"},
        )

        self.assertEqual(client.entity_calls, [["Q_AUTHOR"], ["Q_GENDER", "Q_COUNTRY"]])

    def test_open_library_requires_exact_title_and_author_match(self):
        client = OpenLibraryClient(
            FakeOpenLibraryHttp(
                [
                    {
                        "docs": [
                            {"key": "/works/OL1W", "title": "Title", "author_name": ["Author"], "first_publish_year": 2001},
                            {"key": "/works/OL2W", "title": "Title", "author_name": ["Other"]},
                        ]
                    },
                    {"description": {"value": "A real work description."}},
                ]
            )
        )
        status, summary, year, candidates = client.summary("Title", ["Author"])
        self.assertEqual((status, summary, year), ("matched", "A real work description.", 2001))
        self.assertEqual([item["id"] for item in candidates if item["matched"]], ["/works/OL1W"])

    def test_open_library_does_not_persist_edition_ids_as_work_ids(self):
        client = OpenLibraryClient(
            FakeOpenLibraryHttp(
                [
                    {
                        "docs": [
                            {"key": "/books/OL1M", "title": "Title", "author_name": ["Author"]},
                        ]
                    }
                ]
            )
        )

        status, summary, year, candidates = client.summary("Title", ["Author"])

        self.assertEqual((status, summary, year), ("not_found", None, None))
        self.assertTrue(candidates[0]["matched"])
        self.assertFalse(candidates[0]["selectable"])

    def test_interactive_open_library_summary_uses_saved_candidates_and_revalidates(self):
        candidate_documents = [
            {"key": "/works/OL1W", "title": "Title", "author_name": ["Author"], "first_publish_year": 2001},
            {"key": "/works/OL2W", "title": "Title", "author_name": ["Author"], "first_publish_year": 2002},
            {"key": "/works/OL3M", "title": "Title", "author_name": ["Author"], "first_publish_year": 2003},
        ]
        open_library = OpenLibraryClient(
            FakeOpenLibraryHttp([{"docs": candidate_documents}, {"description": {"value": "Chosen work summary."}}])
        )
        session = TargetedEnrichmentSession(
            summaries=[{"book_id": "0001", "title": "Title", "authors": ["Author"]}]
        )
        report_row = {
            "type": "book_summary",
            "key": "0001",
            "label": "Title",
            "status": "ambiguous",
            "reason": "",
            "candidates": [
                {"id": "/works/OL1W", "label": "Title", "description": "Author", "matched": True},
                {"id": "/works/OL2W", "label": "Title", "description": "Author", "matched": True},
                {"id": "/works/OL3M", "label": "Title", "description": "Author", "matched": True},
            ],
        }

        counts, reviews = run_enrichment(
            session,
            FakeWikidataClient([], {}),
            open_library,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: "2",
            entity_types={"book_summary"},
            book_ids=["0001"],
            summary_statuses=["ambiguous"],
            report_rows=[report_row],
        )

        summary_query = next(call for call in session.query_calls if call[0] == DUE_SUMMARIES_QUERY)
        self.assertIsNone(summary_query[1]["summary_statuses"])
        query, parameters = session.transaction.calls[0]
        self.assertIn("openlibrary_status", query)
        self.assertEqual(parameters["openlibrary_id"], "/works/OL2W")
        self.assertEqual(parameters["summary"], "Chosen work summary.")
        self.assertEqual(parameters["publication_year"], 2002)
        self.assertEqual(counts["summary_matched"], 1)
        self.assertEqual(reviews, [])

    def test_first_pass_summary_selection_searches_once_then_fetches_work(self):
        http = FakeOpenLibraryHttp(
            [
                {
                    "docs": [
                        {"key": "/works/OL1W", "title": "Title", "author_name": ["Author"], "first_publish_year": 2001},
                        {"key": "/works/OL2W", "title": "Title", "author_name": ["Author"], "first_publish_year": 2002},
                    ]
                },
                {"description": {"value": "Chosen work summary."}},
            ]
        )
        open_library = OpenLibraryClient(http)
        session = TargetedEnrichmentSession(
            summaries=[{"book_id": "0001", "title": "Title", "authors": ["Author"]}]
        )

        counts, reviews = run_enrichment(
            session,
            FakeWikidataClient([], {}),
            open_library,
            refresh=False,
            stale_days=180,
            limit=None,
            interactive=True,
            input_fn=lambda _: "2",
            entity_types={"book_summary"},
            book_ids=["0001"],
        )

        self.assertEqual(len(http.urls), 2)
        self.assertIn("/search.json?", http.urls[0])
        self.assertTrue(http.urls[1].endswith("/works/OL2W.json"))
        self.assertEqual(counts["summary_matched"], 1)
        self.assertEqual(reviews, [])

    def test_open_library_prompt_excludes_nonselectable_and_unmatched_candidates(self):
        candidates = [
            {"id": "/works/OL1W", "label": "Exact", "matched": True},
            {"id": "/works/OL2W", "label": "Other", "matched": False},
            {"id": "/works/OL3M", "label": "Edition", "matched": True},
        ]
        self.assertEqual(prompt_openlibrary_candidate("Title", candidates, lambda _: "1"), "/works/OL1W")
        self.assertIsNone(prompt_openlibrary_candidate("Title", candidates, lambda _: "2"))

    def test_open_library_failure_does_not_fail_wikidata_enrichment(self):
        book_entity = {
            "labels": {"en": {"value": "A Novel"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}]},
        }

        class FailingOpenLibrary:
            def summary(self, title, authors):
                raise RuntimeError("temporary service failure")

        session = EnrichmentSession()
        progress = io.StringIO()
        with redirect_stdout(progress):
            counts, reviews = run_enrichment(
                session,
                MatchedBookWikidataClient([], {"Q_BOOK": book_entity}),
                FailingOpenLibrary(),
                refresh=False,
                stale_days=180,
                limit=None,
            )
        update = session.transaction.calls[0][1]
        self.assertEqual(counts["errors"], 0)
        self.assertEqual(counts["matched"], 1)
        self.assertEqual(counts["summary_errors"], 1)
        self.assertEqual(update["status"], "matched")
        self.assertEqual(reviews[0]["status"], "error")
        self.assertIn("Enrichment queue: 0 authors, 1 books, 1 summaries.", progress.getvalue())
        self.assertIn("[book 1/1] 0001 A Novel: matched", progress.getvalue())


if __name__ == "__main__":
    unittest.main()