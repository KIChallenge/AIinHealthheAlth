/* Input screen and debate chat. All text from results goes in through textContent, so generated
   content can never inject markup. */
const { buildChats, visible, memberChat, findPassage, council: COUNCIL, labels: L } = window.DebateChats;
const READ_MS = 2000;       // how long a message stands before the next one starts typing
const TYPING_MS = 900;
const OPERATORS = ["<", "<=", "=", ">=", ">"];
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

function avatar(message) {
  return text("span", message.initials || initials(message.sender), `avatar ${message.role === "reviewer" ? message.senderId : "actor"}`);
}

function clock(value) {
  if (value === null || value === undefined) return null;
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleTimeString("de-AT", { hour: "2-digit", minute: "2-digit" });
}

function statusMark(status) {
  return h("span", { class: `mark ${status}`, role: "img", "aria-label": L.STATUS[status] || status, title: L.STATUS[status] || status });
}

const SOURCE_TYPES = { Article: "Artikel", LegalText: "Rechtstext", EthicalText: "Ethik-Text", ActorData: "Institutionsdokument" };
const MAX_INLINE_SOURCES = 3;

function germanDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || "");
  return match ? `${match[3]}.${match[2]}.${match[1]}` : null;
}

function sourceLabel(source) {
  if (source.doc_type === "Article") return [source.publisher || source.title, germanDate(source.publication_date)].filter(Boolean).join(", ");
  return source.doc_type === "LegalText" ? source.locator : source.title;
}

function documentIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "12");
  svg.setAttribute("height", "12");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M3 1h7l3 3v11H3zm2 5v1.500h6V6zm0 3v1.500h6V9zm0 3v1.500h4V12z");
  path.setAttribute("fill-rule", "evenodd");
  svg.append(path);
  return svg;
}

function sourceChip(id) {
  const source = (view.data.sources || {})[id];
  if (!source) {
    return h("button", { type: "button", class: "source-chip", disabled: true, title: `${id} ist nicht gespeichert.` },
      documentIcon(), text("span", "Beleg nicht gefunden"));
  }
  return h("button", { type: "button", class: "source-chip", title: source.title, onclick: () => openSource(id) },
    documentIcon(), text("span", SOURCE_TYPES[source.doc_type] || "Quelle", "source-type"), text("span", sourceLabel(source), "source-label"));
}

function sourceList(ids) {
  if (!ids || !ids.length) return null;
  const chips = h("div", { class: "sources" }, ids.map(sourceChip));
  if (ids.length <= MAX_INLINE_SOURCES) return chips;
  return h("details", { class: "source-group" }, text("summary", `${ids.length} Quellen anzeigen`), chips);
}

function bubbleItem(item) {
  return h("li", { class: "bubble-item" },
    item.number ? text("span", item.number, "item-number") : null,
    item.status ? statusMark(item.status) : null,
    h("div", null,
      item.title ? text("p", item.title, "item-title") : null,
      item.text ? text("p", item.text) : null,
      item.note ? text("p", item.note, "item-note") : null,
      sourceList(item.sources)));
}

function cardBody(message) {
  return [
    message.tag ? text("p", message.tag, "tag") : null,
    message.quote ? text("blockquote", message.quote) : null,
    message.text ? message.text.split(/\n+/).map((para) => text("p", para)) : null,
    message.items && message.items.length ? h("ul", { class: "bubble-items" }, message.items.map(bubbleItem)) : null,
    message.note ? text("p", message.note, "item-note") : null,
    sourceList(message.sources),
  ];
}

function systemLine(message) {
  return h("div", { class: message.conclusion ? "system conclusion" : "system" }, text("span", message.text));
}

function cardHead(sender, time) {
  const shown = clock(time);
  return sender || shown ? h("header", { class: "card-head" }, sender ? text("p", sender, "sender") : null, shown ? text("time", shown) : null) : null;
}

function threadButton(message) {
  const count = (message.replies || []).length;
  if (!count) {
    return h("button", { type: "button", class: "thread-button", disabled: true,
      title: "Der Moderatorenrat hat diese Nachricht nicht geprüft." }, "Keine Prüfung");
  }
  return h("button", { type: "button", class: "thread-button", onclick: () => openThread(message.id) },
    "Thread", text("span", count, "thread-count"));
}

