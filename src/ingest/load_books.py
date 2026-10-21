"""Validate and idempotently load the cumulative books CSV into Neo4j."""

import csv
import math
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

RAW_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"
SOURCE_CSV = RAW_DATA_DIR / "books_raw_20261001.csv"
REQUIRED_COLUMNS = {"books_id", "title", "author", "year_month", "score"}
BATCH_SIZE = 100

UPSERT_QUERY = """
UNWIND $rows AS row
MERGE (b:Book {books_id: row.books_id})
ON CREATE SET b.title = row.title
ON MATCH SET b.title = row.title
MERGE (r:Read {books_id: row.books_id})
ON CREATE SET r.date = date(row.date), r.score = row.score
ON MATCH SET r.date = date(row.date), r.score = row.score
MERGE (r)-[:OF_BOOK]->(b)
WITH row, b
UNWIND row.authors AS author_name
MERGE (a:Author {name: author_name})
MERGE (a)-[:WROTE]->(b)
"""

EXISTING_QUERY = """
UNWIND $books_ids AS books_id
OPTIONAL MATCH (b:Book {books_id: books_id})
OPTIONAL MATCH (r:Read {books_id: books_id})
OPTIONAL MATCH (a:Author)-[:WROTE]->(b)
RETURN books_id, b.title AS title, r.date AS date, r.score AS score,
       collect(DISTINCT a.name) AS authors
"""

def normalize_author(name: str) -> list[str]:
    authors = [part.strip() for part in name.split("/")]
    if not name.strip() or any(not author for author in authors):
        raise ValueError("author must contain one or more nonblank names separated by '/'")
    return list(dict.fromkeys(authors))


def to_neo4j_date(year_month: str) -> str:
    match = re.fullmatch(r"(\d{4})_(\d{2})", year_month.strip())
    if match is None:
        raise ValueError("must use YYYY_MM format")
    year, month = map(int, match.groups())
    try:
        return date(year, month, 1).isoformat()
    except ValueError as exc:
        raise ValueError("contains an invalid year or month") from exc


def load_rows(csv_path: Path) -> list[dict]:
    errors = []
    rows = []
    seen_ids = set()
    with csv_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("CSV is empty or missing its header row")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("CSV contains duplicate column headers")
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        for line_number, raw in enumerate(reader, start=2):
            row_errors = []
            books_id = (raw.get("books_id") or "").strip()
            title = (raw.get("title") or "").strip()
            author_text = raw.get("author") or ""
            if None in raw:
                row_errors.append("too many CSV fields")
            if not books_id:
                row_errors.append("books_id is blank")
            elif books_id in seen_ids:
                row_errors.append(f"duplicate books_id {books_id!r}")
            else:
                seen_ids.add(books_id)
            if not title:
                row_errors.append("title is blank")
            try:
                authors = normalize_author(author_text)
            except ValueError as exc:
                row_errors.append(str(exc))
                authors = []
            try:
                date_value = to_neo4j_date(raw.get("year_month") or "")
            except ValueError as exc:
                row_errors.append(f"year_month {exc}")
                date_value = ""
            score_text = (raw.get("score") or "").strip()
            try:
                score_decimal = Decimal(score_text)
                if not score_decimal.is_finite():
                    raise InvalidOperation
                score = float(score_decimal)
                if not math.isfinite(score):
                    raise InvalidOperation
            except (InvalidOperation, ValueError, OverflowError):
                row_errors.append("score must be a finite number")
                score = 0.0

            if row_errors:
                errors.append(f"line {line_number}: {'; '.join(row_errors)}")
            else:
                rows.append(
                    {
                        "books_id": books_id,
                        "title": title,
                        "authors": authors,
                        "date": date_value,
                        "score": score,
                    }
                )

    if errors:
        raise ValueError("Invalid CSV rows; no data was written:\n" + "\n".join(errors))
    return rows


def upsert_batch(tx, rows: list[dict]) -> dict:
    existing = {
        record["books_id"]: record
        for record in tx.run(EXISTING_QUERY, books_ids=[row["books_id"] for row in rows])
        if record["title"] is not None
    }
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for row in rows:
        previous = existing.get(row["books_id"])
        if previous is None:
            counts["inserted"] += 1
            continue
        old_authors = set(previous["authors"])
        if (
            previous["title"] != row["title"]
            or previous["date"].iso_format() != row["date"]
            or float(previous["score"]) != row["score"]
            or not set(row["authors"]).issubset(old_authors)
        ):
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1

    tx.run(UPSERT_QUERY, rows=rows).consume()
    return counts


def sync_rows(session, rows: list[dict]) -> dict:
    totals = {"inserted": 0, "updated": 0, "unchanged": 0}
    for start in range(0, len(rows), BATCH_SIZE):
        batch_counts = session.execute_write(upsert_batch, rows[start : start + BATCH_SIZE])
        for key, value in batch_counts.items():
            totals[key] += value
    return totals


def main() -> None:
    rows = load_rows(SOURCE_CSV)
    print(f"Validated {len(rows)} rows from {SOURCE_CSV}")

    load_dotenv()
    uri = os.environ["NEO4J_URI"]
    user = os.environ["NEO4J_USER"]
    password = os.environ["NEO4J_PASSWORD"]
    database = os.getenv("NEO4J_DB_NAME") or None
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            totals = sync_rows(session, rows)
        print("Ingestion complete:")
        for key, value in totals.items():
            print(f"  {key}: {value}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
