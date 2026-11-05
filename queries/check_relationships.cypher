// Example queries for checking relationships and connections.
// Open http://localhost:7474 (user neo4j, password from .env) and run them one at a time.
// Queries marked [GRAPH] are best viewed in the Browser's graph view.

// 1. Overview: how many nodes of each kind
MATCH (n)
UNWIND [l IN labels(n) WHERE l <> 'Document'] AS label
RETURN label, count(*) AS nodes
ORDER BY nodes DESC;

// 2. Who is connected to what: relationship counts per source node
MATCH (x)-[r]->(d:Document)
RETURN coalesce(x.name, x.actor_id) AS from, type(r) AS relationship,
       [l IN labels(d) WHERE l <> 'Document'][0] AS to_type, count(*) AS links
ORDER BY from, to_type;

// 3. [GRAPH] One actor and its own profile documents
MATCH p = (a:Actor {actor_id: 'aerztekammer'})-[:HAS_PROFILE_SOURCE]->(:ActorData)
RETURN p;

// 4. [GRAPH] Profile documents are shared with TruthSeeker (no copies):
//    every ActorData node with both incoming edges
MATCH p = (a:Actor)-[:HAS_PROFILE_SOURCE]->(d:ActorData)<-[:USES_SOURCE]-(t:Moderator {moderator_id: 'truthseeker'})
RETURN p;

// 5. Same table as text: each profile document with its actor and its reviewers
MATCH (d:ActorData)
RETURN d.source_path AS document, d.title AS title,
       [(a:Actor)-[:HAS_PROFILE_SOURCE]->(d) | a.name] AS actor,
       [(m:Moderator)-[:USES_SOURCE]->(d) | m.name] AS reviewers
ORDER BY document;

// 6. [GRAPH] Small sample of each reviewer's sources (5 per reviewer)
MATCH (m:Moderator)
CALL (m) {
  MATCH p = (m)-[:USES_SOURCE]->(:Document)
  RETURN p LIMIT 5
}
RETURN p;

// 7. Identical content stored in several places (same hash, separate nodes)
MATCH (d:Document)
WITH d.content_hash AS hash, collect(d) AS docs
WHERE size(docs) > 1
RETURN [x IN docs | x.source_path] AS paths, [x IN docs | x.scope] AS scopes, size(docs) AS copies
ORDER BY copies DESC LIMIT 10;

// ---- Consistency checks: every query below should return 0 ----

// 8. Articles or profile documents that TruthSeeker can NOT see
MATCH (d:Document) WHERE (d:Article OR d:ActorData)
  AND NOT EXISTS { (:Moderator {moderator_id: 'truthseeker'})-[:USES_SOURCE]->(d) }
RETURN count(d) AS not_visible_to_truthseeker;

// 9. Profile documents that do not have exactly one actor, or the wrong actor
MATCH (d:ActorData)
OPTIONAL MATCH (a:Actor)-[:HAS_PROFILE_SOURCE]->(d)
WITH d, collect(a.actor_id) AS actors
WHERE size(actors) <> 1 OR actors[0] <> d.actor_id
RETURN count(d) AS wrong_actor_links;

// 10. Relationships that cross scopes (actor_truth / legal / ethics)
MATCH (x)-[r]->(y) WHERE x.scope <> y.scope
RETURN count(r) AS cross_scope_links;

// 11. Actor-article links of any type other than MATCHES_KEYWORD
MATCH (:Actor)-[r]-(:Article) WHERE type(r) <> 'MATCHES_KEYWORD'
RETURN count(r) AS other_actor_article_links;

// 11a. MATCHES_KEYWORD links that disagree with the article's akteure tags
MATCH (d:Article), (a:Actor)
WITH a, d,
     any(l IN coalesce(a.akteure_labels, []) WHERE l IN coalesce(d.akteure, [])) AS tagged,
     EXISTS { (a)-[:MATCHES_KEYWORD]->(d) } AS linked
WHERE tagged <> linked
RETURN count(*) AS links_differing_from_tags;

// 12. Duplicate relationships between the same two nodes
MATCH (x)-[r]->(y)
WITH x, y, type(r) AS t, count(r) AS c WHERE c > 1
RETURN count(*) AS duplicate_links;

// 13. LegalEye or EthicMind linked to the wrong document type
MATCH (m:Moderator)-[:USES_SOURCE]->(d)
WHERE (m.moderator_id = 'legaleye'  AND NOT d:LegalText)
   OR (m.moderator_id = 'ethicmind' AND NOT d:EthicalText)
   OR (m.moderator_id = 'truthseeker' AND NOT (d:Article OR d:ActorData))
RETURN count(*) AS wrong_reviewer_links;

// ---- Content-oriented examples ----

// 14. Articles TruthSeeker sees, grouped by newspaper
MATCH (:Moderator {moderator_id: 'truthseeker'})-[:USES_SOURCE]->(a:Article)
RETURN a.publisher AS publisher, count(*) AS articles
ORDER BY articles DESC LIMIT 10;

// 15. Articles per month (by article date, not import date)
MATCH (a:Article) WHERE a.publication_date IS NOT NULL
RETURN substring(a.publication_date, 0, 7) AS month, count(*) AS articles
ORDER BY month;

// 16. One legal provision with its validity dates
//     (dates as published; says nothing about whether it is in force today)
MATCH (:Moderator {moderator_id: 'legaleye'})-[:USES_SOURCE]->(d:LegalText)
WHERE d.law_abbreviation = 'ASVG' AND d.title = 'ASVG § 31'
RETURN d.title, d.valid_from, d.valid_until, d.source_path;

// 17. Documents whose source file has disappeared (should be empty now)
MATCH (d:Document) WHERE d.source_present = false
RETURN d.doc_type, d.source_path, d.missing_since;

// ---- Actor-article links ----

// 18. Number of linked articles per actor
MATCH (a:Actor)
OPTIONAL MATCH (a)-[k:MATCHES_KEYWORD]->(:Article)
RETURN a.name AS actor, a.akteure_labels AS tag_values, count(k) AS articles
ORDER BY articles DESC;

// 19. How many actors each article is linked to
MATCH (d:Article)
WITH d, COUNT { (:Actor)-[:MATCHES_KEYWORD]->(d) } AS actor_links
RETURN actor_links, count(*) AS articles
ORDER BY actor_links;

// 20. [GRAPH] Articles tagged with all three actors (sample)
MATCH (d:Article)
WHERE COUNT { (:Actor)-[:MATCHES_KEYWORD]->(d) } = 3
WITH d LIMIT 3
MATCH p = (:Actor)-[:MATCHES_KEYWORD]->(d)
RETURN p;

// 21. Newest articles linked to one actor
MATCH (:Actor {actor_id: 'aerztekammer'})-[:MATCHES_KEYWORD]->(d:Article)
WHERE d.publication_date IS NOT NULL
RETURN d.publication_date, d.publisher, d.title, d.akteure
ORDER BY d.publication_date DESC LIMIT 10;

// 22. All akteure tag values stored on articles, linked or not
MATCH (d:Article)
UNWIND d.akteure AS value
RETURN value, count(*) AS articles
ORDER BY articles DESC;

// 23. Articles tagged only with list-only values (no actor link)
MATCH (d:Article)
WHERE size(d.akteure) > 0 AND NOT EXISTS { (:Actor)-[:MATCHES_KEYWORD]->(d) }
RETURN count(d) AS tagged_without_actor_link;