/* A message in the group feed. Institutions get a thread button even without a review, so the
   absence of a review is visible instead of looking like a missing control. */
function groupCard(message) {
  if (message.side === "system") return systemLine(message);
  const own = message.side === "out";
  return h("div", { class: `row ${message.side}` },
    own ? null : avatar(message),
    h("article", { class: `card ${message.side}`, "data-message": message.id },
      cardHead(own ? null : message.sender, message.time),
      cardBody(message),
      message.role === "actor" || (message.replies || []).length ? threadButton(message) : null));
}

/* A message in the council sidebar: everyone by name, moderators with their initials as well. */
function threadCard(message, authorId) {
  if (message.side === "system") return systemLine(message);
  const side = message.senderId === authorId ? "out" : "in";
  return h("div", { class: `row ${side}` },
    side === "in" ? avatar(message) : null,
    h("article", { class: `card ${side}` },
      cardHead(message.sender, message.time),
      cardBody(message)));
}

function metaCard(turn) {
  const side = turn.role === "du" ? "out" : "in";
  return h("div", { class: `row ${side}` }, h("article", { class: `card ${side}` },
    cardBody({ text: turn.text, note: turn.refs && turn.refs.length ? `Belege: ${turn.refs.join(", ")}` : null }),
    clock(turn.at) ? text("time", clock(turn.at)) : null));
}

/* ---- chat view ------------------------------------------------------------ */
const view = { result: null, data: null, step: 0, chat: "gruppe", thread: null, json: false,
  stored: false, turns: [], asking: false };

const currentChat = () => view.data.chats.find((c) => c.id === view.chat) || view.data.chats[0];

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
  $("chat-title").replaceChildren(text("h2", chat.name), text("p", chat.subtitle));
}

/* ---- council sidebar: the debate behind one message, played back message by message ---- */
const player = { pos: 1, playing: false, typing: false, timer: null, member: null };

function threadParent() {
  if (!view.thread) return null;
  return currentChat().messages.find((m) => m.id === view.thread && m.step <= view.step) || null;
}

function stopPlayer() {
  clearTimeout(player.timer);
  Object.assign(player, { playing: false, typing: false, timer: null });
}

const speakerOf = (message) => (message.side === "system" ? "Ergebnis" : message.sender);
const memberOf = (id) => COUNCIL.find((member) => member.id === id);

function typingLine(message) {
  return h("div", { class: "typing" }, text("span", message.side === "system" ? "Ergebnis folgt …" : `${speakerOf(message)} schreibt …`));
}

function drawPlayer(messages) {
  const atEnd = player.pos >= messages.length;
  const state = player.playing ? "pause" : atEnd ? "replay" : "play";
  $("t-play").textContent = { pause: "Pause", replay: "Nochmal abspielen", play: "Abspielen" }[state];
  $("t-play").setAttribute("aria-pressed", String(player.playing));
  $("t-prev").disabled = player.pos <= 1;
  $("t-next").disabled = atEnd;
  $("track-count").textContent = `${player.pos} von ${messages.length}`;
  $("track").replaceChildren(...messages.map((message, index) => h("button", {
    type: "button", class: `marker${index < player.pos ? " done" : ""}${index === player.pos - 1 ? " current" : ""}`,
    title: speakerOf(message), "aria-label": `Nachricht ${index + 1}: ${speakerOf(message)}`,
    "aria-current": index === player.pos - 1 ? "step" : null, onclick: () => seekThread(index + 1),
  })));
}

