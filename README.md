# AIinHealthheAlth

Neo4j database foundation for the UC2 **HealthPolicy Simulator** (Austrian healthcare
negotiation prototype). It stores the stakeholder actors, the three specialist reviewers,
their source documents, and the access relationships between them. The agents themselves
run outside Neo4j.

> **Actor–article links come from the `akteure` tags in the article front matter.** The tags are
> produced by an external system; this repo does no matching of its own. Status reported as
> `actor_article_matching: active_from_akteure_tags`.

## Contents

| Path | Purpose |
|---|---|
| `healthdb/` | importer, schema, retrieval functions, CLI (`python -m healthdb`) |
| `config/actors.json` | actor configuration (add stakeholders here), including which `akteure` values link to each actor |
| `config/import.json` | import options: exclusion patterns, batch size, `akteure` values stored without links |
| `queries/check_relationships.cypher` | example queries for checking relationships |
| `.env.example` | connection configuration template (copy to `.env`, which is git-ignored) |
| `docker-compose.yml` | local Neo4j 5.26 Community (+ throwaway test instance) |
| `tests/` | unit tests (no DB) and integration tests (test DB); fixtures in `tests/fixtures/` |
| `reports/` | generated machine-readable import reports (git-ignored) |
| `COMPLETION_REPORT.md` | result of the first real import |

Dependencies: Python ≥ 3.10, the official `neo4j` Python driver (6.x), `pytest`. Everything
else is the standard library.

## Setup (exact commands)

