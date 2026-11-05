# HealthPolicy Simulator

A local application that simulates a structured, source-grounded negotiation about Austrian
healthcare policy. It runs on LangGraph, uses an Azure deployment of GPT-5.4 Mini for all roles,
and reads the existing Neo4j source graph without changing it. All generated content is German.

The actors are simulated institutional perspectives, not official statements. Results are a
simulation, and no output is legal advice.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env        # fill in Neo4j and Azure values
docker compose up -d neo4j         # the source graph; see README.md for the import
.venv\Scripts\python -m healthsim check-connection
```

`AZURE_OPENAI_DEPLOYMENT_NAME` is the name in the **Deployments** list of the Azure portal, which
is not necessarily the model name. `LLM_API_MODE` must be `responses_v1`.

Tested versions are pinned in `requirements.txt` (Python 3.13.2).

## Commands

```powershell
.venv\Scripts\python -m healthsim check-connection
.venv\Scripts\python -m healthsim run --input examples\request.json --output result.json
Get-Content examples\request.json | .venv\Scripts\python -m healthsim run     # stdin to stdout
.venv\Scripts\python -m healthsim resume <debate_id> --output result.json
.venv\Scripts\python -m healthsim export-schemas
.venv\Scripts\python -m healthsim export-diagram
.venv\Scripts\python -m pytest tests_sim -q
```

Offline run with fake answers and artificial sources. This must be selected explicitly and proves
nothing about Azure or Neo4j:

```powershell
.venv\Scripts\python -m healthsim run --input examples\request.json --llm fake --sources fixture --fixture examples\fixture_sources.json
```

| Exit code | Status |
|---|---|
| 0 | `completed` |
| 1 | `incomplete` |
| 2 | `needs_clarification` |
| 3 | `invalid_input` |
| 4 | `failed` (connection check: a service failed) |

Stdout carries one JSON object and nothing else. Logs go to stderr. The callable entry points are
`healthsim.app.run_text`, `resume_debate` and `check_connections`.

## Debate chat

A local page for JSON input and for following a debate round by round, built like a group chat:

```powershell
.venv\Scripts\python -m healthsim serve            # http://127.0.0.1:8765
.venv\Scripts\python -m healthsim serve --llm fake --sources fixture --fixture examples\fixture_sources.json
```

- **Input:** paste or drop a request JSON and start a debate, or open a result file or a saved debate.
  Starting asks for confirmation, because a debate uses Azure quota and takes up to 13 minutes.
- **You are the moderator:** the meta-moderator's proposals and reports appear as your own outgoing
  messages. The institutions and reviewers are the other participants.
- **Group chat "Debatte":** only what was published in the debate. Each round shows negotiation, your
  proposal, the clause reviews and the decisions. Preparation stays private until publication, as in
  the real workflow.
- **Individual chats:** one per institution (profile, priorities, confidential feedback, assessments)
  and one per reviewer (all of its reports).
- **Above the chat:** the original question, the current step, the reference date, the proposal
  version and the met priorities per institution.
- **`Weiter`** appends the next round to the chat and jumps to it. `Zurück` goes back a round. The
  number beside a chat counts its new messages in the current step.
- **JSON:** `JSON` shows the data behind the current chat and round, and `Download` saves the full
  result.

| Key | Action |
|---|---|
| Right / Left arrow | Next / previous round |
| Home / End | Preparation / final step |
| Space | Play / pause (12 s per round) |
| J | Chat / JSON |
| Esc | Back to input |

The page also works without the server: open `frontend/index.html` and load a result file. A link
like `http://127.0.0.1:8765/?debatte=<debate_id>#4` opens a saved debate at the fourth step.

Every message is built from stored records; no chat text is invented. Moderator messages exist only
for what the meta-moderator actually wrote, so individual chats have no outgoing messages.

The server binds to 127.0.0.1, serves only the fixed frontend files, rejects foreign `Host` headers
and accepts only `application/json` on POST. It runs one debate at a time as a CLI child process.
Generated text is only ever inserted as text. The visual language follows `DESIGN-WHATSAPP.md`,
without any brand name or logo. Green marks only the primary action, and status uses shape instead
of colour, because the palette has no status colours.

## Request

See `examples/request.json` and `schemas/request.schema.json`. Required fields are `actors` and
`question`. Unknown fields are rejected, including `language` and `allow_law_changes`.

