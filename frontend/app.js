/* Input screen and debate chat. All text from results goes in through textContent, so generated
   content can never inject markup. */
const { buildChats, visible, labels: L } = window.DebateChats;
const AUTOPLAY_SECONDS = 12;
const SAMPLE = {
  schema_version: "1.0",
  actors: ["Gesundheitsministerium", "Sozialversicherung", "Ärztekammer"],
  question: "Entwickeln Sie einen gemeinsamen Vorschlag zur Verbesserung der ambulanten Versorgung in Österreich innerhalb des geltenden Rechtsrahmens. Berücksichtigen Sie den Zugang zur Versorgung, personelle Auswirkungen und Finanzierung. Kennzeichnen Sie Annahmen, rechtliche Unsicherheiten und offene Datenfragen ausdrücklich.",
  reference_date: "2026-11-05", scenario_assumptions: [], hard_constraints: [],
};

const $ = (id) => document.getElementById(id);

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined && child !== false) {
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
  }
  return node;
}

const text = (tag, value, cls) => h(tag, cls ? { class: cls } : null, value);

function initials(name) {
  const words = name.replace(/[()]/g, "").split(/\s+/).filter(Boolean);
  return (words.length > 1 ? words[0][0] + words[1][0] : name.slice(0, 2)).toUpperCase();
}

function avatar(chat) {
  if (chat.kind === "group") {
    return h("span", { class: "avatar group", "aria-hidden": "true" }, h("span"), h("span"), h("span"));
  }
  return h("span", { class: `avatar ${chat.kind}`, "aria-hidden": "true" }, initials(chat.name));
}

function clock(value) {
  if (value === null || value === undefined) return null;
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleTimeString("de-AT", { hour: "2-digit", minute: "2-digit" });
}

function statusMark(status) {
  return h("span", { class: `mark ${status}`, role: "img", "aria-label": L.STATUS[status] || status, title: L.STATUS[status] || status });
}

function bubbleItem(item) {
  return h("li", { class: "bubble-item" },
    item.number ? text("span", item.number, "item-number") : null,
    item.status ? statusMark(item.status) : null,
    h("div", null,
      item.title ? text("p", item.title, "item-title") : null,
      item.text ? text("p", item.text) : null,
      item.note ? text("p", item.note, "item-note") : null));
}

function messageNode(message, chat) {
  if (message.side === "system") {
    return h("div", { class: "system" }, text("span", message.text));
  }
  const time = clock(message.time);
  const showSender = message.side === "in" && (chat.kind === "group" || message.senderId !== chat.id);
  return h("article", { class: `bubble ${message.side}` },
    showSender ? text("p", message.sender, "sender") : null,
    message.tag ? text("p", message.tag, "tag") : null,
    message.quote ? text("blockquote", message.quote) : null,
    message.text ? message.text.split(/\n+/).map((para) => text("p", para)) : null,
    message.items && message.items.length ? h("ul", { class: "bubble-items" }, message.items.map(bubbleItem)) : null,
    message.note ? text("p", message.note, "item-note") : null,
    time ? text("time", time) : null);
}

/* ---- chat view ------------------------------------------------------------ */
const view = { result: null, data: null, step: 0, chat: "gruppe", timer: null, json: false };

const currentChat = () => view.data.chats.find((c) => c.id === view.chat) || view.data.chats[0];

function renderList() {
  const items = view.data.chats.map((chat) => {
    const shown = visible(chat, view.step);
    const lastMessage = [...shown].reverse().find((m) => m.side !== "system");
    const fresh = shown.filter((m) => m.step === view.step && m.side !== "system").length;
    const preview = lastMessage ? (lastMessage.text || lastMessage.tag || "") : "Noch keine Nachricht";
    return h("li", null, h("button", {
      type: "button", class: `chat-entry${chat.id === view.chat ? " active" : ""}`,
      "aria-current": chat.id === view.chat ? "true" : null,
      onclick: () => { view.chat = chat.id; view.json = false; document.body.dataset.pane = "chat"; render(true); },
    }, avatar(chat), h("span", { class: "chat-entry-body" },
      text("span", chat.name, "chat-name"), text("span", preview, "chat-preview")),
    fresh ? h("span", { class: "badge", title: `${fresh} neue Nachrichten in diesem Schritt` }, fresh) : null));
  });
  $("chat-list").replaceChildren(...items);
}

