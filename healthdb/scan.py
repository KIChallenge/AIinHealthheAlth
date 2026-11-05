"""Filesystem inventory and validation. No database access.

Pass 1 (`scan`) walks the data directory, validates every file and keeps only
light-weight entries (ids, hashes, titles) so that identifier conflicts are
detected before anything is written. Pass 2 (`load_document`) re-reads one
file and builds the full property map for writing; it refuses files whose
content changed since pass 1.
"""

import fnmatch
import hashlib
import os
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import model
from .config import akteure_mapping
from .legalxml import LegalXmlError, parse_legal_xml
from .markdown import FrontMatterError, parse_markdown

ERROR, WARNING, INFO = "error", "warning", "info"

# Optional metadata properties a Document may carry. Absent values are stored
# as null (which removes a stale property on update); nothing is invented.
OPTIONAL_PROPERTIES = (
    "publisher", "publication_date", "publication_date_raw", "source_version",
    "valid_from", "valid_until", "source_url", "author", "media_type", "country",
    "language", "press_review", "source_pdf", "source_pages",
    "omitted_facsimile_pages", "law_title", "law_abbreviation", "section_type",
    "section_number", "amendment_title", "ris_dokid", "persistent_id",
    "valid_from_raw", "valid_until_raw", "akteure",
)


@dataclass
class Issue:
    level: str
    code: str
    path: str
    reason: str

    def as_dict(self):
        return {"level": self.level, "code": self.code, "path": self.path, "reason": self.reason}


@dataclass
class Entry:
    """A validated source file (light-weight, no content kept)."""
    abs_path: Path
    source_path: str          # NFC, POSIX, relative to the data directory
    doc_type: str
    scope: str
    actor_id: str | None
    doc_id: str
    content_hash: str
    title: str
    title_origin: str
    persistent_id: str | None = None
    ris_dokid: str | None = None
    has_front_matter: bool = False
    matched_actor_ids: tuple = ()   # actors linked via MATCHES_KEYWORD (Articles only)


@dataclass
class ScanResult:
    data_dir: Path
    entries: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    folders: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    # Units whose directory scan completed without folder-level problems.
    # Only these may be used to decide that a database record is missing.
    complete_doc_types: set = field(default_factory=set)      # Article/LegalText/EthicalText
    complete_actor_ids: set = field(default_factory=set)      # ActorData per actor
    # Every file path found on disk (including rejected/excluded ones).
    present_paths: set = field(default_factory=set)
    fatal: bool = False
    akteure_map: dict = field(default_factory=dict)      # tag value -> actor_id
    akteure_known: set = field(default_factory=set)      # all accepted tag values

    def add(self, level, code, path, reason):
        self.issues.append(Issue(level, code, str(path), reason))

    def count(self, level):
        return sum(1 for i in self.issues if i.level == level)


def normalise_rel_path(path, data_dir):
    rel = PurePosixPath(Path(path).relative_to(data_dir).as_posix())
    return unicodedata.normalize("NFC", str(rel))


def default_doc_id(scope, source_path):
    """Stable, collision-safe id derived from scope and normalised path."""
    return hashlib.sha256(f"{scope}\n{source_path}".encode("utf-8")).hexdigest()


def persistent_doc_id(scope, persistent_id):
    return f"{scope}:{persistent_id}"


def _read_and_parse(abs_path, doc_type):
    """Return (raw_bytes, decoded_text, parsed) or raise the specific error."""
    data = abs_path.read_bytes()
    if not data.strip():
        raise _EmptyFile()
    text = data.decode("utf-8")              # UnicodeDecodeError on bad input
    if text.startswith("﻿"):
        text = text[1:]
    stem = unicodedata.normalize("NFC", abs_path.stem)
    if abs_path.suffix.lower() == ".xml":
        parsed = parse_legal_xml(data, stem)
    else:
        parsed = parse_markdown(text.replace("\r\n", "\n").replace("\r", "\n"), stem)
    return data, text, parsed


