"""Neo4j access: schema, importer writes, validation queries.

All queries are parameterised. The only values interpolated into query text
are labels taken from the fixed whitelist in `model.DOC_TYPES`.
"""

from datetime import datetime, timezone

from neo4j import GraphDatabase

from . import model

I = model.IMPORTER

SCHEMA_STATEMENTS = (
    "CREATE CONSTRAINT actor_id_unique IF NOT EXISTS "
    "FOR (n:Actor) REQUIRE n.actor_id IS UNIQUE",
    "CREATE CONSTRAINT moderator_id_unique IF NOT EXISTS "
    "FOR (n:Moderator) REQUIRE n.moderator_id IS UNIQUE",
    "CREATE CONSTRAINT document_id_unique IF NOT EXISTS "
    "FOR (n:Document) REQUIRE n.doc_id IS UNIQUE",
    "CREATE INDEX document_source_path IF NOT EXISTS FOR (n:Document) ON (n.source_path)",
    "CREATE INDEX document_scope_type IF NOT EXISTS FOR (n:Document) ON (n.scope, n.doc_type)",
)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(settings):
    # Server notifications (e.g. "label does not exist" on an empty database) are
    # informational for this importer; real problems surface as exceptions.
    driver = GraphDatabase.driver(settings.uri, auth=(settings.user, settings.password),
                                  notifications_min_severity="OFF")
    driver.verify_connectivity()
    return driver


def run_read(driver, database, query, **params):
    records, _, _ = driver.execute_query(query, params, database_=database, routing_="r")
    return [r.data() for r in records]


def run_write(driver, database, query, **params):
    records, summary, _ = driver.execute_query(query, params, database_=database, routing_="w")
    return [r.data() for r in records], summary.counters


def ensure_schema(driver, database):
    for stmt in SCHEMA_STATEMENTS:
        driver.execute_query(stmt, database_=database)


# --------------------------------------------------------------------------
# Read helpers used before writing

def fetch_existing_documents(driver, database):
    """doc_id -> {content_hash, source_path, doc_type, source_present, importer}."""
    rows = run_read(driver, database, """
        MATCH (d:Document)
        RETURN d.doc_id AS doc_id, d.content_hash AS content_hash,
               d.source_path AS source_path, d.doc_type AS doc_type,
               d.actor_id AS actor_id, d.source_present AS source_present,
               d.importer AS importer, d.parser_version AS parser_version
    """)
    return {r["doc_id"]: r for r in rows}


def fetch_keyword_links(driver, database):
    """Importer-owned MATCHES_KEYWORD links as a set of (doc_id, actor_id)."""
    rows = run_read(driver, database, """
        MATCH (a:Actor)-[r:MATCHES_KEYWORD {importer: $importer}]->(d:Document)
        RETURN d.doc_id AS doc_id, a.actor_id AS actor_id
    """, importer=I)
    return {(r["doc_id"], r["actor_id"]) for r in rows}


def ownership_conflicts(driver, database, actor_ids):
    """Actor/Moderator nodes with our ids that this importer does not own."""
    return run_read(driver, database, """
        MATCH (n) WHERE (n:Actor AND n.actor_id IN $actor_ids)
                     OR (n:Moderator AND n.moderator_id IN $moderator_ids)
        WITH n WHERE coalesce(n.importer, '') <> $importer
        RETURN labels(n) AS labels, coalesce(n.actor_id, n.moderator_id) AS id,
               n.importer AS importer
    """, actor_ids=list(actor_ids),
        moderator_ids=[m["moderator_id"] for m in model.MODERATORS], importer=I)


def actors_not_in_config(driver, database, actor_ids):
    return run_read(driver, database, """
        MATCH (a:Actor {importer: $importer}) WHERE NOT a.actor_id IN $actor_ids
        RETURN a.actor_id AS actor_id, a.name AS name
    """, actor_ids=list(actor_ids), importer=I)


# --------------------------------------------------------------------------
# Writes

def upsert_actors_and_moderators(driver, database, actors, now):
    _, c1 = run_write(driver, database, """
        UNWIND $actors AS row
        MERGE (a:Actor {actor_id: row.actor_id})
        ON CREATE SET a.importer = $importer, a.created_at = $now
        WITH a, row WHERE a.importer = $importer
        SET a.name = row.name, a.folder = row.folder, a.scope = $scope,
            a.akteure_labels = row.akteure_labels
    """, actors=[{"actor_id": a.actor_id, "name": a.name, "folder": a.folder,
                  "akteure_labels": list(a.akteure_labels)} for a in actors],
        importer=I, now=now, scope=model.SCOPE_ACTOR_TRUTH)
    _, c2 = run_write(driver, database, """
        UNWIND $mods AS row
        MERGE (m:Moderator {moderator_id: row.moderator_id})
        ON CREATE SET m.importer = $importer, m.created_at = $now
        WITH m, row WHERE m.importer = $importer
        SET m.name = row.name, m.scope = row.scope
    """, mods=list(model.MODERATORS), importer=I, now=now)
    return {"nodes_created": c1.nodes_created + c2.nodes_created}