function renderHeader(chat) {
  const step = view.data.steps[view.step];
  $("question").textContent = view.data.header.question || view.data.header.status;
  const facts = [step.label, `Schritt ${view.step + 1} von ${view.data.steps.length}`];
  if (view.data.header.reference_date) facts.push(`Stichtag ${view.data.header.reference_date}`);
  const proposal = view.data.proposalAt ? view.data.proposalAt(view.step) : null;
  if (proposal) facts.push(`Fassung ${proposal.version}: ${L.ADMISSIBILITY[proposal.legal_admissibility] || ""}`);
  $("facts").replaceChildren(...facts.map((fact) => h("li", null, fact)));
  const scores = view.data.satisfactionAt ? view.data.satisfactionAt(view.step) : [];
  $("scores").replaceChildren(...scores.filter((s) => s.total).map((s) => h("li", null, `${s.name}: ${s.met} von ${s.total} erfüllt`)));
  $("chat-title").replaceChildren(avatar(chat), h("div", null, text("h2", chat.name), text("p", chat.subtitle)));
}

function render(jump) {
  const chat = currentChat();
  const shown = visible(chat, view.step);
  renderList();
  renderHeader(chat);
  const log = $("log");
  if (view.json) {
    log.replaceChildren(h("pre", { class: "json" }, JSON.stringify(shown.filter((m) => m.step === view.step), null, 2)));
  } else {
    const nodes = [];
    let drawn = -1;
    for (const message of shown) {
      if (message.step !== drawn) {
        drawn = message.step;
        nodes.push(h("div", { class: "divider", "data-step": drawn }, text("span", view.data.steps[drawn].label)));
      }
      nodes.push(messageNode(message, chat));
    }
    if (!nodes.length) nodes.push(h("div", { class: "system" }, text("span", "In diesem Chat gibt es bis hierhin keine Nachricht.")));
    log.replaceChildren(...nodes);
  }
  const atEnd = view.step === view.data.steps.length - 1;
  $("prev").disabled = view.step === 0;
  $("next").disabled = atEnd;
  $("next").textContent = atEnd ? "Ende erreicht" : `${view.data.steps[view.step + 1].label} ›`;
  $("step-label").textContent = view.data.steps[view.step].label;
  $("json-toggle").textContent = view.json ? "Chat" : "JSON";
  $("json-toggle").setAttribute("aria-pressed", String(view.json));
  if (jump) {
    const start = log.querySelector(`[data-step="${view.step}"]`);
    log.scrollTop = start ? start.offsetTop - 8 : 0;      // .log is the offset parent
  }
  try { history.replaceState(null, "", `#${view.step + 1}`); } catch { /* file:// in some browsers */ }
}

function go(step) {
  view.step = Math.max(0, Math.min(step, view.data.steps.length - 1));
  render(true);
}

function setPlaying(on) {
  clearInterval(view.timer);
  view.timer = null;
  if (on) {
    view.timer = setInterval(() => {
      if (view.step >= view.data.steps.length - 1) setPlaying(false);
      else go(view.step + 1);
    }, AUTOPLAY_SECONDS * 1000);
  }
  $("play").textContent = on ? "Pause" : "Abspielen";
  $("play").setAttribute("aria-pressed", String(on));
}

function openResult(result) {
  const chat = new URLSearchParams(location.search).get("chat") || "gruppe";
  Object.assign(view, { result, data: buildChats(result), chat, json: false });
  document.body.dataset.view = "chat";
  document.body.dataset.pane = "chat";
  const wanted = Number(location.hash.slice(1));
  go(Number.isInteger(wanted) && wanted >= 1 ? wanted - 1 : 0);
}

function closeChat() {
  setPlaying(false);
  document.body.dataset.view = "input";
  try { history.replaceState(null, "", location.pathname); } catch { /* ignore */ }
}

function downloadResult() {
  const blob = new Blob([JSON.stringify(view.result, null, 2)], { type: "application/json" });
  const link = h("a", { href: URL.createObjectURL(blob), download: `debatte-${view.result.debate_id}.json` });
  link.click();
  URL.revokeObjectURL(link.href);
}

/* ---- input screen -------------------------------------------------------- */
function notice(message) {
  $("notice").textContent = message || "";
}

function looksLikeResult(data) {
  return data && typeof data === "object" && "debate_id" in data && "status" in data;
}

function loadText(raw) {
  let data;
  try {
    data = JSON.parse(raw);
  } catch (error) {
    notice(`Kein gültiges JSON: ${error.message}`);
    return;
  }
  if (looksLikeResult(data)) {
    notice("");
    openResult(data);
  } else {
    $("request").value = JSON.stringify(data, null, 2);
    notice("Das ist eine Anfrage. Sie steht jetzt im Eingabefeld.");
  }
}

