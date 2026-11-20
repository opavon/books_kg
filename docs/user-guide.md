# Books KG User Guide

This guide starts from a fresh computer/database setup and walks through updating the graph from the one cumulative CSV, enriching it, and exploring the results. Commands below are for PowerShell, opened at the repository root (`books_kg`).

## Contents

- [Books KG User Guide](#books-kg-user-guide)
  - [Contents](#contents)
  - [What You Are Running](#what-you-are-running)
  - [1. Start Neo4j](#1-start-neo4j)
  - [2. Prepare Python and Credentials](#2-prepare-python-and-credentials)
  - [3. Apply the Graph Schema](#3-apply-the-graph-schema)
    - [Optional: Use Cypher Shell](#optional-use-cypher-shell)
  - [4. Maintain the Cumulative CSV](#4-maintain-the-cumulative-csv)
  - [5. Update the Graph from the CSV](#5-update-the-graph-from-the-csv)
  - [6. Enrich the Graph](#6-enrich-the-graph)
    - [Correcting or Resetting Enrichment](#correcting-or-resetting-enrichment)
      - [Correct a CSV author name](#correct-a-csv-author-name)
      - [Replace a Wikidata match](#replace-a-wikidata-match)
      - [Reset a match manually](#reset-a-match-manually)
      - [Remove one incorrect genre relationship](#remove-one-incorrect-genre-relationship)
      - [Refresh facts from the same Wikidata match](#refresh-facts-from-the-same-wikidata-match)
  - [7. Explore the Graph](#7-explore-the-graph)
    - [Use Case: Find Authors You Read Often](#use-case-find-authors-you-read-often)
    - [Use Case: Compare Scores by Month or Season](#use-case-compare-scores-by-month-or-season)
    - [Use Case: See What You Read Around a Book](#use-case-see-what-you-read-around-a-book)
    - [Use Case: Discover Authors with Shared Genres](#use-case-discover-authors-with-shared-genres)
    - [Use Case: Explore Gender and Citizenship Coverage](#use-case-explore-gender-and-citizenship-coverage)
    - [Use Case: Count Books by Reading Language](#use-case-count-books-by-reading-language)
    - [Use Case: Compare Canonical and Read Titles](#use-case-compare-canonical-and-read-titles)
    - [Use Case: View the Graph](#use-case-view-the-graph)
  - [Troubleshooting](#troubleshooting)

## What You Are Running

The knowledge graph stores information as nodes and relationships. A `Book` node represents a book in your list, an `Author` node represents an author, and a `Read` node stores when you finished the book and your score. `WROTE`, `OF_BOOK`, and other relationships connect those facts.

The CSV is the source of truth for books you have read. Wikidata supplies optional enrichment such as genre, publication year, author gender, and country of citizenship. Enrichment does not replace or edit the source CSV.

## 1. Start Neo4j

1. Install [Neo4j Desktop](https://neo4j.com/download/) if it is not already installed.
2. In Neo4j Desktop, create a local DBMS (database management system). Go to `Data services > Instances > Create instance`. Choose a Neo4j version, set a password for the `neo4j` user, and start the DBMS. Neo4j Desktop's labels can vary by version; look for the local DBMS/instance area.
3. Use the default `neo4j` database for this project. Record the Bolt URI (commonly `bolt://localhost:7687`), username (`neo4j`), and the password you just set. The scripts do not create a DBMS or database.

Use that same DBMS and database for the schema, ingestion, enrichment, and queries. Do not use your Neo4j website sign-in as the database password.

## 2. Prepare Python and Credentials

Install `uv` from PowerShell with [WinGet](https://docs.astral.sh/uv/getting-started/installation/#winget):

```powershell
winget install --id=astral-sh.uv -e
```

Close and reopen PowerShell, then move to the repository root. The project requires `Python 3.14.3`; install it and the project dependencies with:

```powershell
uv python install 3.14.3
uv sync
Copy-Item .env.example .env
```

Open `.env` and replace the example values with your local Neo4j connection settings:

```dotenv
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-local-database-password
NEO4J_DB_NAME=neo4j
BOOKS_CSV_PATH=data/raw/books_raw_<date>.csv
```

Keep `NEO4J_DB_NAME=neo4j` and set `NEO4J_URI` to the Bolt connection URI shown by Neo4j Desktop. `BOOKS_CSV_PATH` selects the cumulative CSV; change the example value to your file's path if needed. Relative paths are resolved from the repository root, and absolute paths are also accepted. Keep `.env` private; do not commit it or paste its password into messages. The raw CSV is also ignored by Git because it contains personal reading data.

## 3. Apply the Graph Schema

The schema file creates uniqueness constraints used by the loader and enrichment script to prevent duplicate authors, books, reads, genres, countries, and genders. A book's stable `book_id` is its identity; its title is display text.

The easiest option to implement the schema is Neo4j Desktop's Query tool:

1. Open the running local DBMS in Neo4j Desktop and choose **Query** for its `neo4j` database. Confirm the editor is connected to that database.
2. In VSCode, open `schema/constraints.cypher` in VS Code.
3. Copy and run the complete file. Its `//` lines are comments and can remain.

Apply the constraints once, before the first import. They use `IF NOT EXISTS`, so they are safe to reapply; routine CSV updates do not need this step.

### Optional: Use Cypher Shell

Cypher Shell is not needed if you use Neo4j Desktop's Query tool. To run the schema from PowerShell instead, install the standalone [Cypher Shell](https://neo4j.com/docs/operations-manual/current/tools/cypher-shell/) from the [Neo4j Deployment Center](https://neo4j.com/deployment-center/?cypher-shell). Standalone Cypher Shell requires Java 21. Install a Windows x64 Java 21 JDK, such as [Eclipse Temurin](https://adoptium.net/temurin/releases/?version=21), and enable the installer options to add Java to `PATH` and set `JAVA_HOME`; confirm it with `java -version`. Extract the Cypher Shell package, then either add its `bin` directory to `PATH` or invoke `cypher-shell.bat` by its full path. Verify it with `cypher-shell --version`. Then run this at the repository root, substituting your connection details:

```powershell
cypher-shell -a bolt://localhost:7687 -u neo4j -d neo4j -f schema/constraints.cypher
```

Omitting `-p` lets Cypher Shell prompt for the database password.

## 4. Maintain the Cumulative CSV

The source CSV is selected by `BOOKS_CSV_PATH` in `.env`. The sample value is `data/raw/books_raw_<date>.csv`; use your actual file path if its name or location differs.

Its header row must be:

```text
book_id,title,title_read,language_read,author,year_month,score
```

Append one row per new book. The current data contains examples like:

```csv
0284,EXAMPLE BOOK,EXAMPLE BOOK,en,Jane Writer/John Writer,2026_10,8.5
```

`title_read` and `language_read` are required, trimmed, and stored on the `Read` node. They must not be blank. `Book.title` remains the canonical title; `Read.title_read` preserves the title as you encountered it while reading. `Read.language_read` can be used to group reading events by language.

Follow these rules:

- `book_id` must be unique, stable, and kept as text. Preserve leading zeros such as `0001`; do not reuse an ID for another book. If editing in Excel, format this column as Text before entering IDs and check the exported CSV.
- Add each book only once. This source models one `Read` per book; it does not represent rereads.
- Keep the title as it appears in your data. It is display text, not the graph's identity key.
- Separate multiple authors with `/`. The loader trims each name and creates a separate author node/relationship.
- Enter `year_month` as `YYYY_MM`, such as `2026_10`. The graph stores the date as the first day of that month.
- Scores must be finite numbers. Decimal scores and any score scale are accepted; the script does not impose a rating range.
- Use normal CSV quoting for fields that contain commas. Avoid blank IDs, titles, `title_read`, `language_read`, or authors.
- Use a consistent language code or label (for example, `en`) so language counts group as expected.
- Escape literal double quotes inside a quoted CSV field by doubling them (`""`). The loader rejects malformed quoting and reports the CSV line number.

The normal workflow is append-only. If an existing row needs correction, keep its `book_id` and reload the cumulative CSV rather than assigning a new ID. The loader adds `WROTE` relationships for the CSV's current authors, but it does not remove relationships to old author names or delete stale author nodes. See [Correct a CSV author name](#correct-a-csv-author-name) for the cleanup steps.

## 5. Update the Graph from the CSV

Make sure Neo4j is running and `.env` points to the correct database. From the repository root, run:

```powershell
uv run python src/ingest/load_books.py
```

or, if already on a terminal with the activated uv environment, run:

```powershell
python src/ingest/load_books.py
```

The loader validates the entire CSV before it writes anything. If it reports invalid rows, correct the CSV and rerun; it will not load only the valid subset. On success it reports inserted, updated, and unchanged rows. Rerunning the same cumulative file is safe: existing `Book` and `Read` nodes are matched by `book_id`, and authors and relationships are merged rather than duplicated.

Apply the constraints once before the first import, as described above. If you are migrating a graph created by an older title-keyed loader, use a fresh database; this loader does not migrate that graph. Slash-separated CSV authors are normalized into separate `Author` nodes and `WROTE` relationships.

## 6. Enrich the Graph

Run enrichment after ingestion:

```powershell
uv run python src/enrich/wikidata_enrich.py
```

The default run checks new, unresolved, and stale Wikidata author/book records. It automatically accepts only a unique exact-label candidate that passes entity validation; book candidates are also checked against known author claims. Open Library summary lookups have independent status and retry tracking.

Wikidata statuses and actions:

| Status | Meaning | Next action |
| --- | --- | --- |
| `matched` | One unique exact candidate was accepted, automatically or by review. | Normally skipped until stale; `--refresh` refreshes facts for its stored QID. |
| `needs_review` | Suitable candidates exist but no unique exact match was found. | Use `--interactive` or replay the report with `--from-report`. |
| `no_candidate` | The bounded search returned no suitable candidate; this does not prove absence. | Retry later, correct the source title/name if needed, or supply a manually verified QID. A later search may find candidates to review. |
| `error` | The external lookup failed. | A normal run retries it; persistent errors remain report rows. |

Open Library summary statuses are separate from Wikidata:

| Status | Meaning | Next action |
| --- | --- | --- |
| `matched` | A work was accepted; its ID is stored even if it has no description. | It is not prompted again unless summary refresh is requested. |
| `ambiguous` | Multiple exact title-and-author works were found. Candidate `matched: true` means exact title and author; only exact selectable work IDs are offered interactively. | Use `--interactive --entity-type book_summary --summary-status ambiguous` or replay the report. |
| `not_found` | No exact Open Library work was found. | Retry later or verify the source title/author; there is no candidate to select. |
| `error` | The Open Library request failed. | Summary errors are retried independently on a normal run. |

You can run a small trial first:

```powershell
uv run python src/enrich/wikidata_enrich.py --limit 5
```

`--limit 5` checks up to five authors, five books, and five summaries after sorting each queue; it is not an ID selector. `--refresh` re-fetches facts for stored Wikidata QIDs and does not search for a replacement. It may make many network requests. To refresh Open Library summaries independently, use:

```powershell
uv run python src/enrich/wikidata_enrich.py --refresh-summaries
```

Each HTTP request waits a randomized 5-10 second base interval plus 0.42–1.42
seconds of positive jitter (an actual wait of 5.42–11.42 seconds). You can
adjust these values with `--request-interval-min`, `--request-interval-max`,
`--jitter-min`, and `--jitter-max`. You can optionally set a contactable client
identifier in `.env`:

```dotenv
WIKIDATA_USER_AGENT=books-kg/0.1 (your contact information)
```

Each run writes a timestamped report under `data/reports/`. Accepted matches are omitted; unresolved/skipped rows and errors remain. Open the report to inspect candidate IDs, labels, descriptions, aliases, and book author IDs.

For a second pass, `needs_review` and `book_summary: ambiguous` rows use the candidates saved in the report. `no_candidate` and `error` rows have no saved choices, so their lookup is retried. Run:

```powershell
uv run python src/enrich/wikidata_enrich.py --from-report data/reports/enrichment_review_<timestamp>.csv --interactive
```

Choose one or more `--entity-type` values (`author`, `book`, `book_summary`); repeat the option to select multiple types. Choose one or more `--status` values for Wikidata and/or `--summary-status` values for Open Library. Use `--author "Exact Graph Name"` or `--book-id 0001` to target specific records. Repeat an option to include multiple values. `--interactive` prompts for Wikidata `needs_review` candidates and Open Library `book_summary: ambiguous` candidates. Press Enter to skip. Skipped items remain unresolved in the next report. Candidate IDs are revalidated before persistence: authors must be humans, books must pass the supported book-type and known-author checks, and Open Library choices must still match the current title-and-author search.

A prior report can also be filtered in the same command, for example:

```powershell
uv run python src/enrich/wikidata_enrich.py --from-report data/reports/enrichment_review_<timestamp>.csv --entity-type book_summary --summary-status ambiguous --interactive
```

If you found a Wikidata item manually for a `no_candidate` row, pass its QID for validation and storage:

```powershell
uv run python src/enrich/wikidata_enrich.py --manual-book-qid "0001=Q123"
uv run python src/enrich/wikidata_enrich.py --manual-author-qid "Author Name=Q456"
```

The script validates that manually supplied IDs resolve to the right entity type (and checks known book authors). A stored QID is re-used by normal runs. To search for a replacement, use `--interactive --entity-type book --book-id 0001 --rematch-book-id 0001` or the author equivalents; skipping keeps the existing match. `--refresh` does not rematch.

Wikidata P27 is country of citizenship, which is not necessarily nationality; the graph relationship is named `CITIZEN_OF`. Gender and genre are represented as nodes. Open Library summary status is separate from Wikidata status. A unique exact title-and-author work is accepted automatically; ambiguous exact matches can be selected interactively. The source of any stored summary is recorded on the Book.

### Correcting or Resetting Enrichment

#### Correct a CSV author name

If an author name in an already-loaded CSV row was an alias or typo, correct the `author` field without changing the row's `book_id`, then reload the cumulative CSV. Confirm that the new author is connected to the expected book:

```cypher
MATCH (a:Author {name: $new_name})-[:WROTE]->(b:Book {book_id: $book_id})
RETURN a.name, b.title;
```

Inspect the old author's remaining books before removing the stale relationship:

```cypher
MATCH (a:Author {name: $old_name})-[:WROTE]->(b:Book)
RETURN a.name, collect({book_id: b.book_id, title: b.title}) AS books;
```

If only the corrected book was attached to the old name, remove that relationship and delete the old node only if no `WROTE` relationships remain. `DETACH DELETE` also removes the old node's enrichment relationships:

```cypher
MATCH (a:Author {name: $old_name})-[r:WROTE]->(:Book {book_id: $book_id})
DELETE r;

MATCH (a:Author {name: $old_name})
WHERE NOT (a)-[:WROTE]->()
DETACH DELETE a;
```

Then enrich the corrected author by exact graph name:

```powershell
uv run python src/enrich/wikidata_enrich.py --entity-type author --author "NEW AUTHOR NAME" --interactive
```

#### Replace a Wikidata match

Use the enrichment CLI for identity changes. If you skip the prompt, the old ID and its facts remain. If you accept a replacement, the script removes the affected node's old Wikidata-derived links before applying the replacement. It preserves CSV-owned fields, `Book.title`, and Open Library summaries.

For a book rematch:

```powershell
uv run python src/enrich/wikidata_enrich.py --interactive --entity-type book --book-id 0130 --rematch-book-id 0130
```

For an author rematch:

```powershell
uv run python src/enrich/wikidata_enrich.py --interactive --entity-type author --author "AUTHOR NAME" --rematch-author "AUTHOR NAME"
```

#### Reset a match manually

Use Cypher only when you intend to change graph state manually. Run parameterized statements in Neo4j Query after setting `$name` or `$book_id` to the exact graph key. To remove an author's Wikidata-derived relationships:

```cypher
MATCH (a:Author {name: $name})-[r:HAS_GENDER|CITIZEN_OF]->()
DELETE r;
```

Then clear the stored match:

```cypher
MATCH (a:Author {name: $name})
REMOVE a.wikidata_id, a.wikidata_status, a.wikidata_checked_at;
```

To remove a book's Wikidata-derived genre relationships:

```cypher
MATCH (b:Book {book_id: $book_id})-[r:HAS_GENRE]->()
DELETE r;
```

Then clear the stored match while preserving an Open Library-sourced publication year:

```cypher
MATCH (b:Book {book_id: $book_id})
FOREACH (_ IN CASE WHEN b.publication_year_source = 'wikidata' THEN [1] ELSE [] END |
  REMOVE b.publication_year, b.publication_year_source)
REMOVE b.wikidata_id, b.wikidata_status, b.wikidata_checked_at;
```

Then rerun the corresponding rematch command above.

#### Remove one incorrect genre relationship

To delete one incorrect genre link while keeping the book's valid Wikidata match, remove only that relationship:

```cypher
MATCH (b:Book {book_id: $book_id})-[r:HAS_GENRE]->(g:Genre {name: $genre_name})
DELETE r;
```

Do not rerun enrichment for that book unless you want Wikidata to rebuild its genre links; refreshing the stored match may re-add this relationship.

#### Refresh facts from the same Wikidata match

To refresh facts without searching for a replacement, keep the stored `wikidata_id` and clear only the checked timestamp. The targeted command reuses that QID, refetches its claims, and rebuilds its Wikidata-derived relationships.

For an author, clear the timestamp:

```cypher
MATCH (a:Author {name: $name})
REMOVE a.wikidata_checked_at;
```

Then target that author:

```powershell
uv run python src/enrich/wikidata_enrich.py --entity-type author --author "AUTHOR NAME"
```

For a book, clear the timestamp:

```cypher
MATCH (b:Book {book_id: $book_id})
REMOVE b.wikidata_checked_at;
```

Then target that book:

```powershell
uv run python src/enrich/wikidata_enrich.py --entity-type book --book-id 0001
```

`--refresh` is the all-record version of this refresh.

For a manually verified Wikidata entity, prefer the validated CLI options:

```powershell
uv run python src/enrich/wikidata_enrich.py --manual-book-qid "0130=Q12345"
uv run python src/enrich/wikidata_enrich.py --manual-author-qid "AUTHOR NAME=Q67890"
```

The script verifies the QID resolves to the required entity type and checks known book authors. For a manually verified genre fact, a parameterized `MERGE` is additive:

```cypher
MATCH (b:Book {book_id: $book_id})
MERGE (g:Genre {name: $genre_name})
ON CREATE SET g.wikidata_id = $genre_qid
MERGE (b)-[:HAS_GENRE]->(g);
```

Do not delete shared `Genre`, `Gender`, or `Country` nodes when correcting one entity; delete only the affected relationship.

To remove one incorrect author fact while keeping the existing Wikidata ID, delete only that relationship and do not rerun Wikidata enrichment unless you intend it to be re-added:

```cypher
MATCH (a:Author {name: $name})-[r:HAS_GENDER]->(g:Gender {name: $gender_name})
DELETE r;
```

To discard only a bad Open Library summary/work choice and search again, remove its summary state. This does not change Wikidata status or ID:

```cypher
MATCH (b:Book {book_id: $book_id})
REMOVE b.openlibrary_id, b.summary, b.summary_source,
	   b.openlibrary_status, b.openlibrary_checked_at;
```

Then target that summary lookup:

```powershell
uv run python src/enrich/wikidata_enrich.py --entity-type book_summary --book-id 0001 --interactive
```

## 7. Explore the Graph

Open **Query** for the same running database in Neo4j Desktop. Open `src/queries/analytics.cypher`, then copy and run one query at a time. Neo4j Browser/Query supports the Cypher statements directly. Queries 1, 2, and 5–8 do not need parameters. Query 4 needs genre enrichment to have returned usable genre matches.

### Use Case: Find Authors You Read Often

Run query 1. It lists authors with more than three linked books and compares their average score to the overall average. The `diffFromOverall` column shows whether your average score for that author is above or below your overall score.

### Use Case: Compare Scores by Month or Season

Run query 2. The first part groups reading scores by calendar month; the second groups them into winter, spring, summer, and fall. Each result includes the number of books in that group, so you can see how much data supports the average.

### Use Case: See What You Read Around a Book

Query 3 takes a `$book_id` parameter. In the Query tool, set the parameter to an ID from the CSV, such as `0001`, then run it. It returns other books with reading dates within one month before or after the target book. Using the ID avoids problems when two books share a title.

### Use Case: Discover Authors with Shared Genres

After enrichment has added `HAS_GENRE` relationships, run query 4. It finds the five authors with the highest average score, then lists other authors whose books share genres with them. `overlapCount` is the number of shared genre nodes, not a personalized recommendation score.

### Use Case: Explore Gender and Citizenship Coverage

Run query 5 to group reads by known author gender, and query 6 to group them by known author citizenship. Each result includes `classifiedReads` and `totalReads`; compare these to see how many reads have relevant enrichment. Unknown or unmatched authors are not silently assigned to a category. A book with multiple authors may contribute to multiple categories when those authors have different known facts, so category totals can overlap.

### Use Case: Count Books by Reading Language

Run query 7 to count your `Read` nodes by `language_read`. The loader requires this value and trims surrounding whitespace; use consistent language codes or labels in the CSV to avoid splitting one language across multiple groups.

### Use Case: Compare Canonical and Read Titles

Run query 8 to return `Book.title` alongside `Read.title_read`, language, and reading date. This keeps the canonical work title separate from the title you recorded as read.

### Use Case: View the Graph

To display the full connected graph in Query, run this and select the **Graph** results view:

```cypher
MATCH (n)
OPTIONAL MATCH (n)-[r]->(m)
RETURN n, r, m;
```

For a smaller preview, return a limited set of reading paths:

```cypher
MATCH p = (a:Author)-[:WROTE]->(b:Book)<-[:OF_BOOK]-(r:Read)
RETURN p
LIMIT 25;
```

## Troubleshooting

- **Connection refused:** Check that the Neo4j DBMS is started and that `NEO4J_URI` uses its Bolt address.
- **Authentication/database error:** Check `NEO4J_USER`, `NEO4J_PASSWORD`, and `NEO4J_DB_NAME` in `.env`. Confirm the database exists and is selected in the Query tool.
- **Database not found:** Confirm `NEO4J_DB_NAME=neo4j` in `.env` and that the local DBMS is running. The scripts do not create databases.
- **Missing or invalid CSV data:** Check the header row and the line numbers in the loader error. No CSV rows are written if validation fails.
- **No genre/similarity results:** Run enrichment, then inspect the review report. Some books may not have an unambiguous Wikidata match or genre claim.
- **Some reads are not in gender/citizenship groups:** Those queries intentionally include only authors with accepted enrichment facts.