The simulator asks for clarification, before any timer starts, when:

- `reference_date` is missing. It decides the applicable law and permitted sources, so it is
  always treated as material and never replaced by today's date.
- The question asks for a change of law.
- A percentage constraint has no `baseline` and `period`.

Actor names need an exact match with a graph `Actor`, or an entry in `actor_aliases` in
`config/simulator.json`. No model guesses an institution.

## Configuration

Precedence: process environment, then `.env`, then `config/simulator.json`.

| Setting | Meaning |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Origin or v1 base URL, authoritative when set; `/openai/v1/` is added exactly once |
| `AZURE_OPENAI_ACCOUNT_NAME` | Resource name, used for `https://<name>.openai.azure.com` when no endpoint is set |
| `LLM_REASONING_EFFORT` | `none`, `low`, `medium`, `high` or `xhigh`; per role via `role_reasoning` |
| `LLM_RATE_LIMIT_RPM`, `LLM_RATE_LIMIT_TPM` | Optional extra limits; empty means unknown and is reported as a warning |
| `graph_mapping` | Property names of the source graph, validated as plain identifiers |
| `retrieval`, `limits` | Search sizes, maximum clauses, correction limit |

The operation timeouts are 45 s (ordinary) and 60 s (final report). This is a project decision and
replaces the 60 s and 90 s of the original specification. Other values are honoured and reported as
a warning in every result.

## Workflow

`docs/workflow.mmd` is the Mermaid diagram exported from the executable graph.

| Spec step | Graph nodes |
|---|---|
| 1-2 validate, preserve | `validate_request` |
| 3 release question, start timer | `release_question` |
| 4-5 profile, stance, priorities | `prepare_actor`, `prepare_barrier` |
| 6-7 first review, challenges | `initial_review`, `initial_challenge`, `initial_reply` and their barriers |
| 8 revision | `revise_actor`, `revision_barrier` |
| 9 second review | `second_review`, `second_challenge`, `second_reply` and their barriers |
| 10 publication, baseline freeze | `publish_package` |
| 11 negotiation | `negotiate_actor`, `negotiation_barrier` |
| 12 candidate | `assemble_candidate` |
| 13 clause review, amendment | `candidate_review`, `candidate_challenge`, `candidate_reply`, `amend_candidate` |
| 14 assessment, decision | `assess_actor`, `assess_barrier` |
| 15 assessment review | `assessment_review`, `assessment_challenge`, `assessment_reply` |
| 15-16 round result, stop or loop | `close_round` |
| 17 freeze | `freeze` |
| 18 final standpoints | `final_actor`, `final_actor_barrier` |
| 19 final checks | `final_review`, `final_review_barrier` |
| 20 reports | `meta_report`, `meta_evolution`, `report_barrier` |
| 21 validated JSON | `build_output` |

Every barrier routes to `freeze` when the deliberation deadline has passed, and to `build_output`
on a hard failure or when the finalisation deadline has passed.

## Rules enforced by the program

- **Satisfaction:** `5 * met_count >= 4 * priority_count`. Other statuses count zero, and an empty
  priority list gives null.
- **Stopping:** two consecutive successful rounds, or the deliberation deadline. A round is
  successful when it is complete and all actors reach the threshold.
- **Complete candidate versus successful round:** a candidate that every actor has fully assessed
  can be frozen. Incomplete review coverage prevents a successful round, and it is shown on the
  frozen candidate as `review_complete: false` plus a warning. It never counts as approval.
- **Words:** whitespace-separated units of everything the model wrote, including citations.
  Program labels, enum values, ids and JSON punctuation are not counted.
- **Words for proposals:** the spec sets no allowance for a whole candidate. This application
  allows up to 8 clauses of at most 100 words each.
- **Repairs:** one mechanical repair per output. The rejected version and reason are kept in the
  event record. If the repair fails, the contribution is marked incomplete.
- **Existing-law scope:** requests for legal change are stopped at intake. Proposals are checked
  with a sentence-level pattern rule, and LegalEye's `requires_legal_amendment` makes a clause
  inadmissible. The rule ignores sentences that rule a change out, so a badly worded proposal can
  slip past it. It is a safeguard only; legality remains LegalEye's reasoned judgement.