class _EmptyFile(Exception):
    pass


def _list_dir(path, result, unit_ok):
    try:
        return sorted(os.scandir(path), key=lambda e: e.name)
    except OSError as exc:
        result.add(ERROR, "unreadable_directory", path, str(exc))
        unit_ok[0] = False
        return []


def _walk_files(root, result, unit_ok):
    """Recursively yield files under root (symlinks are not followed)."""
    def onerror(exc):
        result.add(ERROR, "unreadable_directory", getattr(exc, "filename", root), str(exc))
        unit_ok[0] = False
    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name


def scan(data_dir, actors, options):
    """Inventory and validate the data directory."""
    data_dir = Path(data_dir).resolve()
    result = ScanResult(data_dir=data_dir)
    result.akteure_map = akteure_mapping(actors)
    result.akteure_known = set(result.akteure_map) | set(options.akteure_list_only)
    overlap = set(result.akteure_map) & set(options.akteure_list_only)
    if overlap:
        result.add(ERROR, "akteure_config_conflict", "config",
                   f"values both mapped to an actor and list-only: {sorted(overlap)}")
        result.fatal = True
        return result
    if not data_dir.is_dir():
        result.add(ERROR, "missing_data_directory", data_dir, "data directory does not exist")
        result.fatal = True
        return result

    top_ok = [True]
    top = {}
    for item in _list_dir(data_dir, result, top_ok):
        key = item.name.casefold()
        if item.is_dir(follow_symlinks=False) and key in model.SOURCE_FOLDERS:
            if key in top:
                result.add(ERROR, "ambiguous_folder", item.path,
                           f"both {top[key].name!r} and {item.name!r} map to {key!r}")
                top[key] = None
            else:
                top[key] = item
        elif item.is_dir(follow_symlinks=False):
            result.add(WARNING, "unknown_top_level_folder", item.path,
                       "not a configured source folder; contents not imported")
        else:
            result.add(WARNING, "file_outside_source_folder", item.path,
                       "files directly in the data directory are not imported")
    if not top_ok[0]:
        result.fatal = True
        return result

    for key, spec in model.SOURCE_FOLDERS.items():
        folder = top.get(key)
        if folder is None:
            if key not in top:
                result.add(WARNING, "missing_folder", data_dir / key,
                           "expected source folder does not exist")
                result.folders.append({"folder": key, "path": str(data_dir / key),
                                       "status": "missing", "files": 0})
            continue
        if spec["doc_type"] == model.ACTOR_DATA:
            _scan_actor_data(Path(folder.path), actors, options, result)
        else:
            unit_ok = [True]
            n = _scan_unit(Path(folder.path), key, spec, None, options, result, unit_ok)
            status = "ok" if unit_ok[0] else "incomplete"
            if n == 0 and unit_ok[0]:
                status = "empty"
                result.add(WARNING, "empty_folder", folder.path, "folder exists but contains no files")
            result.folders.append({"folder": key, "path": folder.path, "status": status, "files": n})
            if unit_ok[0]:
                result.complete_doc_types.add(spec["doc_type"])

    _check_identifiers(result)
    return result