function drawThread(arrived) {
  const parent = threadParent();
  if (!parent) view.thread = null;
  document.querySelector(".window").classList.toggle("with-thread", Boolean(parent));
  $("thread").hidden = !parent;
  for (const card of $("log").querySelectorAll(".card.active")) card.classList.remove("active");
  if (!parent) { stopPlayer(); return; }
  const card = $("log").querySelector(`[data-message="${parent.id}"]`);
  if (card) card.classList.add("active");

  const messages = memberChat(parent, player.member);
  const nodes = messages.slice(0, player.pos).map((message) => threadCard(message, parent.senderId));
  const upcoming = player.typing ? messages[player.pos] : null;
  const current = upcoming || messages[player.pos - 1];
  const newest = nodes[nodes.length - 1];
  if (arrived) newest.classList.add("arrive");
  if (upcoming) nodes.push(typingLine(upcoming));
  const log = $("thread-log");
  const kept = log.scrollTop;
  log.replaceChildren(...nodes);
  const chatWith = player.member ? ` · Chat mit ${memberOf(player.member).name}` : "";
  $("thread-title").textContent = `${parent.tag || "Nachricht"} · ${parent.sender}${chatWith}`;
  for (const button of $("council-members").querySelectorAll(".member")) {
    const id = button.dataset.member;       // empty: the whole thread
    button.classList.toggle("selected", id === (player.member || ""));
    button.setAttribute("aria-pressed", String(id === (player.member || "")));
    button.classList.toggle("speaking", Boolean(id) && id === current.senderId);
    button.disabled = Boolean(id) && !parent.replies.some((m) => m.senderId === id);
  }
  drawPlayer(messages);
  if (upcoming) log.scrollTop = log.scrollHeight;
  else if (arrived) log.scrollTop = newest.offsetTop - 8;      // .log is the offset parent
  else log.scrollTop = kept;
}

function openThread(id) {
  stopPlayer();
  view.thread = id;
  showMember(null);
}

/* Shows the history first; Play goes through it again. */
function showMember(member) {
  stopPlayer();
  player.member = member;
  const parent = threadParent();
  player.pos = parent ? memberChat(parent, member).length : 1;
  drawThread(false);
  $("thread-log").scrollTop = 0;      // only after it is visible: a hidden element ignores this
}

function closeThread() {
  stopPlayer();
  view.thread = null;
  drawThread(false);
}

function scheduleNext() {
  player.timer = setTimeout(() => {
    player.typing = true;
    drawThread(false);
    player.timer = setTimeout(() => {
      player.typing = false;
      player.pos += 1;
      if (player.pos >= memberChat(threadParent(), player.member).length) player.playing = false;
      drawThread(true);
      if (player.playing) scheduleNext();
    }, TYPING_MS);
  }, READ_MS);
}

function togglePlay() {
  if (player.playing) {
    stopPlayer();
    drawThread(false);
    return;
  }
  if (player.pos >= memberChat(threadParent(), player.member).length) player.pos = 1;
  player.playing = true;
  drawThread(true);
  scheduleNext();
}

function seekThread(pos) {
  const parent = threadParent();
  stopPlayer();
  const moved = Math.max(1, Math.min(pos, memberChat(parent, player.member).length));
  const forward = moved > player.pos;
  player.pos = moved;
  drawThread(forward || moved === 1);
}

function metaNodes() {
  const nodes = [h("div", { class: "system" }, text("span", view.stored
    ? "Der Meta-Moderator antwortet nur aus dem Protokoll dieser debAIte. Die debAIte selbst ändert sich dadurch nicht."
    : "Nachfragen gehen nur bei gespeicherten debAItes, nicht bei einer geöffneten Datei."))];
  nodes.push(...view.turns.map(metaCard));
  if (view.asking) nodes.push(h("div", { class: "typing" }, text("span", "Meta-Moderator schreibt …")));
  return nodes;
}

function render(jump) {
  const chat = currentChat();
  const isMeta = chat.kind === "meta";
  document.body.dataset.screen = isMeta ? "meta" : "group";
  for (const [id, active] of [["chat-gruppe", !isMeta], ["chat-meta", isMeta]]) {
    $(id).classList.toggle("active", active);
    $(id).setAttribute("aria-current", String(active));
  }
  $("chat-meta").hidden = !view.data.chats.some((c) => c.kind === "meta");
  renderHeader(chat);
  const log = $("log");
  $("composer").hidden = !isMeta;
  $("ask-input").disabled = !view.stored || view.asking;
  $("ask-send").disabled = !view.stored || view.asking;
  const shown = isMeta ? [] : visible(chat, view.step);
  if (isMeta) {
    log.replaceChildren(...metaNodes());
    log.scrollTop = log.scrollHeight;
  } else if (view.json) {
    log.replaceChildren(h("pre", { class: "json" }, JSON.stringify(shown.filter((m) => m.step === view.step), null, 2)));
  } else {
    const nodes = [];
    let drawn = -1;
    for (const message of shown) {
      if (message.step !== drawn) {
        drawn = message.step;
        nodes.push(h("div", { class: "divider", "data-step": drawn }, text("span", view.data.steps[drawn].label)));
      }
      nodes.push(groupCard(message));
    }
    log.replaceChildren(...nodes);
  }
  const atEnd = view.step === view.data.steps.length - 1;
  $("prev").disabled = view.step === 0;
  $("next").disabled = atEnd;
  $("next").textContent = atEnd ? "Ende erreicht" : `${view.data.steps[view.step + 1].label} ›`;
  $("step-label").textContent = view.data.steps[view.step].label;
  $("json-toggle").textContent = view.json ? "Chat" : "JSON";
  $("json-toggle").setAttribute("aria-pressed", String(view.json));
  drawThread(false);
  if (jump && !isMeta) {
    const start = log.querySelector(`[data-step="${view.step}"]`);
    log.scrollTop = start ? start.offsetTop - 8 : 0;      // .log is the offset parent
  }
  try { history.replaceState(null, "", `${location.search}#${view.step + 1}`); } catch { /* file:// in some browsers */ }
}

