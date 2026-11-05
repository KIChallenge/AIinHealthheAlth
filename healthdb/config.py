"""Configuration: connection settings, actor definitions, import options.

Connection settings come from environment variables. A local `.env` file
(ignored by git) is read as a fallback; real environment variables win.
"""

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACTORS_FILE = PROJECT_ROOT / "config" / "actors.json"
DEFAULT_IMPORT_FILE = PROJECT_ROOT / "config" / "import.json"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

ACTOR_ID_RE = re.compile(r"^[a-z0-9_]+$")


class ConfigError(Exception):
    """Raised for invalid or missing configuration."""


@dataclass(frozen=True)
class Actor:
    actor_id: str
    name: str
    folder: str
    # `akteure` tag values in article front matter that link to this actor.
    akteure_labels: tuple = ()


@dataclass(frozen=True)
class Exclusion:
    folder: str      # top-level source folder key, e.g. "articles"
    pattern: str     # fnmatch pattern applied to the file name
    reason: str


@dataclass
class ImportOptions:
    exclusions: list = field(default_factory=list)
    batch_size: int = 500
    # `akteure` values kept on the article but not linked to any actor.
    akteure_list_only: list = field(default_factory=list)


@dataclass(frozen=True)
class ConnectionSettings:
    uri: str
    user: str
    password: str
    database: str


def read_env_file(path):
    """Parse a simple KEY=VALUE file. Blank lines and # comments are ignored."""
    values = {}
    path = Path(path)
    if not path.is_file():
        return values
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path}:{lineno}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def get_setting(name, default=None, env_file=PROJECT_ROOT / ".env"):
    if name in os.environ:
        return os.environ[name]
    return read_env_file(env_file).get(name, default)


def connection_settings(prefix="NEO4J"):
    """Read connection settings; raise ConfigError if any are missing."""
    names = {"uri": f"{prefix}_URI", "user": f"{prefix}_USER",
             "password": f"{prefix}_PASSWORD", "database": f"{prefix}_DATABASE"}
    values = {k: get_setting(v) for k, v in names.items()}
    if not values["database"]:
        values["database"] = "neo4j"
    missing = [names[k] for k, v in values.items() if not v]
    if missing:
        raise ConfigError("missing connection settings: " + ", ".join(missing))
    return ConnectionSettings(**values)


def data_dir():
    return Path(get_setting("HEALTHDB_DATA_DIR") or DEFAULT_DATA_DIR)


def load_actors(path=None):
    """Load and validate the actor configuration.

    Each entry has id, name, folder and optionally akteure_labels.
    """
    path = Path(path or get_setting("HEALTHDB_ACTORS_FILE") or DEFAULT_ACTORS_FILE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read actor configuration {path}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: expected a non-empty JSON list of actors")
    actors, ids, folders, labels_seen = [], set(), set(), set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or not {"id", "name", "folder"} <= set(entry) \
                or not set(entry) <= {"id", "name", "folder", "akteure_labels"}:
            raise ConfigError(f"{path}[{i}]: each actor needs id, name, folder "
                              "(and optionally akteure_labels)")
        actor_id, name, folder = entry["id"], entry["name"], entry["folder"]
        labels = entry.get("akteure_labels", [])
        if not isinstance(labels, list) \
                or not all(isinstance(v, str) and v.strip() for v in labels):
            raise ConfigError(f"{path}[{i}]: akteure_labels must be a list of non-empty strings")
        labels = tuple(unicodedata.normalize("NFC", v.strip()) for v in labels)
        for label in labels:
            if label in labels_seen:
                raise ConfigError(f"{path}[{i}]: akteure label {label!r} is mapped twice")
            labels_seen.add(label)
        if not all(isinstance(v, str) and v.strip() for v in (actor_id, name, folder)):
            raise ConfigError(f"{path}[{i}]: id, name and folder must be non-empty strings")
        if not ACTOR_ID_RE.match(actor_id):
            raise ConfigError(f"{path}[{i}]: actor id {actor_id!r} must match {ACTOR_ID_RE.pattern}")
        if actor_id in ids:
            raise ConfigError(f"{path}[{i}]: duplicate actor id {actor_id!r}")
        if folder.casefold() in folders:
            raise ConfigError(f"{path}[{i}]: duplicate actor folder {folder!r}")
        ids.add(actor_id)
        folders.add(folder.casefold())
        actors.append(Actor(actor_id, name, folder, labels))
    return actors


def akteure_mapping(actors):
    """Return {akteure tag value: actor_id}."""
    return {label: a.actor_id for a in actors for label in a.akteure_labels}


def load_import_options(path=None):
    path = Path(path or DEFAULT_IMPORT_FILE)
    if not path.is_file():
        return ImportOptions()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    unknown = set(raw) - {"exclusions", "batch_size", "akteure_list_only"}
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    exclusions = []
    for i, e in enumerate(raw.get("exclusions", [])):
        if not isinstance(e, dict) or set(e) != {"folder", "pattern", "reason"}:
            raise ConfigError(f"{path}: exclusions[{i}] needs exactly folder, pattern, reason")
        exclusions.append(Exclusion(e["folder"].casefold(), e["pattern"], e["reason"]))
    batch_size = raw.get("batch_size", 500)
    if not isinstance(batch_size, int) or batch_size < 1:
        raise ConfigError(f"{path}: batch_size must be a positive integer")
    list_only = raw.get("akteure_list_only", [])
    if not isinstance(list_only, list) \
            or not all(isinstance(v, str) and v.strip() for v in list_only):
        raise ConfigError(f"{path}: akteure_list_only must be a list of non-empty strings")
    list_only = [unicodedata.normalize("NFC", v.strip()) for v in list_only]
    return ImportOptions(exclusions=exclusions, batch_size=batch_size, akteure_list_only=list_only)