def _scan_actor_data(root, actors, options, result):
    spec = model.SOURCE_FOLDERS["actordata"]
    ok = [True]
    by_folder = {a.folder.casefold(): a for a in actors}
    found = {}
    entries = _list_dir(root, result, ok)
    for item in entries:
        if item.is_dir(follow_symlinks=False):
            actor = by_folder.get(item.name.casefold())
            if actor is None:
                n = sum(1 for _ in _walk_files(item.path, result, [True]))
                result.add(WARNING, "unconfigured_actor_folder", item.path,
                           f"no configured actor uses folder {item.name!r}; {n} file(s) not imported")
                result.folders.append({"folder": f"actordata/{item.name}", "path": item.path,
                                       "status": "unconfigured_actor", "files": n})
                continue
            if actor.actor_id in found:
                result.add(ERROR, "ambiguous_folder", item.path,
                           f"several folders map to actor {actor.actor_id!r}")
                found[actor.actor_id] = None
                continue
            found[actor.actor_id] = item
        else:
            result.add(WARNING, "file_outside_actor_folder", item.path,
                       "ActorData files must be inside an actor folder; not imported")
            result.present_paths.add(normalise_rel_path(item.path, result.data_dir))
    for actor in actors:
        item = found.get(actor.actor_id)
        label = f"actordata/{actor.folder}"
        if item is None:
            if actor.actor_id not in found:
                result.add(WARNING, "missing_actor_folder", root / actor.folder,
                           f"configured actor {actor.actor_id!r} has no folder; it gets no profile sources")
                result.folders.append({"folder": label, "path": str(root / actor.folder),
                                       "status": "missing", "files": 0, "actor_id": actor.actor_id})
            continue
        unit_ok = [ok[0]]
        n = _scan_unit(Path(item.path), "actordata", spec, actor.actor_id, options, result, unit_ok)
        status = "ok" if unit_ok[0] else "incomplete"
        if n == 0 and unit_ok[0]:
            status = "empty"
            result.add(WARNING, "empty_folder", item.path, "actor folder exists but contains no files")
        result.folders.append({"folder": label, "path": item.path, "status": status,
                               "files": n, "actor_id": actor.actor_id})
        if unit_ok[0]:
            result.complete_actor_ids.add(actor.actor_id)


def _scan_unit(root, folder_key, spec, actor_id, options, result, unit_ok):
    doc_type = spec["doc_type"]
    scope = model.SCOPE_FOR_TYPE[doc_type]
    exclusions = [e for e in options.exclusions if e.folder == folder_key]
    n = 0
    for path in _walk_files(root, result, unit_ok):
        n += 1
        rel = normalise_rel_path(path, result.data_dir)
        result.present_paths.add(rel)
        excl = next((e for e in exclusions if fnmatch.fnmatchcase(path.name, e.pattern)), None)
        if excl is not None:
            result.excluded.append({"path": rel, "reason": excl.reason, "pattern": excl.pattern})
            continue
        if path.suffix.lower() not in spec["extensions"]:
            result.add(WARNING, "unsupported_extension", rel,
                       f"expected {', '.join(spec['extensions'])}; not imported")
            continue
        entry = _validate_file(path, rel, doc_type, scope, actor_id, result)
        if entry is not None:
            result.entries.append(entry)
    return n


def _validate_file(path, rel, doc_type, scope, actor_id, result):
    try:
        data, _text, parsed = _read_and_parse(path, doc_type)
    except _EmptyFile:
        result.add(WARNING, "empty_file", rel, "file is empty; not imported")
        return None
    except UnicodeDecodeError as exc:
        result.add(ERROR, "utf8_decode_error", rel, f"not valid UTF-8: {exc}")
        return None
    except OSError as exc:
        result.add(ERROR, "unreadable_file", rel, str(exc))
        return None
    except FrontMatterError as exc:
        result.add(ERROR, "invalid_metadata", rel, f"front matter rejected: {exc}")
        return None
    except LegalXmlError as exc:
        result.add(ERROR, "invalid_xml", rel, str(exc))
        return None
    for w in parsed.warnings:
        result.add(WARNING, "content_warning", rel, w)
    meta = parsed.metadata
    akteure = meta.get("akteure")
    if akteure is not None and doc_type != model.ARTICLE:
        result.add(ERROR, "invalid_metadata", rel, "akteure is only allowed on Articles")
        return None
    unknown = [v for v in akteure or [] if v not in result.akteure_known]
    if unknown:
        result.add(ERROR, "unknown_akteur", rel,
                   f"akteure values not in the configuration: {unknown}; file not imported")
        return None
    if doc_type == model.ARTICLE and akteure is None:
        result.add(INFO, "missing_akteure", rel, "no akteure line; no actor links")
    matched = tuple(sorted({result.akteure_map[v] for v in akteure or []
                            if v in result.akteure_map}))
    has_fm = getattr(parsed, "has_front_matter", False)
    if path.suffix.lower() == ".md" and not has_fm:
        result.add(INFO, "missing_optional_metadata", rel, "no front matter; optional metadata absent")
    if parsed.title_origin == "filename":
        result.add(INFO, "title_from_filename", rel, "no metadata title or heading; filename used")
    pid = meta.get("id")
    doc_id = persistent_doc_id(scope, pid) if pid else default_doc_id(scope, rel)
    return Entry(
        abs_path=path, source_path=rel, doc_type=doc_type, scope=scope, actor_id=actor_id,
        doc_id=doc_id, content_hash=hashlib.sha256(data).hexdigest(), title=parsed.title,
        title_origin=parsed.title_origin, persistent_id=pid,
        ris_dokid=getattr(parsed, "ris_dokid", None) or None, has_front_matter=has_fm,
        matched_actor_ids=matched,
    )


