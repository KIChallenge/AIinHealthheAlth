// Run by test_frontend.py: node chats_check.js <result.json> ...
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { buildChats, visible } = require(path.join(__dirname, "..", "frontend", "chats.js"));

const walk = (messages) => messages.flatMap((m) => [m, ...(m.replies || [])]);

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
  const privates = data.chats.filter((c) => c.kind === "private");
  assert.strictEqual(privates.length, result.actors.length * 3, "one chat per reviewer and actor");
  assert.strictEqual(data.chats[data.chats.length - 1].kind, "meta");
  assert.strictEqual(data.steps[0].id, "vorbereitung");
  assert.strictEqual(data.steps[data.steps.length - 1].id, "abschluss");

  for (const chat of data.chats) {
    const all = walk(chat.messages);
    assert.strictEqual(new Set(all.map((m) => m.id)).size, all.length, "unique ids");
    // "Weiter" only ever appends: earlier messages stay in place
    let before = [];
    for (let step = 0; step < data.steps.length; step += 1) {
      const shown = visible(chat, step).map((m) => m.id);
      assert.deepStrictEqual(shown.slice(0, before.length), before, `${chat.id} step ${step}`);
      before = shown;
    }
    assert.strictEqual(before.length, chat.messages.length, "every message reachable");
    for (const message of all) {
      assert.ok(message.step >= 0 && message.step < data.steps.length);
      // the moderator speaks only from the user's side, and only in the group
      if (message.side === "out") {
        assert.strictEqual(message.role, "moderator");
        assert.strictEqual(chat.kind, "group");
      } else if (message.side !== "system") {
        assert.notStrictEqual(message.role, "moderator");
      }
      if (message.side === "peer") assert.strictEqual(message.role, "reviewer");
    }
    for (const parent of chat.messages) for (const reply of parent.replies || []) assert.strictEqual(reply.step, parent.step);
  }

  // Threads exist where the debate has follow-ups
  if (result.proposal_history.length) assert.ok(walk(group.messages).length > group.messages.length, "group has threads");
  for (const proposal of result.proposal_history) {
    const post = group.messages.find((m) => m.tag === `Vorschlag, Fassung ${proposal.version}`);
    assert.ok(post, `proposal ${proposal.version}`);
    if (proposal.review_complete) assert.ok(post.replies.length > 0, `reviews under proposal ${proposal.version}`);
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

  // A private chat holds exactly its two parties, and each confidential review reaches its chat
  for (const chat of privates) {
    const [reviewer, actor] = chat.id.split(":");
    for (const m of walk(chat.messages)) {
      if (m.side !== "system") assert.ok(m.senderId === reviewer || m.senderId === actor, `${chat.id}: ${m.sender}`);
    }
    const text = JSON.stringify(chat.messages);
    for (const review of result.reviews.filter((r) => r.reviewer === reviewer && r.actor_id === actor)) {
      for (const f of review.findings) assert.ok(text.includes(f.reason), f.finding_id);
    }
  }

  // Nothing is dropped from the group: every negotiation item and round decision appears
  for (const item of result.negotiation) assert.ok(groupText.includes(JSON.stringify(item.text)), item.item_id);
  for (const decision of result.decisions.filter((d) => !d.decision_id.startsWith("D-final"))) {
    assert.ok(groupText.includes(JSON.stringify(decision.reason)), decision.decision_id);
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
