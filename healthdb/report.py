"""Writing the machine-readable report and a short console summary."""

import json
from pathlib import Path


def write_json(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def summary_lines(report):
    lines = [
        f"status: {report['status']}" + ("  (DRY RUN - nothing written)" if report["dry_run"] else ""),
        f"actor_article_matching: {report['features']['actor_article_matching']}",
        "folders:",
    ]
    for f in report["folders"]:
        lines.append(f"  {f['folder']:<40} {f['status']:<20} files={f['files']}")
    inv = report.get("inventory", {})
    lines.append(f"valid documents: {inv.get('valid_documents')}  by type: {inv.get('by_type')}")
    lines.append(f"excluded files: {len(report.get('excluded', []))}")
    if report.get("planned"):
        lines.append(f"planned: { {k: v for k, v in report['planned'].items() if k != 'by_type'} }")
    if report.get("counts"):
        lines.append(f"nodes: {report['counts']['nodes']}")
        lines.append(f"relationships: {report['counts']['relationship_totals']}")
    if report.get("validation"):
        failed = {k: v["count"] for k, v in report["validation"].items() if not v["passed"]}
        lines.append("validation: all checks passed" if not failed else f"validation FAILED: {failed}")
    lines.append(f"missing from source inventory: {len(report.get('missing_from_source') or [])}")
    lines.append(f"issues: {report.get('issue_counts', {})}")
    for code, n in sorted(report.get("issue_codes", {}).items()):
        lines.append(f"  {code}: {n}")
    errors = [i for i in report.get("issues", []) if i["level"] == "error"]
    for i in errors[:20]:
        lines.append(f"  ERROR {i['code']}: {i['path']}: {i['reason']}")
    if len(errors) > 20:
        lines.append(f"  ... {len(errors) - 20} more errors in the JSON report")
    if report.get("failure"):
        lines.append(f"FAILURE: {report['failure']}")
        committed = [c for c in report.get("committed", [])]
        lines.append(f"steps already committed before the failure: {len(committed)}")
    return lines