function selectChat(id) {
  stopPlayer();
  Object.assign(view, { chat: id, thread: null, json: false });
  render(true);
}

function go(step) {
  stopPlayer();
  view.step = Math.max(0, Math.min(step, view.data.steps.length - 1));
  view.thread = null;
  render(true);
}

function openResult(result, stored) {
  const chat = new URLSearchParams(location.search).get("chat") || "gruppe";
  Object.assign(view, { result, data: buildChats(result), chat, thread: null, json: false, stored: Boolean(stored), turns: [] });
  document.body.dataset.view = "chat";
  const wanted = Number(location.hash.slice(1));
  go(Number.isInteger(wanted) && wanted >= 1 ? wanted - 1 : 0);
  if (stored) loadTurns();
}

async function loadTurns() {
  try {
    const response = await fetch(`api/ask/${encodeURIComponent(view.result.debate_id)}`);
    if (response.ok) {
      view.turns = await response.json();
      if (currentChat().kind === "meta") render(false);
    }
  } catch { /* the chat works without earlier questions */ }
}

async function ask(event) {
  event.preventDefault();
  const question = $("ask-input").value.trim();
  if (!question || view.asking) return;
  $("ask-input").value = "";
  view.turns.push({ role: "du", text: question, at: Date.now() / 1000 });
  view.asking = true;
  render(false);
  try {
    const response = await fetch("api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ debate_id: view.result.debate_id, question }) });
    const answer = await response.json();
    view.turns.push({ role: "meta", text: response.ok ? answer.antwort : answer.message, refs: answer.refs || [], at: Date.now() / 1000 });
  } catch {
    view.turns.push({ role: "meta", text: "Der lokale Server ist nicht erreichbar.", at: Date.now() / 1000 });
  }
  view.asking = false;
  render(false);
  $("ask-input").focus();
}

/* ---- source viewer: the passage exactly as the debate read it ---------------- */
const reader = { id: null, passage: null };

function passageNodes(passage) {
  return [passage.context_before ? text("p", `… ${passage.context_before}`, "source-context") : null,
    text("mark", passage.text), passage.context_after ? text("p", `${passage.context_after} …`, "source-context") : null].filter(Boolean);
}

function sourceMeta(source) {
  const valid = [germanDate(source.valid_from) && `gültig ab ${germanDate(source.valid_from)}`,
    germanDate(source.valid_until) && `bis ${germanDate(source.valid_until)}`].filter(Boolean).join(" ");
  return [source.publisher, germanDate(source.publication_date), valid, source.locator, source.date_unknown ? "undatiert" : null].filter(Boolean).join(" · ");
}

async function openSource(id) {
  const source = view.data.sources[id];
  Object.assign(reader, { id, passage: null });
  $("source-kind").textContent = SOURCE_TYPES[source.doc_type] || "Quelle";
  $("source-title").textContent = source.title;
  $("source-meta").textContent = sourceMeta(source);
  $("source-note").textContent = "";
  $("source-full").hidden = true;
  $("source-full").disabled = false;
  const external = /^https?:\/\//i.test(source.source_url || "");
  $("source-link").hidden = !external;
  if (external) $("source-link").href = source.source_url;
  $("source").showModal();
  if (!view.stored) {
    $("source-body").replaceChildren(text("p", "Der Text ist nur bei gespeicherten debAItes verfügbar, nicht bei einer geöffneten Datei."));
    return;
  }
  $("source-body").replaceChildren(text("p", "Text wird geladen …", "item-note"));
  try {
    const response = await fetch(`api/source/${encodeURIComponent(view.result.debate_id)}/${encodeURIComponent(id)}`);
    const answer = await response.json();
    if (reader.id !== id) return;       // another source was opened meanwhile
    if (!response.ok) { $("source-body").replaceChildren(text("p", answer.message)); return; }
    reader.passage = answer;
    $("source-body").replaceChildren(...passageNodes(answer));
    $("source-note").textContent = "Das markierte Stück ist genau der Text, den das System in dieser debAIte gelesen hat.";
    $("source-full").hidden = false;
    $("source-body").scrollTop = $("source-body").querySelector("mark").offsetTop - 60;
  } catch {
    if (reader.id === id) $("source-body").replaceChildren(text("p", "Der lokale Server ist nicht erreichbar."));
  }
}

async function openWholeDocument() {
  const id = reader.id;
  $("source-full").disabled = true;
  $("source-note").textContent = "Dokument wird geladen …";
  try {
    const response = await fetch(`api/source/${encodeURIComponent(view.result.debate_id)}/${encodeURIComponent(id)}/document`);
    const answer = await response.json();
    if (reader.id !== id) return;
    if (!response.ok) {
      $("source-note").textContent = answer.message;
      $("source-full").disabled = false;
      return;
    }
    const found = findPassage(answer.text, reader.passage.text);
    $("source-body").replaceChildren(found
      ? h("div", { class: "source-document" }, answer.text.slice(0, found.start), text("mark", answer.text.slice(found.start, found.end)), answer.text.slice(found.end))
      : h("div", { class: "source-document" }, answer.text));
    $("source-note").textContent = [answer.unchanged ? "Stand der debAIte." : "Achtung: Das Dokument wurde seit dieser debAIte geändert.",
      found ? "Die zitierte Stelle ist markiert." : "Die zitierte Stelle ließ sich hier nicht wortgleich finden."].join(" ");
    const mark = $("source-body").querySelector("mark");
    $("source-body").scrollTop = mark ? mark.offsetTop - 60 : 0;
  } catch {
    if (reader.id === id) {
      $("source-note").textContent = "Der lokale Server ist nicht erreichbar.";
      $("source-full").disabled = false;
    }
  }
}

function closeChat() {
  stopPlayer();
  document.body.dataset.view = "input";
  try { history.replaceState(null, "", location.pathname); } catch { /* ignore */ }
}

function downloadResult() {
  const blob = new Blob([JSON.stringify(view.result, null, 2)], { type: "application/json" });
  const link = h("a", { href: URL.createObjectURL(blob), download: `debaite-${view.result.debate_id}.json` });
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
    openResult(data, false);
  } else {
    setRequest(data);
    notice("Das ist eine Anfrage. Sie steht jetzt in der Eingabe.");
  }
}

