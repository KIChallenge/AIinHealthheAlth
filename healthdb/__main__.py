"""Command line interface.

    python -m healthdb init-schema
    python -m healthdb import [--dry-run] [--prune-missing] [--report PATH]
    python -m healthdb diagnostics [--report PATH]
    python -m healthdb sources {actor ID | articles ID | truthseeker | legaleye | ethicmind} [--limit N]
    python -m healthdb doc DOC_ID [--content]

Exit codes: 0 success, 1 completed with errors, 2 failed.
"""

import argparse
import json
import sys

from . import config, graph, retrieval
from .importer import run_import
from .report import summary_lines, write_json

DEFAULT_REPORT = config.PROJECT_ROOT / "reports" / "import_report.json"
EXIT = {"success": 0, "completed_with_errors": 1, "failed": 2}


def _print_json(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="healthdb")
    p.add_argument("--data-dir", help="data directory (default: $HEALTHDB_DATA_DIR or ./data)")
    p.add_argument("--actors", help="actor configuration JSON (default: config/actors.json)")
    p.add_argument("--import-config", help="import options JSON (default: config/import.json)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-schema", help="create constraints and indexes")
    pi = sub.add_parser("import", help="import source documents")
    pi.add_argument("--dry-run", action="store_true", help="validate and plan; write nothing")
    pi.add_argument("--prune-missing", action="store_true",
                    help="delete importer-owned documents whose source files are gone")
    pi.add_argument("--report", default=str(DEFAULT_REPORT), help="JSON report path")
    pd = sub.add_parser("diagnostics", help="counts, checks, missing sources")
    pd.add_argument("--report", default=str(DEFAULT_REPORT), help="last import report to include")
    ps = sub.add_parser("sources", help="list sources for an actor or moderator")
    ps.add_argument("who", help="'actor', 'articles', 'truthseeker', 'legaleye' or 'ethicmind'")
    ps.add_argument("actor_id", nargs="?")
    ps.add_argument("--limit", type=int, default=20)
    ps.add_argument("--type", action="append", dest="types", help="TruthSeeker: Article/ActorData")
    ps.add_argument("--institution", action="append", help="TruthSeeker: actor id filter")
    pdoc = sub.add_parser("doc", help="show one document by id")
    pdoc.add_argument("doc_id")
    pdoc.add_argument("--content", action="store_true", help="include full content")
    args = p.parse_args(argv)

    try:
        actors = config.load_actors(args.actors)
        options = config.load_import_options(args.import_config)
    except config.ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    data_dir = args.data_dir or config.data_dir()

    driver, database, conn_error = None, None, None
    try:
        settings = config.connection_settings()
        database = settings.database
        driver = graph.connect(settings)
    except Exception as exc:          # config missing or server unreachable
        conn_error = f"{type(exc).__name__}: {exc}"

    try:
        if args.cmd == "import":
            if conn_error:
                print(f"database connection unavailable: {conn_error}", file=sys.stderr)
            report = run_import(data_dir, actors, options, driver, database or "neo4j",
                                dry_run=args.dry_run, prune=args.prune_missing)
            if conn_error:
                report["connection_error"] = conn_error
            path = write_json(report, args.report)
            print("\n".join(summary_lines(report)))
            print(f"report written to {path}")
            return EXIT[report["status"]]

        if conn_error:
            print(f"database connection unavailable: {conn_error}", file=sys.stderr)
            return 2
        if args.cmd == "init-schema":
            graph.ensure_schema(driver, database)
            print("constraints and indexes ensured")
        elif args.cmd == "diagnostics":
            _print_json(retrieval.import_diagnostics(driver, database, args.report))
        elif args.cmd == "sources":
            if args.who == "actor":
                if not args.actor_id:
                    p.error("sources actor needs an actor id")
                docs = retrieval.actor_profile_sources(driver, database, args.actor_id, limit=args.limit)
            elif args.who == "articles":
                if not args.actor_id:
                    p.error("sources articles needs an actor id")
                docs = retrieval.actor_articles(driver, database, args.actor_id, limit=args.limit)
            elif args.who == "truthseeker":
                docs = retrieval.truthseeker_sources(driver, database, args.types, args.institution,
                                                     limit=args.limit)
            elif args.who == "legaleye":
                docs = retrieval.legaleye_sources(driver, database, limit=args.limit)
            elif args.who == "ethicmind":
                docs = retrieval.ethicmind_sources(driver, database, limit=args.limit)
            else:
                p.error(f"unknown source owner {args.who!r}")
            _print_json(docs)
        elif args.cmd == "doc":
            doc = retrieval.get_document(driver, database, args.doc_id)
            if doc is None:
                print(f"no document {args.doc_id!r}", file=sys.stderr)
                return 1
            if not args.content:
                for k in ("original_content", "text"):
                    if doc.get(k):
                        doc[k] = doc[k][:500] + ("..." if len(doc[k]) > 500 else "")
            _print_json(doc)
        return 0
    finally:
        if driver is not None:
            driver.close()


if __name__ == "__main__":
    sys.exit(main())