def _upsert_query(doc_type):
    if doc_type not in model.DOC_TYPES:
        raise ValueError(f"unknown document type {doc_type!r}")
    other = "".join(f" REMOVE d:{t}" for t in model.DOC_TYPES if t != doc_type)
    profile = ""
    if doc_type == model.ACTOR_DATA:
        profile = """
        WITH d, row
        MATCH (a:Actor {actor_id: row.actor_id, importer: $importer})
        MERGE (a)-[p:HAS_PROFILE_SOURCE]->(d)
        ON CREATE SET p.importer = $importer, p.created_at = $now"""
    elif doc_type == model.ARTICLE:
        profile = """
        WITH d, row
        CALL (d, row) {
            UNWIND row.matched_actor_ids AS matched_id
            MATCH (a:Actor {actor_id: matched_id, importer: $importer})
            MERGE (a)-[k:MATCHES_KEYWORD]->(d)
            ON CREATE SET k.importer = $importer, k.created_at = $now, k.source = $keyword_source
        }"""
    return f"""
        UNWIND $rows AS row
        MERGE (d:Document {{doc_id: row.doc_id}})
        ON CREATE SET d.importer = $importer, d.first_imported_at = $now
        WITH d, row WHERE d.importer = $importer
        SET d:{doc_type}{other}
        FOREACH (_ IN CASE WHEN d.content_hash IS NULL
                             OR d.content_hash <> row.content_hash
                             OR d.source_path <> row.source_path
                             OR d.doc_type <> row.props.doc_type
                             OR coalesce(d.actor_id, '') <> coalesce(row.actor_id, '')
                             OR coalesce(d.parser_version, 0) <> row.props.parser_version
                           THEN [1] ELSE [] END |
            SET d += row.props, d.updated_at = $now)
        SET d.last_seen_at = $now, d.source_present = true, d.missing_since = null
        WITH d, row
        MATCH (m:Moderator {{moderator_id: row.moderator_id, importer: $importer}})
        MERGE (m)-[u:USES_SOURCE]->(d)
        ON CREATE SET u.importer = $importer, u.created_at = $now{profile}
    """


# Remove importer-owned incoming relationships that no longer match the rules
# (e.g. a document whose persistent id moved to another folder).
_CLEAN_RELS = """
    UNWIND $rows AS row
    MATCH (d:Document {doc_id: row.doc_id, importer: $importer})<-[r]-(x)
    WHERE r.importer = $importer AND NOT (
        (type(r) = 'USES_SOURCE' AND x:Moderator AND x.moderator_id = row.moderator_id)
        OR (type(r) = 'HAS_PROFILE_SOURCE' AND x:Actor AND row.actor_id IS NOT NULL
            AND x.actor_id = row.actor_id)
        OR (type(r) = 'MATCHES_KEYWORD' AND x:Actor AND x.actor_id IN row.matched_actor_ids))
    DELETE r
"""


def write_document_batch(driver, database, doc_type, rows, now):
    slim = [{k: r[k] for k in ("doc_id", "content_hash", "source_path", "moderator_id",
                               "actor_id", "matched_actor_ids", "props")} for r in rows]

    def work(tx):
        res = tx.run(_upsert_query(doc_type), rows=slim, importer=I, now=now,
                     keyword_source=model.KEYWORD_SOURCE)
        c1 = res.consume().counters
        res = tx.run(_CLEAN_RELS, rows=slim, importer=I)
        c2 = res.consume().counters
        return {"nodes_created": c1.nodes_created,
                "relationships_created": c1.relationships_created,
                "relationships_deleted": c2.relationships_deleted,
                "properties_set": c1.properties_set}

    with driver.session(database=database) as session:
        return session.execute_write(work)


_MISSING_FILTER = """
    d.importer = $importer
    AND NOT d.doc_id IN $seen_ids AND NOT d.source_path IN $present_paths
    AND ( (d.doc_type IN $complete_types)
          OR (d.doc_type = 'ActorData' AND d.actor_id IN $complete_actor_ids) )
"""


def missing_params(scan_result):
    return {
        "importer": I,
        "seen_ids": [e.doc_id for e in scan_result.entries],
        "present_paths": sorted(scan_result.present_paths),
        "complete_types": sorted(t for t in scan_result.complete_doc_types),
        "complete_actor_ids": sorted(scan_result.complete_actor_ids),
    }


