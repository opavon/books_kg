"""Enrich graph entities from Wikidata, with Open Library summary fallback."""

import argparse
import csv
import json
import os
import random
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from neo4j import GraphDatabase

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
OPEN_LIBRARY_API = "https://openlibrary.org"
HUMAN_QID = "Q5"
BOOK_TYPES = {"Q571", "Q7725634", "Q47461344"}

DUE_AUTHORS_QUERY = """
MATCH (a:Author)
WHERE $refresh OR a.wikidata_status IS NULL OR a.wikidata_status <> 'matched'
   OR a.wikidata_checked_at IS NULL
   OR a.wikidata_checked_at < datetime() - duration({days: $stale_days})
RETURN a.name AS name, a.wikidata_id AS wikidata_id
ORDER BY a.name
"""

DUE_BOOKS_QUERY = """
MATCH (b:Book)
WHERE b.book_id IS NOT NULL AND (
      $refresh OR b.wikidata_status IS NULL OR b.wikidata_status <> 'matched'
   OR b.wikidata_checked_at IS NULL
   OR b.wikidata_checked_at < datetime() - duration({days: $stale_days}))
OPTIONAL MATCH (a:Author)-[:WROTE]->(b)
RETURN b.book_id AS book_id, b.title AS title, b.wikidata_id AS wikidata_id,
       b.summary AS summary,
       collect(DISTINCT {name: a.name, wikidata_id: a.wikidata_id}) AS authors
ORDER BY b.book_id
"""

UPDATE_AUTHOR_QUERY = """
MATCH (a:Author {name: $name})
SET a.wikidata_status = $status, a.wikidata_checked_at = datetime()
FOREACH (_ IN CASE WHEN $wikidata_id IS NULL THEN [] ELSE [1] END |
   SET a.wikidata_id = $wikidata_id)
"""

UPDATE_BOOK_QUERY = """
MATCH (b:Book {book_id: $book_id})
SET b.wikidata_status = $status, b.wikidata_checked_at = datetime()
FOREACH (_ IN CASE WHEN $wikidata_id IS NULL THEN [] ELSE [1] END |
   SET b.wikidata_id = $wikidata_id)
FOREACH (_ IN CASE WHEN $openlibrary_id IS NULL THEN [] ELSE [1] END |
   SET b.openlibrary_id = $openlibrary_id)
FOREACH (_ IN CASE WHEN $publication_year IS NULL THEN [] ELSE [1] END |
   SET b.publication_year = $publication_year)
FOREACH (_ IN CASE WHEN $summary IS NULL THEN [] ELSE [1] END |
   SET b.summary = $summary, b.summary_source = $summary_source)
"""

MERGE_GENDER_QUERY = """
MATCH (a:Author {name: $author_name})
MERGE (g:Gender {name: $name})
ON CREATE SET g.wikidata_id = $wikidata_id
MERGE (a)-[:HAS_GENDER]->(g)
"""

MERGE_COUNTRY_QUERY = """
MATCH (a:Author {name: $author_name})
MERGE (c:Country {name: $name})
ON CREATE SET c.wikidata_id = $wikidata_id
MERGE (a)-[:CITIZEN_OF]->(c)
"""

MERGE_GENRE_QUERY = """
MATCH (b:Book {book_id: $book_id})
MERGE (g:Genre {name: $name})
ON CREATE SET g.wikidata_id = $wikidata_id
MERGE (b)-[:HAS_GENRE]->(g)
"""


def normalize_text(value: str) -> str:
   normalized = unicodedata.normalize("NFKC", value).casefold()
   return " ".join(normalized.split())


def claim_entity_ids(entity: dict, property_id: str) -> list[str]:
   ids = []
   for claim in entity.get("claims", {}).get(property_id, []):
      value = claim.get("mainsnak", {}).get("datavalue", {}).get("value", {})
      entity_id = value.get("id") if isinstance(value, dict) else None
      if entity_id and entity_id not in ids:
         ids.append(entity_id)
   return ids


def entity_label(entity: dict) -> str:
   labels = entity.get("labels", {})
   if "en" in labels:
      return labels["en"]["value"]
   return next(iter(labels.values()), {}).get("value", "")


def is_entity_type(entity: dict, entity_type: str) -> bool:
   instance_ids = set(claim_entity_ids(entity, "P31"))
   if entity_type == "author":
      return HUMAN_QID in instance_ids
   return bool(instance_ids & BOOK_TYPES) or any(
      entity.get("claims", {}).get(prop)
      for prop in ("P50", "P577", "P1476")
   )


