"""Import orchestration: scan -> classify -> write -> mark missing -> validate."""

import traceback
from collections import Counter

from . import graph, model
from .scan import ERROR, INFO, WARNING, load_document, scan

MAX_BATCH_BYTES = 16 * 1024 * 1024
DOC_TYPE_ORDER = (model.ACTOR_DATA, model.ARTICLE, model.ETHICAL_TEXT, model.LEGAL_TEXT)


def _classify(entries, existing, result):
    """Return {doc_id: action} with action in create/update/unchanged/reappear.

    Entries that conflict with existing records are removed and reported.
    """
    by_path = {e.source_path: e for e in result.entries}
    plan, keep = {}, []
    for e in entries:
        old = existing.get(e.doc_id)
        if old is None:
            plan[e.doc_id] = "create"
            keep.append(e)
            continue
        if old.get("importer") != model.IMPORTER:
            result.add(ERROR, "document_id_owned_by_other", e.source_path,
                       f"document {e.doc_id!r} exists but is not owned by {model.IMPORTER}; skipped")
            continue
        if old.get("source_path") != e.source_path:
            other = by_path.get(old.get("source_path"))
            if not e.persistent_id:
                result.add(ERROR, "document_id_collision", e.source_path,
                           f"id {e.doc_id!r} already belongs to {old.get('source_path')!r}; skipped")
                continue
            if other is not None and other is not e:
                result.add(ERROR, "conflicting_persistent_id", e.source_path,
                           f"persistent id {e.persistent_id!r} is stored for "
                           f"{old.get('source_path')!r}, which still exists; skipped")
                continue
            result.add(INFO, "persistent_id_moved", e.source_path,
                       f"persistent id {e.persistent_id!r} moved from {old.get('source_path')!r}")
        if old.get("source_present") is False:
            plan[e.doc_id] = "reappear"
        elif (old.get("content_hash") == e.content_hash and old.get("source_path") == e.source_path
              and old.get("doc_type") == e.doc_type and old.get("actor_id") == e.actor_id
              and old.get("parser_version") == model.PARSER_VERSION):
            plan[e.doc_id] = "unchanged"
        else:
            plan[e.doc_id] = "update"
        keep.append(e)
    return plan, keep