- **Review coverage:** `coverage` says whether the targets were examined, not whether the evidence
  was sufficient. `incomplete` requires naming the unexamined targets in `unreviewed_targets`.
  Missing evidence is a finding (`insufficient_information`, `unresolved_issue`), so a round can
  succeed with evidence gaps still open. Those gaps stay visible as findings.
- **Citations:** an id must belong to a passage retrieved for that role in this debate. The
  program checks resolution and version; TruthSeeker judges whether the passage supports the claim.
- **Arithmetic:** a claim may carry a structured calculation, which the program recomputes. No
  model-generated code is run.

## How proposals evolve

The actors negotiate independently, so the meta-moderator merges their positions into one candidate
each round (spec step 12). From the first assessment on, the moderator and the actors see a gap list
built by program code from stored records: each fixed priority with its acceptance condition, latest
status and reason, open ones first. A clause that needs no change is marked `unchanged_from`, and the
program copies its text word for word, so a met condition is not lost in a rewrite. Kept clauses are
reviewed again, because a combination of clauses can raise new issues.

## Concurrency, rate limits and deadlines

- One process per `STATE_DIR`, enforced by a lock file. Limits are shared by all roles and debates
  in that process and are not coordinated across processes.
- At most nine requests run at once.
- **Per-second policy:** a sliding one-second log of reservations. A reservation is the input
  estimate plus the full output allowance, which covers reasoning. A request is admitted only if
  the reservations of the last second plus its own stay within `LLM_RATE_LIMIT_TPS`, so no burst
  can exceed the limit. After a response the reservation is lowered to the reported usage. This is
  application bookkeeping, not Azure's accounting.
- A request larger than the limit is an error, never an endless wait.
- One retry layer: SDK retries are off, and transient failures get one retry that honours
  `retry-after`, within the operation budget.
- The operation budget is `min(configured timeout, remaining phase time)`, enforced by cancelling
  the call. Nothing is dispatched once a phase has no time left.
- Deadlines are stored as absolute times. A resume never restarts them, and a late response is
  discarded and recorded.

## Sources

| Role | Can search |
|---|---|
| Actor | Own `HAS_PROFILE_SOURCE` documents and own `MATCHES_KEYWORD` articles |
| TruthSeeker | All Articles and ActorData, plus cited passages of this debate |
| LegalEye | LegalText, in force at the reference date |
| EthicMind | EthicalText |
| Meta-moderator | No search; receives the record and references |

- Role identity comes from the controller. All queries run in read transactions with parameters
  and allowlisted identifiers. The model never writes Cypher.
- **Search coverage:** the graph has no fulltext index, so search is lexical: it counts literal
  matches of terms derived from the question and the prose under review, per document. JSON field
  names and ids are not search terms. It misses synonyms and inflected
  forms, and long documents tend to rank high. A missing hit never proves absence. An existing
  fulltext index on `Document.text` is detected and used.
- Undated sources are flagged, because their relation to the reference date is unknown.
- Referenced legal provisions are followed deterministically through SOZDOK `rvolink` elements.
  Unresolved references are reported.
- Passages are saved in `state/events.sqlite`, so cited material can be reopened in the version
  used. Changed sources are detected through the content hash.

## State

| File | Content |
|---|---|
| `state/checkpoints.sqlite` | LangGraph checkpoints, internal |
| `state/events.sqlite` | Append-only events, source snapshots, requests and results |

Clients and credentials never enter graph state. No hosted LangGraph service or LangSmith account
is used. The state folder contains source excerpts and is ignored by git.

## Tests

`tests_sim/` runs offline with fake answers, fixture sources and an injected clock. It exercises the
executable LangGraph workflow and the helpers. See "Verification status" for what fake runs cannot
show.

## Limitations

- Azure reports only the deployment name, so the model family cannot be confirmed without
  management access. Results state this as `model_identity_verified: false`.
- Azure's effective deployment limits could not be read; the published limits are enforced.
- Token estimates use a conservative character ratio, because no tokenizer dependency is included.
- The German language of generated text and the absence of personal names rely on instructions
  and are not checked by the program.
- Responses are not streamed; deadlines cancel the whole request.
- Exactly-once external execution is not claimed: an interrupted request may be repeated, while its
  stored records are not duplicated.
- Graph separation is not access control. These rules apply to this application, not to someone
  with direct database access.
