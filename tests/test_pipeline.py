import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.enrich.wikidata_enrich import (
    JsonHttpClient,
    OpenLibraryClient,
    WikidataClient,
    claim_entity_ids,
    confirm_author_candidate,
    confirm_book_candidate,
    normalize_text,
    prompt_author_candidate,
    prompt_book_candidate,
    run_enrichment,
    year_from_claims,
)
from src.ingest.load_books import (
    EXISTING_QUERY,
    UPSERT_QUERY,
    load_rows,
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

    def search(self, label):
        self.search_calls.append(label)
        return self.results

    def entities(self, entity_ids):
        return self.entity_data


class FakeOpenLibraryHttp:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get_json(self, url):
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


class MatchedBookWikidataClient(FakeWikidataClient):
    def resolve(self, label, entity_type, known_id=None, related_author_ids=None):
        return "matched", "Q_BOOK", self.entity_data["Q_BOOK"], []


class PipelineTests(unittest.TestCase):
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
            "book_id,title,author,year_month,score\n"
            "0007,Example,A / B,2024_02,8.5\n"
        )
        self.assertEqual(rows[0]["book_id"], "0007")
        self.assertEqual(rows[0]["score"], 8.5)
        self.assertEqual(rows[0]["authors"], ["A", "B"])
        self.assertEqual(rows[0]["date"], "2024-02-01")

    def test_csv_reports_duplicate_ids_bad_dates_and_scores(self):
        with self.assertRaisesRegex(ValueError, "line 3: duplicate book_id"):
            self.parse_csv(
                "book_id,title,author,year_month,score\n"
                "001,One,A,2024_01,8\n"
                "001,Two,B,2024_13,nan\n"
            )

    def test_csv_rejects_empty_author_segments(self):
        with self.assertRaisesRegex(ValueError, "nonblank names"):
            self.parse_csv("book_id,title,author,year_month,score\n1,Title,A//B,2024_01,8\n")

    def test_csv_rejects_float_overflow(self):
        with self.assertRaisesRegex(ValueError, "finite number"):
            self.parse_csv("book_id,title,author,year_month,score\n1,Title,A,2024_01,1e10000\n")

    def test_csv_requires_all_columns(self):
        with self.assertRaisesRegex(ValueError, "missing required columns: score"):
            self.parse_csv("book_id,title,author,year_month\n1,Title,A,2024_01\n")

    def test_csv_rejects_duplicate_headers(self):
        with self.assertRaisesRegex(ValueError, "duplicate column headers"):
            self.parse_csv("book_id,title,author,year_month,score,score\n")

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
            "authors": ["A", "B"],
            "date": "2024-01-01",
            "score": 8.5,
        }
        self.assertEqual(upsert_batch(transaction, [row]), {"inserted": 1, "updated": 0, "unchanged": 0})
        self.assertEqual(upsert_batch(transaction, [row]), {"inserted": 0, "updated": 0, "unchanged": 1})
        changed = {**row, "score": 9.0}
        self.assertEqual(upsert_batch(transaction, [changed]), {"inserted": 0, "updated": 1, "unchanged": 0})
        self.assertIn("MERGE (b:Book {book_id: row.book_id})", UPSERT_QUERY)
        self.assertIn("MERGE (r:Read {book_id: row.book_id})", UPSERT_QUERY)

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
        self.assertEqual(update["status"], "matched")
        self.assertEqual(reviews[0]["status"], "error")
        self.assertIn("Enrichment queue: 0 authors, 1 books.", progress.getvalue())
        self.assertIn("[book 1/1] 0001 A Novel: matched", progress.getvalue())


if __name__ == "__main__":
    unittest.main()