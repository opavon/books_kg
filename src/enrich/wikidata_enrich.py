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
WIKIDATA_ENTITY_BATCH_SIZE = 50

DUE_AUTHORS_QUERY = """
MATCH (a:Author)
WHERE ($refresh OR $force_selected OR a.wikidata_status IS NULL OR a.wikidata_status <> 'matched'
   OR a.wikidata_checked_at IS NULL
   OR a.wikidata_checked_at < datetime() - duration({days: $stale_days}))
  AND ($author_names IS NULL OR a.name IN $author_names)
  AND ($statuses IS NULL OR a.wikidata_status IN $statuses)
RETURN a.name AS name, a.wikidata_id AS wikidata_id, a.wikidata_status AS wikidata_status
ORDER BY a.name
"""

DUE_BOOKS_QUERY = """
MATCH (b:Book)
WHERE b.book_id IS NOT NULL AND (
    $refresh OR $force_selected OR b.wikidata_status IS NULL OR b.wikidata_status <> 'matched'
   OR b.wikidata_checked_at IS NULL
   OR b.wikidata_checked_at < datetime() - duration({days: $stale_days}))
  AND ($book_ids IS NULL OR b.book_id IN $book_ids)
  AND ($statuses IS NULL OR b.wikidata_status IN $statuses)
OPTIONAL MATCH (a:Author)-[:WROTE]->(b)
RETURN b.book_id AS book_id, b.title AS title, b.wikidata_id AS wikidata_id,
       b.wikidata_status AS wikidata_status,
     b.summary AS summary, b.publication_year AS publication_year,
     b.publication_year_source AS publication_year_source,
       collect(DISTINCT {name: a.name, wikidata_id: a.wikidata_id}) AS authors
ORDER BY b.book_id
"""

DUE_SUMMARIES_QUERY = """
MATCH (b:Book)
WHERE b.book_id IS NOT NULL
   AND ($refresh_summaries OR $force_selected OR b.openlibrary_status = 'error'
        OR (b.summary IS NULL AND b.openlibrary_id IS NULL))
    AND ($refresh_summaries OR $force_selected OR b.openlibrary_status IS NULL
       OR b.openlibrary_status = 'error'
     OR b.openlibrary_checked_at IS NULL
     OR b.openlibrary_checked_at < datetime() - duration({days: $stale_days}))
   AND ($force_selected OR $refresh_summaries OR $summary_book_ids IS NULL OR b.book_id IN $summary_book_ids)
  AND ($summary_statuses IS NULL OR b.openlibrary_status IN $summary_statuses)
OPTIONAL MATCH (a:Author)-[:WROTE]->(b)
RETURN b.book_id AS book_id, b.title AS title,
   b.summary AS summary, b.openlibrary_id AS openlibrary_id,
   b.openlibrary_status AS openlibrary_status,
   b.publication_year_source AS publication_year_source,
     collect(DISTINCT a.name) AS authors
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
   SET b.publication_year = $publication_year,
       b.publication_year_source = $publication_year_source)
FOREACH (_ IN CASE WHEN $summary IS NULL THEN [] ELSE [1] END |
   SET b.summary = $summary, b.summary_source = $summary_source)
"""

UPDATE_BOOK_SUMMARY_QUERY = """
MATCH (b:Book {book_id: $book_id})
SET b.openlibrary_status = $status, b.openlibrary_checked_at = datetime()
FOREACH (_ IN CASE WHEN $openlibrary_id IS NULL THEN [] ELSE [1] END |
   SET b.openlibrary_id = $openlibrary_id)
FOREACH (_ IN CASE WHEN $publication_year IS NULL OR b.publication_year_source = 'wikidata'
                   THEN [] ELSE [1] END |
   SET b.publication_year = $publication_year,
       b.publication_year_source = 'openlibrary')
FOREACH (_ IN CASE WHEN $summary IS NULL THEN [] ELSE [1] END |
   SET b.summary = $summary, b.summary_source = 'openlibrary')
"""

RECONCILE_AUTHOR_MATCH_QUERY = """
MATCH (a:Author {name: $name})
OPTIONAL MATCH (a)-[r:HAS_GENDER|CITIZEN_OF]->()
DELETE r
WITH DISTINCT a
FOREACH (_ IN CASE WHEN a.wikidata_id IS NULL THEN [] ELSE [1] END |
   REMOVE a.wikidata_id)
"""

RECONCILE_BOOK_MATCH_QUERY = """
MATCH (b:Book {book_id: $book_id})
OPTIONAL MATCH (b)-[r:HAS_GENRE]->()
DELETE r
WITH DISTINCT b
FOREACH (_ IN CASE WHEN b.publication_year_source = 'wikidata' THEN [1] ELSE [] END |
   REMOVE b.publication_year, b.publication_year_source)
FOREACH (_ IN CASE WHEN b.wikidata_id IS NULL THEN [] ELSE [1] END |
   REMOVE b.wikidata_id)
"""