def find_missing(driver, database, scan_result):
    return run_read(driver, database, f"""
        MATCH (d:Document) WHERE {_MISSING_FILTER}
        RETURN d.doc_id AS doc_id, d.source_path AS source_path, d.doc_type AS doc_type,
               d.source_present AS previously_present, d.missing_since AS missing_since
        ORDER BY d.source_path
    """, **missing_params(scan_result))


def mark_missing(driver, database, scan_result, now):
    rows, counters = run_write(driver, database, f"""
        MATCH (d:Document) WHERE {_MISSING_FILTER} AND d.source_present = true
        SET d.source_present = false, d.missing_since = $now
        RETURN d.doc_id AS doc_id, d.source_path AS source_path
    """, now=now, **missing_params(scan_result))
    return rows


def prune_missing(driver, database, scan_result):
    """Delete importer-owned documents that the current complete scan found missing.

    A document is kept (and reported) if any relationship touching it is not
    owned by this importer.
    """
    params = missing_params(scan_result)
    blocked = run_read(driver, database, f"""
        MATCH (d:Document) WHERE {_MISSING_FILTER} AND d.source_present = false
        MATCH (d)-[r]-()
        WHERE coalesce(r.importer, '') <> $importer
        RETURN DISTINCT d.doc_id AS doc_id, d.source_path AS source_path
    """, **params)
    rows, counters = run_write(driver, database, f"""
        MATCH (d:Document) WHERE {_MISSING_FILTER} AND d.source_present = false
          AND NOT d.doc_id IN $blocked
        WITH d, d.doc_id AS doc_id, d.source_path AS source_path
        DETACH DELETE d
        RETURN doc_id, source_path
    """, blocked=[b["doc_id"] for b in blocked], **params)
    return rows, blocked


# --------------------------------------------------------------------------
# Validation / diagnostics (read-only)

VALIDATION_QUERIES = {
    "truthseeker_missing_article_or_actordata": """
        MATCH (d:Document) WHERE (d:Article OR d:ActorData)
          AND NOT EXISTS { MATCH (:Moderator {moderator_id: 'truthseeker'})-[:USES_SOURCE]->(d) }
        RETURN count(d) AS n""",
    "legaleye_missing_legaltext": """
        MATCH (d:LegalText)
        WHERE NOT EXISTS { MATCH (:Moderator {moderator_id: 'legaleye'})-[:USES_SOURCE]->(d) }
        RETURN count(d) AS n""",
    "ethicmind_missing_ethicaltext": """
        MATCH (d:EthicalText)
        WHERE NOT EXISTS { MATCH (:Moderator {moderator_id: 'ethicmind'})-[:USES_SOURCE]->(d) }
        RETURN count(d) AS n""",
    "actordata_without_exactly_one_actor": """
        MATCH (d:ActorData)
        WITH d, COUNT { (:Actor)-[:HAS_PROFILE_SOURCE]->(d) } AS c WHERE c <> 1
        RETURN count(d) AS n""",
    "profile_edge_actor_mismatch": """
        MATCH (a:Actor)-[:HAS_PROFILE_SOURCE]->(d)
        WHERE NOT d:ActorData OR d.actor_id <> a.actor_id
        RETURN count(*) AS n""",
    "uses_source_wrong_moderator": """
        MATCH (m)-[:USES_SOURCE]->(d)
        WHERE NOT m:Moderator OR NOT d:Document
           OR NOT ( (m.moderator_id = 'truthseeker' AND (d:Article OR d:ActorData))
                 OR (m.moderator_id = 'legaleye' AND d:LegalText)
                 OR (m.moderator_id = 'ethicmind' AND d:EthicalText) )
        RETURN count(*) AS n""",
    "profile_edge_wrong_endpoints": """
        MATCH (x)-[:HAS_PROFILE_SOURCE]->(y) WHERE NOT x:Actor OR NOT y:Document
        RETURN count(*) AS n""",
    "cross_scope_relationships": """
        MATCH (x)-[r]->(y) WHERE x.scope IS NOT NULL AND y.scope IS NOT NULL
          AND x.scope <> y.scope
        RETURN count(r) AS n""",
    "actor_article_relationships_other_than_matches_keyword": """
        MATCH (:Actor)-[r]-(:Article) WHERE type(r) <> 'MATCHES_KEYWORD'
        RETURN count(r) AS n""",
    "matches_keyword_wrong_endpoints": """
        MATCH (x)-[:MATCHES_KEYWORD]->(y) WHERE NOT x:Actor OR NOT y:Article
        RETURN count(*) AS n""",
    "matches_keyword_without_source": """
        MATCH ()-[r:MATCHES_KEYWORD]->() WHERE r.source IS NULL RETURN count(r) AS n""",
    "matches_keyword_differs_from_akteure_tags": """
        MATCH (d:Article), (a:Actor)
        WITH a, d,
             any(l IN coalesce(a.akteure_labels, []) WHERE l IN coalesce(d.akteure, [])) AS tagged,
             EXISTS { (a)-[:MATCHES_KEYWORD]->(d) } AS linked
        WHERE tagged <> linked
        RETURN count(*) AS n""",
    "documents_without_exactly_one_type_label": """
        MATCH (d:Document)
        WITH d, size([l IN labels(d) WHERE l IN ['Article','ActorData','LegalText','EthicalText']]) AS k
        WHERE k <> 1 RETURN count(d) AS n""",
    "documents_with_wrong_scope": """
        MATCH (d:Document)
        WHERE NOT ( ((d:Article OR d:ActorData) AND d.scope = 'actor_truth')
                 OR (d:LegalText AND d.scope = 'legal')
                 OR (d:EthicalText AND d.scope = 'ethics') )
           OR d.doc_type IS NULL OR NOT d.doc_type IN labels(d)
        RETURN count(d) AS n""",
    "nodes_with_wrong_scope": """
        MATCH (n) WHERE (n:Actor AND n.scope <> 'actor_truth')
           OR (n:Moderator AND NOT ((n.moderator_id = 'truthseeker' AND n.scope = 'actor_truth')
                                 OR (n.moderator_id = 'legaleye' AND n.scope = 'legal')
                                 OR (n.moderator_id = 'ethicmind' AND n.scope = 'ethics')))
        RETURN count(n) AS n""",
    "duplicate_relationships": """
        MATCH (x)-[r]->(y) WITH x, y, type(r) AS t, count(r) AS c WHERE c > 1
        RETURN count(*) AS n""",
    "documents_missing_required_properties": """
        MATCH (d:Document)
        WHERE d.doc_id IS NULL OR d.scope IS NULL OR d.doc_type IS NULL OR d.title IS NULL
           OR d.title_origin IS NULL OR d.original_content IS NULL OR d.text IS NULL
           OR d.source_path IS NULL OR d.content_hash IS NULL OR d.first_imported_at IS NULL
           OR d.updated_at IS NULL OR d.importer IS NULL
        RETURN count(d) AS n""",
}