/* ---- request form: the same request as the JSON tab, kept in sync ------------- */
const form = { tab: "form", extra: {}, actors: [...SAMPLE.actors] };

function labelled(label, control) {
  return h("label", { class: "field" }, text("span", label, "field-label small"), control);
}

function removeButton(row) {
  return h("button", { type: "button", class: "pill small", "aria-label": "Eintrag entfernen", onclick: () => row.remove() }, "Entfernen");
}

function assumptionRow(item = {}) {
  const row = h("li", { class: "form-row", "data-id": item.id || null });
  row.append(h("input", { type: "text", class: "f-description", "aria-label": "Annahme", placeholder: "Annahme beschreiben", value: item.description || "" }), removeButton(row));
  return row;
}

function constraintRow(item = {}) {
  const row = h("li", { class: "form-block", "data-id": item.id || null });
  const select = h("select", { class: "f-operator", "aria-label": "Vergleich" },
    h("option", { value: "" }, "Vergleich"), OPERATORS.map((op) => h("option", { value: op }, op)));
  select.value = item.operator || "";
  const hasNumber = [item.metric, item.value, item.unit, item.baseline, item.period].some((v) => v !== null && v !== undefined && v !== "");
  row.append(
    h("div", { class: "form-row" }, h("input", { type: "text", class: "f-description", "aria-label": "Beschränkung", placeholder: "Beschränkung beschreiben", value: item.description || "" }), removeButton(row)),
    h("details", { open: hasNumber || null }, text("summary", "Als Messwert angeben"),
      h("div", { class: "form-grid" },
        labelled("Kennzahl", h("input", { type: "text", class: "f-metric", value: item.metric || "" })), labelled("Vergleich", select),
        labelled("Wert", h("input", { type: "number", step: "any", class: "f-value", value: item.value ?? "" })),
        labelled("Einheit", h("input", { type: "text", class: "f-unit", value: item.unit || "" })),
        labelled("Bezugsgröße", h("input", { type: "text", class: "f-baseline", value: item.baseline || "" })),
        labelled("Zeitraum", h("input", { type: "text", class: "f-period", value: item.period || "" })))));
  return row;
}

