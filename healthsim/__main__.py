"""Command line interface. Stdout carries only the JSON result; logs go to stderr.

    python -m healthsim run [--input FILE|-] [--output FILE|-]
    python -m healthsim resume DEBATE_ID [--output FILE|-]
    python -m healthsim check-connection
    python -m healthsim export-schemas
    python -m healthsim export-diagram

Exit codes: 0 completed, 1 incomplete, 2 needs_clarification, 3 invalid_input, 4 failed.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .app import (EXIT_CODES, check_connections, export_diagram, export_schemas, failure_result, new_debate_id,
                  resume_debate, run_text, to_stdout)
from .errors import SimError


def write(result, output):
    if output in (None, "-"):
        to_stdout(result)
    else:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    return EXIT_CODES[result["status"]]


def main(argv=None):
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="healthsim", description="HealthPolicy Simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Debatte ausführen")
    run.add_argument("--input", default="-", help="JSON-Datei oder - für stdin")
    run.add_argument("--output", default="-", help="Ausgabedatei oder - für stdout")
    resume = sub.add_parser("resume", help="Unterbrochene Debatte fortsetzen")
    resume.add_argument("debate_id")
    resume.add_argument("--output", default="-")
    for command in (run, resume):
        command.add_argument("--llm", choices=("azure", "fake"), default="azure",
                             help="fake nur ausdrücklich für Offline-Läufe")
        command.add_argument("--sources", choices=("neo4j", "fixture"), default="neo4j")
        command.add_argument("--fixture", help="JSON-Datei für --sources fixture")
    web = sub.add_parser("serve", help="Debatten-Player lokal starten")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--llm", choices=("azure", "fake"), default="azure")
    web.add_argument("--sources", choices=("neo4j", "fixture"), default="neo4j")
    web.add_argument("--fixture")
    sub.add_parser("check-connection", help="Neo4j und Azure prüfen")
    sub.add_parser("export-schemas", help="JSON-Schemas schreiben")
    sub.add_parser("export-diagram", help="Mermaid-Diagramm des Workflows schreiben")
    args = parser.parse_args(argv)

    if args.command == "run":
        try:
            raw = sys.stdin.buffer.read().decode("utf-8") if args.input == "-" \
                else Path(args.input).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return write(failure_result(new_debate_id(), "input_unreadable",
                                        f"Die Eingabe ist nicht lesbar: {type(exc).__name__}."), args.output)
        return write(asyncio.run(run_text(raw, args.llm, args.sources, args.fixture)), args.output)
    if args.command == "resume":
        return write(asyncio.run(resume_debate(args.debate_id, args.llm, args.sources, args.fixture)), args.output)
    if args.command == "serve":
        from .webapp import serve
        options = ["--llm", args.llm, "--sources", args.sources]
        serve(args.port, options + (["--fixture", args.fixture] if args.fixture else []))
        return 0
    if args.command == "check-connection":
        try:
            report = asyncio.run(check_connections())
        except SimError as exc:
            to_stdout({"neo4j": {"ok": False}, "azure": {"ok": False}, "error": exc.message})
            return 4
        to_stdout(report)
        return 0 if report["neo4j"]["ok"] and report["azure"]["ok"] else 4
    if args.command == "export-schemas":
        to_stdout({"verzeichnis": str(export_schemas())})
    elif args.command == "export-diagram":
        to_stdout({"datei": str(export_diagram())})
    return 0


if __name__ == "__main__":
    sys.exit(main())
