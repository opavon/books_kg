# Books KG User Guide

This guide starts from a fresh computer/database setup and walks through updating the graph from the one cumulative CSV, enriching it, and exploring the results. Commands below are for PowerShell, opened at the repository root (`books_kg`).

## What You Are Running

The knowledge graph stores information as nodes and relationships. A `Book` node represents a book in your list, an `Author` node represents an author, and a `Read` node stores when you finished the book and your score. `WROTE`, `OF_BOOK`, and other relationships connect those facts.

The CSV is the source of truth for books you have read. Wikidata supplies optional enrichment such as genre, publication year, author gender, and country of citizenship. Enrichment does not replace or edit the source CSV.

## 1. Start Neo4j

1. Install [Neo4j Desktop](https://neo4j.com/download/) if it is not already installed.
2. In Neo4j Desktop, create a local DBMS (database management system). Go to `Data services > Local instances > Create instance`. Choose a Neo4j version, set a password for the `neo4j` user, and start the DBMS. Neo4j Desktop's labels can vary by version; look for the local DBMS/instance area.
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

Apply the constraints once, before the first import. They use `IF NOT EXISTS`, so they are safe to reapply; routine CSV updates do not need this step. No `DROP` statement or migration is needed for a new database.

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

Do not edit already-loaded rows as part of the normal workflow. The file is intended to be append-only. If an existing book's details or ID need correction, pause and handle it deliberately rather than assigning that book a new ID.

## 5. Update the Graph from the CSV

Make sure Neo4j is running and `.env` points to the correct database. From the repository root, run:

```powershell
uv run python src/ingest/load_books.py
```

or, if already on a terminal with the activated uv environment, run"

```powershell
python src/ingest/load_books.py
```

The loader validates the entire CSV before it writes anything. If it reports invalid rows, correct the CSV and rerun; it will not load only the valid subset. On success it reports inserted, updated, and unchanged rows. Rerunning the same cumulative file is safe: existing `Book` and `Read` nodes are matched by `book_id`, and authors and relationships are merged rather than duplicated.

Apply the constraints once before the first import, as described above. Start with a fresh database: the loader does not migrate data from an older title-keyed graph.
The CSV already contains a stable `book_id` for every row, so the loader does
not migrate data from an older title-keyed graph. Slash-separated authors in
the CSV are normalized into separate Author nodes and `WROTE` relationships.

## 6. Enrich the Graph

Run enrichment after ingestion:

```powershell
uv run python src/enrich/wikidata_enrich.py
```

The script checks new and unresolved authors/books and refreshes successful matches older than 180 days. Wikidata is the primary source. An author is automatically matched only when exactly one candidate has the same normalized English label and is verified as a human. A book is automatically matched only when exactly one candidate has the same normalized English label and passes the existing book type check. When known authors have Wikidata IDs, a book candidate is excluded if its `P50` claims conflict with all of them; candidates without `P50` claims or with at least one matching author remain eligible.

For either type, suitable candidates without a unique exact match produce `needs_review`. `no_candidate` means the attempted search found no suitable candidate (a verified human for an author, or a book passing its type and known-author checks). It does not prove that the person or work is absent from Wikidata: results are ranked and limited. Non-human results are excluded from author candidates, and non-book or known-author-conflicting results are excluded from book candidates.

You can run a small trial first:

```powershell
uv run python src/enrich/wikidata_enrich.py --limit 5
```

`--limit 5` checks up to five authors and five books. To force a refresh of every author and book, including recent matches, use:

```powershell
uv run python src/enrich/wikidata_enrich.py --refresh
```

Each HTTP request waits a randomized 5-10 second base interval plus 0.42–1.42
seconds of positive jitter (an actual wait of 5.42–11.42 seconds). You can
adjust these values with `--request-interval-min`, `--request-interval-max`,
`--jitter-min`, and `--jitter-max`. You can optionally set a contactable client
identifier in `.env`:

```dotenv
WIKIDATA_USER_AGENT=books-kg/0.1 (your contact information)
```

Author and book `needs_review`/`no_candidate` results and failed lookups are reported in a timestamped CSV under `data/reports/`. Open that report in a spreadsheet to inspect candidate IDs, labels, descriptions, and available aliases or book author IDs. Successful Wikidata entity IDs are stored for future runs, so accepted matches do not need to be searched by name again. Lookup errors do not erase previously stored facts.

For author or book candidates that need review, run:

```powershell
uv run python src/enrich/wikidata_enrich.py --interactive
```

The prompt lists candidate IDs, labels, descriptions, and available English aliases; book candidates also show their Wikidata author IDs when present. Enter a candidate number to accept it, or press Enter to skip. The selection must belong to the current candidate list and be revalidated before it is saved: authors must be humans, and books must pass the book type and known-author checks. Accepted IDs are reused on later runs; skipped candidates remain in the review CSV. Without `--interactive`, the script never prompts and still writes the report. `no_candidate` means only that the attempted search found no suitable candidates; it does not prove that an entry is absent from Wikidata.

Wikidata P27 is country of citizenship, which is not necessarily nationality; the graph relationship is named `CITIZEN_OF`. Gender and genre are represented as nodes. Wikidata book statuses are separate from Open Library summary lookup. If no summary is stored, Open Library is tried as a secondary source, and a description is stored only after a unique exact title-and-author match. The source is recorded on the Book.

## 7. Explore the Graph

Open **Query** for the same running database in Neo4j Desktop. Open `src/queries/analytics.cypher`, then copy and run one query at a time. Neo4j Browser/Query supports the Cypher statements directly. Queries 1, 2, and 5–8 do not need parameters. Query 4 needs genre enrichment to have returned usable genre matches.

### Use Case: Compare Scores by Month or Season

Run query 2. The first part groups reading scores by calendar month; the second groups them into winter, spring, summer, and fall. Each result includes the number of books in that group, so you can see how much data supports the average.

### Use Case: Find Authors You Read Often

Run query 1. It lists authors with more than three linked books and compares their average score to the overall average. The `diffFromOverall` column shows whether your average score for that author is above or below your overall score.

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

To display a small graph, run this in Query and select the **Graph** results view:

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