function renderActors(selected) {
  const names = [...new Set([...form.actors, ...selected])];
  $("f-actors").replaceChildren(...names.map((name) => h("label", { class: "check" },
    h("input", { type: "checkbox", value: name, checked: selected.includes(name) || null }), h("span", null, name))));
}

function fillForm(data) {
  const { actors, question, reference_date: date, scenario_assumptions: assumptions, hard_constraints: constraints, ...extra } = data;
  form.extra = extra;
  renderActors(Array.isArray(actors) ? actors : []);
  $("f-question").value = question || "";
  $("f-date").value = date || "";
  $("f-assumptions").replaceChildren(...(Array.isArray(assumptions) ? assumptions : []).map((a) => assumptionRow(a)));
  $("f-constraints").replaceChildren(...(Array.isArray(constraints) ? constraints : []).map((c) => constraintRow(c)));
}

function readForm() {
  const value = (row, cls) => row.querySelector(cls).value.trim();
  const withId = (row, fields) => ({ ...(row.dataset.id ? { id: row.dataset.id } : {}), ...fields });
  const request = { schema_version: "1.0", ...form.extra,
    actors: [...$("f-actors").querySelectorAll("input:checked")].map((box) => box.value), question: $("f-question").value };
  if ($("f-date").value) request.reference_date = $("f-date").value;
  request.scenario_assumptions = [...$("f-assumptions").children].filter((row) => value(row, ".f-description"))
    .map((row) => withId(row, { description: value(row, ".f-description") }));
  request.hard_constraints = [...$("f-constraints").children].filter((row) => value(row, ".f-description")).map((row) => {
    const item = withId(row, { description: value(row, ".f-description") });
    for (const [key, cls] of [["metric", ".f-metric"], ["operator", ".f-operator"], ["unit", ".f-unit"], ["baseline", ".f-baseline"], ["period", ".f-period"]]) {
      if (value(row, cls)) item[key] = value(row, cls);
    }
    if (value(row, ".f-value") !== "") item.value = Number(value(row, ".f-value"));
    return item;
  });
  return request;
}

/* The active tab is the source; the other one is refreshed when you switch. */
function setRequest(data) {
  fillForm(data);
  $("request").value = JSON.stringify(data, null, 2);
}

function currentRequest() {
  return form.tab === "form" ? readForm() : JSON.parse($("request").value);
}

function setTab(tab) {
  if (tab === form.tab) return;
  try {
    const data = currentRequest();
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("erwartet wird ein Objekt");
    setRequest(data);
  } catch (error) {
    notice(`Kein gültiges JSON: ${error.message}`);
    return;
  }
  notice("");
  form.tab = tab;
  for (const name of ["form", "json"]) {
    $(`tab-${name}`).setAttribute("aria-selected", String(name === tab));
    $(`panel-${name}`).hidden = name !== tab;
  }
}

async function loadFile(file) {
  if (file) loadText(await file.text());
}

const server = { available: false, scenarios: [] };

async function loadActors() {
  const response = await fetch("api/actors");
  if (!response.ok) return;
  const names = await response.json();
  if (names.length) {
    form.actors = names;
    view.actorNames = names;
    renderActors(readForm().actors);
  }
}

