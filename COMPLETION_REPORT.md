# Completion report: Neo4j database foundation

Date: 2026-11-05. Server: Neo4j 5.26.32 Community (Docker `healthdb-neo4j`). Driver: `neo4j` 6.4.1 (Python 3.13.2).
Machine-readable report: `reports/import_report.json`.

This report replaces the first version. Since then the articles gained `akteure` tags and 2024 files, and actor–article links were added on request. Actor–article matching is therefore no longer deferred.

## Implemented

* Schema: uniqueness constraints on `Actor.actor_id`, `Moderator.moderator_id`, `Document.doc_id`; range indexes on `Document.source_path` and `(Document.scope, Document.doc_type)`.
* Markdown importer (strict front matter with German press-review keys, title rules, readable-text extraction) and RIS XML importer for LegalEye.
* Configurable actors (`config/actors.json`) and import options (`config/import.json`). Connection template in `.env.example`; local credentials in the git-ignored `.env`.
* Idempotent upserts, missing-source marking, explicit `--prune-missing`, `--dry-run`, ownership protection, and post-import validation checks.
* `(Actor)-[:MATCHES_KEYWORD]->(Article)` links derived from the `akteure` tags. The tags come from an external system; this repo does no matching of its own.
* Retrieval functions A–F plus `actor_articles`, the CLI `python -m healthdb`, and example queries in `queries/check_relationships.cypher`.
* Tests: 103 in total. README with setup and run commands.

## Folders processed

| Folder | Status | Files | Imported | Type / scope |
|---|---|---|---|---|
| `data/articles` | ok | 12,928 | 12,496 | Article / actor_truth |
| `data/actordata/gesundheitsministerium` | ok | 5 | 5 | ActorData / actor_truth |
| `data/actordata/sozialversicherung` | ok | 4 | 4 | ActorData / actor_truth |
| `data/actordata/aerztekammer` | ok | 4 | 4 | ActorData / actor_truth |
| `data/legaleye` | ok | 14,312 (.xml) | 14,312 | LegalText / legal |
| `data/ethicmind` | ok | 16 | 16 | EthicalText / ethics |

432 files `articles/*_Pressespiegel_gesamt.md` were **excluded by configuration**. They are daily press-review compilations that repeat the individual article files. All 432 are listed in the report with the reason `compilation_not_single_article`. The articles include 3,698 files from 2024.

## Counts

| Nodes | | Relationships | |
|---|---|---|---|
| Actor | 3 | HAS_PROFILE_SOURCE | 13 (gesundheitsministerium 5, sozialversicherung 4, aerztekammer 4) |
| Moderator | 3 | USES_SOURCE: TruthSeeker | 12,509 (12,496 Article + 13 ActorData) |
| Document | 26,837 | USES_SOURCE: LegalEye | 14,312 |
| Article | 12,496 | USES_SOURCE: EthicMind | 16 |
| ActorData | 13 | MATCHES_KEYWORD | 10,718 |
| LegalText | 14,312 | cross-scope relationships | 0 |
| EthicalText | 16 | | |

**TruthSeeker coverage:** yes. It is connected to every Article (12,496/12,496) and every ActorData document (13/13, all three institutions). All 17 validation checks returned 0 violations, covering coverage, endpoints, type labels, scopes, duplicate edges, required properties, and links matching the tags.

**Idempotency:** a dry run after the import reports `unchanged: 26837` documents and `unchanged: 10718` links, with nothing to create or delete.

Import time: about 55 s.

## Actor–article links

| `akteure` value | Actor | Linked articles |
|---|---|---|
| Dachverband SV | Sozialversicherung | 5,591 |
| Ärztekammer | Ärztekammer | 2,984 |
| Gesundheitsministerium | Gesundheitsministerium | 2,143 |
| Krankenhäuser | none, stored on the article only | 5,001 tagged |
| Patientenvertretung | none, stored on the article only | 476 tagged |

| Articles | Count |
|---|---|
| Linked to 1 actor | 4,961 |
| Linked to 2 actors | 2,295 |
| Linked to 3 actors | 389 |
| Tagged only with Krankenhäuser and/or Patientenvertretung, no link | 2,080 |
| Empty `akteure:` line, no link | 2,771 |

A link means the external system tagged the article. It does not state the actor's position, and a missing link does not mean the article is irrelevant. The tags were not checked for accuracy.

## Warnings and missing inputs