RECONCILE_BOOK_SUMMARY_QUERY = """
MATCH (b:Book {book_id: $book_id})
REMOVE b.summary, b.summary_source
FOREACH (_ IN CASE WHEN b.publication_year_source = 'openlibrary' THEN [1] ELSE [] END |
   REMOVE b.publication_year, b.publication_year_source)
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
   return bool(instance_ids & BOOK_TYPES)


def book_matches_known_authors(entity: dict, related_author_ids: list[str] | None) -> bool:
   author_ids = claim_entity_ids(entity, "P50")
   return not related_author_ids or not author_ids or bool(set(related_author_ids) & set(author_ids))


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
         {"action": "wbsearchentities", "search": label, "language": "en", "format": "json", "type": "item", "limit": 50}
      )
      return self.http.get_json(f"{WIKIDATA_API}?{params}").get("search", [])

   def entities(self, entity_ids: list[str]) -> dict:
      if not entity_ids:
         return {}
      params = urlencode(
         {
            "action": "wbgetentities",
            "ids": "|".join(entity_ids[:50]),
            "props": "labels|descriptions|aliases|claims",
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
         if (
            entity
            and "missing" not in entity
            and is_entity_type(entity, entity_type)
            and (entity_type != "book" or book_matches_known_authors(entity, related_author_ids))
         ):
            return "matched", known_id, entity, []
         return "error", known_id, None, [
            {"id": known_id, "label": "", "description": "stored entity is missing or fails type/author validation"}
         ]

      search_results = self.search(label)
      result_ids = [result["id"] for result in search_results]
      entities = self.entities(result_ids)
      if entity_type == "author":
         human_results = [
            (result, entities[result["id"]])
            for result in search_results
            if result["id"] in entities and is_entity_type(entities[result["id"]], "author")
         ]
         exact = [
            (result["id"], entity)
            for result, entity in human_results
            if normalize_text(entity_label(entity)) == normalize_text(label)
         ]
         review_candidates = [
            {
               "id": result["id"],
               "label": entity_label(entity),
               "description": entity.get("descriptions", {}).get("en", {}).get("value", ""),
               "aliases": [
                  alias["value"]
                  for alias in entity.get("aliases", {}).get("en", [])
                  if alias.get("value")
               ],
            }
            for result, entity in human_results
         ]
         if len(exact) == 1:
            return "matched", exact[0][0], exact[0][1], review_candidates
         if human_results:
            return "needs_review", None, None, review_candidates
         return "no_candidate", None, None, []

      suitable_books = [
         (result, entities[result["id"]])
         for result in search_results
         if result["id"] in entities
         and is_entity_type(entities[result["id"]], "book")
         and book_matches_known_authors(entities[result["id"]], related_author_ids)
      ]
      exact = [
         (result["id"], entity)
         for result, entity in suitable_books
         if normalize_text(entity_label(entity)) == normalize_text(label)
      ]
      review_candidates = [
         {
            "id": result["id"],
            "label": entity_label(entity) or result.get("label", ""),
            "description": entity.get("descriptions", {}).get("en", {}).get("value", ""),
            "aliases": [alias["value"] for alias in entity.get("aliases", {}).get("en", []) if alias.get("value")],
            "author_ids": claim_entity_ids(entity, "P50"),
         }
         for result, entity in suitable_books
      ]
      if len(exact) == 1:
         return "matched", exact[0][0], exact[0][1], review_candidates
      if suitable_books:
         return "needs_review", None, None, review_candidates
      return "no_candidate", None, None, []

   def validate_manual_id(
      self,
      entity_id: str,
      entity_type: str,
      related_author_ids: list[str] | None = None,
   ) -> dict:
      if not re.fullmatch(r"Q\d+", entity_id):
         raise ValueError(f"Invalid Wikidata QID: {entity_id}")
      entity = self.entities([entity_id]).get(entity_id)
      if (
         not entity
         or "missing" in entity
         or not is_entity_type(entity, entity_type)
         or (entity_type == "book" and not book_matches_known_authors(entity, related_author_ids))
      ):
         raise ValueError(f"Wikidata ID {entity_id} does not validate as a matching {entity_type}")
      return entity


class OpenLibraryClient:
   def __init__(self, http: JsonHttpClient):
      self.http = http

   def summary(
      self,
      title: str,
      authors: list[str],
      selected_id: str | None = None,
   ) -> tuple[str, str | None, int | None, list[dict]]:
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
      selectable_matches = [
         item for item in matches if re.fullmatch(r"/works/OL\d+W", item.get("key", ""))
      ]
      candidates = [
         {
            "id": item.get("key", ""),
            "label": item.get("title", ""),
            "description": ", ".join(item.get("author_name", [])),
            "first_publish_year": item.get("first_publish_year"),
            "matched": item.get("key") in matched_keys,
            "selectable": item.get("key") in matched_keys
            and bool(re.fullmatch(r"/works/OL\d+W", item.get("key", ""))),
         }
         for item in payload.get("docs", [])
      ]
      if selected_id is not None:
         selected = next(
            (
               item
               for item in selectable_matches
               if item.get("key") == selected_id
            ),
            None,
         )
         if selected is None:
            return "invalid_selection", None, None, candidates
      elif len(selectable_matches) != 1:
         return ("ambiguous" if selectable_matches else "not_found", None, None, candidates)
      else:
         selected = selectable_matches[0]

      summary, year = self.fetch_work_summary(
         selected["key"], selected.get("first_publish_year")
      )
      return "matched", summary, year, candidates

   def fetch_work_summary(self, work_key: str, year: int | None = None) -> tuple[str | None, int | None]:
      if not re.fullmatch(r"/works/OL\d+W", work_key):
         raise ValueError(f"Invalid Open Library work ID: {work_key}")
      work = self.http.get_json(f"{OPEN_LIBRARY_API}{work_key}.json")
      description = work.get("description")
      summary = description.get("value") if isinstance(description, dict) else description
      return summary.strip() if isinstance(summary, str) and summary.strip() else None, year


def year_from_claims(entity: dict) -> int | None:
   years = []
   for claim in entity.get("claims", {}).get("P577", []):
      value = claim.get("mainsnak", {}).get("datavalue", {}).get("value", {})
      match = re.match(r"^[+-]?(\d{4})", value.get("time", ""))
      if match:
         years.append(int(match.group(1)))
   return min(years) if years else None


def labels_for_ids(client: WikidataClient, entity: dict, property_id: str) -> list[dict]:
   return labels_for_properties(client, entity, [property_id])[property_id]


def labels_for_properties(
   client: WikidataClient, entity: dict, property_ids: list[str]
) -> dict[str, list[dict]]:
   ids_by_property = {
      property_id: claim_entity_ids(entity, property_id)
      for property_id in property_ids
   }
   all_ids = list(dict.fromkeys(
      entity_id
      for ids in ids_by_property.values()
      for entity_id in ids
   ))
   entities = {}
   for start in range(0, len(all_ids), WIKIDATA_ENTITY_BATCH_SIZE):
      entities.update(
         client.entities(all_ids[start : start + WIKIDATA_ENTITY_BATCH_SIZE])
      )
   return {
      property_id: [
         {"id": entity_id, "name": entity_label(entities[entity_id])}
         for entity_id in ids
         if entity_id in entities and entity_label(entities[entity_id])
      ]
      for property_id, ids in ids_by_property.items()
   }


def write_author(
   tx,
   name: str,
   status: str,
   wikidata_id: str | None,
   genders: list[dict],
   countries: list[dict],
   previous_wikidata_id: str | None = None,
) -> None:
   if previous_wikidata_id and wikidata_id and previous_wikidata_id != wikidata_id:
      tx.run(RECONCILE_AUTHOR_MATCH_QUERY, name=name).consume()
   tx.run(UPDATE_AUTHOR_QUERY, name=name, status=status, wikidata_id=wikidata_id).consume()
   for gender in genders:
      tx.run(MERGE_GENDER_QUERY, author_name=name, **{"name": gender["name"], "wikidata_id": gender["id"]}).consume()
   for country in countries:
      tx.run(MERGE_COUNTRY_QUERY, author_name=name, **{"name": country["name"], "wikidata_id": country["id"]}).consume()


def write_book(tx, record: dict) -> None:
   previous_wikidata_id = record.get("previous_wikidata_id")
   if previous_wikidata_id and record.get("wikidata_id") and previous_wikidata_id != record["wikidata_id"]:
      tx.run(RECONCILE_BOOK_MATCH_QUERY, book_id=record["book_id"]).consume()
   update = {key: value for key, value in record.items() if key != "previous_wikidata_id"}
   tx.run(UPDATE_BOOK_QUERY, **update).consume()
   for genre in record["genres"]:
      tx.run(MERGE_GENRE_QUERY, book_id=record["book_id"], name=genre["name"], wikidata_id=genre["id"]).consume()


def write_book_summary(tx, record: dict) -> None:
   previous_openlibrary_id = record.get("previous_openlibrary_id")
   if (
      previous_openlibrary_id
      and record.get("openlibrary_id")
      and previous_openlibrary_id != record["openlibrary_id"]
   ):
      tx.run(RECONCILE_BOOK_SUMMARY_QUERY, book_id=record["book_id"]).consume()
   update = {key: value for key, value in record.items() if key != "previous_openlibrary_id"}
   tx.run(UPDATE_BOOK_SUMMARY_QUERY, **update).consume()


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


def load_review_report(path: Path) -> list[dict]:
   required = {"type", "key", "label", "status", "reason", "candidates"}
   with path.open(newline="", encoding="utf-8-sig") as source:
      reader = csv.DictReader(source, strict=True)
      if reader.fieldnames is None:
         raise ValueError(f"Review report is empty: {path}")
      missing = required - set(reader.fieldnames)
      if missing:
         raise ValueError(f"Review report is missing columns: {', '.join(sorted(missing))}")
      rows = []
      for line_number, row in enumerate(reader, start=2):
         if None in row:
            raise ValueError(f"Malformed review report near line {line_number}")
         if row["type"] not in {"author", "book", "book_summary"}:
            raise ValueError(f"Unsupported review type on line {line_number}: {row['type']}")
         try:
            candidates = json.loads(row["candidates"] or "[]")
         except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid candidates JSON on report line {line_number}") from exc
         if not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
            raise ValueError(f"Candidates must be a JSON list of objects on report line {line_number}")
         rows.append({**row, "candidates": candidates, "report_line": line_number})
   return rows


def filter_review_rows(
   rows: list[dict],
   entity_types: list[str] | None = None,
   statuses: list[str] | None = None,
   summary_statuses: list[str] | None = None,
   author_names: list[str] | None = None,
   book_ids: list[str] | None = None,
) -> list[dict]:
   selected = []
   entity_type_set = set(entity_types or [])
   status_set = set(statuses or [])
   summary_status_set = set(summary_statuses or [])
   author_name_set = set(author_names or [])
   book_id_set = set(book_ids or [])
   for row in rows:
      kind = row["type"]
      key = row["key"]
      if entity_type_set and kind not in entity_type_set:
         continue
      if author_name_set or book_id_set:
         author_match = kind == "author" and key in author_name_set
         book_match = kind in {"book", "book_summary"} and key in book_id_set
         if not (author_match or book_match):
            continue
      if kind == "author":
         if status_set and row["status"] not in status_set:
            continue
      else:
         if kind == "book" and status_set and row["status"] not in status_set:
            continue
         if kind == "book_summary" and summary_status_set and row["status"] not in summary_status_set:
            continue
      selected.append(row)
   return selected


def parse_key_qid(values: list[str] | None, option_name: str) -> dict[str, str]:
   parsed = {}
   for value in values or []:
      key, separator, entity_id = value.partition("=")
      if not separator or not key.strip() or not re.fullmatch(r"Q\d+", entity_id.strip()):
         raise ValueError(f"{option_name} values must use KEY=QID format")
      key = key.strip()
      if key in parsed:
         raise ValueError(f"Duplicate {option_name} key: {key}")
      parsed[key] = entity_id.strip()
   return parsed


def build_argument_parser() -> argparse.ArgumentParser:
   parser = argparse.ArgumentParser(description="Enrich books and authors from Wikidata and Open Library.")
   parser.add_argument("--refresh", action="store_true", help="Refresh due Wikidata facts, including stored matches; does not rematch stored IDs")
   parser.add_argument("--refresh-summaries", action="store_true", help="Retry Open Library summaries independently of Wikidata refresh")
   parser.add_argument("--stale-days", type=int, default=180, help="Refresh accepted matches or summary lookups older than this")
   parser.add_argument("--limit", type=int, help="Limit each selected queue after sorting; not an ID selector")
   parser.add_argument("--interactive", action="store_true", help="Prompt for eligible Wikidata and Open Library candidates")
   parser.add_argument("--entity-type", choices=("author", "book", "book_summary"), action="append")
   parser.add_argument("--author", dest="author_names", action="append", help="Select an exact graph author name; repeatable")
   parser.add_argument("--book-id", dest="book_ids", action="append", help="Select a stable book_id; repeatable")
   parser.add_argument("--status", choices=("matched", "needs_review", "no_candidate", "error"), action="append")
   parser.add_argument("--summary-status", choices=("matched", "ambiguous", "not_found", "error"), action="append")
   parser.add_argument("--from-report", type=Path, help="Process only rows from an earlier review report")
   parser.add_argument("--rematch-author", action="append", default=[], help="Interactively replace this author's stored Wikidata match")
   parser.add_argument("--rematch-book-id", action="append", default=[], help="Interactively replace this book's stored Wikidata match")
   parser.add_argument("--manual-author-qid", action="append", default=[], metavar="NAME=QID", help="Set a manually verified author match")
   parser.add_argument("--manual-book-qid", action="append", default=[], metavar="BOOK_ID=QID", help="Set a manually verified book match")
   parser.add_argument("--request-interval-min", type=float, default=5.0, help="Minimum randomized base interval in seconds")
   parser.add_argument("--request-interval-max", type=float, default=10.0, help="Maximum randomized base interval in seconds")
   parser.add_argument("--jitter-min", type=float, default=0.42, help="Minimum positive request jitter in seconds")
   parser.add_argument("--jitter-max", type=float, default=1.42, help="Maximum positive request jitter in seconds")
   parser.add_argument("--review-report", type=Path, help="Review CSV output path")
   return parser


def prompt_author_candidate(name: str, candidates: list[dict], input_fn=None) -> str | None:
   print(f"\nChoose a Wikidata candidate for {name}, or press Enter to skip:")
   for index, candidate in enumerate(candidates, start=1):
      description = candidate.get("description", "")
      aliases = ", ".join(candidate.get("aliases", []))
      details = "; ".join(value for value in (description, f"Aliases: {aliases}" if aliases else "") if value)
      print(f"  {index}. {candidate['id']}  {candidate.get('label', '')}  {details}")
   choice = (input_fn or input)("Selection: ").strip()
   if not choice:
      return None
   try:
      index = int(choice)
   except ValueError:
      print("Invalid selection; leaving the author for review.")
      return None
   if index < 1 or index > len(candidates):
      print("Invalid selection; leaving the author for review.")
      return None
   return candidates[index - 1]["id"]


def confirm_author_candidate(candidate_id: str | None, candidates: list[dict], wikidata: WikidataClient) -> tuple[str, dict] | None:
   candidate_ids = {candidate.get("id") for candidate in candidates}
   if not candidate_id or candidate_id not in candidate_ids:
      return None
   entity = wikidata.entities([candidate_id]).get(candidate_id)
   if not entity or "missing" in entity or not is_entity_type(entity, "author"):
      return None
   return candidate_id, entity


def prompt_book_candidate(title: str, candidates: list[dict], input_fn=None) -> str | None:
   print(f"\nChoose a Wikidata candidate for {title}, or press Enter to skip:")
   for index, candidate in enumerate(candidates, start=1):
      details = [candidate.get("description", "")]
      if candidate.get("author_ids"):
         details.append(f"Author IDs: {', '.join(candidate['author_ids'])}")
      if candidate.get("aliases"):
         details.append(f"Aliases: {', '.join(candidate['aliases'])}")
      print(f"  {index}. {candidate['id']}  {candidate.get('label', '')}  {'; '.join(value for value in details if value)}")
   choice = (input_fn or input)("Selection: ").strip()
   if not choice:
      return None
   try:
      index = int(choice)
   except ValueError:
      print("Invalid selection; leaving the book for review.")
      return None
   if index < 1 or index > len(candidates):
      print("Invalid selection; leaving the book for review.")
      return None
   return candidates[index - 1]["id"]


def prompt_openlibrary_candidate(title: str, candidates: list[dict], input_fn=None) -> str | None:
   selectable = [
      candidate
      for candidate in candidates
      if candidate.get("matched")
      and re.fullmatch(r"/works/OL\d+W", candidate.get("id", ""))
   ]
   if not selectable:
      print(f"\nNo selectable Open Library work candidates for {title}; leaving the summary unresolved.")
      return None
   print(f"\nChoose an exact Open Library work for {title}, or press Enter to skip:")
   for index, candidate in enumerate(selectable, start=1):
      print(f"  {index}. {candidate['id']}  {candidate.get('label', '')}  {candidate.get('description', '')}")
   choice = (input_fn or input)("Selection: ").strip()
   if not choice:
      return None
   try:
      index = int(choice)
   except ValueError:
      print("Invalid selection; leaving the summary for review.")
      return None
   if index < 1 or index > len(selectable):
      print("Invalid selection; leaving the summary for review.")
      return None
   return selectable[index - 1]["id"]


def confirm_book_candidate(
   candidate_id: str | None,
   candidates: list[dict],
   wikidata: WikidataClient,
   related_author_ids: list[str] | None = None,
) -> tuple[str, dict] | None:
   candidate_ids = {candidate.get("id") for candidate in candidates}
   if not candidate_id or candidate_id not in candidate_ids:
      return None
   entity = wikidata.entities([candidate_id]).get(candidate_id)
   if (
      not entity
      or "missing" in entity
      or not is_entity_type(entity, "book")
      or not book_matches_known_authors(entity, related_author_ids)
   ):
      return None
   return candidate_id, entity


def run_enrichment(
   session,
   wikidata: WikidataClient,
   open_library: OpenLibraryClient,
   refresh: bool,
   stale_days: int,
   limit: int | None,
   interactive: bool = False,
   input_fn=None,
   entity_types: set[str] | None = None,
   author_names: list[str] | None = None,
   book_ids: list[str] | None = None,
   statuses: list[str] | None = None,
   summary_statuses: list[str] | None = None,
   report_rows: list[dict] | None = None,
   rematch_authors: set[str] | None = None,
   rematch_books: set[str] | None = None,
   manual_author_qids: dict[str, str] | None = None,
   manual_book_qids: dict[str, str] | None = None,
   refresh_summaries: bool = False,
   summary_book_ids: list[str] | None = None,
) -> tuple[dict, list[dict]]:
   entity_types = entity_types or {"author", "book", "book_summary"}
   rematch_authors = rematch_authors or set()
   rematch_books = rematch_books or set()
   manual_author_qids = manual_author_qids or {}
   manual_book_qids = manual_book_qids or {}
   report_rows = report_rows or []
   report_index = {(row["type"], row["key"]): row for row in report_rows}
   query_statuses = None if report_rows else statuses
   query_summary_statuses = None if report_rows else summary_statuses
   report_author_names = {row["key"] for row in report_rows if row["type"] == "author"}
   report_book_ids = {row["key"] for row in report_rows if row["type"] == "book"}
   report_summary_ids = {row["key"] for row in report_rows if row["type"] == "book_summary"}
   force_authors = bool(author_names or statuses or report_author_names or rematch_authors or manual_author_qids)
   force_books = bool(book_ids or statuses or report_book_ids or rematch_books or manual_book_qids)
   force_summaries = bool(summary_book_ids or summary_statuses or report_summary_ids or refresh_summaries)
   print("Finding selected entities that need enrichment or review...", flush=True)
   authors = []
   books = []
   summaries = []
   if "author" in entity_types:
      authors = list(
         session.run(
            DUE_AUTHORS_QUERY,
            refresh=refresh,
            stale_days=stale_days,
            force_selected=force_authors,
            author_names=author_names or None,
            statuses=query_statuses or None,
         )
      )
   if "book" in entity_types:
      books = list(
         session.run(
            DUE_BOOKS_QUERY,
            refresh=refresh,
            stale_days=stale_days,
            force_selected=force_books,
            book_ids=book_ids or None,
            statuses=query_statuses or None,
         )
      )
   if "book_summary" in entity_types and open_library is not None:
      summaries = list(
         session.run(
            DUE_SUMMARIES_QUERY,
            stale_days=stale_days,
            refresh_summaries=refresh_summaries,
            force_selected=force_summaries,
            summary_book_ids=summary_book_ids or None,
            summary_statuses=query_summary_statuses or None,
         )
      )
   found_author_names = {author["name"] for author in authors}
   missing_author_targets = (set(manual_author_qids) | rematch_authors) - found_author_names
   if missing_author_targets:
      raise ValueError(f"Author target(s) not found: {', '.join(sorted(missing_author_targets))}")
   found_book_ids = {book["book_id"] for book in books}
   missing_book_targets = (set(manual_book_qids) | rematch_books) - found_book_ids
   if missing_book_targets:
      raise ValueError(f"Book target(s) not found: {', '.join(sorted(missing_book_targets))}")
   if limit is not None:
      authors = authors[:limit]
      books = books[:limit]
      summaries = summaries[:limit]

   print(
      f"Enrichment queue: {len(authors)} authors, {len(books)} books, "
      f"{len(summaries)} summaries.",
      flush=True,
   )
   counts = {
      "authors_checked": 0,
      "books_checked": 0,
      "summaries_checked": 0,
      "matched": 0,
      "unresolved": 0,
      "summary_matched": 0,
      "summary_unresolved": 0,
      "errors": 0,
      "summary_errors": 0,
   }
   reviews = []
   for index, author in enumerate(authors, start=1):
      name = author["name"]
      previous_wikidata_id = author["wikidata_id"]
      rematching = name in rematch_authors
      replacement_accepted = not rematching
      progress_status = "error"
      progress_detail = ""
      try:
         if name in manual_author_qids:
            wikidata_id = manual_author_qids[name]
            entity = wikidata.validate_manual_id(wikidata_id, "author")
            status, candidates = "matched", []
            replacement_accepted = True
         else:
            cached = report_index.get(("author", name))
            if interactive and cached and cached["status"] == "needs_review" and not rematching and not previous_wikidata_id:
               status, wikidata_id, entity, candidates = "needs_review", None, None, cached["candidates"]
            else:
               status, wikidata_id, entity, candidates = wikidata.resolve(
                  name, "author", None if rematching else previous_wikidata_id
               )
         if interactive and candidates and (status == "needs_review" or rematching):
            selection = prompt_author_candidate(name, candidates, input_fn)
            accepted = confirm_author_candidate(selection, candidates, wikidata)
            if accepted:
               wikidata_id, entity = accepted
               status = "matched"
               replacement_accepted = True
         if rematching and not replacement_accepted:
            counts["authors_checked"] += 1
            counts["unresolved"] += 1
            append_review(reviews, "author", name, name, status, candidates, "Existing match was retained.")
            progress_status = f"{status}; existing match retained"
            print(f"[author {index}/{len(authors)}] {name}: {progress_status}", flush=True)
            continue
         author_facts = labels_for_properties(wikidata, entity, ["P21", "P27"]) if entity else {}
         genders = author_facts.get("P21", [])
         countries = author_facts.get("P27", [])
         session.execute_write(
            write_author,
            name,
            status,
            wikidata_id,
            genders,
            countries,
            previous_wikidata_id,
         )
         counts["authors_checked"] += 1
         counts["matched" if status == "matched" else "unresolved"] += 1
         progress_status = status
         if status != "matched":
            append_review(reviews, "author", name, name, status, candidates)
      except Exception as exc:
         if not rematching and name not in manual_author_qids:
            session.execute_write(write_author, name, "error", previous_wikidata_id, [], [])
         counts["authors_checked"] += 1
         counts["errors"] += 1
         append_review(reviews, "author", name, name, "error", [], str(exc))
         progress_detail = f": {exc}"
      print(f"[author {index}/{len(authors)}] {name}: {progress_status}{progress_detail}", flush=True)

   for index, book in enumerate(books, start=1):
      title = book["title"]
      key = book["book_id"]
      previous_wikidata_id = book["wikidata_id"]
      rematching = key in rematch_books
      replacement_accepted = not rematching
      book_authors = [author for author in (book["authors"] or []) if author.get("name")]
      authors_for_book = [author["name"] for author in book_authors]
      author_ids = [author["wikidata_id"] for author in book_authors if author.get("wikidata_id")]
      progress_status = "error"
      progress_detail = ""
      try:
         if key in manual_book_qids:
            wikidata_id = manual_book_qids[key]
            entity = wikidata.validate_manual_id(wikidata_id, "book", author_ids)
            status, candidates = "matched", []
            replacement_accepted = True
         else:
            cached = report_index.get(("book", key))
            if interactive and cached and cached["status"] == "needs_review" and not rematching and not previous_wikidata_id:
               status, wikidata_id, entity, candidates = "needs_review", None, None, cached["candidates"]
            else:
               status, wikidata_id, entity, candidates = wikidata.resolve(
                  title,
                  "book",
                  None if rematching else previous_wikidata_id,
                  related_author_ids=author_ids,
               )
         if interactive and candidates and (status == "needs_review" or rematching):
            selection = prompt_book_candidate(title, candidates, input_fn)
            accepted = confirm_book_candidate(selection, candidates, wikidata, author_ids)
            if accepted:
               wikidata_id, entity = accepted
               status = "matched"
               replacement_accepted = True
         genres = labels_for_ids(wikidata, entity, "P136") if entity else []
         publication_year = year_from_claims(entity) if entity else None
         publication_year_source = "wikidata" if publication_year is not None else None

         if rematching and not replacement_accepted:
            counts["books_checked"] += 1
            counts["unresolved"] += 1
            append_review(reviews, "book", key, title, status, candidates, "Existing match was retained.")
            progress_status = f"{status}; existing match retained"
            print(f"[book {index}/{len(books)}] {key} {title}: {progress_status}", flush=True)
            continue

         record = {
            "book_id": key,
            "status": status,
            "wikidata_id": wikidata_id,
            "openlibrary_id": None,
            "publication_year": publication_year,
            "publication_year_source": publication_year_source,
            "summary": None,
            "summary_source": None,
            "previous_wikidata_id": previous_wikidata_id,
            "genres": genres,
         }
         session.execute_write(write_book, record)
         counts["books_checked"] += 1
         counts["matched" if status == "matched" else "unresolved"] += 1
         progress_status = status
         if status != "matched":
            append_review(reviews, "book", key, title, status, candidates)
      except Exception as exc:
         if not rematching and key not in manual_book_qids:
            session.execute_write(
               write_book,
               {
                  "book_id": key,
                  "status": "error",
                  "wikidata_id": previous_wikidata_id,
                  "openlibrary_id": None,
                  "previous_wikidata_id": previous_wikidata_id,
                  "publication_year": None,
                  "publication_year_source": None,
                  "summary": None,
                  "summary_source": None,
                  "genres": [],
               },
            )
         counts["books_checked"] += 1
         counts["errors"] += 1
         append_review(reviews, "book", key, title, "error", [], str(exc))
         progress_detail = f": {exc}"
      print(f"[book {index}/{len(books)}] {key} {title}: {progress_status}{progress_detail}", flush=True)

   for index, book in enumerate(summaries, start=1):
      key = book["book_id"]
      title = book["title"]
      if not refresh_summaries and (
         book.get("summary")
         or (book.get("openlibrary_id") and book.get("openlibrary_status") == "matched")
      ):
         continue
      authors = [name for name in (book["authors"] or []) if name]
      cached = report_index.get(("book_summary", key))
      candidates = []
      selected_id = None
      summary = None
      year = None
      reason = ""
      try:
         if interactive and cached and cached["status"] == "ambiguous":
            candidates = cached["candidates"]
            selected_id = prompt_openlibrary_candidate(title, candidates, input_fn)
            if selected_id is None:
               status = "ambiguous"
            else:
               status, summary, year, candidates = open_library.summary(title, authors, selected_id)
         else:
            status, summary, year, candidates = open_library.summary(title, authors)
            if status == "ambiguous" and interactive:
               selected_id = prompt_openlibrary_candidate(title, candidates, input_fn)
               if selected_id is not None:
                  selected_candidate = next(
                     (candidate for candidate in candidates if candidate.get("id") == selected_id),
                     None,
                  )
                  if selected_candidate is None or not selected_candidate.get("selectable"):
                     status = "ambiguous"
                     reason = "Selected work is not in the current exact-match candidate list."
                  else:
                     summary, year = open_library.fetch_work_summary(
                        selected_id, selected_candidate.get("first_publish_year")
                     )
                     status = "matched"
         if status == "invalid_selection":
            status = "ambiguous"
            reason = "Selected work no longer matches the current title-and-author search."
         openlibrary_id = selected_id if status == "matched" and selected_id else None
         if status == "matched" and openlibrary_id is None:
            openlibrary_id = next(
               (item["id"] for item in candidates if item.get("selectable") and item.get("matched")),
               None,
            )
         session.execute_write(
            write_book_summary,
            {
               "book_id": key,
               "status": status,
               "openlibrary_id": openlibrary_id,
               "previous_openlibrary_id": book.get("openlibrary_id"),
               "summary": summary if status == "matched" else None,
               "publication_year": year if status == "matched" else None,
            },
         )
         counts["summaries_checked"] += 1
         if status == "matched":
            counts["summary_matched"] += 1
         else:
            counts["summary_unresolved"] += 1
            append_review(reviews, "book_summary", key, title, status, candidates, reason)
      except Exception as exc:
         status = "error"
         session.execute_write(
            write_book_summary,
            {
               "book_id": key,
               "status": "error",
               "openlibrary_id": None,
               "previous_openlibrary_id": book.get("openlibrary_id"),
               "summary": None,
               "publication_year": None,
            },
         )
         counts["summaries_checked"] += 1
         counts["summary_errors"] += 1
         append_review(reviews, "book_summary", key, title, "error", candidates, str(exc))
      print(f"[summary {index}/{len(summaries)}] {key} {title}: {status}", flush=True)
   return counts, reviews


def main() -> None:
   parser = build_argument_parser()
   args = parser.parse_args()
   if args.stale_days < 1 or args.limit is not None and args.limit < 1:
      parser.error("stale-days and limit must be positive")
   if args.request_interval_min <= 0 or args.request_interval_max < args.request_interval_min:
      parser.error("request interval bounds must be positive and ordered")
   if args.jitter_min < 0 or args.jitter_max < args.jitter_min:
      parser.error("jitter bounds must be nonnegative and ordered")
   if (args.rematch_author or args.rematch_book_id) and not args.interactive:
      parser.error("--rematch-author and --rematch-book-id require --interactive")
   try:
      manual_author_qids = parse_key_qid(args.manual_author_qid, "--manual-author-qid")
      manual_book_qids = parse_key_qid(args.manual_book_qid, "--manual-book-qid")
   except ValueError as exc:
      parser.error(str(exc))

   report_rows = None
   if args.from_report:
      try:
         report_rows = filter_review_rows(
            load_review_report(args.from_report),
            args.entity_type,
            args.status,
            args.summary_status,
            args.author_names,
            args.book_ids,
         )
      except (OSError, ValueError, csv.Error) as exc:
         parser.error(f"cannot load review report: {exc}")
      if not report_rows:
         parser.error("no review rows match the requested report filters")

   explicit_types = set(args.entity_type or [])
   author_targeted = bool(args.author_names or args.rematch_author or manual_author_qids)
   if explicit_types:
      if author_targeted and "author" not in explicit_types:
         parser.error("author selectors require --entity-type author")
      if (args.rematch_book_id or manual_book_qids) and "book" not in explicit_types:
         parser.error("book rematch/manual-ID options require --entity-type book")
      if args.book_ids and not explicit_types.intersection({"book", "book_summary"}):
         parser.error("--book-id requires --entity-type book or book_summary")
      if args.summary_status and "book_summary" not in explicit_types:
         parser.error("--summary-status requires --entity-type book_summary")
      if args.status and not explicit_types.intersection({"author", "book"}):
         parser.error("--status applies to Wikidata authors/books, not summaries")
      if args.status and explicit_types == {"book_summary"}:
         parser.error("use --summary-status for book_summary rows")
   if args.status and (args.rematch_author or args.rematch_book_id or manual_author_qids or manual_book_qids):
      parser.error("--status cannot be combined with manual-ID or rematch operations")

   selected_author_names = set(args.author_names or [])
   selected_author_names.update(args.rematch_author)
   selected_author_names.update(manual_author_qids)
   selected_book_ids = set(args.book_ids or [])
   selected_book_ids.update(args.rematch_book_id)
   selected_book_ids.update(manual_book_qids)
   selected_summary_book_ids = set(args.book_ids or [])
   if report_rows is not None:
      selected_author_names.update(row["key"] for row in report_rows if row["type"] == "author")
      selected_book_ids.update(row["key"] for row in report_rows if row["type"] == "book")
      selected_summary_book_ids.update(row["key"] for row in report_rows if row["type"] == "book_summary")

   entity_types = set(args.entity_type or [])
   if not entity_types:
      if report_rows is not None:
         entity_types = {row["type"] for row in report_rows}
      else:
         if args.author_names or args.rematch_author or manual_author_qids:
            entity_types.add("author")
         if args.rematch_book_id or manual_book_qids:
            entity_types.add("book")
         if args.book_ids:
            entity_types.update({"book", "book_summary"})
         if args.status:
            entity_types.update({"author", "book"})
         if args.summary_status or args.refresh_summaries:
            entity_types.add("book_summary")
      if not entity_types:
         entity_types = {"author", "book", "book_summary"}
   if args.entity_type is None:
      if args.rematch_author or manual_author_qids:
         entity_types.add("author")
      if args.rematch_book_id or manual_book_qids:
         entity_types.add("book")
   if args.status and not entity_types.intersection({"author", "book"}):
      parser.error("--status applies to Wikidata authors/books, not summaries")
   if args.summary_status and "book_summary" not in entity_types:
      parser.error("--summary-status requires the book_summary entity type")
   if args.refresh and not entity_types.intersection({"author", "book"}):
      parser.error("--refresh applies to Wikidata author/book records; use --refresh-summaries for Open Library")
   if args.refresh_summaries and "book_summary" not in entity_types:
      parser.error("--refresh-summaries requires the book_summary entity type")

   book_ids = sorted(selected_book_ids) or None
   summary_book_ids = sorted(selected_summary_book_ids) or None
   author_names = sorted(selected_author_names) or None

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
         counts, reviews = run_enrichment(
            session,
            wikidata,
            open_library,
            args.refresh,
            args.stale_days,
            args.limit,
            interactive=args.interactive,
            entity_types=entity_types,
            author_names=author_names,
            book_ids=book_ids,
            statuses=args.status,
            summary_statuses=args.summary_status,
            report_rows=report_rows,
            rematch_authors=set(args.rematch_author),
            rematch_books=set(args.rematch_book_id),
            manual_author_qids=manual_author_qids,
            manual_book_qids=manual_book_qids,
            refresh_summaries=args.refresh_summaries,
            summary_book_ids=summary_book_ids,
         )
      write_review_report(report_path, reviews)
      print("Enrichment complete:")
      for key, value in counts.items():
         print(f"  {key}: {value}")
      print(f"Review report: {report_path}")
   finally:
      driver.close()


if __name__ == "__main__":
   main()
