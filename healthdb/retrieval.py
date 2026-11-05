"""Retrieval functions for the agents.

Each function follows one fixed relationship pattern and checks the scope; no
open-ended graph traversal is used. These functions are a convenience, NOT
access control: anyone with unrestricted Cypher access to the database can
read every scope.
"""

import json
from pathlib import Path

from . import graph, model

_SUMMARY = """d {
    .doc_id, .scope, .doc_type, .title, .title_origin, .source_path, .content_hash,
    .source_present, .missing_since, .actor_id, .publisher, .publication_date,
    .source_version, .valid_from, .valid_until, .author, .media_type, .akteure,
    .first_imported_at, .updated_at, .last_seen_at
}"""
_WITH_CONTENT = _SUMMARY[:-1] + ", .text, .original_content }"


def _projection(include_content):
    return _WITH_CONTENT if include_content else _SUMMARY


def _page(limit, skip):
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a non-negative integer or None")
    if not isinstance(skip, int) or skip < 0:
        raise ValueError("skip must be a non-negative integer")
    return {"limit": limit if limit is not None else 2**62, "skip": skip}


def actor_profile_sources(driver, database, actor_id, include_content=False, limit=None, skip=0):
    """A. Documents linked to one actor via HAS_PROFILE_SOURCE (never via TruthSeeker)."""
    if not graph.run_read(driver, database, "MATCH (a:Actor {actor_id: $id}) RETURN a.actor_id AS id",
                          id=actor_id):
        raise LookupError(f"unknown actor {actor_id!r}")
    rows = graph.run_read(driver, database, f"""
        MATCH (a:Actor {{actor_id: $actor_id}})-[:HAS_PROFILE_SOURCE]->(d:Document:ActorData)
        WHERE d.scope = 'actor_truth' AND d.actor_id = $actor_id
        RETURN {_projection(include_content)} AS doc
        ORDER BY d.source_path SKIP $skip LIMIT $limit
    """, actor_id=actor_id, **_page(limit, skip))
    return [r["doc"] for r in rows]


def truthseeker_sources(driver, database, doc_types=None, institutions=None,
                        include_content=False, limit=None, skip=0):
    """B. Article and ActorData documents TruthSeeker uses.

    doc_types: subset of ["Article", "ActorData"] (default both).
    institutions: actor ids restricting ActorData documents (default all).
    """
    allowed = {model.ARTICLE, model.ACTOR_DATA}
    types = list(doc_types) if doc_types else sorted(allowed)
    bad = set(types) - allowed
    if bad:
        raise ValueError(f"TruthSeeker only uses Article and ActorData, not {sorted(bad)}")
    rows = graph.run_read(driver, database, f"""
        MATCH (:Moderator {{moderator_id: 'truthseeker'}})-[:USES_SOURCE]->(d:Document)
        WHERE d.scope = 'actor_truth' AND (d:Article OR d:ActorData) AND d.doc_type IN $types
          AND ($institutions IS NULL OR d:Article OR d.actor_id IN $institutions)
        RETURN {_projection(include_content)} AS doc
        ORDER BY d.doc_type, d.source_path SKIP $skip LIMIT $limit
    """, types=types, institutions=list(institutions) if institutions else None,
        **_page(limit, skip))
    return [r["doc"] for r in rows]


def _moderator_sources(driver, database, moderator_id, label, scope, include_content, limit, skip):
    rows = graph.run_read(driver, database, f"""
        MATCH (:Moderator {{moderator_id: $mid}})-[:USES_SOURCE]->(d:Document:{label})
        WHERE d.scope = $scope
        RETURN {_projection(include_content)} AS doc
        ORDER BY d.source_path SKIP $skip LIMIT $limit
    """, mid=moderator_id, scope=scope, **_page(limit, skip))
    return [r["doc"] for r in rows]


def legaleye_sources(driver, database, include_content=False, limit=None, skip=0):
    """C. LegalText documents connected to LegalEye."""
    return _moderator_sources(driver, database, model.LEGALEYE, model.LEGAL_TEXT,
                              model.SCOPE_LEGAL, include_content, limit, skip)


