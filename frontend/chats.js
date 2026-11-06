/* Turns a debate result into the group chat, its threads and round steps. Pure data, no DOM, so it
   can be tested in Node. Every message comes from a stored record. The feed shows only what was
   published in the debate; the council's reviews sit in the thread of the message they concern. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.DebateChats = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const REVIEWERS = { truthseeker: "TruthSeeker", legaleye: "LegalEye", ethicmind: "EthicMind" };
  const COUNCIL = [{ id: "truthseeker", initials: "TS", name: "TruthSeeker", role: "Fakten" },
    { id: "legaleye", initials: "LE", name: "LegalEye", role: "Recht" },
    { id: "ethicmind", initials: "EM", name: "EthicMind", role: "Ethik" }];
  const STATUS = { met: "erfüllt", partly_met: "teilweise", unmet: "nicht erfüllt", unclear: "unklar" };
  const DECISION = { accept: "Annahme", conditional: "Bedingte Annahme", reject: "Ablehnung", withdraw: "Rückzug" };
  const NEGOTIATION = { offer: "Angebot", counteroffer: "Gegenangebot", concession: "Zugeständnis",
    condition: "Bedingung", disagreement: "Differenz" };
  const RESULT = { completed: "Abgeschlossen", incomplete: "Unvollständig", needs_clarification: "Rückfrage",
    invalid_input: "Ungültige Eingabe", failed: "Fehlgeschlagen" };
  const STOP = { satisfaction_threshold: "Schwelle in zwei Runden erreicht",
    deliberation_deadline: "Frist abgelaufen", operator_stop: "Von dir gestoppt", execution_error: "Ausführungsfehler" };
  const ADMISSIBILITY = { admissible_within_reviewed_sources: "Kein Konflikt in den geprüften Quellen",
    conditional: "Vertretbar unter Bedingungen", unresolved: "Rechtlich ungeklärt", inadmissible: "Unzulässig",
    not_reviewed: "Nicht geprüft" };
  const LEGAL = { not_reviewed: "nicht geprüft", no_conflict_identified: "kein Konflikt erkannt",
    defensible_with_conditions: "vertretbar mit Bedingungen", unresolved_issue: "ungeklärt",
    apparent_legal_conflict: "offenbarer Konflikt", requires_legal_amendment: "braucht Rechtsänderung" };
  const ASSESSMENT = { ...LEGAL, supported: "gestützt", qualified_support: "eingeschränkt gestützt",
    no_support: "nicht gestützt", contradiction: "Widerspruch", reasoning_problem: "Begründungsproblem",
    insufficient_information: "zu wenig Information", no_concern_identified: "kein Bedenken",
    concern_with_mitigation: "Bedenken mit Abhilfe", serious_concern: "ernstes Bedenken",
    conflict_of_principles: "Prinzipienkonflikt" };
  const STAGE = { initial: "Erste Prüfung", second: "Zweite Prüfung", candidate: "Klauselprüfung",
    assessment: "Prüfung der Bewertung", final: "Abschlussprüfung" };
  const REPLY = { uphold: "Befund bleibt", amend: "Befund geändert", withdraw: "Befund zurückgezogen" };
  const MODERATOR = "Du (Moderation)";

  const list = (value) => (Array.isArray(value) ? value : []);
  const cited = (values) => [...new Set(list(values).filter((v) => typeof v === "string" && v.startsWith("P-")))];
  const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;
  const memberOf = (reviewer) => COUNCIL.find((m) => m.id === reviewer);
  const byReviewer = (a, b) => COUNCIL.indexOf(memberOf(a.reviewer)) - COUNCIL.indexOf(memberOf(b.reviewer));
  const isFinal = (record) => /^(AS|D)-final/.test(record.assessment_id || record.decision_id || "");

  function roundNumbers(result) {
    const rounds = new Set();
    for (const records of [result.negotiation, result.proposal_history, result.priority_history, result.decisions]) {
      for (const record of list(records)) if (record.round >= 1) rounds.add(record.round);
    }
    return [...rounds].sort((a, b) => a - b);
  }

  function roundSatisfaction(result, round) {
    return list(result.actors).map((actor) => {
      const latest = {};
      for (const a of list(result.priority_history)) {
        if (a.round === round && a.actor_id === actor.actor_id && !isFinal(a)) latest[a.priority_id] = a;
      }
      const assessed = Object.values(latest);
      const total = list(actor.priorities).length;
      const met = assessed.filter((a) => a.status === "met").length;
      return { actor_id: actor.actor_id, name: actor.name, met, total, assessed,
        reached: total > 0 && assessed.length === total && 5 * met >= 4 * total };
    });
  }

  function messageResult(result) {
    const texts = [...list(result.clarification_questions).map((q) => q.frage), ...list(result.errors).map((e) => e.message)];
    const messages = [{ id: "status", step: 0, side: "system", text: RESULT[result.status] || "Ergebnis" },
      ...texts.map((text, i) => ({ id: `m-${i}`, step: 0, side: "in", sender: "System", role: "program", text, replies: [] }))];
    return { header: { question: null, reference_date: null, status: RESULT[result.status] || result.status, stop: null },
      steps: [{ id: "meldung", label: "Meldung", round: null }],
      chats: [{ id: "gruppe", name: "debAIte", kind: "group", subtitle: "Keine debAIte gestartet", messages }] };
  }

  function buildChats(result) {
    if (!result || typeof result !== "object") throw new Error("Das Ergebnis ist kein JSON-Objekt.");
    if (!result.task) return messageResult(result);

    const times = {};
    for (const event of list(result.timeline)) times[event.event_id] = event.at;
    const rounds = roundNumbers(result);
    const live = Boolean(result.live);
    const finishing = !live || Boolean((result.timing || {}).deliberation_ended_at);
    const steps = [{ id: "vorbereitung", label: "Vorbereitung", round: null },
      ...rounds.map((round) => ({ id: `runde-${round}`, label: `Runde ${round}`, round })),
      ...(finishing ? [{ id: "abschluss", label: "Abschluss", round: null }] : [])];
    const stepOf = (round) => Math.max(rounds.indexOf(round) + 1, 0);
    const last = steps.length - 1;
    const actors = list(result.actors);
    const reviews = list(result.reviews);
    const challenges = list(result.challenges);
    const nameOf = Object.fromEntries(actors.map((a) => [a.actor_id, a.name]));
    const objective = {};
    for (const actor of actors) for (const p of list(actor.priorities)) objective[p.priority_id] = p.objective;
    const findingById = {};
    for (const review of reviews) for (const f of list(review.findings)) findingById[f.finding_id] = f;

    let counter = 0;
    const message = (step, side, fields) => ({ id: `n${(counter += 1)}`, step, side, replies: [], ...fields });
    const fromActor = (actorId, extra) => ({ sender: nameOf[actorId] || actorId, senderId: actorId, role: "actor", ...extra });
    const fromReviewer = (reviewer, extra) => ({ sender: REVIEWERS[reviewer] || reviewer, senderId: reviewer, role: "reviewer",
      initials: memberOf(reviewer) ? memberOf(reviewer).initials : null, ...extra });
    const moderator = (extra) => ({ sender: MODERATOR, senderId: "moderation", role: "moderator", ...extra });
    const findingItems = (review) => list(review.findings).map((f) => ({
      title: ASSESSMENT[f.assessment] || f.assessment, text: f.reason, note: f.correction ? `Korrektur: ${f.correction}` : null,
      sources: cited([f.reference]) }));

    const group = [];

    const clean = "Keine Befunde.";
    function reviewMessage(step, review, tag) {
      const note = review.status !== "complete" ? "Prüfung nicht abgeschlossen."
        : review.coverage !== "complete" ? review.coverage_note : null;
      const findings = list(review.findings);
      return message(step, "in", fromReviewer(review.reviewer, { tag, items: findingItems(review), note,
        text: !findings.length && !note ? clean : null, time: times[review.review_id] }));
    }

    /* A challenge and its answer, in the thread they belong to. */
    function challengeMessages(step, records) {
      const out = [];
      for (const c of records) {
        const finding = findingById[c.finding_id];
        out.push(message(step, "in", fromActor(c.actor_id, { tag: "Widerspruch", text: c.reason, about: finding ? finding.reviewer : null,
          quote: finding ? finding.reason : null, time: times[c.challenge_id] })));
        if (c.reply_decision && finding) {
          out.push(message(step, "in", fromReviewer(finding.reviewer, { tag: REPLY[c.reply_decision] || c.reply_decision,
            text: c.reply_reason, time: times[`antwort:${c.challenge_id}`] })));
        }
      }
      return out;
    }

    /* The last message of a thread: counted from the records, so playback has a real end. */
    function conclusionText(reviewed, answered) {
      const done = reviewed.filter((r) => r.status === "complete").length;
      const findings = reviewed.reduce((sum, r) => sum + list(r.findings).length, 0);
      const parts = [`${done} von ${reviewed.length} Prüfungen abgeschlossen`, plural(findings, "Befund", "Befunde")];
      if (answered.length) {
        const outcomes = {};
        for (const c of answered) {
          const label = REPLY[c.reply_decision] || "unbeantwortet";
          outcomes[label] = (outcomes[label] || 0) + 1;
        }
        const summary = Object.entries(outcomes).map(([label, count]) => `${count}× ${label}`).join(", ");
        parts.push(`${plural(answered.length, "Widerspruch", "Widersprüche")} (${summary})`);
      } else {
        parts.push("kein Widerspruch");
      }
      return `Ergebnis: ${parts.join(", ")}.`;
    }

    function conclusion(step, reviewed, answered) {
      const perMember = {};
      for (const member of COUNCIL) {
        const own = reviewed.filter((r) => r.reviewer === member.id);
        const theirs = answered.filter((c) => findingById[c.finding_id] && findingById[c.finding_id].reviewer === member.id);
        if (own.length || theirs.length) perMember[member.id] = conclusionText(own, theirs);
      }
      const named = (text) => (live ? text.replace("Ergebnis:", "Zwischenstand:") : text);
      for (const id of Object.keys(perMember)) perMember[id] = named(perMember[id]);
      return message(step, "system", { conclusion: true, perMember, text: named(conclusionText(reviewed, answered)) });
    }

    const assessmentItems = (assessed) => assessed.map((a) => ({
      status: a.status, title: objective[a.priority_id] || a.priority_id, text: a.reason }));
    const ownChallenges = (filter) => challenges.filter((c) => findingById[c.finding_id] && filter(c));

    /* An institution's confidential preparation: priorities, both contributions and every review. */
    function preparationThread(actor, step) {
      const aid = actor.actor_id;
      const versions = list(result.arguments).filter((a) => a.actor_id === aid).sort((a, b) => a.version - b.version);
      const thread = [];
      const reviewed = [];
      const answered = [];
      if (list(actor.priorities).length) {
        thread.push(message(step, "in", fromActor(aid, { tag: "Prioritäten",
          items: actor.priorities.map((p) => ({ text: p.objective, note: `Bedingung: ${p.acceptance_condition}`, sources: cited(p.quellen) })) })));
      }
      for (const [version, stage] of [[1, "initial"], [2, "second"]]) {
        const argument = versions.find((a) => a.version === version);     // a failed revision has none
        if (argument) {
          thread.push(message(step, "in", fromActor(aid, { tag: version === 1 ? "Erster Beitrag" : "Überarbeiteter Beitrag",
            items: list(argument.claims).map((c) => ({ text: c.behauptung, note: c.begruendung, sources: cited(c.quellen) })),
            note: argument.change_explanation, time: times[argument.argument_id] })));
        }
        const stageReviews = reviews.filter((r) => r.actor_id === aid && r.stage === stage).sort(byReviewer);
        thread.push(...stageReviews.map((review) => reviewMessage(step, review, `${STAGE[stage]}, vertraulich`)));
        const own = ownChallenges((c) => c.actor_id === aid && c.stage === stage);
        thread.push(...challengeMessages(step, own));
        reviewed.push(...stageReviews);
        answered.push(...own);
      }
      if (reviewed.length || answered.length) thread.push(conclusion(step, reviewed, answered));
      return thread;
    }

    function standpointPost(actor, step, tag) {
      const thread = preparationThread(actor, step);
      if (!actor.profile && !thread.length) return null;
      const inferred = actor.profile && actor.profile.standpunkt_label === "inferred";
      const entries = list(actor.profile && actor.profile.entries).flatMap((entry) => list(entry.quellen));
      const post = message(step, "in", fromActor(actor.actor_id, { tag, text: actor.profile ? actor.profile.standpunkt : null, sources: cited(entries),
        note: !actor.profile ? "Kein Profil vorhanden." : inferred ? "Erschlossen, keine offizielle Erklärung." : null }));
      post.replies = thread;
      return post;
    }

    /* Preparation is private: the group sees only that it is happening. */
    group.push(message(0, "out", moderator({ tag: "Frage", text: result.task.question })));
    group.push(message(0, "system", { text: `Regelwerk ${result.task.rulebook_version}. Stichtag ${result.task.reference_date || "unbekannt"}.` }));
    group.push(message(0, "system", { text: result.task.legal_scope }));
    group.push(message(0, "system", { text: "Alle bereiten sich getrennt vor. Die vertraulichen Prüfungen stehen danach im Thread der jeweiligen Institution." }));

    /* Rounds */
    rounds.forEach((round) => {
      const step = stepOf(round);
      if (round === rounds[0]) {
        group.push(message(step, "system", { text: "Gemeinsames Paket veröffentlicht. Alle sehen jetzt denselben Stand." }));
        for (const actor of actors) {
          const post = standpointPost(actor, step, "Standpunkt");
          if (post) group.push(post);
        }
      }
      for (const actor of actors) {
        const items = list(result.negotiation).filter((n) => n.round === round && n.actor_id === actor.actor_id);
        if (items.length) {
          group.push(message(step, "in", fromActor(actor.actor_id, { tag: "Verhandlung", time: times[items[0].item_id],
            items: items.map((n) => ({ title: NEGOTIATION[n.kind] || n.kind, text: n.text, sources: cited(n.quellen) })) })));
        }
      }
      const proposals = list(result.proposal_history).filter((p) => p.round === round).sort((a, b) => a.version - b.version);
      const open = live && round === rounds[rounds.length - 1];      // its proposal may still come
      if (!proposals.length && !open) group.push(message(step, "system", { text: "Keine neue Fassung in dieser Runde." }));
      for (const proposal of proposals) {
        const post = message(step, "out", moderator({ tag: `Vorschlag, Fassung ${proposal.version}`, time: proposal.created_at,
          items: list(proposal.clauses).map((c, i) => ({ number: i + 1, text: c.text, note: `Rechtlich: ${LEGAL[c.legal_status] || c.legal_status}`, sources: cited(c.premises) })),
          note: ADMISSIBILITY[proposal.legal_admissibility] || null }));
        const reviewed = reviews.filter((r) => r.stage === "candidate" && r.round === round && r.review_id.includes(`-v${proposal.version}-`)).sort(byReviewer);
        const allAnswered = [];
        for (const review of reviewed) {
          post.replies.push(reviewMessage(step, review, STAGE.candidate));
          const answered = challenges.filter((c) => c.stage === "candidate" && c.round === round
            && list(review.findings).some((f) => f.finding_id === c.finding_id));
          post.replies.push(...challengeMessages(step, answered));
          allAnswered.push(...answered);
        }
        if (reviewed.length) post.replies.push(conclusion(step, reviewed, allAnswered));
        group.push(post);
        if (proposal.meta_update) group.push(message(step, "out", moderator({ text: proposal.meta_update })));
      }

      const satisfaction = roundSatisfaction(result, round);
      for (const s of satisfaction) {
        const decisions = list(result.decisions).filter((d) => d.round === round && d.actor_id === s.actor_id && !isFinal(d));
        const stageReviews = reviews.filter((r) => r.stage === "assessment" && r.round === round && r.actor_id === s.actor_id).sort(byReviewer);
        const own = ownChallenges((c) => c.stage === "assessment" && c.round === round && c.actor_id === s.actor_id);
        let anchor = null;
        for (const decision of decisions) {
          anchor = message(step, "in", fromActor(s.actor_id, { tag: `${DECISION[decision.kind] || decision.kind}: ${s.met} von ${s.total} erfüllt`,
            text: decision.reason, items: list(decision.conditions).map((c) => ({ text: c })), time: decision.at }));
          group.push(anchor);
        }
        if (!anchor && stageReviews.length) {
          anchor = message(step, "in", fromActor(s.actor_id, { tag: "Keine gültige Bewertung in dieser Runde" }));
          group.push(anchor);
        }
        if (anchor) {
          if (s.assessed.length) {
            anchor.replies.push(message(step, "in", fromActor(s.actor_id, { tag: "Bewertung je Priorität", items: assessmentItems(s.assessed) })));
          }
          anchor.replies.push(...stageReviews.map((review) => reviewMessage(step, review, `${STAGE.assessment}, vertraulich`)));
          anchor.replies.push(...challengeMessages(step, own));
          if (stageReviews.length || own.length) anchor.replies.push(conclusion(step, stageReviews, own));
        }
      }
      if (satisfaction.some((s) => s.assessed.length)) {
        group.push(message(step, "system", { text: satisfaction.map((s) => `${s.name}: ${s.met} von ${s.total}`).join(" · ") }));
      }
    });

    /* A debate that ended before publication still shows what each institution prepared. */
    if (!rounds.length) {
      for (const actor of actors) {
        const post = standpointPost(actor, finishing ? last : 0, finishing ? "Vorbereitung, nicht veröffentlicht" : "Vorbereitung, läuft");
        if (post) group.push(post);
      }
    }

    /* Final phase */
    const finalPhase = () => {
    const report = result.report || {};
    const consensus = report.Konsens || {};
    const frozen = result.final_proposal;
    group.push(message(last, "system", { text: `Beratung beendet: ${STOP[result.stop_reason] || "ohne Stoppgrund"}.` }));
    for (const actor of actors) {
      if (actor.final_standpoint) {
        group.push(message(last, "in", fromActor(actor.actor_id, { tag: "Abschließende Stellungnahme", text: actor.final_standpoint, time: times[`abschluss:${actor.actor_id}`] })));
      }
    }
    const finalReviews = reviews.filter((r) => r.stage === "final");
    const finalPost = frozen
      ? message(last, "out", moderator({ tag: `Eingefrorener Vorschlag, Fassung ${frozen.version}`,
        items: list(frozen.clauses).map((c, i) => ({ number: i + 1, text: c.text, note: `Rechtlich: ${LEGAL[c.legal_status] || c.legal_status}`, sources: cited(c.premises) })),
        note: ADMISSIBILITY[frozen.legal_admissibility] || null }))
      : message(last, "system", { text: live ? "Der Vorschlag wird eingefroren …" : "Kein vollständiger Vorschlag. Es wird keine Einigung ausgewiesen." });
    group.push(finalPost);
    for (const review of finalReviews) {
      (frozen ? finalPost.replies : group).push(reviewMessage(last, review, STAGE.final));
    }
    if (frozen && finalReviews.length) finalPost.replies.push(conclusion(last, finalReviews, []));
    if (consensus.einschaetzung) group.push(message(last, "out", moderator({ tag: "Konsens", text: consensus.einschaetzung })));
    const section = (tag, records, line) => {
      if (records.length) group.push(message(last, "out", moderator({ tag, items: records.map(line) })));
    };
    section("Konflikte", list(report.Konflikte), (c) => ({ title: c.thema, text: c.beschreibung }));
    section("Kompromisse", list(report.Kompromisse), (c) => ({ text: c.beschreibung }));
    section("Offene Punkte", list(report.Offene_Punkte), (p) => ({ text: p.punkt }));
    section("Auswirkungen", list(report.Auswirkungen), (i) => ({ text: i.beschreibung }));
    const narrative = (result.debate_evolution || {}).narrative;
    if (narrative) group.push(message(last, "out", moderator({ tag: "So entwickelte sich die debAIte", text: narrative.replace(/\*\*/g, "") })));
    if (!live) for (const missing of list(report.fehlende_abschnitte)) group.push(message(last, "system", { text: `Fehlt: ${missing}` }));
    };
    if (finishing) finalPhase();

    const chats = [{ id: "gruppe", name: "debAIte", kind: "group",
      subtitle: `Du, ${actors.map((a) => a.name).join(", ")}`, messages: group },
    { id: "meta", name: "Meta-Moderator", kind: "meta", subtitle: "Beantwortet deine Fragen aus dem Protokoll", messages: [] }];
    return {
      header: { question: result.task.question, reference_date: result.task.reference_date,
        status: RESULT[result.status] || result.status, stop: STOP[result.stop_reason] || null },
      steps, chats, sources: Object.fromEntries(list(result.sources).map((s) => [s.passage_id, s])),
      satisfactionAt(stepIndex) {
        const step = steps[stepIndex];
        if (step && step.round) return roundSatisfaction(result, step.round);
        return stepIndex === last ? actors.map((a) => ({ name: a.name, ...(a.satisfaction
          ? { met: a.satisfaction.met_count, total: a.satisfaction.priority_count } : { met: null, total: null }) })) : [];
      },
      proposalAt(stepIndex) {
        const shown = list(result.proposal_history).filter((p) => stepOf(p.round) <= stepIndex).sort((a, b) => a.version - b.version);
        return shown.length ? shown[shown.length - 1] : null;
      },
    };
  }

  /* A thread, or within it the chat with one council member: the author's own messages, that
     member's reviews and every challenge to that member's findings. */
  function memberChat(parent, member) {
    const all = [parent, ...list(parent.replies)];
    if (!member) return all;
    const chat = [];
    for (const m of all) {
      if (m.conclusion) {
        if (m.perMember[member]) chat.push({ ...m, text: m.perMember[member] });
      } else if (m === parent || m.senderId === member || m.about === member || (m.senderId === parent.senderId && !m.about)) {
        chat.push(m);
      }
    }
    return chat;
  }

  /* Where a stored passage sits in a document, as { start, end } or null. Only letters and digits
     are compared: legal passages come from the XML, the document text from the import, and the two
     differ in spacing and in signs such as a dash inside "G-ZG". */
  function findPassage(full, passage) {
    const isWord = /[\p{L}\p{N}]/u;
    const wanted = passage.replace(/[^\p{L}\p{N}]/gu, "");
    if (!wanted) return null;
    const positions = [];
    const letters = [];
    for (let i = 0; i < full.length; i += 1) {
      if (isWord.test(full[i])) {
        positions.push(i);
        letters.push(full[i]);
      }
    }
    const at = letters.join("").indexOf(wanted);
    return at < 0 ? null : { start: positions[at], end: positions[at + wanted.length - 1] + 1 };
  }

  /* Messages of a chat up to and including a step: "Weiter" appends the next round. */
  const visible = (chat, stepIndex) => chat.messages.filter((m) => m.step <= stepIndex);

  return { buildChats, visible, memberChat, findPassage, council: COUNCIL, labels: { STATUS, RESULT, ADMISSIBILITY } };
});
