"""
Phase 2 - enrich Author and Book nodes with data from Wikidata
(gender, nationality, genre, publication year, summary).

Not implemented yet. Rough plan:

1. For each Author.name in the graph, query Wikidata's SPARQL endpoint
   (https://query.wikidata.org/sparql) for the matching human entity,
   pulling P21 (sex or gender) and P27 (country of citizenship).
2. For each Book.title (+ author), query for the matching work entity,
   pulling genre (P136) and publication date (P577). Wikidata's book
   coverage is uneven - Open Library or Google Books' API may fill gaps.
3. Write results back with MERGE, e.g.:

    MERGE (c:Country {name: $country})
    WITH c
    MATCH (a:Author {name: $author_name})
    MERGE (a)-[:NATIONALITY]->(c)
    SET a.gender = $gender

4. Log any authors/books that couldn't be matched automatically for
   manual review - name mismatches are the main failure mode.
"""

raise NotImplementedError("Phase 2 - build after Phase 1 ingest is working.")
