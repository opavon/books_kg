import csv
import tempfile
import unittest
from pathlib import Path

from src.enrich.wikidata_enrich import (
    OpenLibraryClient,
    WikidataClient,
    claim_entity_ids,
    normalize_text,
    run_enrichment,
    year_from_claims,
)
from src.ingest.load_books import (
    BACKFILL_BOOK_QUERY,
    EXISTING_QUERY,
    LEGACY_BOOKS_QUERY,
    UPSERT_QUERY,
    load_rows,
    migrate_legacy_books,
    sync_rows,
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
                        "books_id": books_id,
                        "title": record["title"],
                        "date": FakeDate(record["date"]),
                        "score": record["score"],
                        "authors": record["authors"],
                    }
                    for books_id in parameters["books_ids"]
                    if (record := self.records.get(books_id)) is not None
                ]
            )
        if query == UPSERT_QUERY:
            for row in parameters["rows"]:
                previous = self.records.get(row["books_id"], {})
                self.records[row["books_id"]] = {
                    "title": row["title"],
                    "date": row["date"],
                    "score": row["score"],
                    "authors": set(previous.get("authors", set())) | set(row["authors"]),
                }
            return FakeResult()
        raise AssertionError(f"Unexpected query: {query}")


class FakeLegacyTransaction:
    def __init__(self, legacy_books):
        self.legacy_books = legacy_books
        self.backfilled = []

    def run(self, query, **parameters):
        if query == LEGACY_BOOKS_QUERY:
            return FakeResult(self.legacy_books)
        if query == BACKFILL_BOOK_QUERY:
            self.backfilled.append(parameters)
            return FakeResult()
        raise AssertionError(f"Unexpected query: {query}")


class FakeSession:
    def __init__(self, transaction):
        self.transaction = transaction

    def execute_write(self, callback, *arguments):
        return callback(self.transaction, *arguments)


class FakeWikidataClient(WikidataClient):
    def __init__(self, results, entities):
        super().__init__(None)
        self.results = results
        self.entity_data = entities

    def search(self, label):
        return self.results

    def entities(self, entity_ids):
        return self.entity_data


class FakeOpenLibraryHttp:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get_json(self, url):
        return next(self.responses)


class RecordingTransaction:
    def __init__(self):
        self.calls = []

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        return FakeResult()


class EnrichmentSession:
    def __init__(self):
        self.transaction = RecordingTransaction()

    def run(self, query, **parameters):
        if "MATCH (b:Book)" in query:
            return FakeResult(
                [
                    {
                        "books_id": "0001",
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
            "books_id,title,author,year_month,score\n"
            "0007,Example,A / B,2024_02,8.5\n"
        )
        self.assertEqual(rows[0]["books_id"], "0007")
        self.assertEqual(rows[0]["score"], 8.5)
        self.assertEqual(rows[0]["authors"], ["A", "B"])
        self.assertEqual(rows[0]["date"], "2024-02-01")

    def test_csv_reports_duplicate_ids_bad_dates_and_scores(self):
        with self.assertRaisesRegex(ValueError, "line 3: duplicate books_id"):
            self.parse_csv(
                "books_id,title,author,year_month,score\n"
                "001,One,A,2024_01,8\n"
                "001,Two,B,2024_13,nan\n"
            )

    def test_csv_rejects_empty_author_segments(self):
        with self.assertRaisesRegex(ValueError, "nonblank names"):
            self.parse_csv("books_id,title,author,year_month,score\n1,Title,A//B,2024_01,8\n")

    def test_csv_rejects_float_overflow(self):
        with self.assertRaisesRegex(ValueError, "finite number"):
            self.parse_csv("books_id,title,author,year_month,score\n1,Title,A,2024_01,1e10000\n")

    def test_csv_requires_all_columns(self):
        with self.assertRaisesRegex(ValueError, "missing required columns: score"):
            self.parse_csv("books_id,title,author,year_month\n1,Title,A,2024_01\n")

    def test_csv_rejects_duplicate_headers(self):
        with self.assertRaisesRegex(ValueError, "duplicate column headers"):
            self.parse_csv("books_id,title,author,year_month,score,score\n")

    def test_reimport_is_idempotent_and_detects_updates(self):
        transaction = InMemoryTransaction()
        row = {
            "books_id": "0001",
            "title": "Title",
            "authors": ["A", "B"],
            "date": "2024-01-01",
            "score": 8.5,
        }
        self.assertEqual(upsert_batch(transaction, [row]), {"inserted": 1, "updated": 0, "unchanged": 0})
        self.assertEqual(upsert_batch(transaction, [row]), {"inserted": 0, "updated": 0, "unchanged": 1})
        changed = {**row, "score": 9.0}
        self.assertEqual(upsert_batch(transaction, [changed]), {"inserted": 0, "updated": 1, "unchanged": 0})
        self.assertIn("MERGE (b:Book {books_id: row.books_id})", UPSERT_QUERY)
        self.assertIn("MERGE (r:Read {books_id: row.books_id})", UPSERT_QUERY)

    def test_legacy_backfill_only_migrates_unambiguous_single_read(self):
        transaction = FakeLegacyTransaction(
            [
                {"element_id": "1", "title": "Unique", "read_count": 1},
                {"element_id": "2", "title": "Duplicate", "read_count": 1},
                {"element_id": "3", "title": "Many reads", "read_count": 2},
                {"element_id": "4", "title": "Missing", "read_count": 0},
            ]
        )
        rows = [
            {"books_id": "01", "title": "Unique"},
            {"books_id": "02", "title": "Duplicate"},
            {"books_id": "03", "title": "Duplicate"},
            {"books_id": "04", "title": "Many reads"},
        ]
        result = migrate_legacy_books(transaction, rows)
        self.assertEqual(result["migrated"], 1)
        self.assertEqual(transaction.backfilled, [{"element_id": "1", "books_id": "01"}])
        self.assertEqual(
            [issue["reason"] for issue in result["unresolved"]],
            ["ambiguous title", "multiple legacy Read nodes", "unmatched"],
        )

    def test_unresolved_legacy_title_collision_blocks_ingestion(self):
        transaction = FakeLegacyTransaction(
            [{"element_id": "1", "title": "Title", "read_count": 2}]
        )
        row = {
            "books_id": "01",
            "title": "Title",
            "authors": ["A"],
            "date": "2024-01-01",
            "score": 8.0,
        }
        with self.assertRaisesRegex(ValueError, "multiple legacy Read nodes"):
            sync_rows(FakeSession(transaction), [row])

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
        self.assertEqual((status, entity_id), ("ambiguous", None))

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
        self.assertEqual((status, entity_id), ("not_found", None))

    def test_wikidata_genre_claim_alone_does_not_identify_a_book(self):
        item = {
            "labels": {"en": {"value": "A Novel"}},
            "claims": {"P136": [{"mainsnak": {"datavalue": {"value": {"id": "Q_GENRE"}}}}]},
        }
        client = FakeWikidataClient([{"id": "Q_ITEM"}], {"Q_ITEM": item})
        status, entity_id, _, _ = client.resolve("A Novel", "book")
        self.assertEqual((status, entity_id), ("not_found", None))

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


if __name__ == "__main__":
    unittest.main()