def choose_exact_candidate(search_results: list[dict], entities: dict, label: str, entity_type: str) -> tuple[str, dict] | None:
   target = normalize_text(label)
   candidates = []
   for result in search_results:
      entity = entities.get(result["id"])
      if (
         entity
         and normalize_text(entity_label(entity)) == target
         and is_entity_type(entity, entity_type)
      ):
         candidates.append((result["id"], entity))
   if len(candidates) == 1:
      return candidates[0]
   return None


class JsonHttpClient:
   def __init__(
      self,
      user_agent: str,
      request_interval_min: float = 5.0,
      request_interval_max: float = 10.0,
      jitter_min: float = 0.42,
      jitter_max: float = 1.42,
      opener=urlopen,
      uniform=random.uniform,
      monotonic=time.monotonic,
      sleeper=time.sleep,
   ):
      if request_interval_min <= 0 or request_interval_max < request_interval_min:
         raise ValueError("request interval bounds must be positive and ordered")
      if jitter_min < 0 or jitter_max < jitter_min:
         raise ValueError("jitter bounds must be nonnegative and ordered")
      self.user_agent = user_agent
      self.request_interval_min = request_interval_min
      self.request_interval_max = request_interval_max
      self.jitter_min = jitter_min
      self.jitter_max = jitter_max
      self.opener = opener
      self.uniform = uniform
      self.monotonic = monotonic
      self.sleeper = sleeper
      self.last_request = None

   def get_json(self, url: str) -> dict:
      if self.last_request is not None:
         interval = self.uniform(self.request_interval_min, self.request_interval_max)
         jitter = self.uniform(self.jitter_min, self.jitter_max)
         delay = interval + jitter - (self.monotonic() - self.last_request)
         if delay > 0:
            self.sleeper(delay)
      request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
      try:
         with self.opener(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
      except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
         raise RuntimeError(f"external lookup failed: {exc}") from exc
      finally:
         self.last_request = self.monotonic()
      return payload


class WikidataClient:
   def __init__(self, http: JsonHttpClient):
      self.http = http

   def search(self, label: str) -> list[dict]:
      params = urlencode(
         {"action": "wbsearchentities", "search": label, "language": "en", "format": "json", "type": "item", "limit": 10}
      )
      return self.http.get_json(f"{WIKIDATA_API}?{params}").get("search", [])

   def entities(self, entity_ids: list[str]) -> dict:
      if not entity_ids:
         return {}
      params = urlencode(
         {
            "action": "wbgetentities",
            "ids": "|".join(entity_ids[:50]),
            "props": "labels|descriptions|claims",
            "languages": "en",
            "format": "json",
         }
      )
      return self.http.get_json(f"{WIKIDATA_API}?{params}").get("entities", {})

   def resolve(
      self,
      label: str,
      entity_type: str,
      known_id: str | None = None,
      related_author_ids: list[str] | None = None,
   ) -> tuple[str, str | None, dict | None, list[dict]]:
      if known_id:
         entity = self.entities([known_id]).get(known_id)
         if entity and "missing" not in entity:
            return "matched", known_id, entity, []
         return "error", known_id, None, [{"id": known_id, "label": "", "description": "stored entity no longer resolves"}]

      search_results = self.search(label)
      result_ids = [result["id"] for result in search_results]
      entities = self.entities(result_ids)
      exact = [
         (result["id"], entities[result["id"]])
         for result in search_results
         if result["id"] in entities
         and normalize_text(entity_label(entities[result["id"]])) == normalize_text(label)
         and is_entity_type(entities[result["id"]], entity_type)
         and (
            entity_type != "book"
            or not related_author_ids
            or not claim_entity_ids(entities[result["id"]], "P50")
            or bool(set(related_author_ids) & set(claim_entity_ids(entities[result["id"]], "P50")))
         )
      ]
      review_candidates = [
         {
            "id": result["id"],
            "label": entity_label(entities[result["id"]]) if result["id"] in entities else result.get("label", ""),
            "description": entities.get(result["id"], {}).get("descriptions", {}).get("en", {}).get("value", ""),
         }
         for result in search_results
      ]
      if len(exact) == 1:
         return "matched", exact[0][0], exact[0][1], review_candidates
      if len(exact) > 1:
         return "ambiguous", None, None, review_candidates
      return "not_found", None, None, review_candidates


class OpenLibraryClient:
   def __init__(self, http: JsonHttpClient):
      self.http = http

   def summary(self, title: str, authors: list[str]) -> tuple[str, str | None, int | None, list[dict]]:
      params = urlencode(
         {
            "title": title,
            "author": authors[0] if authors else "",
            "fields": "key,title,author_name,first_publish_year",
            "limit": 10,
         }
      )
      payload = self.http.get_json(f"{OPEN_LIBRARY_API}/search.json?{params}")
      matches = []
      for document in payload.get("docs", []):
         title_matches = normalize_text(document.get("title", "")) == normalize_text(title)
         author_names = {normalize_text(value) for value in document.get("author_name", [])}
         if title_matches and author_names.intersection(normalize_text(name) for name in authors):
            matches.append(document)
      matched_keys = {item.get("key") for item in matches}
      candidates = [
         {
            "id": item.get("key", ""),
            "label": item.get("title", ""),
            "description": ", ".join(item.get("author_name", [])),
            "matched": item.get("key") in matched_keys,
         }
         for item in payload.get("docs", [])
      ]
      if len(matches) != 1:
         return ("ambiguous" if len(matches) > 1 else "not_found", None, None, candidates)

      work_key = matches[0].get("key", "")
      work = self.http.get_json(f"{OPEN_LIBRARY_API}{work_key}.json")
      description = work.get("description")
      summary = description.get("value") if isinstance(description, dict) else description
      year = matches[0].get("first_publish_year")
      return "matched", summary.strip() if isinstance(summary, str) and summary.strip() else None, year, candidates


def year_from_claims(entity: dict) -> int | None:
   years = []
   for claim in entity.get("claims", {}).get("P577", []):
      value = claim.get("mainsnak", {}).get("datavalue", {}).get("value", {})
      match = re.match(r"^[+-]?(\d{4})", value.get("time", ""))
      if match:
         years.append(int(match.group(1)))
   return min(years) if years else None


def labels_for_ids(client: WikidataClient, entity: dict, property_id: str) -> list[dict]:
   ids = claim_entity_ids(entity, property_id)
   entities = client.entities(ids)
   return [
      {"id": entity_id, "name": entity_label(entities[entity_id])}
      for entity_id in ids
      if entity_id in entities and entity_label(entities[entity_id])
   ]


def write_author(tx, name: str, status: str, wikidata_id: str | None, genders: list[dict], countries: list[dict]) -> None:
   tx.run(UPDATE_AUTHOR_QUERY, name=name, status=status, wikidata_id=wikidata_id).consume()
   for gender in genders:
      tx.run(MERGE_GENDER_QUERY, author_name=name, **{"name": gender["name"], "wikidata_id": gender["id"]}).consume()
   for country in countries:
      tx.run(MERGE_COUNTRY_QUERY, author_name=name, **{"name": country["name"], "wikidata_id": country["id"]}).consume()


def write_book(tx, record: dict) -> None:
   tx.run(UPDATE_BOOK_QUERY, **record).consume()
   for genre in record["genres"]:
      tx.run(MERGE_GENRE_QUERY, book_id=record["book_id"], name=genre["name"], wikidata_id=genre["id"]).consume()


def append_review(reviews: list[dict], kind: str, key: str, label: str, status: str, candidates: list[dict], reason: str = "") -> None:
   reviews.append(
      {
         "type": kind,
         "key": key,
         "label": label,
         "status": status,
         "reason": reason,
         "candidates": json.dumps(candidates, ensure_ascii=False),
      }
   )


def write_review_report(path: Path, reviews: list[dict]) -> None:
   path.parent.mkdir(parents=True, exist_ok=True)
   with path.open("w", newline="", encoding="utf-8") as output:
      writer = csv.DictWriter(output, fieldnames=["type", "key", "label", "status", "reason", "candidates"])
      writer.writeheader()
      writer.writerows(reviews)


def run_enrichment(session, wikidata: WikidataClient, open_library: OpenLibraryClient, refresh: bool, stale_days: int, limit: int | None) -> tuple[dict, list[dict]]:
   authors = list(session.run(DUE_AUTHORS_QUERY, refresh=refresh, stale_days=stale_days))
   if limit is not None:
      authors = authors[:limit]

   counts = {"authors_checked": 0, "books_checked": 0, "matched": 0, "unresolved": 0, "errors": 0}
   reviews = []
   for author in authors:
      name = author["name"]
      try:
         status, wikidata_id, entity, candidates = wikidata.resolve(name, "author", author["wikidata_id"])
         genders = labels_for_ids(wikidata, entity, "P21") if entity else []
         countries = labels_for_ids(wikidata, entity, "P27") if entity else []
         session.execute_write(write_author, name, status, wikidata_id, genders, countries)
         counts["authors_checked"] += 1
         counts["matched" if status == "matched" else "unresolved"] += 1
         if status != "matched":
            append_review(reviews, "author", name, name, status, candidates)
      except Exception as exc:
         session.execute_write(write_author, name, "error", author["wikidata_id"], [], [])
         counts["authors_checked"] += 1
         counts["errors"] += 1
         append_review(reviews, "author", name, name, "error", [], str(exc))

   books = list(session.run(DUE_BOOKS_QUERY, refresh=refresh, stale_days=stale_days))
   if limit is not None:
      books = books[:limit]
   for book in books:
      title = book["title"]
      key = book["book_id"]
      book_authors = [author for author in (book["authors"] or []) if author.get("name")]
      authors_for_book = [author["name"] for author in book_authors]
      author_ids = [author["wikidata_id"] for author in book_authors if author.get("wikidata_id")]
      try:
         status, wikidata_id, entity, candidates = wikidata.resolve(
            title, "book", book["wikidata_id"], related_author_ids=author_ids
         )
         genres = labels_for_ids(wikidata, entity, "P136") if entity else []
         publication_year = year_from_claims(entity) if entity else None
         summary = None
         summary_source = None
         openlibrary_id = None
         if not book.get("summary") or refresh:
            try:
               ol_status, summary, ol_year, ol_candidates = open_library.summary(title, authors_for_book)
               if ol_status == "matched":
                  openlibrary_id = next((item["id"] for item in ol_candidates if item.get("matched")), None)
                  publication_year = publication_year or ol_year
                  summary_source = "openlibrary" if summary else None
               elif ol_status == "ambiguous":
                  append_review(reviews, "book_summary", key, title, ol_status, ol_candidates)
            except Exception as exc:
               append_review(reviews, "book_summary", key, title, "error", [], str(exc))

         record = {
            "book_id": key,
            "status": status,
            "wikidata_id": wikidata_id,
            "openlibrary_id": openlibrary_id,
            "publication_year": publication_year,
            "summary": summary,
            "summary_source": summary_source,
            "genres": genres,
         }
         session.execute_write(write_book, record)
         counts["books_checked"] += 1
         counts["matched" if status == "matched" else "unresolved"] += 1
         if status != "matched":
            append_review(reviews, "book", key, title, status, candidates)
      except Exception as exc:
         session.execute_write(
            write_book,
            {
               "book_id": key,
               "status": "error",
               "wikidata_id": book["wikidata_id"],
               "openlibrary_id": None,
               "publication_year": None,
               "summary": None,
               "summary_source": None,
               "genres": [],
            },
         )
         counts["books_checked"] += 1
         counts["errors"] += 1
         append_review(reviews, "book", key, title, "error", [], str(exc))
   return counts, reviews


def main() -> None:
   parser = argparse.ArgumentParser(description="Enrich books and authors from Wikidata.")
   parser.add_argument("--refresh", action="store_true", help="Recheck all entities, including accepted matches")
   parser.add_argument("--stale-days", type=int, default=180, help="Refresh accepted matches older than this")
   parser.add_argument("--limit", type=int, help="Limit each entity type, useful for a small test run")
   parser.add_argument("--request-interval-min", type=float, default=5.0, help="Minimum randomized base interval in seconds")
   parser.add_argument("--request-interval-max", type=float, default=10.0, help="Maximum randomized base interval in seconds")
   parser.add_argument("--jitter-min", type=float, default=0.42, help="Minimum positive request jitter in seconds")
   parser.add_argument("--jitter-max", type=float, default=1.42, help="Maximum positive request jitter in seconds")
   parser.add_argument("--review-report", type=Path, help="Review CSV output path")
   args = parser.parse_args()
   if args.stale_days < 1 or args.limit is not None and args.limit < 1:
      parser.error("stale-days and limit must be positive")
   if args.request_interval_min <= 0 or args.request_interval_max < args.request_interval_min:
      parser.error("request interval bounds must be positive and ordered")
   if args.jitter_min < 0 or args.jitter_max < args.jitter_min:
      parser.error("jitter bounds must be nonnegative and ordered")

   load_dotenv()
   driver = GraphDatabase.driver(
      os.environ["NEO4J_URI"],
      auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
   )
   database = os.getenv("NEO4J_DB_NAME") or None
   user_agent = os.getenv("WIKIDATA_USER_AGENT", "books-kg/0.1 (personal knowledge graph)")
   http = JsonHttpClient(
      user_agent,
      args.request_interval_min,
      args.request_interval_max,
      args.jitter_min,
      args.jitter_max,
   )
   wikidata = WikidataClient(http)
   open_library = OpenLibraryClient(http)
   report_path = args.review_report or PROJECT_ROOT / "data" / "reports" / f"enrichment_review_{datetime.now():%Y%m%d_%H%M%S}.csv"
   try:
      with driver.session(database=database) as session:
         counts, reviews = run_enrichment(session, wikidata, open_library, args.refresh, args.stale_days, args.limit)
      write_review_report(report_path, reviews)
      print("Enrichment complete:")
      for key, value in counts.items():
         print(f"  {key}: {value}")
      print(f"Review report: {report_path}")
   finally:
      driver.close()


if __name__ == "__main__":
   main()