def _batches(entries, batch_size):
    batch, size = [], 0
    for e in entries:
        est = e.abs_path.stat().st_size * 3
        if batch and (len(batch) >= batch_size or size + est > MAX_BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(e)
        size += est
    if batch:
        yield batch


def run_import(data_dir, actors, options, driver=None, database="neo4j",
               dry_run=False, prune=False):
    report = {
        "status": None,
        "dry_run": dry_run,
        "prune_missing": prune,
        "started_at": graph.now_iso(),
        "finished_at": None,
        "data_dir": str(data_dir),
        "database": database,
        "features": {"actor_article_matching": model.ACTOR_ARTICLE_MATCHING_STATUS},
        "actors": [{"actor_id": a.actor_id, "name": a.name, "folder": a.folder} for a in actors],
        "folders": [],
        "inventory": {},
        "excluded": [],
        "planned": {},
        "committed": [],
        "missing_from_source": [],
        "missing_marking": None,
        "pruned": [],
        "prune_blocked": [],
        "counts": None,
        "validation": None,
        "issues": [],
        "failure": None,
    }
    now = graph.now_iso()
    result = scan(data_dir, actors, options)
    report["folders"] = result.folders
    report["excluded"] = result.excluded

    def finish(status):
        report["status"] = status
        report["issues"] = [i.as_dict() for i in result.issues]
        report["issue_counts"] = dict(Counter(i.level for i in result.issues))
        report["issue_codes"] = dict(Counter(f"{i.level}:{i.code}" for i in result.issues))
        report["finished_at"] = graph.now_iso()
        return report

    report["inventory"] = {
        "valid_documents": len(result.entries),
        "by_type": dict(Counter(e.doc_type for e in result.entries)),
        "by_actor": dict(Counter(e.actor_id for e in result.entries if e.actor_id)),
        "excluded_files": len(result.excluded),
        "title_origin": dict(Counter(e.title_origin for e in result.entries)),
        "matches_keyword_per_actor": dict(Counter(
            a for e in result.entries for a in e.matched_actor_ids)),
        "complete_doc_types": sorted(result.complete_doc_types),
        "complete_actor_ids": sorted(result.complete_actor_ids),
    }
    if result.fatal:
        report["failure"] = "directory scan failed; nothing was written"
        return finish("failed")

    if driver is None:
        if not dry_run:
            result.add(ERROR, "no_database", "", "no database connection; nothing was written")
            report["failure"] = "no database connection"
            return finish("failed")
        report["planned"] = {"note": "database not reachable; existing state unknown",
                             "documents_to_import": len(result.entries)}
        return finish("completed_with_errors" if result.count(ERROR) else "success")

    try:
        existing = graph.fetch_existing_documents(driver, database)
        conflicts = graph.ownership_conflicts(driver, database, [a.actor_id for a in actors])
        for c in conflicts:
            result.add(ERROR, "node_owned_by_other", c["id"],
                       f"{c['labels']} node {c['id']!r} exists with importer={c['importer']!r}")
        for a in graph.actors_not_in_config(driver, database, [a.actor_id for a in actors]):
            result.add(WARNING, "actor_in_db_not_in_config", a["actor_id"],
                       "actor exists in the database but not in the configuration; left unchanged")
        plan, entries = _classify(result.entries, existing, result)
        report["planned"] = dict(Counter(plan.values()))
        report["planned"]["by_type"] = {
            t: dict(Counter(plan[e.doc_id] for e in entries if e.doc_type == t))
            for t in DOC_TYPE_ORDER}
        wanted = {(e.doc_id, a) for e in entries for a in e.matched_actor_ids}
        current = graph.fetch_keyword_links(driver, database)
        kept_ids = {e.doc_id for e in entries}
        report["planned"]["matches_keyword"] = {
            "create": len(wanted - current),
            "delete": sum(1 for doc_id, a in current - wanted if doc_id in kept_ids),
            "unchanged": len(wanted & current),
        }
        missing = graph.find_missing(driver, database, result)
        report["missing_from_source"] = missing
        report["missing_marking"] = {
            "units_checked": {"doc_types": sorted(result.complete_doc_types),
                              "actor_ids": sorted(result.complete_actor_ids)},
            "note": "only units whose directory scan completed are compared",
        }
        if conflicts:
            report["failure"] = "ownership conflict on Actor/Moderator nodes; nothing was written"
            return finish("failed")
        if dry_run:
            report["planned"]["missing_to_mark"] = sum(1 for m in missing if m["previously_present"])
            if prune:
                report["planned"]["to_prune"] = len(missing)
            return finish("completed_with_errors" if result.count(ERROR) else "success")

        graph.ensure_schema(driver, database)
        report["committed"].append({"step": "schema"})
        c = graph.upsert_actors_and_moderators(driver, database, actors, now)
        report["committed"].append({"step": "actors_and_moderators", **c})

        for doc_type in DOC_TYPE_ORDER:
            typed = [e for e in entries if e.doc_type == doc_type]
            for n, batch in enumerate(_batches(typed, options.batch_size), 1):
                rows = []
                for e in batch:
                    try:
                        rows.append(load_document(e, now))
                    except Exception as exc:   # file changed / vanished since scan
                        result.add(ERROR, "file_changed_during_import", e.source_path, str(exc))
                if not rows:
                    continue
                c = graph.write_document_batch(driver, database, doc_type, rows, now)
                report["committed"].append({"step": "documents", "doc_type": doc_type,
                                            "batch": n, "documents": len(rows), **c})

        marked = graph.mark_missing(driver, database, result, now)
        report["missing_marking"]["newly_marked"] = marked
        report["committed"].append({"step": "mark_missing", "documents": len(marked)})
        if prune:
            pruned, blocked = graph.prune_missing(driver, database, result)
            report["pruned"], report["prune_blocked"] = pruned, blocked
            report["committed"].append({"step": "prune_missing", "documents": len(pruned)})
            for b in blocked:
                result.add(WARNING, "prune_blocked", b["source_path"],
                           "not deleted: has relationships not owned by this importer")
        report["missing_from_source"] = graph.find_missing(driver, database, result)
        report["validation"] = graph.validate(driver, database)
        report["counts"] = graph.counts(driver, database)
    except Exception as exc:
        report["failure"] = f"{type(exc).__name__}: {exc}"
        report["failure_traceback"] = traceback.format_exc()
        result.add(ERROR, "database_error", "", report["failure"])
        return finish("failed")

    failed_checks = [k for k, v in report["validation"].items() if not v["passed"]]
    for k in failed_checks:
        result.add(ERROR, "validation_failed", "", f"{k}: {report['validation'][k]['count']}")
    return finish("completed_with_errors" if result.count(ERROR) else "success")
