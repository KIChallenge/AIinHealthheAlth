/* Turns a debate result into chats and round steps. Pure data, no DOM, so it can be tested in
   Node. Every message comes from a stored record. The group shows only what was published in
   the debate; private work stays in the individual chats. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.DebateChats = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const REVIEWERS = { truthseeker: "TruthSeeker", legaleye: "LegalEye", ethicmind: "EthicMind" };
  const REVIEWER_ROLE = { truthseeker: "Prüft Belege und Begründung", legaleye: "Prüft das geltende Recht",
    ethicmind: "Prüft ethische Fragen" };
  const STATUS = { met: "erfüllt", partly_met: "teilweise", unmet: "nicht erfüllt", unclear: "unklar" };
  const DECISION = { accept: "Annahme", conditional: "Bedingte Annahme", reject: "Ablehnung", withdraw: "Rückzug" };
  const NEGOTIATION = { offer: "Angebot", counteroffer: "Gegenangebot", concession: "Zugeständnis",
    condition: "Bedingung", disagreement: "Differenz" };
  const RESULT = { completed: "Abgeschlossen", incomplete: "Unvollständig", needs_clarification: "Rückfrage",
    invalid_input: "Ungültige Eingabe", failed: "Fehlgeschlagen" };
  const STOP = { satisfaction_threshold: "Schwelle in zwei Runden erreicht",
    deliberation_deadline: "Frist abgelaufen", execution_error: "Ausführungsfehler" };
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
  const isFinal = (record) => /^(AS|D)-final/.test(record.assessment_id || record.decision_id || "");
  const proposalKey = (p) => `${p.proposal_id}-v${p.version}`;

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
      ...texts.map((text, i) => ({ id: `m-${i}`, step: 0, side: "in", sender: "System", role: "program", text }))];
    return { header: { question: null, reference_date: null, status: RESULT[result.status] || result.status, stop: null },
      steps: [{ id: "meldung", label: "Meldung", round: null }],
      chats: [{ id: "gruppe", name: "Debatte", kind: "group", subtitle: "Keine Debatte gestartet", messages }] };
  }

  function buildChats(result) {
    if (!result || typeof result !== "object") throw new Error("Das Ergebnis ist kein JSON-Objekt.");
    if (!result.task) return messageResult(result);

    const times = {};
    for (const event of list(result.timeline)) times[event.event_id] = event.at;
    const rounds = roundNumbers(result);
    const steps = [{ id: "vorbereitung", label: "Vorbereitung", round: null },
      ...rounds.map((round) => ({ id: `runde-${round}`, label: `Runde ${round}`, round })),
      { id: "abschluss", label: "Abschluss", round: null }];
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

    const group = [];
    const single = Object.fromEntries([...actors.map((a) => [a.actor_id, []]), ...Object.keys(REVIEWERS).map((r) => [r, []])]);
    let counter = 0;
    const push = (target, step, side, fields) => target.push({ id: `n${(counter += 1)}`, step, side, ...fields });
    const fromActor = (actorId, extra) => ({ sender: nameOf[actorId] || actorId, senderId: actorId, role: "actor", ...extra });
    const fromReviewer = (reviewer, extra) => ({ sender: REVIEWERS[reviewer] || reviewer, senderId: reviewer, role: "reviewer", ...extra });
    const moderator = (extra) => ({ sender: MODERATOR, senderId: "moderation", role: "moderator", ...extra });
    const findingItems = (review) => list(review.findings).map((f) => ({
      title: ASSESSMENT[f.assessment] || f.assessment, text: f.reason, note: f.correction ? `Korrektur: ${f.correction}` : null }));

    function pushReview(target, step, review, tag) {
      const note = review.status !== "complete" ? "Prüfung nicht abgeschlossen."
        : review.coverage !== "complete" ? review.coverage_note : null;
      if (!list(review.findings).length && !note) return;
      push(target, step, "in", fromReviewer(review.reviewer, { tag, items: findingItems(review), note, time: times[review.review_id] }));
    }

    function pushChallenges(target, step, records) {
      for (const c of records) {
        const finding = findingById[c.finding_id];
        push(target, step, "in", fromActor(c.actor_id, { tag: "Widerspruch", text: c.reason,
          quote: finding ? finding.reason : null, time: times[c.challenge_id] }));
        if (c.reply_decision && finding) {
          push(target, step, "in", fromReviewer(finding.reviewer, { tag: REPLY[c.reply_decision] || c.reply_decision,
            text: c.reply_reason, time: times[`antwort:${c.challenge_id}`] }));
        }
      }
    }

    function assessmentItems(assessed) {
      return assessed.map((a) => ({ status: a.status, title: objective[a.priority_id] || a.priority_id, text: a.reason }));
    }

    /* Preparation: private work goes to the individual chats, reviewer reports to their own. */
    push(group, 0, "out", moderator({ tag: "Frage", text: result.task.question }));
    push(group, 0, "system", { text: `Regelwerk ${result.task.rulebook_version}. Stichtag ${result.task.reference_date || "unbekannt"}.` });
    push(group, 0, "system", { text: result.task.legal_scope });
    push(group, 0, "system", { text: "Alle bereiten sich getrennt vor. Ihre Beiträge stehen in den Einzelchats." });

    for (const actor of actors) {
      const aid = actor.actor_id;
      const target = single[aid];
      const profile = actor.profile;
      if (profile) {
        push(target, 0, "in", fromActor(aid, { tag: "Profil", text: profile.standpunkt,
          note: profile.standpunkt_label === "inferred" ? "Erschlossen, keine offizielle Erklärung." : null,
          items: list(profile.entries).map((e) => ({ text: e.aussage })) }));
      }
      if (list(actor.priorities).length) {
        push(target, 0, "in", fromActor(aid, { tag: "Prioritäten",
          items: actor.priorities.map((p) => ({ text: p.objective, note: `Bedingung: ${p.acceptance_condition}` })) }));
      }
      const versions = list(result.arguments).filter((a) => a.actor_id === aid).sort((a, b) => a.version - b.version);
      const prepared = reviews.filter((r) => r.actor_id === aid && (r.stage === "initial" || r.stage === "second"));
      for (const argument of versions) {
        const stage = argument.version === 1 ? "initial" : "second";
        push(target, 0, "in", fromActor(aid, { tag: argument.version === 1 ? "Erster Beitrag" : "Überarbeiteter Beitrag",
          items: list(argument.claims).map((c) => ({ text: c.behauptung, note: c.begruendung })),
          note: argument.change_explanation, time: times[argument.argument_id] }));
        for (const review of prepared.filter((r) => r.stage === stage)) pushReview(target, 0, review, `${STAGE[stage]}, vertraulich`);
        pushChallenges(target, 0, challenges.filter((c) => c.actor_id === aid && c.stage === stage));
      }
    }

    /* Rounds */
    rounds.forEach((round) => {
      const step = stepOf(round);
      if (round === rounds[0]) push(group, step, "system", { text: "Gemeinsames Paket veröffentlicht. Alle sehen jetzt denselben Stand." });
      for (const actor of actors) {
        for (const item of list(result.negotiation).filter((n) => n.round === round && n.actor_id === actor.actor_id)) {
          push(group, step, "in", fromActor(actor.actor_id, { tag: NEGOTIATION[item.kind] || item.kind, text: item.text, time: times[item.item_id] }));
        }
      }
      const proposals = list(result.proposal_history).filter((p) => p.round === round).sort((a, b) => a.version - b.version);
      if (!proposals.length) push(group, step, "system", { text: "Keine neue Fassung in dieser Runde." });
      for (const proposal of proposals) {
        push(group, step, "out", moderator({ tag: `Vorschlag, Fassung ${proposal.version}`, time: proposal.created_at,
          items: list(proposal.clauses).map((c, i) => ({ number: i + 1, text: c.text, note: `Rechtlich: ${LEGAL[c.legal_status] || c.legal_status}` })),
          note: ADMISSIBILITY[proposal.legal_admissibility] || null }));
        if (proposal.meta_update) push(group, step, "out", moderator({ text: proposal.meta_update }));
        for (const review of reviews.filter((r) => r.stage === "candidate" && r.round === round && r.review_id.includes(`-v${proposal.version}-`))) {
          pushReview(group, step, review, `${STAGE.candidate}, Fassung ${proposal.version}`);
          pushReview(single[review.reviewer], step, review, `${STAGE.candidate}, Fassung ${proposal.version}`);
        }
      }
      pushChallenges(group, step, challenges.filter((c) => c.stage === "candidate" && c.round === round));

      const satisfaction = roundSatisfaction(result, round);
      for (const s of satisfaction) {
        const decisions = list(result.decisions).filter((d) => d.round === round && d.actor_id === s.actor_id && !isFinal(d));
        const target = single[s.actor_id];
        if (s.assessed.length) {
          push(target, step, "in", fromActor(s.actor_id, { tag: `Bewertung: ${s.met} von ${s.total} erfüllt`, items: assessmentItems(s.assessed) }));
        } else {
          push(target, step, "system", { text: "Keine gültige Bewertung in dieser Runde." });
        }
        for (const decision of decisions) {
          const fields = { tag: `${DECISION[decision.kind] || decision.kind}: ${s.met} von ${s.total} erfüllt`, text: decision.reason,
            items: list(decision.conditions).map((c) => ({ text: c })), time: decision.at };
          push(group, step, "in", fromActor(s.actor_id, fields));
          push(target, step, "in", fromActor(s.actor_id, fields));
        }
        for (const review of reviews.filter((r) => r.stage === "assessment" && r.round === round && r.actor_id === s.actor_id)) {
          pushReview(target, step, review, STAGE.assessment);
          pushReview(single[review.reviewer], step, review, `${STAGE.assessment}: ${s.name}`);
        }
        pushChallenges(target, step, challenges.filter((c) => c.stage === "assessment" && c.round === round && c.actor_id === s.actor_id));
      }
      if (satisfaction.some((s) => s.assessed.length)) {
        push(group, step, "system", { text: satisfaction.map((s) => `${s.name}: ${s.met} von ${s.total}`).join(" · ") });
      }
    });

    /* Preparation reviews also belong to each reviewer's own chat. */
    for (const review of reviews.filter((r) => r.stage === "initial" || r.stage === "second")) {
      pushReview(single[review.reviewer], 0, review, `${STAGE[review.stage]}: ${nameOf[review.actor_id] || ""}`);
    }

    /* Final phase */
    const report = result.report || {};
    const consensus = report.Konsens || {};
    const frozen = result.final_proposal;
    push(group, last, "system", { text: `Beratung beendet: ${STOP[result.stop_reason] || "ohne Stoppgrund"}.` });
    push(group, last, "system", { text: frozen
      ? `Fassung ${frozen.version} ist eingefroren. ${ADMISSIBILITY[frozen.legal_admissibility] || ""}.`
      : "Kein vollständiger Vorschlag. Es wird keine Einigung ausgewiesen." });
    for (const actor of actors) {
      if (actor.final_standpoint) {
        const fields = { tag: "Abschließende Stellungnahme", text: actor.final_standpoint, time: times[`abschluss:${actor.actor_id}`] };
        push(group, last, "in", fromActor(actor.actor_id, fields));
        push(single[actor.actor_id], last, "in", fromActor(actor.actor_id, fields));
      } else {
        push(single[actor.actor_id], last, "system", { text: "Keine abschließende Stellungnahme." });
      }
    }
    for (const review of reviews.filter((r) => r.stage === "final")) {
      pushReview(group, last, review, STAGE.final);
      pushReview(single[review.reviewer], last, review, STAGE.final);
    }
    if (consensus.einschaetzung) push(group, last, "out", moderator({ tag: "Konsens", text: consensus.einschaetzung }));
    const section = (tag, records, line) => {
      if (records.length) push(group, last, "out", moderator({ tag, items: records.map(line) }));
    };
    section("Konflikte", list(report.Konflikte), (c) => ({ title: c.thema, text: c.beschreibung }));
    section("Kompromisse", list(report.Kompromisse), (c) => ({ text: c.beschreibung }));
    section("Offene Punkte", list(report.Offene_Punkte), (p) => ({ text: p.punkt }));
    section("Auswirkungen", list(report.Auswirkungen), (i) => ({ text: i.beschreibung }));
    const narrative = (result.debate_evolution || {}).narrative;
    if (narrative) push(group, last, "out", moderator({ tag: "So entwickelte sich die Debatte", text: narrative.replace(/\*\*/g, "") }));
    for (const missing of list(report.fehlende_abschnitte)) push(group, last, "system", { text: `Fehlt: ${missing}` });

    const inOrder = (messages) => [...messages].sort((a, b) => a.step - b.step);     // stable: keeps order within a step
    const chats = [
      { id: "gruppe", name: "Debatte", kind: "group", subtitle: `Du, ${actors.map((a) => a.name).join(", ")}, Prüfer`, messages: inOrder(group) },
      ...actors.map((a) => ({ id: a.actor_id, name: a.name, kind: "actor", subtitle: "Simulierte Perspektive", messages: inOrder(single[a.actor_id]) })),
      ...Object.keys(REVIEWERS).map((r) => ({ id: r, name: REVIEWERS[r], kind: "reviewer", subtitle: REVIEWER_ROLE[r], messages: inOrder(single[r]) })),
    ];
    return {
      header: { question: result.task.question, reference_date: result.task.reference_date,
        status: RESULT[result.status] || result.status, stop: STOP[result.stop_reason] || null },
      steps, chats,
      satisfactionAt(stepIndex) {
        const step = steps[stepIndex];
        if (step && step.round) return roundSatisfaction(result, step.round);
        return stepIndex === last ? list(actors).map((a) => ({ name: a.name, ...(a.satisfaction ? {
          met: a.satisfaction.met_count, total: a.satisfaction.priority_count } : { met: null, total: null }) })) : [];
      },
      proposalAt(stepIndex) {
        const shown = list(result.proposal_history).filter((p) => stepOf(p.round) <= stepIndex);
        return shown.length ? shown[shown.length - 1] : null;
      },
    };
  }

  /* Messages of a chat up to and including a step: "Weiter" appends the next round. */
  const visible = (chat, stepIndex) => chat.messages.filter((m) => m.step <= stepIndex);

  return { buildChats, visible, proposalKey, labels: { STATUS, RESULT, ADMISSIBILITY } };
});