def _check_identifiers(result):
    by_id = defaultdict(list)
    for e in result.entries:
        by_id[e.doc_id].append(e)
    rejected = set()
    for doc_id, group in by_id.items():
        if len(group) > 1:
            paths = ", ".join(e.source_path for e in group)
            for e in group:
                result.add(ERROR, "duplicate_document_id", e.source_path,
                           f"document id {doc_id!r} is claimed by several files: {paths}")
            rejected.add(doc_id)
    if rejected:
        result.entries = [e for e in result.entries if e.doc_id not in rejected]

    by_dokid = defaultdict(list)
    for e in result.entries:
        if e.ris_dokid:
            by_dokid[e.ris_dokid].append(e.source_path)
    for dokid, paths in by_dokid.items():
        if len(paths) > 1:
            result.add(WARNING, "duplicate_ris_dokid", paths[0],
                       f"RIS dokid {dokid!r} appears in {len(paths)} files: {', '.join(paths)}")

    by_hash = defaultdict(list)
    for e in result.entries:
        by_hash[(e.scope, e.content_hash)].append(e.source_path)
    for (scope, _h), paths in by_hash.items():
        if len(paths) > 1:
            result.add(INFO, "identical_content", paths[0],
                       f"identical content stored as separate documents in scope {scope}: "
                       + ", ".join(paths))


def load_document(entry, now=None):
    """Pass 2: re-read a file and return the full row for writing.

    Raises ValueError if the file changed since it was scanned.
    """
    data, text, parsed = _read_and_parse(entry.abs_path, entry.doc_type)
    content_hash = hashlib.sha256(data).hexdigest()
    if content_hash != entry.content_hash:
        raise ValueError("file changed between validation and import")
    meta = dict(parsed.metadata)
    if entry.persistent_id:
        meta["persistent_id"] = entry.persistent_id
    if entry.ris_dokid:
        meta["ris_dokid"] = entry.ris_dokid
    props = {k: meta.get(k) for k in OPTIONAL_PROPERTIES}
    props.update({
        "scope": entry.scope,
        "doc_type": entry.doc_type,
        "title": parsed.title,
        "title_origin": parsed.title_origin,
        "original_content": text,
        "content_format": "ris_xml" if entry.abs_path.suffix.lower() == ".xml" else "markdown",
        "text": parsed.text,
        "source_path": entry.source_path,
        "content_hash": content_hash,
        "actor_id": entry.actor_id,
        "importer": model.IMPORTER,
        "parser_version": model.PARSER_VERSION,
    })
    return {
        "doc_id": entry.doc_id,
        "content_hash": content_hash,
        "source_path": entry.source_path,
        "moderator_id": model.MODERATOR_FOR_TYPE[entry.doc_type],
        "actor_id": entry.actor_id,
        "matched_actor_ids": list(entry.matched_actor_ids),
        "props": props,
        "size": len(data),
    }
