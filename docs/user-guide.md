# Books KG User Guide

This guide starts from a fresh computer/database setup and walks through updating the graph from the one cumulative CSV, enriching it, and exploring the results. Commands below are for PowerShell, opened at the repository root (`books_kg`).

## What You Are Running

The knowledge graph stores information as nodes and relationships. A `Book` node represents a book in your list, an `Author` node represents an author, and a `Read` node stores when you finished the book and your score. `WROTE`, `OF_BOOK`, and other relationships connect those facts.

The CSV is the source of truth for books you have read. Wikidata supplies optional enrichment such as genre, publication year, author gender, and country of citizenship. Enrichment does not replace or edit the source CSV.

## 1. Start Neo4j

1. Install Neo4j Desktop from [neo4j.com/download](https://neo4j.com/download/) if you do not already have it.
2. In Desktop, create a local DBMS and start it. A DBMS is the local database server; it must be running while you apply the schema or run either script.
3. Use the default database (commonly named `neo4j`), or create/use the database name you prefer. Record the Bolt URI, database name, username, and password. A common local URI is `bolt://localhost:7687`. The database name must already exist in the running DBMS; `books_kg` is not created automatically by these scripts.

Use that same DBMS and database for the schema, ingestion, enrichment, and queries. Do not use your Neo4j website sign-in as the database password.

## 2. Prepare Python and Credentials

Install `uv` if needed, then open PowerShell in the repository folder and run:

```powershell
uv sync
Copy-Item .env.example .env
```

Open `.env` and replace the example values with your local Neo4j connection settings:

```dotenv
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-local-database-password
NEO4J_DB_NAME=neo4j
```

Set `NEO4J_DB_NAME` to the database name you selected in Neo4j Desktop, and `NEO4J_URI` to the connection URI in Neo4j Desktop. Keep `.env` private; do not commit it or paste its password into messages. The raw CSV is also ignored by Git because it contains personal reading data.

## 3. Apply the Graph Schema

The schema file creates uniqueness constraints used by the loader to prevent duplicate authors, books, reads, genres, countries, and genders. It also removes the old title-based book constraint, because a stable `book_id` is now the book identity and title is display text.

The easiest option is Neo4j Desktop's Query tool:

1. Open the running DBMS and choose **Query** for the target database.
2. Open `schema/constraints.cypher` in VS Code.
3. Copy and run each Cypher statement, including the `DROP CONSTRAINT` statement. Do not run the `//` comment lines.

If `cypher-shell` is installed and available in PowerShell, you can run the whole file instead. Substitute your connection details:

```powershell
cypher-shell -a bolt://localhost:7687 -u neo4j -d neo4j -f schema/constraints.cypher
```

Cypher Shell prompts for the password when `-p` is omitted. The constraints use `IF NOT EXISTS` and can be applied again. The title constraint is explicitly dropped so the graph can use `book_id` for identity.

## 4. Maintain the Cumulative CSV

The only source file is:

```text
data/raw/books_raw_20261001.csv
```

Its header row must be:

```text
book_id,title,author,year_month,score
```

Append one row per new book. The current data contains examples like:

```csv
0284,EXAMPLE BOOK,Jane Writer/John Writer,2026_10,8.5
```

Follow these rules:

- `book_id` must be unique, stable, and kept as text. Preserve leading zeros such as `0001`; do not reuse an ID for another book. If editing in Excel, format this column as Text before entering IDs and check the exported CSV.
- Add each book only once. This source models one `Read` per book; it does not represent rereads.
- Keep the title as it appears in your data. It is display text, not the graph's identity key.
- Separate multiple authors with `/`. The loader trims each name and creates a separate author node/relationship.
- Enter `year_month` as `YYYY_MM`, such as `2026_10`. The graph stores the date as the first day of that month.
- Scores must be finite numbers. Decimal scores and any score scale are accepted; the script does not impose a rating range.
- Use normal CSV quoting for fields that contain commas. Avoid blank IDs, titles, or authors.

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

Start with a fresh database and apply the constraints before the first import.
The CSV already contains a stable `book_id` for every row, so the loader does
not migrate data from an older title-keyed graph. Slash-separated authors in
the CSV are normalized into separate Author nodes and `WROTE` relationships.

## 6. Enrich the Graph

Run enrichment after ingestion:

```powershell
uv run python src/enrich/wikidata_enrich.py
```

The script checks new and unresolved authors/books and refreshes successful matches older than 180 days. Wikidata is the primary source. It automatically accepts only one exact-label candidate with the expected entity type; for books, known author Wikidata IDs also help reject candidates with conflicting creators.

You can run a small trial first:

```powershell
uv run python src/enrich/wikidata_enrich.py --limit 5
```

`--limit 5` checks up to five authors and five books. To force a refresh of every author and book, including recent matches, use:

```powershell
uv run python src/enrich/wikidata_enrich.py --refresh
```

Each HTTP request waits a randomized 5–10 second base interval plus 0.42–1.42
seconds of positive jitter (an actual wait of 5.42–11.42 seconds). You can
adjust these values with `--request-interval-min`, `--request-interval-max`,
`--jitter-min`, and `--jitter-max`. You can optionally set a contactable client
identifier in `.env`:

```dotenv
WIKIDATA_USER_AGENT=books-kg/0.1 (your contact information)
```

Ambiguous, missing, and failed matches are reported in a timestamped CSV under `data/reports/`. Open that report in a spreadsheet to inspect candidate labels and descriptions. The script does not guess between ambiguous candidates. Successful Wikidata entity IDs are stored for future runs, so accepted matches do not need to be searched by name again. Lookup errors do not erase previously stored facts.

Wikidata P27 is country of citizenship, which is not necessarily nationality; the graph relationship is named `CITIZEN_OF`. Gender and genre are represented as nodes. Wikidata descriptions are not treated as book summaries. If no summary is stored, Open Library is tried as a secondary source, and a description is stored only after a unique exact title-and-author match. The source is recorded on the Book.

## 7. Explore the Graph

Open **Query** for the same running database in Neo4j Desktop. Open `src/queries/analytics.cypher`, then copy and run one query at a time. Neo4j Browser/Query supports the Cypher statements directly. Queries 1, 2, 5, and 6 do not need parameters. Query 4 needs genre enrichment to have returned usable genre matches.

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
- **Database not found:** `NEO4J_DB_NAME` in `.env` must match a database that already exists in this DBMS. For a typical Neo4j Desktop setup, change `NEO4J_DB_NAME=books_kg` to `NEO4J_DB_NAME=neo4j`. Alternatively, create a database named `books_kg` in Neo4j Desktop. Save `.env` and rerun the script.
- **Missing or invalid CSV data:** Check the header row and the line numbers in the loader error. No CSV rows are written if validation fails.
- **No genre/similarity results:** Run enrichment, then inspect the review report. Some books may not have an unambiguous Wikidata match or genre claim.
- **Some reads are not in gender/citizenship groups:** Those queries intentionally include only authors with accepted enrichment facts.
