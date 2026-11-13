// Run by test_frontend.py: node report_check.js <result.json> ...
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { buildReport, buildPlenum, reportTree, reportHtml } = require(path.join(__dirname, "..", "frontend", "chats.js"));

const LIGHTS = ["gruen", "gelb", "rot"];

for (const file of process.argv.slice(2)) {
  const result = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!result.task) continue;
  const report = buildReport(result);
  assert.ok(LIGHTS.includes(report.overall.light), file);
  for (const clause of report.clauses) {
    assert.ok(LIGHTS.includes(clause.light), clause.id);
    assert.ok(clause.reasons.length > 0, "every colour says why");
  }
  assert.strictEqual(report.counts.gruen + report.counts.gelb + report.counts.rot, report.clauses.length);
  assert.strictEqual(report.stakeholders.length, (result.actors || []).length);
  for (const person of report.stakeholders) {
    assert.ok(LIGHTS.includes(person.light), person.id);
    for (const priority of person.priorities) assert.ok(LIGHTS.includes(priority.light), priority.id);
  }
  if (!result.final_proposal) assert.strictEqual(report.overall.light, "rot", "no final version, no consensus");
  // The plenum: every round coloured by the same rules, never a version nobody assessed yet.
  const rounds = [...new Set((result.proposal_history || []).map((p) => p.round))];
  for (const round of [...rounds, null]) {
    const plenum = buildPlenum(result, round);
    assert.ok(LIGHTS.includes(plenum.overall.light), `${file} round ${round}`);
    assert.strictEqual(plenum.stakeholders.length, (result.actors || []).length);
    for (const s of plenum.stakeholders) assert.ok(LIGHTS.includes(s.light), s.id);
    for (const c of plenum.clauses) for (const v of Object.values(c.perActor)) assert.ok(v === null || LIGHTS.includes(v), c.id);
    if (round && plenum.pending) assert.ok(plenum.pending > plenum.version, "only a newer version waits");
  }
  const html = reportHtml(reportTree(report), "");
  assert.ok(html.startsWith("<!doctype html>"), file);
  assert.ok(!/<script/i.test(html), "the report holds no script");
}

// The final version counts, not a newer one the deadline left unreviewed.
const clause = (id) => ({ clause_id: id, text: `Satz zu ${id}.`, legal_status: "no_conflict_identified" });
const assessment = (version, id, status, references) => ({ assessment_id: id, priority_id: "PR-a-1", actor_id: "a", round: version,
  proposal_version: version, status, previous_status: status, reason: "Grund.", references });
const base = {
  debate_id: "0123456789abcdef", status: "completed", stop_reason: "deliberation_deadline",
  task: { question: "Frage?", reference_date: "2026-11-05" },
  final_proposal: { proposal_id: "PROP-1", version: 1, legal_admissibility: "conditional", clauses: [clause("KL-v1-1")] },
  proposal_history: [],
  priority_history: [assessment(1, "AS-final-PR-a-1-v1", "partly_met", ["KL-v1-1"]), assessment(2, "AS-2-PR-a-1-v2", "met", ["KL-v2-1"])],
  actors: [{ actor_id: "a", name: "Akteur", priorities: [{ priority_id: "PR-a-1", objective: "Ziel" }],
    latest_assessments: [assessment(2, "AS-2-PR-a-1-v2", "met", ["KL-v2-1"])],
    acceptance_decisions: [{ decision_id: "D-final-a-v1", target_version: 1, kind: "conditional", conditions: ["Bedingung"] }] }],
  reviews: [], challenges: [], report: {},
};
let report = buildReport(base);
assert.strictEqual(report.stakeholders[0].priorities[0].status, "teilweise");
assert.strictEqual(report.clauses[0].light, "gelb");
assert.strictEqual(report.stakeholders[0].light, "rot", "nothing met, under 50 %");

// Rules at their edges.
const withFindings = (assessments) => ({ ...base, reviews: [{ stage: "final", findings: assessments.map((a, i) => (
  { finding_id: `F-${i}`, reviewer: "legaleye", target_id: "KL-v1-1", assessment: a, reason: "Befund." })) }] });
const met = { ...base, priority_history: [assessment(1, "AS-final-PR-a-1-v1", "met", ["KL-v1-1"])] };
assert.strictEqual(buildReport(met).clauses[0].light, "gruen");
assert.strictEqual(buildReport({ ...met, reviews: withFindings(["requires_legal_amendment"]).reviews }).clauses[0].light, "rot");
assert.strictEqual(buildReport({ ...met, reviews: withFindings(["unresolved_issue"]).reviews }).clauses[0].light, "gelb");
const fourOpen = { ...met, reviews: withFindings(["unresolved_issue", "insufficient_information"]).reviews,
  report: { Offene_Punkte: [{ art: "data", punkt: "Eins", refs: ["KL-v1-1"] }, { art: "legal", punkt: "Zwei", refs: ["KL-v1-1"] }] } };
assert.strictEqual(buildReport(fourOpen).clauses[0].light, "rot");
const withdrawn = { ...met, reviews: withFindings(["requires_legal_amendment"]).reviews, challenges: [{ finding_id: "F-0", reply_decision: "withdraw" }] };
assert.strictEqual(buildReport(withdrawn).clauses[0].light, "gruen", "a withdrawn finding does not count");
const refused = { ...met, actors: [{ ...met.actors[0], acceptance_decisions: [{ decision_id: "D-final-a-v1", target_version: 1, kind: "reject", conditions: [] }] }] };
assert.strictEqual(buildReport(refused).stakeholders[0].light, "rot");
assert.strictEqual(buildReport(refused).overall.light, "rot");

// Generated text stays text in the saved file.
const hostile = { ...met, task: { question: '<script>alert("x")</script>', reference_date: null } };
const html = reportHtml(reportTree(buildReport(hostile)), "");
assert.ok(!html.includes("<script"), "escaped");
assert.ok(html.includes("&lt;script&gt;"), "shown as text");

// Plenum per round: a round without a version keeps the last assessed one.
const rounds = {
  ...base, final_proposal: null,
  proposal_history: [{ proposal_id: "PROP-1", version: 1, round: 1, legal_admissibility: "conditional", clauses: [clause("KL-v1-1")] },
    { proposal_id: "PROP-1", version: 2, round: 3, legal_admissibility: "conditional", clauses: [clause("KL-v2-1")] }],
  priority_history: [assessment(1, "AS-1-PR-a-1-v1", "met", ["KL-v1-1"])],
  actors: [{ ...base.actors[0], acceptance_decisions: [{ decision_id: "D-1-a-v1", round: 1, target_version: 1, kind: "accept", conditions: [] }] }],
};
const first = buildPlenum(rounds, 1);
assert.strictEqual(first.version, 1);
assert.strictEqual(first.stakeholders[0].light, "gruen");
const second = buildPlenum(rounds, 2);
assert.ok(second.noProposal, "round 2 has no version of its own");
assert.deepStrictEqual(second.stakeholders, first.stakeholders, "and keeps the consensus");
const third = buildPlenum(rounds, 3);
assert.strictEqual(third.version, 1, "version 2 is not assessed yet");
assert.strictEqual(third.pending, 2);
