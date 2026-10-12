"""
Load books.csv into Neo4j.

Expects a CSV in data/raw with columns: title, author, year_month, score
  - year_month formatted like "2023_06"
  - score numeric (e.g. 1-5 or 1-10, whatever scale you used)

Usage:
        python src/ingest/load_books.py [csv_filename]
"""

import argparse
import csv
import os
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

NEO4J_URI = os.environ["NEO4J_URI"]
NEO4J_USER = os.environ["NEO4J_USER"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]

RAW_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"

LOAD_QUERY = """
MERGE (a:Author {name: $author})
MERGE (b:Book {title: $title})
MERGE (a)-[:WROTE]->(b)
CREATE (r:Read {date: date($date), score: toInteger($score)})
CREATE (r)-[:OF_BOOK]->(b)
"""


def normalize_author(name: str) -> str:
    """Basic cleanup - expand this as you find inconsistencies in your data.
    e.g. collapse 'Tolkien, J.R.R.' vs 'J.R.R. Tolkien' to one form."""
    return name.strip()


def to_neo4j_date(year_month: str) -> str:
    """'2023_06' -> '2023_06_01' (Neo4j date() needs a full date)."""
    year, month = year_month.strip().split("_")
    return f"{year}-{month}-01"


def load_rows(csv_path: Path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            rows.append(
                {
                    "title": row["title"].strip(),
                    "author": normalize_author(row["author"]),
                    "date": to_neo4j_date(row["year_month"]),
                    "score": row["score"].strip(),
                }
            )
        return rows


def main():
    parser = argparse.ArgumentParser(description="Load a books CSV from data/raw into Neo4j.")
    parser.add_argument(
        "csv_filename",
        nargs="?",
        default="books.csv",
        help="CSV filename inside data/raw (default: books.csv)",
    )
    args = parser.parse_args()
    csv_path = RAW_DATA_DIR / args.csv_filename

    rows = load_rows(csv_path)
    print(f"Loaded {len(rows)} rows from {csv_path}")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        with driver.session() as session:
            for row in rows:
                session.run(LOAD_QUERY, **row)
        print("Done. Rows written to Neo4j.")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
