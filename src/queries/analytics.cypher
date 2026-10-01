// ---------------------------------------------------------------------
// 1. Authors read more than 3 times: their avg score vs. my overall avg
// ---------------------------------------------------------------------
MATCH (allReads:Read)
WITH avg(allReads.score) AS overallAvg
MATCH (a:Author)-[:WROTE]->(:Book)<-[:OF_BOOK]-(r:Read)
WITH overallAvg, a, count(r) AS booksRead, avg(r.score) AS authorAvg
WHERE booksRead > 3
RETURN a.name AS author,
       booksRead,
       round(authorAvg, 2) AS avgScore,
       round(overallAvg, 2) AS overallAvg,
       round(authorAvg - overallAvg, 2) AS diffFromOverall
ORDER BY diffFromOverall DESC;


// ---------------------------------------------------------------------
// 2. Do I score higher in certain months or seasons?
// ---------------------------------------------------------------------

// By month
MATCH (r:Read)
RETURN r.date.month AS month, avg(r.score) AS avgScore, count(*) AS numBooks
ORDER BY month;

// By season
MATCH (r:Read)
WITH r,
     CASE
       WHEN r.date.month IN [12, 1, 2] THEN 'Winter'
       WHEN r.date.month IN [3, 4, 5]  THEN 'Spring'
       WHEN r.date.month IN [6, 7, 8]  THEN 'Summer'
       ELSE 'Fall'
     END AS season
RETURN season, avg(r.score) AS avgScore, count(*) AS numBooks
ORDER BY avgScore DESC;


// ---------------------------------------------------------------------
// 3. What did I read in the same period as book X?
//    ("same period" = within 1 month either side; adjust as you like)
// ---------------------------------------------------------------------
MATCH (target:Book {title: $title})<-[:OF_BOOK]-(targetRead:Read)
MATCH (other:Book)<-[:OF_BOOK]-(r:Read)
WHERE other <> target
  AND r.date >= targetRead.date - duration({months: 1})
  AND r.date <= targetRead.date + duration({months: 1})
RETURN other.title AS book, r.date AS dateFinished, r.score AS score
ORDER BY r.date;


// ---------------------------------------------------------------------
// 4. Which authors are similar to the ones I score highest?
//    Requires Phase 2 (Book)-[:HAS_GENRE]->(Genre) to exist.
//    Naive version: shared-genre overlap with your top-5 rated authors.
// ---------------------------------------------------------------------
MATCH (a:Author)-[:WROTE]->(:Book)<-[:OF_BOOK]-(r:Read)
WITH a, avg(r.score) AS avgScore
ORDER BY avgScore DESC
LIMIT 5
WITH collect(a) AS topAuthors
UNWIND topAuthors AS topAuthor
MATCH (topAuthor)-[:WROTE]->(:Book)-[:HAS_GENRE]->(g:Genre)
      <-[:HAS_GENRE]-(:Book)<-[:WROTE]-(other:Author)
WHERE NOT other IN topAuthors
RETURN other.name AS candidateAuthor,
       collect(DISTINCT g.name) AS sharedGenres,
       count(DISTINCT g) AS overlapCount
ORDER BY overlapCount DESC;

// A stronger version later: Neo4j Graph Data Science `gds.nodeSimilarity`
// over an Author-Genre bipartite projection, which scores similarity
// (e.g. Jaccard) rather than a raw overlap count.