PowerShell (Windows):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env          # then set NEO4J_PASSWORD and NEO4J_TEST_PASSWORD (min. 8 chars)
docker compose up -d neo4j                       # main DB: bolt://localhost:7687, browser http://localhost:7474
docker compose --profile test up -d neo4j-test   # test DB: bolt://localhost:7688 (data in tmpfs)
```

bash:

```bash
python -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
cp .env.example .env    # edit passwords
docker compose up -d neo4j && docker compose --profile test up -d neo4j-test
```

(On Windows use `.venv\Scripts\python`, on Linux/macOS `.venv/bin/python` in the commands below.)

## Running

```powershell
.venv\Scripts\python -m healthdb init-schema                      # constraints + indexes (import also does this)
.venv\Scripts\python -m healthdb import --dry-run --report reports\dry_run.json
.venv\Scripts\python -m healthdb import --report reports\import_report.json
.venv\Scripts\python -m healthdb diagnostics                      # counts, checks, missing sources, last report
.venv\Scripts\python -m healthdb sources actor aerztekammer         # profile documents
.venv\Scripts\python -m healthdb sources articles aerztekammer      # linked articles
.venv\Scripts\python -m healthdb sources truthseeker --type ActorData --institution sozialversicherung
.venv\Scripts\python -m healthdb sources legaleye --limit 5
.venv\Scripts\python -m healthdb sources ethicmind
.venv\Scripts\python -m healthdb doc <doc_id> [--content]
.venv\Scripts\python -m healthdb import --prune-missing           # explicit deletion of vanished sources
```

Global options: `--data-dir`, `--actors`, `--import-config`. Exit codes: `0` success,
`1` completed with errors, `2` failed. A partial import is never reported as `success`.

Configuration (environment variables or `.env`; real environment variables win):
`NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`, `HEALTHDB_DATA_DIR`
(default `./data`), `HEALTHDB_ACTORS_FILE` (default `config/actors.json`), and
`NEO4J_TEST_*` for the integration tests.

## Tests

```powershell
.venv\Scripts\python -m pytest -q -rs
```

Unit tests always run. Integration tests run against the **test** instance
(`NEO4J_TEST_URI`, port 7688) and are reported as *skipped* if it is not configured or not
reachable. They wipe that database before each test and refuse to run if `NEO4J_TEST_URI`
equals `NEO4J_URI`. Test documents live only in `tests/fixtures/`.

## Source folders

Relative to the data directory. Folder names are matched case-insensitively
(`Articles` = `articles`). Files are read recursively.

| Folder | Document label | Scope | Used by | Extensions |
|---|---|---|---|---|
| `Articles/` | `Article` | `actor_truth` | TruthSeeker | `.md` |
| `ActorData/<actor folder>/` | `ActorData` | `actor_truth` | that actor + TruthSeeker | `.md` |
| `LegalEye/` | `LegalText` | `legal` | LegalEye | `.xml` (RIS), `.md` |
| `EthicMind/` | `EthicalText` | `ethics` | EthicMind | `.md` |

There is no TruthSeeker folder; TruthSeeker uses the Article and ActorData nodes.
Files matching an exclusion in `config/import.json` are skipped and listed in the report
(currently `Articles/*_Pressespiegel_gesamt.md`, the daily press-review compilations that
repeat the individual articles). Other extensions, files outside source folders, ActorData
folders without a configured actor, missing folders and empty folders are all reported,
never silently ignored.

## Graph model

```
(:Actor {actor_id, name, folder, scope:'actor_truth'})
    -[:HAS_PROFILE_SOURCE]-> (:Document:ActorData)
(:Moderator {moderator_id:'truthseeker', scope:'actor_truth'})
    -[:USES_SOURCE]-> (:Document:Article) and (:Document:ActorData)
(:Moderator {moderator_id:'legaleye',  scope:'legal'})  -[:USES_SOURCE]-> (:Document:LegalText)
(:Moderator {moderator_id:'ethicmind', scope:'ethics'}) -[:USES_SOURCE]-> (:Document:EthicalText)
(:Actor) -[:MATCHES_KEYWORD {source:'akteure_front_matter'}]-> (:Document:Article)
```

* Each Document carries exactly one type label. One file = one Document node; an ActorData
  node has exactly two incoming edges (its actor and TruthSeeker), with no copies.
* Relationships are stored once, in one direction, and never cross scopes.
* `MATCHES_KEYWORD` is the only direct Actor–Article relationship (see "Actor–article links").
  An indirect path Actor → ActorData ← TruthSeeker → Article is **not** a match or a stance.
* Identical content in several scopes is stored as separate scoped nodes (same `content_hash`).
* Constraints: unique `Actor.actor_id`, `Moderator.moderator_id`, `Document.doc_id`;
  indexes on `Document.source_path` and `(Document.scope, Document.doc_type)`.
* Every importer-created node and relationship carries `importer: "svs_healthdb"`; the
  importer modifies nothing else and never clears the database.

### Document properties

Required: `doc_id`, `scope`, `doc_type`, `title`, `title_origin` (`metadata` | `heading` |
`filename`), `original_content` (full original Markdown/XML), `text` (extracted readable text),
`content_format` (`markdown` | `ris_xml`), `source_path` (NFC, `/`-separated, relative to the
data dir), `content_hash` (SHA-256 of the file bytes), `first_imported_at`, `updated_at`,
`last_seen_at`, `source_present`, `importer`; `actor_id` on ActorData.

Optional, only when present in the source (never invented): `publisher`, `publication_date`
(+ `publication_date_raw`), `source_version`, `valid_from`, `valid_until`, `source_url`,
`author`, `media_type`, `country`, `language`, `press_review`, `source_pdf`, `source_pages`,
`omitted_facsimile_pages`, `akteure` (list, Articles only), and for RIS XML `law_title`, `law_abbreviation`, `section_type`,
`section_number`, `amendment_title`, `ris_dokid`.

Dates are ISO strings (`YYYY-MM-DD`), timestamps ISO-8601 UTC strings. Publication dates,
validity dates and import timestamps are separate fields. A recent import says nothing about
whether a legal text is currently valid.

### Identifiers

* Default `doc_id` = SHA-256 of `"<scope>\n<source_path>"`. Editing a file keeps its id (the
  hash and `updated_at` change); renaming or moving a file creates a new id.
* Optional persistent id: front matter `id: <value>` (`[A-Za-z0-9._:-]+`), stored as
  `"<scope>:<value>"`. It survives renames within the same scope. Two files claiming the same id
  are both rejected. An id whose stored record points to another file that still exists is
  rejected as conflicting.
* Actors and moderators use the stable ids from `config/actors.json` and `healthdb/model.py`,
  not display names.

## Front matter (optional metadata)

A block at the very first line of a Markdown file:

```
---
titel: "Ärztemangel: \"Wir brauchen Hausärzte\""
quelle: "Kurier"
datum: "04.04.2025"
faksimile_seiten_ausgelassen: 0
---
```

* Line format `key: value`; values are `"double-quoted"` (escapes `\" \\ \n \t \/`),
  `'single-quoted'` (`''` = quote), bare integers or bare text. Lists, maps, multi-line
  values, anchors etc. are rejected.
* Canonical keys: `id title publisher publication_date source_version valid_from valid_until
  source_url author media_type country language press_review source_pdf source_pages
  omitted_facsimile_pages akteure`.
* German aliases (press-review export): `titel→title`, `quelle→publisher`,
  `datum→publication_date`, `autor→author`, `typ→media_type`, `land→country`,
  `sprache→language`, `pressespiegel→press_review`, `pdf→source_pdf`,
  `pdf_seiten→source_pages`, `faksimile_seiten_ausgelassen→omitted_facsimile_pages`.
* Dates: `YYYY-MM-DD` or `DD.MM.YYYY`.
* `akteure` is a comma-separated list and may be empty (`akteure:`). It is allowed on Articles
  only. Empty entries, duplicates and values missing from the configuration are errors.
* Unknown key, duplicate key (including alias + canonical), bad date, empty value, unterminated
  quote, missing closing `---` → the file is rejected with an `invalid_metadata` error.
  A file without front matter is fine (`missing_optional_metadata`, info).

**Display title:** metadata title, else the first Markdown heading, else the filename
(`title_origin` records which). A first heading that only repeats the filename (as the PDF
converter writes it) counts as a filename, not a title, and is removed from `text`. That way
future keyword matching cannot match on filenames.

**Readable text:** front matter, HTML comments, URL destinations, reference-link definitions,
autolinks, HTML tags and Markdown markers are removed. Visible text, link labels, image alt
text, table cells (tab-separated) and code-block contents are kept.

**RIS XML (LegalEye):** title = `abkrvo` + section label from `gldart`/`gldnr` (`§ n`, `Art. n`,
`Z n`, `Anl. n`, `Anh. n`), for other section types the first `§` symbol in the text; `urheber`→publisher,
`pubdat`→publication_date, `ikra`→valid_from, `akra`→valid_until (`8888-08-08` = open end, not
stored), `fassung`→source_version. Text is the content of `<nutzdaten>`; link URLs and
embedded images are excluded. DOCTYPE/ENTITY declarations are rejected.

## Import behaviour

* Idempotent: `MERGE` on stable ids; content is rewritten only when the hash, path, type, actor
  or `model.PARSER_VERSION` changed. Bump that constant whenever parsing changes stored fields. The report splits documents into `create`, `update`, `unchanged`, `reappear`.
* Two passes: all files are validated first (so id conflicts block before any write), then
  written in batches (≤ 500 docs, ≤ 16 MB) in separate transactions. If a batch fails the
  status is `failed` and `committed` lists what was already written.
* Vanished files: records are marked `source_present=false` + `missing_since`, not deleted.
  This only happens for folders whose scan completed; a missing or unreadable folder never
  marks records missing. `--prune-missing` deletes such importer-owned documents (unless
  foreign relationships touch them).
* `--dry-run` validates and computes the plan (including missing/prune candidates) with
  read-only queries; nothing is written.
* Existing Actor/Moderator/Document nodes with the same id but not owned by the importer
  stop the import before any write.

The JSON report (`reports/import_report.json`) contains the status, folders, inventory,
excluded files, planned and committed changes, missing sources, node/relationship counts,
the validation checks, and every issue with `level`, `code`, `path`, `reason`.

## Retrieval (`healthdb/retrieval.py`)

| Function | Rule |
|---|---|
| `actor_profile_sources(driver, db, actor_id)` | **A** only `(:Actor {actor_id})-[:HAS_PROFILE_SOURCE]->(:ActorData)`; never expanded via TruthSeeker |
| `truthseeker_sources(driver, db, doc_types=None, institutions=None)` | **B** Article + ActorData via TruthSeeker's `USES_SOURCE`; filter by type and, for ActorData, by actor |
| `legaleye_sources(driver, db)` | **C** LegalText via LegalEye |
| `ethicmind_sources(driver, db)` | **D** EthicalText via EthicMind |
| `get_document(driver, db, doc_id)` | **E** content, metadata, provenance incl. incoming relationships |
| `import_diagnostics(driver, db, report_path)` | **F** counts, validation checks, missing sources, last report's warnings/errors |
| `actor_articles(driver, db, actor_id)` | Articles linked via `MATCHES_KEYWORD`, newest first, undated last. A missing link means the article was not tagged, not that it is irrelevant to the actor |

List functions return metadata without full content by default (`include_content=True` adds
`text` and `original_content`), support `limit`/`skip`, and always include `source_present`.

Equivalent Cypher (parameters in `$`):

```cypher
// A
MATCH (a:Actor {actor_id: $actor_id})-[:HAS_PROFILE_SOURCE]->(d:Document:ActorData)
WHERE d.scope = 'actor_truth' RETURN d.doc_id, d.title, d.source_present;
// B
MATCH (:Moderator {moderator_id: 'truthseeker'})-[:USES_SOURCE]->(d:Document)
WHERE d.scope = 'actor_truth' AND (d:Article OR d:ActorData) RETURN d.doc_id, d.doc_type, d.title;
// C / D
MATCH (:Moderator {moderator_id: 'legaleye'})-[:USES_SOURCE]->(d:Document:LegalText) RETURN d;
MATCH (:Moderator {moderator_id: 'ethicmind'})-[:USES_SOURCE]->(d:Document:EthicalText) RETURN d;
// E
MATCH (d:Document {doc_id: $doc_id}) OPTIONAL MATCH (x)-[r]->(d) RETURN d, collect([type(r), x]);
```

**Not access control:** these functions apply the access rules for the agents' convenience.
Graph separation does not stop anyone with unrestricted Cypher access from reading every scope.
Real isolation would need separate databases or credentials.

## Actor–article links

Article front matter carries an `akteure` line set by an external system. The configuration
decides what each value does:

| `akteure` value | Effect | Configured in |
|---|---|---|
| `Dachverband SV` | link to Sozialversicherung | `config/actors.json` → `akteure_labels` |
| `Ärztekammer` | link to Ärztekammer | `config/actors.json` → `akteure_labels` |
| `Gesundheitsministerium` | link to Gesundheitsministerium | `config/actors.json` → `akteure_labels` |
| `Krankenhäuser`, `Patientenvertretung` | stored on the article, no link | `config/import.json` → `akteure_list_only` |

The full list stays on the article as `d.akteure`. Links are reconciled on every import, so
changed tags add or remove links, and a configuration change takes effect without touching the
article files. A value that is not configured is an `unknown_akteur` error and that file is
not imported. Each import validates that the links match the tags exactly.

A link means the external system tagged the article. It does not state the actor's position.

## Adding stakeholders

Add `{"id": "...", "name": "...", "folder": "...", "akteure_labels": ["..."]}` to
`config/actors.json`, create `data/ActorData/<folder>/` and import. The new actor's documents
get both the `HAS_PROFILE_SOURCE` edge and TruthSeeker's `USES_SOURCE` edge. To turn a list-only
value into an actor, also remove it from `akteure_list_only`; its article links are created on
the next import.