async function loadScenarios() {
  const response = await fetch("api/scenarios");
  if (!response.ok) return;
  server.scenarios = await response.json();
  $("scenario-field").hidden = !server.scenarios.length;
  $("scenario").append(...server.scenarios.map((s) => h("option", { value: s.id }, s.label)));
}

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
  openResult(await response.json(), true);
}

const PHASES = { deliberation: "Start", preparation: "Vorbereitung", review: "Prüfung", challenge: "Widersprüche", revision: "Überarbeitung",
  publication: "Veröffentlichung", negotiation: "Verhandlung", candidate: "Vorschlag", assessment: "Bewertung", freeze: "Einfrieren",
  finalization: "Abschluss" };

function showRun(running, message) {
  document.body.dataset.run = running ? "on" : "off";
  $("start").disabled = running || !server.available;
  notice(message);
}

function renderProgress(status) {
  const p = status.progress || {};
  const minutes = Math.floor(status.elapsed_seconds / 60);
  const seconds = String(Math.round(status.elapsed_seconds) % 60).padStart(2, "0");
  const facts = [`Laufzeit ${minutes}:${seconds}`, `Phase: ${PHASES[p.phase] || "Start"}`];
  if (p.round) facts.push(`Runde ${p.round}`);
  if (p.proposal) facts.push(`Fassung ${p.proposal.version}`);
  if (p.rejected) facts.push(`${p.rejected} Wiederholungen`);
  if (status.stopping) facts.push("Wird ausgewertet");
  $("stop-run").disabled = Boolean(status.stopping);
  $("stop-run").textContent = status.stopping ? "Wird ausgewertet" : "Stoppen und auswerten";
  $("run-facts").replaceChildren(...facts.map((fact) => h("li", null, fact)));
  const rounds = p.rounds || [];
  const actors = rounds.length ? rounds[rounds.length - 1].satisfaction.map((s) => s.actor_id) : [];
  const names = Object.fromEntries((view.actorNames || []).map((n) => [n.toLowerCase().replace("ä", "ae"), n]));
  $("run-rounds").replaceChildren(...(rounds.length ? [h("table", { class: "run-table" },
    h("thead", null, h("tr", null, text("th", "Runde"), actors.map((a) => text("th", names[a] || a)), text("th", "Fertig"))),
    h("tbody", null, rounds.map((r) => h("tr", null, text("td", r.round),
      r.satisfaction.map((s) => text("td", `${s.met_count} von ${s.priority_count}`)), text("td", r.complete ? "ja" : "nein")))))]
    : [text("p", "Noch keine Runde abgeschlossen.", "item-note")]));
  $("run-proposal").replaceChildren(...(p.proposal ? [text("p", `Aktueller Vorschlag, Fassung ${p.proposal.version}`, "run-title"),
    h("ol", null, p.proposal.clauses.map((clause) => text("li", clause)))] : []));
}