* 0 errors, 0 warnings. 30 info entries:
  * 29 × `missing_optional_metadata`: the 13 ActorData and 16 EthicMind Markdown files have no front matter, so publisher, dates and version are absent. Nothing was invented.
  * 1 × `identical_content`: `Bundes-Zielsteuerungsvertrag_2024-2028.md` is byte-identical in all three actor folders. It is stored as 3 ActorData nodes, one per institution, each also used by TruthSeeker.
* No missing folders, no empty folders, no unconfigured actor folders, no missing source files.
* 345 articles have no `datum` and therefore no publication date.
* **Weak display titles:** the rule (metadata → first heading → filename) gives a few generic titles, because the first heading of the PDF conversion is a front-matter page. Examples: `Gesundheitsfoerderungsstrategie_2024` → "Impressum", `Gesundheit_UG24_Teilheft_2026` → "Teilheft", `Jahresbericht_oesterreichische_Sozialversicherung_2026` → "INHALTSVERZEICHNIS", `OeAK_Programm_Bundesregierung_2024-2029` → "2024–2029", `Bioethikkommission_Ressourcenknappheit_2024` → "Impressum", `EMRK_Europarat_aktuelle_deutsche_Ausgabe` → "INHALT", `EU_Vertraege_und_Grundrechtecharta_2016_DE` → "E", `Patientencharta_Bund_Salzburg` → "Langtitel". Fix: add a `title:` front-matter line to those files and re-import. Only the title changes; the ids stay the same.
* **Repeated legal titles:** 72 legal documents share 28 titles. These are section types without a usable number, such as contents pages and preambles (e.g. "GO-HV OEGK"). Each is still a separate document with its own id and path.
* Some extracted text reproduces garbled text from the PDF sources (e.g. the cover page of `Bioethikkommission_Medizin_und_Oekonomie_2018.md`). It is stored as found.
* LegalEye holds one file per provision: no law, section type and number occurs twice. `akra=8888-08-08` (open end) is stored as no `valid_until`. Nothing says whether a provision is in force today.
* 1,068 LegalEye filenames differ from the RIS `dokid` inside the file because of filename sanitisation (`/` → `_`, truncation). The original `dokid` is stored as `ris_dokid`, and none are duplicated.

## Correction to the first version

The first version said LegalEye contains "several historical versions of the same provision". That was wrong: the repeated titles came from a bug. Amendment articles took their title from the first `§` in the text, which cites another law, so Art. 1, 2 and 3 of one regulation were all titled "§ 1". Titles now come from the section metadata, and this data is imported.

## Tested

`.venv\Scripts\python -m pytest -q -rs` → **103 passed, 0 failed, 0 skipped** (integration tests ran against `neo4j-test`, port 7688).

Covered by the tests:
* actor and reviewer creation
* folder → type/scope mapping, including case-insensitive folders
* actor-specific profile edges
* TruthSeeker coverage of all Articles and all ActorData
* one shared ActorData node per file
* a newly configured actor reaching TruthSeeker
* actor retrieval limited to that actor's own documents
* LegalEye and EthicMind edges
* `akteure` parsing, including empty, unknown, duplicate and misplaced values
* links created from tags, updated or removed when tags change, and kept without duplicates on re-import
* list-only values stored without links, and later becoming an actor through configuration alone
* validation failing when links differ from tags, or when another actor–article relationship exists
* no cross-scope edges
* identical content kept separate across scopes
* re-import creating no duplicates
* edit → same id with a new hash
* a parser version change rewriting unchanged files
* legal titles taken from section metadata
* UTF-8, umlauts and NFC/NFD filenames
* metadata vs. heading vs. filename titles, including the filename-artifact heading
* invalid metadata and duplicate or conflicting ids producing visible errors
* missing files reported but not deleted, and pruned only on request
* an incomplete scan not marking records missing
* dry run leaving the graph unchanged
* foreign nodes not being modified
* persistent ids surviving a rename

On the real data I also ran the dry run, the import, a follow-up dry run, retrieval for actor articles, and all queries in `queries/check_relationships.cypher`.

## Not verified

* A database failure in the middle of an import: the `failed` status and the list of committed steps are implemented but not covered by a test.
* Unreadable files and directories (permission errors): implemented but not tested, because they are hard to reproduce on Windows/NTFS in tests.
* `--prune-missing` on the real data set: tested only on fixtures, since no real source is missing.
* Persistent `id:` front matter and `.md` files in LegalEye: tested only on fixtures, because the current data uses neither.
* The accuracy of the `akteure` tags, which come from an external system.
* Repeat imports still re-send all content, so each takes about a minute and will grow with the data.