def ethicmind_sources(driver, database, include_content=False, limit=None, skip=0):
    """D. EthicalText documents connected to EthicMind."""
    return _moderator_sources(driver, database, model.ETHICMIND, model.ETHICAL_TEXT,
                              model.SCOPE_ETHICS, include_content, limit, skip)


def get_document(driver, database, doc_id):
    """E. One document with content, metadata and provenance, or None."""
    rows = graph.run_read(driver, database, """
        MATCH (d:Document {doc_id: $doc_id})
        OPTIONAL MATCH (x)-[r]->(d)
        RETURN properties(d) AS doc, [l IN labels(d) WHERE l <> 'Document'] AS type_labels,
               collect(CASE WHEN r IS NULL THEN NULL ELSE {
                   relationship: type(r), from_label: head(labels(x)),
                   from_id: coalesce(x.actor_id, x.moderator_id),
                   importer: r.importer, created_at: r.created_at } END) AS incoming
    """, doc_id=doc_id)
    if not rows:
        return None
    r = rows[0]
    doc = r["doc"]
    doc["type_labels"] = r["type_labels"]
    doc["provenance"] = {
        "source_path": doc.get("source_path"),
        "source_present": doc.get("source_present"),
        "missing_since": doc.get("missing_since"),
        "content_hash": doc.get("content_hash"),
        "importer": doc.get("importer"),
        "first_imported_at": doc.get("first_imported_at"),
        "updated_at": doc.get("updated_at"),
        "last_seen_at": doc.get("last_seen_at"),
        "incoming_relationships": [i for i in r["incoming"] if i],
    }
    return doc


def import_diagnostics(driver, database, report_path=None):
    """F. Counts, validation checks, source presence and the last report's issues."""
    out = {
        "features": {"actor_article_matching": model.ACTOR_ARTICLE_MATCHING_STATUS},
        "counts": graph.counts(driver, database),
        "validation": graph.validate(driver, database),
        "missing_from_source": graph.run_read(driver, database, """
            MATCH (d:Document) WHERE d.source_present = false
            RETURN d.doc_id AS doc_id, d.source_path AS source_path, d.doc_type AS doc_type,
                   d.missing_since AS missing_since ORDER BY d.source_path"""),
        "last_import_report": None,
    }
    if report_path and Path(report_path).is_file():
        rep = json.loads(Path(report_path).read_text(encoding="utf-8"))
        out["last_import_report"] = {
            "path": str(report_path),
            "status": rep.get("status"),
            "finished_at": rep.get("finished_at"),
            "dry_run": rep.get("dry_run"),
            "issue_counts": rep.get("issue_counts"),
            "errors": [i for i in rep.get("issues", []) if i["level"] == "error"],
            "warnings": [i for i in rep.get("issues", []) if i["level"] == "warning"],
        }
    return out


def actor_articles(driver, database, actor_id, include_content=False, limit=None, skip=0):
    """Articles linked to one actor via MATCHES_KEYWORD.

    The links come from the `akteure` tags of an external system. A link means
    the article was tagged, not that it states the actor's position. An
    indirect path through TruthSeeker is NOT a keyword match or a stance.
    """
    if not graph.run_read(driver, database, "MATCH (a:Actor {actor_id: $id}) RETURN a.actor_id AS id",
                          id=actor_id):
        raise LookupError(f"unknown actor {actor_id!r}")
    rows = graph.run_read(driver, database, f"""
        MATCH (:Actor {{actor_id: $actor_id}})-[:MATCHES_KEYWORD]->(d:Document:Article)
        WHERE d.scope = 'actor_truth'
        RETURN {_projection(include_content)} AS doc
        ORDER BY d.publication_date IS NULL, d.publication_date DESC, d.source_path
        SKIP $skip LIMIT $limit
    """, actor_id=actor_id, **_page(limit, skip))
    return {
        "actor_id": actor_id,
        "status": model.ACTOR_ARTICLE_MATCHING_STATUS,
        "results": [r["doc"] for r in rows],
        "note": "Links come from akteure tags made by an external system. A missing link "
                "means the article was not tagged, not that it is irrelevant to the actor.",
    }
