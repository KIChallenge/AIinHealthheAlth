// Run by test_frontend.py: node chats_check.js <result.json> ...
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { buildChats, visible } = require(path.join(__dirname, "..", "frontend", "chats.js"));

for (const file of process.argv.slice(2)) {
  const result = JSON.parse(fs.readFileSync(file, "utf8"));
  const data = buildChats(result);
  assert.ok(data.steps.length > 0 && data.chats.length > 0, file);
  if (!result.task) {
    assert.strictEqual(data.chats.length, 1);
    continue;
  }
  const group = data.chats[0];
  assert.strictEqual(group.kind, "group");
  assert.deepStrictEqual(data.chats.filter((c) => c.kind === "actor").map((c) => c.id), result.actors.map((a) => a.actor_id));
  assert.deepStrictEqual(data.chats.filter((c) => c.kind === "reviewer").map((c) => c.name), ["TruthSeeker", "LegalEye", "EthicMind"]);
  assert.strictEqual(data.steps[0].id, "vorbereitung");
  assert.strictEqual(data.steps[data.steps.length - 1].id, "abschluss");

  for (const chat of data.chats) {
    assert.strictEqual(new Set(chat.messages.map((m) => m.id)).size, chat.messages.length, "unique ids");
    // "Weiter" only ever appends: earlier messages stay in place
    let before = [];
    for (let step = 0; step < data.steps.length; step += 1) {
      const shown = visible(chat, step).map((m) => m.id);
      assert.deepStrictEqual(shown.slice(0, before.length), before, `${chat.id} step ${step}`);
      before = shown;
    }
    assert.strictEqual(before.length, chat.messages.length, "every message reachable");
    for (const message of chat.messages) {
      assert.ok(message.step >= 0 && message.step < data.steps.length);
      // only the moderator speaks from the user's side, and only in the group
      if (message.side === "out") {
        assert.strictEqual(message.role, "moderator");
        assert.strictEqual(chat.kind, "group");
      } else if (message.side === "in") {
        assert.notStrictEqual(message.role, "moderator");
      }
    }
  }

  // Privacy: before publication the group shows no actor or reviewer material
  assert.ok(visible(group, 0).every((m) => m.side === "system" || m.role === "moderator"));
  // Confidential reviews stay out of the group. Identical wording in a public review (scripted
  // fixtures repeat one sentence) is not a leak.
  const publicReasons = new Set(result.reviews.filter((r) => r.stage === "candidate" || r.stage === "final")
    .flatMap((r) => r.findings.map((f) => f.reason)));
  const groupText = JSON.stringify(group.messages);
  for (const review of result.reviews.filter((r) => ["initial", "second", "assessment"].includes(r.stage))) {
    for (const f of review.findings) {
      if (!publicReasons.has(f.reason)) assert.ok(!groupText.includes(f.reason), `private finding in group: ${f.finding_id}`);
    }
  }

  // Each actor's chat holds only that actor's own material
  for (const actor of result.actors) {
    const chat = data.chats.find((c) => c.id === actor.actor_id);
    const others = chat.messages.filter((m) => m.role === "actor" && m.senderId !== actor.actor_id);
    assert.strictEqual(others.length, 0, actor.actor_id);
  }

  // Nothing is dropped from the group: every negotiation item and round decision appears
  const texts = group.messages.map((m) => m.text);
  for (const item of result.negotiation) assert.ok(texts.includes(item.text), item.item_id);
  for (const decision of result.decisions.filter((d) => !d.decision_id.startsWith("D-final"))) {
    assert.ok(texts.includes(decision.reason), decision.decision_id);
  }
  for (const proposal of result.proposal_history) {
    assert.ok(group.messages.some((m) => m.tag === `Vorschlag, Fassung ${proposal.version}`), proposal.version);
  }

  // Header numbers come from stored assessments and match the exact rule
  data.steps.forEach((step, index) => {
    for (const s of data.satisfactionAt(index)) {
      if (step.round) {
        assert.strictEqual(s.met, s.assessed.filter((a) => a.status === "met").length);
        assert.strictEqual(s.reached, s.total > 0 && s.assessed.length === s.total && 5 * s.met >= 4 * s.total);
      }
    }
  });
}
assert.throws(() => buildChats(null));
console.log("ok");