def validate(driver, database):
    """Run all consistency checks. Each check passes when its count is 0."""
    checks = {}
    for name, query in VALIDATION_QUERIES.items():
        n = run_read(driver, database, query)[0]["n"]
        checks[name] = {"count": n, "passed": n == 0}
    return checks


def counts(driver, database):
    nodes = run_read(driver, database, """
        CALL () {
          MATCH (n:Actor) RETURN 'Actor' AS label, count(n) AS n
          UNION ALL MATCH (n:Moderator) RETURN 'Moderator' AS label, count(n) AS n
          UNION ALL MATCH (n:Document) RETURN 'Document' AS label, count(n) AS n
          UNION ALL MATCH (n:Article) RETURN 'Article' AS label, count(n) AS n
          UNION ALL MATCH (n:ActorData) RETURN 'ActorData' AS label, count(n) AS n
          UNION ALL MATCH (n:LegalText) RETURN 'LegalText' AS label, count(n) AS n
          UNION ALL MATCH (n:EthicalText) RETURN 'EthicalText' AS label, count(n) AS n
        } RETURN label, n""")
    rels = run_read(driver, database, """
        MATCH (x)-[r]->(d:Document)
        RETURN type(r) AS type, coalesce(x.moderator_id, x.actor_id) AS from_id, count(r) AS n
        ORDER BY type, from_id""")
    presence = run_read(driver, database, """
        MATCH (d:Document)
        RETURN d.doc_type AS doc_type, coalesce(d.source_present, false) AS source_present,
               count(d) AS n ORDER BY doc_type, source_present""")
    return {
        "nodes": {r["label"]: r["n"] for r in nodes},
        "relationships": rels,
        "relationship_totals": _totals(rels),
        "source_presence": presence,
    }


def _totals(rels):
    out = {}
    for r in rels:
        out[r["type"]] = out.get(r["type"], 0) + r["n"]
    return out


def graph_fingerprint(driver, database):
    """Counts and a checksum of all node/relationship state (used by tests)."""
    return run_read(driver, database, """
        MATCH (n) WITH count(n) AS nodes
        OPTIONAL MATCH ()-[r]->() WITH nodes, count(r) AS rels
        OPTIONAL MATCH (d:Document)
        RETURN nodes, rels, collect(d.doc_id + ':' + coalesce(d.content_hash, '') + ':'
                                    + coalesce(d.last_seen_at, '')) AS docs""")[0]
