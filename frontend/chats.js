/* Turns a debate result into the group chat, its threads and round steps, and into the closing
   report. Pure data, no DOM, so it can be tested in Node. Every message comes from a stored record.
   The feed shows only what was published in the debate; the council's reviews sit in the thread of
   the message they concern. */
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
      chats: [{ id: "gruppe", name: "debAIte", kind: "group", subtitle: "Keine Debatte gestartet", messages }] };
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

    /* A round without a version: nothing could be compared with one's own position, so there is no new
       assessment and the consensus of the version still in force stands. */
    const inForce = (round) => list(result.proposal_history).filter((p) => p.round < round).sort((a, b) => b.version - a.version)[0] || null;
    const noProposalText = (round) => {
      const before = inForce(round);
      return before
        ? `Keine neue Fassung in dieser Runde: Der Vorschlag kam nicht gültig zustande. Es gilt weiter Fassung ${before.version}, `
          + "und ohne neue Fassung gibt es nichts neu zu bewerten. Der Konsens bleibt auf dem Stand der Vorrunde."
        : "Keine neue Fassung in dieser Runde: Der Vorschlag kam nicht gültig zustande. Es gibt noch keine Fassung, also auch nichts zu bewerten.";
    };
    const hasProposal = (round) => list(result.proposal_history).some((p) => p.round === round);

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
      const declared = list(result.timeline).some((e) => e.type === "no_proposal" && e.round === round);
      const open = live && round === rounds[rounds.length - 1] && !declared;      // its proposal may still come
      if (!proposals.length && !open) group.push(message(step, "system", { text: noProposalText(round) }));
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
    if (narrative) group.push(message(last, "out", moderator({ tag: "So entwickelte sich die Debatte", text: narrative.replace(/\*\*/g, "") })));
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
        if (step && step.round) {
          // A round without a version keeps the numbers of the last round that assessed one.
          const shown = hasProposal(step.round) ? step.round : [...rounds].reverse().find((r) => r < step.round && hasProposal(r));
          return roundSatisfaction(result, shown || step.round);
        }
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

  /* ---- closing report ------------------------------------------------------------------------
     A traffic light for every clause of the final version and every stakeholder. It follows fixed
     rules over stored records, no model is asked, so the same result always gives the same colours
     and the rules can be printed in the report itself (REPORT_RULES). */
  const LIGHT = { gruen: { name: "Grün", title: "Priorität, weil unstrittig" },
    gelb: { name: "Gelb", title: "Auf gutem Weg, noch Bedarf" },
    rot: { name: "Rot", title: "Sehr diskussionswürdig" } };
  const LIGHT_ORDER = ["gruen", "gelb", "rot"];
  const OPEN_KIND = { legal: "Recht", evidence: "Belege", ethical: "Ethik", negotiation: "Verhandlung", data: "Daten" };
  const HARD_FINDINGS = new Set(["apparent_legal_conflict", "requires_legal_amendment", "no_support", "contradiction",
    "reasoning_problem", "serious_concern", "conflict_of_principles"]);
  const RED_FLAGS = 4;        // this many open issues on one clause make it red
  const REPORT_RULES = [
    ["Punkt der Schlussfassung", "Grün, wenn alle verknüpften Prioritäten erfüllt sind und keine Frage offen ist. Gelb ab einer offenen Frage. "
      + `Rot ab ${RED_FLAGS} offenen Fragen, bei einer nicht erfüllten oder unklaren Priorität oder wenn der Rat einen Konflikt, ein ernstes Bedenken oder fehlende Stützung meldet. `
      + "Als offene Frage zählen eine ungeklärte Rechtslage, zu wenig Information, jeder offene Punkt aus dem Schlussbericht und jeder Stakeholder mit nur teilweise erfüllter Priorität."],
    ["Priorität", "Grün, wenn erfüllt. Gelb, wenn teilweise erfüllt, erst zuletzt erfüllt oder an einem roten Punkt hängend. Rot, wenn nicht erfüllt oder unklar."],
    ["Stakeholder", "Grün, wenn mindestens 80 % der Prioritäten grün sind und keine unklar ist. Rot bei Ablehnung, Rückzug, einer nicht erfüllten Priorität oder wenn weniger als die Hälfte erfüllt ist. Sonst gelb."],
    ["Gesamtkonsens", "Grün nur, wenn alle uneingeschränkt annehmen, die Rechtslage geklärt ist und kein Punkt rot ist. Rot bei Ablehnung, ohne Schlussfassung oder wenn die Hälfte der Punkte rot ist oder die Hälfte der Stakeholder unter der Schwelle liegt. Sonst gelb."],
  ];

  /* The assessments of the version under review. Not latest_assessments: when the deadline falls
     inside a round that holds a newer, unreviewed version, and that newer version is not the result. */
  function assessmentsFor(result, version) {
    const ofVersion = list(result.priority_history).filter((a) => a.proposal_version === version);
    const closing = ofVersion.filter((a) => String(a.assessment_id).startsWith("AS-final-"));
    const chosen = closing.length ? closing : ofVersion;
    if (!chosen.length) return list(result.actors).flatMap((a) => list(a.latest_assessments));
    const newest = new Map();
    for (const a of chosen) {
      const seen = newest.get(a.priority_id);
      if (!seen || a.round >= seen.round) newest.set(a.priority_id, a);
    }
    return [...newest.values()];
  }

  function buildReport(result, interim = false) {
    const history = list(result.proposal_history);
    const newest = [...history].sort((a, b) => b.version - a.version)[0] || null;
    const proposal = result.final_proposal || newest;
    const clauses = proposal ? list(proposal.clauses) : [];
    const numbers = new Map(clauses.map((c, i) => [c.clause_id, i + 1]));
    const readable = (value) => String(value || "")
      .replace(/\s*\[[^\]]*\b(?:KL|D|NEG|F|P|PR|AS|CH)-[^\]]*\]/g, "")
      .replace(/KL-v\d+-\d+/g, (id) => (numbers.has(id) ? `Punkt ${numbers.get(id)}` : "eine frühere Fassung")).trim();
    const assessed = proposal ? assessmentsFor(result, proposal.version) : list(result.actors).flatMap((a) => list(a.latest_assessments));
    const actorName = (id) => (list(result.actors).find((a) => a.actor_id === id) || {}).name || id;
    const withdrawn = new Set(list(result.challenges).filter((c) => c.reply_decision === "withdraw").map((c) => c.finding_id));
    const reviewed = list(result.reviews).flatMap((r) => list(r.findings).map((f) => ({ ...f, stage: r.stage })))
      .filter((f) => numbers.has(f.target_id) && !withdrawn.has(f.finding_id));
    const findings = reviewed.some((f) => f.stage === "final") ? reviewed.filter((f) => f.stage === "final") : reviewed;
    const report = result.report || {};
    const openPoints = list(report.Offene_Punkte);

    const reportClauses = clauses.map((clause) => {
      const mine = findings.filter((f) => f.target_id === clause.clause_id);
      const linked = assessed.filter((a) => list(a.references).includes(clause.clause_id));
      const open = openPoints.filter((p) => list(p.refs).includes(clause.clause_id));
      const reasons = [];
      let flags = 0;
      let hard = false;
      for (const f of mine) {
        if (HARD_FINDINGS.has(f.assessment)) {
          hard = true;
          reasons.push(`${REVIEWERS[f.reviewer] || f.reviewer}: ${ASSESSMENT[f.assessment]}. ${readable(f.reason)}`);
        }
      }
      const unresolved = mine.find((f) => f.assessment === "unresolved_issue");
      if (unresolved || clause.legal_status === "unresolved_issue") {
        flags += 1;
        reasons.push(`Rechtslage ungeklärt${unresolved ? `: ${readable(unresolved.reason)}` : "."}`);
      }
      const thin = mine.find((f) => f.assessment === "insufficient_information");
      if (thin) {
        flags += 1;
        reasons.push(`${REVIEWERS[thin.reviewer] || thin.reviewer}, zu wenig Information: ${readable(thin.reason)}`);
      }
      for (const point of open) {
        flags += 1;
        reasons.push(`Offener Punkt (${OPEN_KIND[point.art] || point.art}): ${readable(point.punkt)}`);
      }
      const lacking = linked.filter((a) => a.status !== "met");
      const missed = lacking.filter((a) => a.status !== "partly_met");
      for (const a of missed) {
        hard = true;
        reasons.push(`${actorName(a.actor_id)}: Priorität ${STATUS[a.status] || a.status}. ${readable(a.reason)}`);
      }
      const partly = [...new Set(lacking.filter((a) => a.status === "partly_met").map((a) => a.actor_id))];
      flags += partly.length;
      for (const id of partly) {
        const first = lacking.find((a) => a.actor_id === id && a.status === "partly_met");
        reasons.push(`${actorName(id)}: Priorität nur teilweise erfüllt. ${readable(first.reason)}`);
      }
      const light = hard || flags >= RED_FLAGS ? "rot" : flags ? "gelb" : "gruen";
      if (!reasons.length) reasons.push(linked.length ? "Alle verknüpften Prioritäten sind erfüllt, und der Rat meldet keine offene Frage."
        : "Keine Priorität ist direkt verknüpft, und der Rat meldet keine offene Frage.");
      const perActor = {};
      for (const actor of list(result.actors)) {
        const own = linked.filter((a) => a.actor_id === actor.actor_id);
        perActor[actor.actor_id] = !own.length ? null
          : own.some((a) => a.status === "unmet" || a.status === "unclear") ? "rot"
            : own.some((a) => a.status === "partly_met") ? "gelb" : "gruen";
      }
      const firstSentence = String(clause.text || "").split(/(?<=[.;:])\s/)[0].replace(/[.;:]$/, "");
      return { id: clause.clause_id, number: numbers.get(clause.clause_id), light, reasons, perActor,
        title: firstSentence.length > 96 ? `${firstSentence.slice(0, 95).trimEnd()} …` : firstSentence,
        text: readable(clause.text),
        checks: mine.map((f) => `${REVIEWERS[f.reviewer] || f.reviewer}: ${ASSESSMENT[f.assessment] || f.assessment}`) };
    });

    const clauseLight = new Map(reportClauses.map((c) => [c.id, c.light]));
    const stakeholders = list(result.actors).map((actor) => {
      const decisions = list(actor.acceptance_decisions);
      const closing = decisions.filter((d) => String(d.decision_id).startsWith("D-final-") && (!proposal || d.target_version === proposal.version));
      const decision = closing[closing.length - 1] || decisions[decisions.length - 1] || null;
      const priorities = list(actor.priorities).map((priority) => {
        const a = assessed.find((x) => x.priority_id === priority.priority_id) || null;
        const status = a ? a.status : "unclear";
        const clauseRefs = a ? list(a.references).filter((r) => numbers.has(r)) : [];
        const onRed = clauseRefs.filter((r) => clauseLight.get(r) === "rot").map((r) => numbers.get(r));
        const late = status === "met" && a.previous_status && a.previous_status !== "met";
        const light = status === "unmet" || status === "unclear" ? "rot" : status === "partly_met" || late || onRed.length ? "gelb" : "gruen";
        const notes = [late ? "Erst in der Schlussbewertung erfüllt." : null,
          onRed.length ? `Hängt an Punkt ${onRed.join(", ")}, der rot ist.` : null].filter(Boolean);
        return { id: priority.priority_id, light, status: STATUS[status] || status, objective: readable(priority.objective),
          reason: a ? readable(a.reason) : "Keine Bewertung in dieser Fassung.", notes,
          clauses: clauseRefs.map((r) => numbers.get(r)).sort((x, y) => x - y) };
      }).sort((x, y) => LIGHT_ORDER.indexOf(x.light) - LIGHT_ORDER.indexOf(y.light));
      const total = priorities.length;
      const met = priorities.filter((p) => p.status === STATUS.met).length;
      const percent = total ? Math.round((1000 * met) / total) / 10 : null;
      const reached = total > 0 && 5 * met >= 4 * total;
      const refused = decision && (decision.kind === "reject" || decision.kind === "withdraw");
      const anyMissed = priorities.some((p) => p.status === STATUS.unmet);
      const anyUnclear = priorities.some((p) => p.status === STATUS.unclear);
      const settled = priorities.filter((p) => p.light === "gruen").length;
      const light = refused || anyMissed || percent === null || percent < 50 ? "rot"
        : 5 * settled >= 4 * total && !anyUnclear ? "gruen" : "gelb";
      return { id: actor.actor_id, name: actor.name, light, met, total, percent, reached, priorities,
        decision: decision ? DECISION[decision.kind] || decision.kind : "Keine Entscheidung", decisionKind: decision ? decision.kind : null,
        conditions: decision ? list(decision.conditions).map(readable) : [], standpoint: readable(actor.final_standpoint),
        reason: decision ? readable(decision.reason) : "" };
    });

    const count = (items, key) => items.filter((i) => i.light === key).length;
    const refusals = stakeholders.filter((s) => s.decisionKind === "reject" || s.decisionKind === "withdraw").length;
    const below = stakeholders.filter((s) => !s.reached).length;
    const allAccept = stakeholders.length > 0 && stakeholders.every((s) => s.decisionKind === "accept");
    const admissibility = proposal ? proposal.legal_admissibility : null;
    const redClauses = count(reportClauses, "rot");
    const frozen = Boolean(result.final_proposal);
    const overall = refusals || (!frozen && !interim) || !reportClauses.length || 2 * redClauses >= reportClauses.length
      || 2 * below >= stakeholders.length ? "rot"
      : allAccept && !redClauses && admissibility === "admissible_within_reviewed_sources" ? "gruen" : "gelb";
    const kinds = ["accept", "conditional", "reject", "withdraw"].map((k) => [DECISION[k], stakeholders.filter((s) => s.decisionKind === k).length])
      .filter(([, n]) => n).map(([name, n]) => `${name}: ${n}`).join(", ");
    const facts = [`${stakeholders.length - below} von ${stakeholders.length} Stakeholdern erreichen ${interim ? "in diesem Stand" : "in der Schlussbewertung"} die Schwelle von 80 %.`,
      kinds ? `Schlussentscheidungen: ${kinds}.` : null,
      admissibility ? `Rechtslage: ${ADMISSIBILITY[admissibility] || admissibility}.` : null,
      STOP[result.stop_reason] ? `Ende der Debatte: ${STOP[result.stop_reason]}.` : null].filter(Boolean);
    const consent = report.Konsens || {};
    const clauseIds = new Set(numbers.keys());
    return {
      debateId: result.debate_id, question: result.task ? result.task.question : "", referenceDate: result.task ? result.task.reference_date : null,
      status: RESULT[result.status] || result.status, version: proposal ? proposal.version : null, frozen,
      overall: { light: overall, facts, assessment: readable(consent.einschaetzung), conditions: list(consent.auflagen).map(readable) },
      counts: Object.fromEntries(LIGHT_ORDER.map((k) => [k, count(reportClauses, k)])),
      clauses: reportClauses, stakeholders,
      moreOpen: openPoints.filter((p) => !list(p.refs).some((r) => clauseIds.has(r)))
        .map((p) => ({ kind: OPEN_KIND[p.art] || p.art, text: readable(p.punkt) })),
    };
  }

  /* ---- plenary table ---------------------------------------------------------------------------
     The same rules as the report, applied to the debate as it stood after one round: the version in
     force then and the assessments made of it by then. A round without a version keeps the previous one. */
  function snapshot(result, round) {
    const upTo = (record) => record.round <= round && !isFinal(record);
    const assessments = list(result.priority_history).filter(upTo);
    const judged = Math.max(0, ...assessments.map((a) => a.proposal_version));
    // A version nobody has assessed yet says nothing about consensus: the last assessed one stands.
    return { ...result, final_proposal: null, report: null,
      proposal_history: list(result.proposal_history).filter((p) => p.round <= round && (!judged || p.version <= judged)),
      priority_history: assessments,
      reviews: list(result.reviews).filter((r) => r.round <= round && r.stage !== "final"),
      challenges: list(result.challenges).filter((c) => c.round <= round && c.stage !== "final"),
      actors: list(result.actors).map((a) => ({ ...a, final_standpoint: null, latest_assessments: [],
        acceptance_decisions: list(a.acceptance_decisions).filter(upTo) })) };
  }

  /* round null: the closing state, as in the report. */
  function buildPlenum(result, round) {
    const r = round ? buildReport(snapshot(result, round), true) : buildReport(result);
    const own = round ? list(result.proposal_history).some((p) => p.round === round) : true;
    const newest = Math.max(0, ...list(result.proposal_history).filter((p) => !round || p.round <= round).map((p) => p.version));
    const assessed = list(result.priority_history).some((a) => (!round || (a.round <= round && !isFinal(a))) && a.proposal_version === r.version);
    return { round, final: !round, version: r.version, noProposal: Boolean(round) && !own,
      pending: newest && newest !== r.version ? newest : null,          // newer, not yet assessed
      unassessed: Boolean(round) && r.version !== null && !assessed,
      overall: r.overall, counts: r.counts,
      clauses: r.clauses.map((c) => ({ id: c.id, number: c.number, title: c.title, text: c.text, light: c.light,
        perActor: c.perActor, reasons: c.reasons })),
      stakeholders: r.stakeholders.map((s) => ({ id: s.id, name: s.name, light: s.light, met: s.met, total: s.total,
        percent: s.percent, decision: s.decision, decisionKind: s.decisionKind, reason: s.reason })) };
  }

  /* The report as a plain tree: the page turns it into DOM nodes and the export into escaped HTML,
     so both show the same thing and neither can run generated text. */
  const reportNode = (tag, cls, ...kids) => ({ tag, cls, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false && k !== "") });

  function reportTree(r) {
    const n = reportNode;
    const lamp = (key) => n("span", `lamp ${key}`, LIGHT[key].name);
    const clauseCard = (c) => n("section", "report-card", n("div", "report-card-head", n("h4", null, `Punkt ${c.number}`), lamp(c.light)),
      n("p", null, c.text),
      n("ul", "report-reasons", c.reasons.map((reason) => n("li", null, reason))),
      c.checks.length ? n("p", "report-note", `Rat: ${c.checks.join(" · ")}`) : null);
    const priorityRow = (p) => n("li", "report-priority", n("div", "report-card-head", n("strong", null, p.objective), lamp(p.light)),
      n("p", null, `Status: ${p.status}. ${p.reason}`),
      p.notes.length ? n("p", "report-note", p.notes.join(" ")) : null,
      p.clauses.length ? n("p", "report-note", `Bezug: Punkt ${p.clauses.join(", ")}`) : null);
    const percent = (s) => (s.percent === null ? "keine Prioritäten" : `${String(s.percent).replace(".", ",")} % erfüllt (${s.met} von ${s.total})`);
    return n("article", "report",
      n("header", "report-head", n("p", "report-label", "Gesamt-Report"), n("h2", null, "Konsens und Stakeholder im Überblick"),
        n("p", null, r.question),
        n("p", "report-note", [r.referenceDate ? `Stichtag ${r.referenceDate}` : null, r.status,
          r.version ? `Fassung ${r.version}` : null, `Debatte ${String(r.debateId).slice(0, 8)}`].filter(Boolean).join(" · "))),
      n("section", "report-legend", LIGHT_ORDER.map((key) => n("p", null, lamp(key), ` ${LIGHT[key].title}`))),
      !r.frozen ? n("p", "report-warning", r.version ? `Keine eingefrorene Schlussfassung. Gezeigt wird Fassung ${r.version} ohne abschließende Prüfung.`
        : "Es liegt keine Fassung vor, deshalb gibt es keine Punkte zu bewerten.") : null,
      n("section", "report-section", n("div", "report-card-head", n("h3", null, "Gesamtkonsens"), lamp(r.overall.light)),
        n("p", "report-title", LIGHT[r.overall.light].title),
        n("ul", "report-reasons", r.overall.facts.map((fact) => n("li", null, fact))),
        n("ul", "report-counts", LIGHT_ORDER.map((key) => n("li", null, lamp(key), ` ${r.counts[key]} ${r.counts[key] === 1 ? "Punkt" : "Punkte"}`))),
        r.overall.assessment ? n("p", null, r.overall.assessment) : null),
      r.clauses.length ? n("section", "report-section", n("h3", null, "Wer steht wo"),
        n("p", "report-note", "Die Spalten zeigen, wie weit die Prioritäten des jeweiligen Stakeholders erfüllt sind, ein Strich heißt: keine verknüpft. Die Spalte Gesamt zählt zusätzlich die Prüfung des Rats und offene Fragen, deshalb kann sie gelber sein."),
        n("div", "report-scroll", n("table", "report-matrix",
          n("thead", null, n("tr", null, n("th", null, "Punkt"), n("th", null, "Gesamt"), r.stakeholders.map((s) => n("th", null, s.name)))),
          n("tbody", null, r.clauses.map((c) => n("tr", null, n("td", null, `${c.number}. ${c.title}`), n("td", null, lamp(c.light)),
            r.stakeholders.map((s) => n("td", null, c.perActor[s.id] ? lamp(c.perActor[s.id]) : "–")))))))) : null,
      LIGHT_ORDER.filter((key) => r.counts[key]).map((key) => n("section", `report-section band-${key}`,
        n("h3", null, `${LIGHT[key].name}: ${LIGHT[key].title}`), r.clauses.filter((c) => c.light === key).map(clauseCard))),
      r.moreOpen.length ? n("section", "report-section", n("h3", null, "Weitere offene Punkte"),
        n("p", "report-note", "Der Schlussbericht ordnet sie keinem einzelnen Punkt zu."),
        n("ul", "report-reasons", r.moreOpen.map((p) => n("li", null, `${p.kind}: ${p.text}`)))) : null,
      r.overall.conditions.length ? n("section", "report-section", n("h3", null, "Auflagen aller Stakeholder"),
        n("ul", "report-reasons", r.overall.conditions.map((c) => n("li", null, c)))) : null,
      n("section", "report-section", n("h3", null, "Stakeholder"),
        r.stakeholders.map((s) => n("section", "report-card", n("div", "report-card-head", n("h4", null, s.name), lamp(s.light)),
          n("p", "report-title", `${s.decision}, ${percent(s)}`),
          s.standpoint ? n("p", null, s.standpoint) : null,
          s.conditions.length ? [n("h5", null, "Bedingungen"), n("ul", "report-reasons", s.conditions.map((c) => n("li", null, c)))] : null,
          s.priorities.length ? [n("h5", null, "Prioritäten"), n("ul", "report-priorities", s.priorities.map(priorityRow))] : null))),
      n("section", "report-section", n("h3", null, "So entsteht die Ampel"),
        n("p", "report-note", "Die Farben folgen festen Regeln aus den gespeicherten Bewertungen. Sie sind keine Empfehlung und ersetzen keine rechtliche Prüfung."),
        n("dl", "report-rules", REPORT_RULES.map(([term, text]) => [n("dt", null, term), n("dd", null, text)]))));
  }

  function reportHtml(tree, css) {
    const escape = (value) => String(value).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const markup = (node) => (typeof node === "object"
      ? `<${node.tag}${node.cls ? ` class="${escape(node.cls)}"` : ""}>${node.kids.map(markup).join("")}</${node.tag}>` : escape(node));
    return `<!doctype html>\n<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">`
      + `<title>Gesamt-Report</title><style>${css}</style></head><body class="report-file">${markup(tree)}</body></html>\n`;
  }

  return { buildChats, visible, memberChat, findPassage, buildReport, buildPlenum, reportTree, reportHtml, council: COUNCIL, labels: { STATUS, RESULT, ADMISSIBILITY, LIGHTS: LIGHT } };
});