async function loadFile(file) {
  if (file) loadText(await file.text());
}

const server = { available: false };

async function refreshResults() {
  try {
    const response = await fetch("api/results");
    if (!response.ok) throw new Error();
    const items = await response.json();
    server.available = true;
    $("start").disabled = false;
    $("saved-title").hidden = !items.length;
    $("saved").replaceChildren(...items.map((item) => h("li", null, h("button", {
      type: "button", class: "text-link", onclick: () => openStored(item.debate_id),
    }, `${new Date(item.created_at * 1000).toLocaleString("de-AT")}, ${L.RESULT[item.status] || item.status} ›`))));
  } catch {
    server.available = false;
    $("start").disabled = true;
    notice("Ohne lokalen Server kannst du nur Ergebnis-Dateien öffnen. Start: python -m healthsim serve");
  }
}

async function openStored(id) {
  const response = await fetch(`api/results/${encodeURIComponent(id)}`);
  if (!response.ok) { notice("Dieses Ergebnis ist nicht verfügbar."); return; }
  openResult(await response.json());
}

function showRun(running, message) {
  $("start").disabled = running || !server.available;
  $("start").textContent = running ? "Debatte läuft" : "Debatte starten";
  notice(message);
}

async function pollRun() {
  const status = await (await fetch("api/run")).json();
  if (status.running) {
    showRun(true, `Läuft seit ${Math.round(status.elapsed_seconds)} s, ${status.events} Ereignisse. Sie dauert bis zu 13 Minuten.`);
    setTimeout(pollRun, 3000);
    return;
  }
  showRun(false, "");
  await refreshResults();
  if (status.result) openResult(status.result);
}

async function startRun() {
  let body;
  try {
    body = JSON.stringify(JSON.parse($("request").value));
  } catch (error) {
    notice(`Kein gültiges JSON: ${error.message}`);
    return;
  }
  if (!confirm("Die Debatte verbraucht Azure-Kontingent und dauert bis zu 13 Minuten. Starten?")) return;
  const response = await fetch("api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body });
  const answer = await response.json();
  if (answer.result) { openResult(answer.result); return; }
  if (!response.ok) { notice(answer.message || "Der Start ist fehlgeschlagen."); return; }
  pollRun();
}

/* ---- wiring ---------------------------------------------------------------- */
function step(action) {
  setPlaying(false);
  action();
}

$("prev").addEventListener("click", () => step(() => go(view.step - 1)));
$("next").addEventListener("click", () => step(() => go(view.step + 1)));
$("play").addEventListener("click", () => setPlaying(!view.timer));
$("json-toggle").addEventListener("click", () => { view.json = !view.json; render(true); });
$("download").addEventListener("click", downloadResult);
$("close").addEventListener("click", closeChat);
$("back").addEventListener("click", () => { document.body.dataset.pane = "list"; });
$("start").addEventListener("click", startRun);
$("file").addEventListener("change", (event) => loadFile(event.target.files[0]));
$("open-pasted").addEventListener("click", () => loadText($("request").value));
$("sample").addEventListener("click", () => { $("request").value = JSON.stringify(SAMPLE, null, 2); notice(""); });

document.addEventListener("keydown", (event) => {
  if (document.body.dataset.view !== "chat" || event.ctrlKey || event.metaKey || event.altKey) return;
  const actions = {
    ArrowRight: () => step(() => go(view.step + 1)), ArrowLeft: () => step(() => go(view.step - 1)),
    Home: () => step(() => go(0)), End: () => step(() => go(view.data.steps.length - 1)),
    " ": () => setPlaying(!view.timer), j: () => $("json-toggle").click(), Escape: closeChat,
  };
  if (actions[event.key]) { event.preventDefault(); actions[event.key](); }
});

for (const type of ["dragover", "drop"]) {
  document.addEventListener(type, (event) => {
    event.preventDefault();
    if (type === "drop") loadFile(event.dataTransfer.files[0]);
  });
}

$("request").value = JSON.stringify(SAMPLE, null, 2);
refreshResults().then(async () => {
  if (!server.available) return;
  const requested = new URLSearchParams(location.search).get("debatte");
  if (requested) await openStored(requested);
  const status = await (await fetch("api/run")).json();
  if (status.running) pollRun();
});
