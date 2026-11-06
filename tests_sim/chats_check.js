// Run by test_frontend.py: node chats_check.js <result.json> ...
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { buildChats, visible, memberChat, findPassage, council } = require(path.join(__dirname, "..", "frontend", "chats.js"));

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
  assert.deepStrictEqual(data.chats.map((c) => c.kind), ["group", "meta"], "the rail has exactly these two chats");
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
      if (message.role === "reviewer") assert.ok(["TS", "LE", "EM"].includes(message.initials), message.sender);
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
  const groupText = JSON.stringify(group.messages.map(({ replies, ...feed }) => feed));      // threads may hold them
  for (const review of result.reviews.filter((r) => ["initial", "second", "assessment"].includes(r.stage))) {
    for (const f of review.findings) {
      if (!publicReasons.has(f.reason)) assert.ok(!groupText.includes(f.reason), `private finding in group: ${f.finding_id}`);
    }
  }

  // The private chats are gone, so every finding and challenge must now be reachable in the group
  const everything = JSON.stringify(walk(group.messages));
  for (const review of result.reviews) {
    for (const f of review.findings) assert.ok(everything.includes(JSON.stringify(f.reason)), f.finding_id);
  }
  for (const c of result.challenges) assert.ok(everything.includes(JSON.stringify(c.reason)), c.challenge_id);

  // A thread holds the author, the council and whoever challenged a finding, and it has an end
  const actorIds = new Set(result.actors.map((a) => a.actor_id));
  const members = new Set(council.map((m) => m.id));
  for (const parent of group.messages) {
    for (const reply of parent.replies) {
      if (reply.side !== "system") assert.ok(reply.senderId === parent.senderId || members.has(reply.senderId) || actorIds.has(reply.senderId), reply.sender);
    }
    const reviewers = parent.replies.filter((r) => r.role === "reviewer");
    if (reviewers.length) assert.ok(parent.replies[parent.replies.length - 1].conclusion, `thread of ${parent.tag} ends in a conclusion`);
    assert.strictEqual(parent.replies.filter((r) => r.conclusion).length, reviewers.length ? 1 : 0, parent.tag);
    // Each moderator's chat: only that moderator, none of their messages missing, and its own result
    if (reviewers.length) {
      const counted = new Set();
      for (const member of council) {
        const chat = memberChat(parent, member.id);
        const own = parent.replies.filter((r) => r.senderId === member.id);
        assert.deepStrictEqual(chat.filter((m) => m.role === "reviewer" && m !== parent).map((m) => m.id), own.map((m) => m.id));
        assert.strictEqual(chat[0], parent);
        for (const m of chat) {
          assert.ok(m === parent || m.side === "system" || m.senderId === member.id || m.about === member.id || m.senderId === parent.senderId, m.tag);
        }
        for (const c of parent.replies.filter((r) => r.about === member.id)) assert.ok(chat.includes(c), "challenge in the chat");
        if (own.length) assert.ok(chat[chat.length - 1].conclusion, "chat ends in a result");
        chat.forEach((m) => counted.add(m.id));
      }
      for (const reply of parent.replies) assert.ok(counted.has(reply.id), `in no moderator chat: ${reply.tag}`);
      assert.strictEqual(memberChat(parent, null).length, parent.replies.length + 1);
    }
    // an institution's standpoint thread is about that institution only
    if (parent.tag === "Standpunkt") assert.ok(parent.replies.every((r) => r.side === "system" || r.senderId === parent.senderId || members.has(r.senderId)));
  }

  // Nothing is dropped from the group: every negotiation item and round decision appears
  for (const item of result.negotiation) assert.ok(groupText.includes(JSON.stringify(item.text)), item.item_id);
  for (const decision of result.decisions.filter((d) => !d.decision_id.startsWith("D-final"))) {
    assert.ok(groupText.includes(JSON.stringify(decision.reason)), decision.decision_id);
  }

  // Everything cited in the debate has a chip, and each chip names a known source or is shown as missing
  const chips = new Set();
  const collect = (m) => {
    (m.sources || []).forEach((id) => chips.add(id));
    (m.items || []).forEach((item) => (item.sources || []).forEach((id) => chips.add(id)));
  };
  walk(group.messages).forEach(collect);
  const citedIds = new Set();
  const isPassage = (v) => typeof v === "string" && v.startsWith("P-");
  for (const review of result.reviews) for (const f of review.findings) if (isPassage(f.reference)) citedIds.add(f.reference);
  for (const n of result.negotiation) n.quellen.filter(isPassage).forEach((id) => citedIds.add(id));
  for (const a of result.arguments) for (const c of a.claims) c.quellen.filter(isPassage).forEach((id) => citedIds.add(id));
  for (const p of result.proposal_history) for (const c of p.clauses) c.premises.filter(isPassage).forEach((id) => citedIds.add(id));
  for (const id of citedIds) assert.ok(chips.has(id), `no chip for ${id}`);
  for (const id of chips) assert.ok(id.startsWith("P-"), id);
  assert.strictEqual(Object.keys(data.sources).length, result.sources.length);

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

// Passages are found despite spacing and dropped signs, and the mark covers them exactly
const doc = "Einleitung.\n\nNach § 23 G–ZG gilt:\nPlanung & Steuerung. Schluss.";
const hit = findPassage(doc, "§ 23 GZG gilt: Planung Steuerung");
assert.strictEqual(doc.slice(hit.start, hit.end), "23 G–ZG gilt:\nPlanung & Steuerung");
assert.strictEqual(findPassage(doc, "steht nicht im Dokument"), null);
assert.strictEqual(findPassage(doc, " - "), null);
console.log("ok");