async function stopRun() {
  if (!confirm("Beratung jetzt beenden und auswerten? Der letzte vollständige Vorschlag wird eingefroren, das dauert noch einige Minuten.")) return;
  const response = await fetch("api/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  if (response.ok) $("stop-run").disabled = true;
  else notice((await response.json()).message);
}

async function cancelRun() {
  if (!confirm("debAIte wirklich abbrechen? Es gibt dann kein Endergebnis.")) return;
  const response = await fetch("api/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  if (!response.ok) notice((await response.json()).message);
}

async function pollRun() {
  const status = await (await fetch("api/run")).json();
  if (status.running) {
    showRun(true, "");
    renderProgress(status);
    setTimeout(pollRun, 3000);
    return;
  }
  showRun(false, status.cancelled ? "Die debAIte wurde abgebrochen." : "");
  await refreshResults();
  if (status.result) openResult(status.result, true);
}

async function startRun() {
  let body;
  try {
    body = JSON.stringify(currentRequest());
  } catch (error) {
    notice(`Kein gültiges JSON: ${error.message}`);
    return;
  }
  if (form.tab === "form") {
    const missing = [!$("f-question").value.trim() && "die Frage", !JSON.parse(body).actors.length && "mindestens eine Institution"].filter(Boolean);
    if (missing.length) { notice(`Bitte ergänze ${missing.join(" und ")}.`); return; }
  }
  if (!confirm("Eine debAIte verbraucht Azure-Kontingent und dauert bis zu 30 Minuten. Starten?")) return;
  const response = await fetch("api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body });
  const answer = await response.json();
  if (answer.result) { openResult(answer.result, false); return; }
  if (!response.ok) { notice(answer.message || "Der Start ist fehlgeschlagen."); return; }
  pollRun();
}

/* ---- wiring ---------------------------------------------------------------- */
$("prev").addEventListener("click", () => go(view.step - 1));
$("next").addEventListener("click", () => go(view.step + 1));
$("json-toggle").addEventListener("click", () => { view.json = !view.json; render(true); });
$("download").addEventListener("click", downloadResult);
$("home").addEventListener("click", closeChat);
$("chat-gruppe").addEventListener("click", () => selectChat("gruppe"));
$("chat-meta").addEventListener("click", () => selectChat("meta"));
$("thread-close").addEventListener("click", closeThread);
$("source-close").addEventListener("click", () => $("source").close());
$("source-full").addEventListener("click", openWholeDocument);
$("t-prev").addEventListener("click", () => seekThread(player.pos - 1));
$("t-next").addEventListener("click", () => seekThread(player.pos + 1));
$("t-play").addEventListener("click", togglePlay);
$("composer").addEventListener("submit", ask);
$("panel-form").addEventListener("submit", (event) => event.preventDefault());
$("start").addEventListener("click", startRun);
$("cancel-run").addEventListener("click", cancelRun);
$("stop-run").addEventListener("click", stopRun);
$("file").addEventListener("change", (event) => loadFile(event.target.files[0]));
$("open-pasted").addEventListener("click", () => loadText($("request").value));
$("scenario").addEventListener("change", (event) => {
  const scenario = server.scenarios.find((s) => s.id === event.target.value);
  setRequest(scenario ? scenario.request : SAMPLE);
  notice("");
});
$("tab-form").addEventListener("click", () => setTab("form"));
$("tab-json").addEventListener("click", () => setTab("json"));
$("add-assumption").addEventListener("click", () => $("f-assumptions").append(assumptionRow()));
$("add-constraint").addEventListener("click", () => $("f-constraints").append(constraintRow()));

document.addEventListener("keydown", (event) => {
  if (document.body.dataset.view !== "chat" || event.ctrlKey || event.metaKey || event.altKey) return;
  if ($("source").open) return;       // the dialog closes itself on Esc
  if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)) return;
  if (event.key === " " && event.target.tagName === "BUTTON") return;       // Space presses the focused button
  const inThread = Boolean(view.thread);
  const isGroup = currentChat().kind === "group";
  const actions = {
    ArrowRight: () => (inThread ? seekThread(player.pos + 1) : isGroup && go(view.step + 1)),
    ArrowLeft: () => (inThread ? seekThread(player.pos - 1) : isGroup && go(view.step - 1)),
    Home: () => isGroup && !inThread && go(0), End: () => isGroup && !inThread && go(view.data.steps.length - 1),
    " ": () => inThread && togglePlay(), j: () => isGroup && $("json-toggle").click(),
    Escape: () => (inThread ? closeThread() : closeChat()),
  };
  if (actions[event.key]) { event.preventDefault(); actions[event.key](); }
});

for (const type of ["dragover", "drop"]) {
  document.addEventListener(type, (event) => {
    event.preventDefault();
    if (type === "drop") loadFile(event.dataTransfer.files[0]);
  });
}

function memberButton(id, badge, name, role) {
  return h("li", null, h("button", { type: "button", class: "member", "data-member": id, onclick: () => showMember(id || null) },
    text("span", badge, `avatar ${id || "all"}`),
    h("span", { class: "member-text" }, text("span", name, "member-name"), text("span", role, "member-role"))));
}

$("council-members").replaceChildren(memberButton("", "Alle", "Alle", "ganzer Verlauf"),
  ...COUNCIL.map((member) => memberButton(member.id, member.initials, member.name, `prüft ${member.role}`)));
setRequest(SAMPLE);
refreshResults().then(async () => {
  if (!server.available) return;
  await Promise.all([loadScenarios(), loadActors()]);
  const requested = new URLSearchParams(location.search).get("debatte");
  if (requested) await openStored(requested);
  const status = await (await fetch("api/run")).json();
  if (status.running) pollRun();
});
