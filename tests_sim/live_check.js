// Run by test_live.py: node live_check.js <partial.json> ...
// Every state of a running debate must display, and it must not claim an end it has not reached.
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { buildChats, visible, memberChat, council } = require(path.join(__dirname, "..", "frontend", "chats.js"));

const walk = (messages) => messages.flatMap((m) => [m, ...(m.replies || [])]);
let sawPreparation = false;
let sawRound = false;
let sawFinish = false;

for (const file of process.argv.slice(2)) {
  const result = JSON.parse(fs.readFileSync(file, "utf8"));
  assert.strictEqual(result.live, true, file);
  const data = buildChats(result);
  const group = data.chats[0];
  const lastStep = data.steps.length - 1;
  const finishing = Boolean(result.timing.deliberation_ended_at);
  assert.strictEqual(data.steps[lastStep].id === "abschluss", finishing, "the end step appears only at the end");
  const texts = JSON.stringify(group.messages);
  if (!finishing) {
    assert.ok(!texts.includes("Beratung beendet"), file);
    assert.ok(!texts.includes("keine Einigung"), file);
    assert.ok(!texts.includes("Keine neue Fassung in dieser Runde") || data.steps.length > 2, file);
  }
  assert.ok(!texts.includes("Fehlt:"), "nothing counts as missing while it runs");
  assert.ok(!texts.includes("Ergebnis:"), "a thread still running shows an interim state");
  for (const message of walk(group.messages)) assert.ok(message.step >= 0 && message.step <= lastStep, file);
  assert.strictEqual(visible(group, lastStep).length, group.messages.length, "the latest step shows everything");
  for (const parent of group.messages) {
    for (const member of council) assert.ok(memberChat(parent, member.id)[0] === parent);
  }
  sawPreparation ||= data.steps.length === 1;
  sawRound ||= data.steps.some((s) => s.round) && !finishing;
  sawFinish ||= finishing;
}
assert.ok(sawPreparation && sawRound && sawFinish, `phases: ${sawPreparation} ${sawRound} ${sawFinish}`);
console.log("ok");
