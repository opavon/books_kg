// Run these once against a fresh database (Neo4j Browser or cypher-shell).
// Safe to re-run: IF NOT EXISTS makes them idempotent.

CREATE CONSTRAINT author_name IF NOT EXISTS
FOR (a:Author) REQUIRE a.name IS UNIQUE;

// Book identity is the stable source ID; title is display data.
DROP CONSTRAINT book_title IF EXISTS;
CREATE CONSTRAINT book_books_id IF NOT EXISTS
FOR (b:Book) REQUIRE b.books_id IS UNIQUE;

// One source row and one Read event per book.
CREATE CONSTRAINT read_books_id IF NOT EXISTS
FOR (r:Read) REQUIRE r.books_id IS UNIQUE;

// Phase 2 (enrichment) — safe to create now, they simply won't be used
// until Genre/Country nodes exist.
CREATE CONSTRAINT genre_name IF NOT EXISTS
FOR (g:Genre) REQUIRE g.name IS UNIQUE;

CREATE CONSTRAINT country_name IF NOT EXISTS
FOR (c:Country) REQUIRE c.name IS UNIQUE;

CREATE CONSTRAINT gender_name IF NOT EXISTS
FOR (g:Gender) REQUIRE g.name IS UNIQUE;
