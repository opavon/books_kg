# Books KG

A personal Neo4j knowledge graph of books read, reading dates and scores, authors, genres, and selected author attributes.

## Data model

```mermaid
graph LR
    Author -->|WROTE| Book
    Read -->|OF_BOOK| Book
    Book -->|HAS_GENRE| Genre
    Author -->|HAS_GENDER| Gender
    Author -->|CITIZEN_OF| Country
```

`Book.book_id` is the stable source identity; `Book.title` is the canonical title. Each book has one `Read` node keyed by the same ID, storing the reading date, score, `title_read`, and `language_read`. The complete model and provenance rules are in [schema/schema.md](schema/schema.md).

## Setup

1. Install [Neo4j Desktop](https://neo4j.com/download/) and create and start a local DBMS. Use its default `neo4j` database; note the Bolt URI, username, and password.
2. Install [uv](https://docs.astral.sh/uv/getting-started/installation/). On Windows, you can install it from PowerShell with:

   ```powershell
   winget install --id=astral-sh.uv -e
   ```

   Reopen PowerShell, then install the project Python version and dependencies:

   ```powershell
   uv python install 3.14.3
   uv sync
   Copy-Item .env.example .env
   ```

3. Set `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DB_NAME`, and `BOOKS_CSV_PATH` in `.env`. The example path points to the supplied CSV; change it if your cumulative file has a different name or location. Relative paths are resolved from the project root. Never commit `.env` or share its password.
4. In Neo4j Desktop, open **Query** for the running `neo4j` database, then run the statements in `schema/constraints.cypher`. This creates the graph's uniqueness constraints; do it once before the first CSV load. The file contains no migration or data-deletion steps. See the [user guide](docs/user-guide.md) for detailed setup and an optional Cypher Shell installation path.

## Cumulative CSV

Maintain one append-only cumulative CSV. The `.env` setting `BOOKS_CSV_PATH` selects the file; the example value is `data/raw/books_raw_<date>.csv`. Its headers are:

```text
book_id,title,title_read,language_read,author,year_month,score
```

Keep `book_id` stable and unique, including leading zeros. Each book appears once. Use `YYYY_MM` for `year_month`; the day is set to `01`. Scores must be finite numbers and may be decimal. Separate authors with `/`; the loader trims each name and creates a separate author relationship. `title_read` and `language_read` are required and must be nonblank; surrounding whitespace is trimmed. They are stored on `Read`, while `Book.title` remains the canonical title.

The loader validates the entire CSV before connecting to Neo4j. From the project root, run:

```powershell
uv run python src/ingest/load_books.py
```

Books and Reads are merged by `book_id`; Authors and relationships are merged. Re-running the full cumulative file is safe and reports inserted, updated, and unchanged rows. Do not reuse a book ID for a different book.

## Wikidata enrichment

Run after ingestion:

```powershell
uv run python src/enrich/wikidata_enrich.py
```

Wikidata is primary. Authors are automatically matched only when exactly one candidate has the same normalized English label and is verified as a human. When there is no unique exact human match but one or more verified human candidates exist, the author is reported as `needs_review`; `no_candidate` means the attempted search returned no verified human candidates, not that the author is absent from Wikidata. Search results are limited and ranked, so they cannot prove absence. Non-human results are excluded from author candidates. For books, automatic matching likewise requires one exact normalized English label and the existing book type check. When known authors have Wikidata IDs, candidates with conflicting `P50` author claims are excluded; candidates with no `P50` claims or at least one matching author remain eligible. Books with suitable candidates but no unique exact match are `needs_review`; `no_candidate` means the search found no candidates passing these checks. This does not prove that the work is absent from Wikidata. Review reports include suitable candidate IDs, labels, descriptions, English aliases, and book author IDs when available.

Use `--interactive` to choose a numbered candidate for each author or book needing review; press Enter to skip. Author choices are revalidated as humans, and book choices are revalidated against the book type and known-author checks, before their IDs are stored. Skipped choices remain in the timestamped CSV under `data/reports/` for later review. Without `--interactive`, enrichment remains non-interactive and still writes the report. Stored accepted Wikidata IDs are reused on future runs. New and unresolved entities are checked each run; accepted matches are refreshed after 180 days. Use `--refresh` to recheck all records or `--limit 5` for a small trial. Each HTTP request waits a randomized 5–10 second base interval plus 0.42–1.42 seconds of positive jitter. Adjust the bounds with `--request-interval-min`, `--request-interval-max`, `--jitter-min`, and `--jitter-max`. The script prints queue sizes and progress after each entity. `WIKIDATA_USER_AGENT` can be set in `.env`.

Lookup errors do not erase stored facts. Gender and genre remain nodes. Wikidata P27 is modeled as `CITIZEN_OF`, not assumed to mean nationality. Open Library summary lookup remains separate from Wikidata book matching: it is used only when no summary exists and a unique exact title-and-author work match is found. Summary source is recorded; Wikidata descriptions are not treated as book summaries.

## Rerun order

After the initial setup, for each update to the cumulative CSV:

1. Run `uv run python src/ingest/load_books.py`.
2. Run `uv run python src/enrich/wikidata_enrich.py` and review `data/reports/`.
3. Run the desired queries from `src/queries/analytics.cypher`.

The enrichment script skips fresh successful matches, retries unresolved entities, and refreshes accepted matches after 180 days. `--refresh` forces refresh. Query 3 takes `$book_id`; query 4 requires genre data. Gender and citizenship analytics include only classified authors; a multi-author book may contribute to multiple categories.

To visualize the graph in Neo4j Browser, run a query that returns a path:

```cypher
MATCH p = (a:Author)-[:WROTE]->(b:Book)<-[:OF_BOOK]-(r:Read)
RETURN p
LIMIT 25;
```

For language counts and reading-title comparisons, run queries 7 and 8 in
`src/queries/analytics.cypher`.

## Roadmap

- [x] Phase 1: core graph schema, cumulative CSV ingestion, and reading analytics
- [x] Phase 2: idempotent ingestion, Wikidata/Open Library enrichment, genre overlap, and gender/citizenship analytics
- [ ] Phase 3: personalized recommendations using Neo4j Graph Data Science
****