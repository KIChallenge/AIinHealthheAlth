/* Turns a debate result into chats, threads and round steps. Pure data, no DOM, so it can be
   tested in Node. Every message comes from a stored record. The group shows only what was
   published in the debate; confidential reviews stay in the private reviewer-actor chats. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.DebateChats = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const REVIEWERS = { truthseeker: "TruthSeeker", legaleye: "LegalEye", ethicmind: "EthicMind" };
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

    let counter = 0;
    const message = (step, side, fields) => ({ id: `n${(counter += 1)}`, step, side, replies: [], ...fields });
    const fromActor = (actorId, extra) => ({ sender: nameOf[actorId] || actorId, senderId: actorId, role: "actor", ...extra });
    const fromReviewer = (reviewer, extra) => ({ sender: REVIEWERS[reviewer] || reviewer, senderId: reviewer, role: "reviewer", ...extra });
    const moderator = (extra) => ({ sender: MODERATOR, senderId: "moderation", role: "moderator", ...extra });
    const findingItems = (review) => list(review.findings).map((f) => ({
      title: ASSESSMENT[f.assessment] || f.assessment, text: f.reason, note: f.correction ? `Korrektur: ${f.correction}` : null }));

    const group = [];
    const pairs = {};
    const pairKey = (reviewer, actorId) => `${reviewer}:${actorId}`;
    for (const actor of actors) for (const reviewer of Object.keys(REVIEWERS)) pairs[pairKey(reviewer, actor.actor_id)] = [];

    function reviewMessage(step, review, tag, side) {
      const note = review.status !== "complete" ? "Prüfung nicht abgeschlossen."
        : review.coverage !== "complete" ? review.coverage_note : null;
      if (!list(review.findings).length && !note) return null;
      return message(step, side, fromReviewer(review.reviewer, { tag, items: findingItems(review), note, time: times[review.review_id] }));
    }

    /* A challenge and its answer, in the thread or chat they belong to. */
    function challengeMessages(step, records, actorSide, reviewerSide) {
      const out = [];
      for (const c of records) {
        const finding = findingById[c.finding_id];
        out.push(message(step, actorSide, fromActor(c.actor_id, { tag: "Widerspruch", text: c.reason,
          quote: finding ? finding.reason : null, time: times[c.challenge_id] })));
        if (c.reply_decision && finding) {
          out.push(message(step, reviewerSide, fromReviewer(finding.reviewer, { tag: REPLY[c.reply_decision] || c.reply_decision,
            text: c.reply_reason, time: times[`antwort:${c.challenge_id}`] })));
        }
      }
      return out;
    }

    const assessmentItems = (assessed) => assessed.map((a) => ({
      status: a.status, title: objective[a.priority_id] || a.priority_id, text: a.reason }));

    /* Preparation is private: the group sees only that it is happening. */
    group.push(message(0, "out", moderator({ tag: "Frage", text: result.task.question })));
    group.push(message(0, "system", { text: `Regelwerk ${result.task.rulebook_version}. Stichtag ${result.task.reference_date || "unbekannt"}.` }));
    group.push(message(0, "system", { text: result.task.legal_scope }));
    group.push(message(0, "system", { text: "Alle bereiten sich getrennt vor. Die vertraulichen Prüfungen stehen in den Privatchats." }));

    for (const actor of actors) {
      const aid = actor.actor_id;
      const versions = list(result.arguments).filter((a) => a.actor_id === aid).sort((a, b) => a.version - b.version);
      for (const reviewer of Object.keys(REVIEWERS)) {
        const target = pairs[pairKey(reviewer, aid)];
        if (actor.profile) {
          target.push(message(0, "in", fromActor(aid, { tag: "Profil zur Prüfung", text: actor.profile.standpunkt,
            note: actor.profile.standpunkt_label === "inferred" ? "Erschlossen, keine offizielle Erklärung." : null,
            items: list(actor.priorities).map((p) => ({ text: p.objective, note: `Bedingung: ${p.acceptance_condition}` })) })));
        }
        for (const [version, stage] of [[1, "initial"], [2, "second"]]) {
          const argument = versions.find((a) => a.version === version);     // a failed revision has none
          if (argument) {
            target.push(message(0, "in", fromActor(aid, { tag: version === 1 ? "Erster Beitrag" : "Überarbeiteter Beitrag",
              items: list(argument.claims).map((c) => ({ text: c.behauptung, note: c.begruendung })),
              note: argument.change_explanation, time: times[argument.argument_id] })));
          }
          for (const review of reviews.filter((r) => r.actor_id === aid && r.reviewer === reviewer && r.stage === stage)) {
            const reply = reviewMessage(0, review, `${STAGE[stage]}, vertraulich`, "peer");
            if (reply) target.push(reply);
          }
          const own = challenges.filter((c) => c.actor_id === aid && c.stage === stage
            && findingById[c.finding_id] && findingById[c.finding_id].reviewer === reviewer);
          target.push(...challengeMessages(0, own, "in", "peer"));
        }
      }
    }

    /* Rounds */
    rounds.forEach((round) => {
      const step = stepOf(round);
      if (round === rounds[0]) {
        group.push(message(step, "system", { text: "Gemeinsames Paket veröffentlicht. Alle sehen jetzt denselben Stand." }));
        for (const actor of actors) {
          if (!actor.profile) continue;
          const post = message(step, "in", fromActor(actor.actor_id, { tag: "Standpunkt", text: actor.profile.standpunkt }));
          if (list(actor.priorities).length) {
            post.replies.push(message(step, "in", fromActor(actor.actor_id, { tag: "Prioritäten",
              items: actor.priorities.map((p) => ({ text: p.objective, note: `Bedingung: ${p.acceptance_condition}` })) })));
          }
          const newest = list(result.arguments).filter((a) => a.actor_id === actor.actor_id).sort((a, b) => b.version - a.version)[0];
          if (newest) {
            post.replies.push(message(step, "in", fromActor(actor.actor_id, { tag: "Argument",
              items: list(newest.claims).map((c) => ({ text: c.behauptung, note: c.begruendung })) })));
          }
          group.push(post);
        }
      }
      for (const actor of actors) {
        const items = list(result.negotiation).filter((n) => n.round === round && n.actor_id === actor.actor_id);
        if (items.length) {
          group.push(message(step, "in", fromActor(actor.actor_id, { tag: "Verhandlung", time: times[items[0].item_id],
            items: items.map((n) => ({ title: NEGOTIATION[n.kind] || n.kind, text: n.text })) })));
        }
      }
      const proposals = list(result.proposal_history).filter((p) => p.round === round).sort((a, b) => a.version - b.version);
      if (!proposals.length) group.push(message(step, "system", { text: "Keine neue Fassung in dieser Runde." }));
      for (const proposal of proposals) {
        const post = message(step, "out", moderator({ tag: `Vorschlag, Fassung ${proposal.version}`, time: proposal.created_at,
          items: list(proposal.clauses).map((c, i) => ({ number: i + 1, text: c.text, note: `Rechtlich: ${LEGAL[c.legal_status] || c.legal_status}` })),
          note: ADMISSIBILITY[proposal.legal_admissibility] || null }));
        for (const review of reviews.filter((r) => r.stage === "candidate" && r.round === round && r.review_id.includes(`-v${proposal.version}-`))) {
          const reply = reviewMessage(step, review, STAGE.candidate, "in");
          if (reply) post.replies.push(reply);
          const answered = challenges.filter((c) => c.stage === "candidate" && c.round === round
            && list(review.findings).some((f) => f.finding_id === c.finding_id));
          post.replies.push(...challengeMessages(step, answered, "in", "in"));
        }
        group.push(post);
        if (proposal.meta_update) group.push(message(step, "out", moderator({ text: proposal.meta_update })));
      }

      const satisfaction = roundSatisfaction(result, round);
      for (const s of satisfaction) {
        const decisions = list(result.decisions).filter((d) => d.round === round && d.actor_id === s.actor_id && !isFinal(d));
        for (const decision of decisions) {
          const post = message(step, "in", fromActor(s.actor_id, { tag: `${DECISION[decision.kind] || decision.kind}: ${s.met} von ${s.total} erfüllt`,
            text: decision.reason, items: list(decision.conditions).map((c) => ({ text: c })), time: decision.at }));
          if (s.assessed.length) {
            post.replies.push(message(step, "in", fromActor(s.actor_id, { tag: "Bewertung je Priorität", items: assessmentItems(s.assessed) })));
          }
          group.push(post);
        }
        for (const reviewer of Object.keys(REVIEWERS)) {
          const target = pairs[pairKey(reviewer, s.actor_id)];
          const stageReviews = reviews.filter((r) => r.stage === "assessment" && r.round === round && r.actor_id === s.actor_id && r.reviewer === reviewer);
          if (!stageReviews.length) continue;
          if (s.assessed.length) {
            target.push(message(step, "in", fromActor(s.actor_id, { tag: `Bewertung: ${s.met} von ${s.total} erfüllt`, items: assessmentItems(s.assessed) })));
          } else {
            target.push(message(step, "system", { text: "Keine gültige Bewertung in dieser Runde." }));
          }
          for (const review of stageReviews) {
            const reply = reviewMessage(step, review, STAGE.assessment, "peer");
            if (reply) target.push(reply);
          }
          const own = challenges.filter((c) => c.stage === "assessment" && c.round === round && c.actor_id === s.actor_id
            && findingById[c.finding_id] && findingById[c.finding_id].reviewer === reviewer);
          target.push(...challengeMessages(step, own, "in", "peer"));
        }
      }
      if (satisfaction.some((s) => s.assessed.length)) {
        group.push(message(step, "system", { text: satisfaction.map((s) => `${s.name}: ${s.met} von ${s.total}`).join(" · ") }));
      }
    });

    /* Final phase */
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
        items: list(frozen.clauses).map((c, i) => ({ number: i + 1, text: c.text, note: `Rechtlich: ${LEGAL[c.legal_status] || c.legal_status}` })),
        note: ADMISSIBILITY[frozen.legal_admissibility] || null }))
      : message(last, "system", { text: "Kein vollständiger Vorschlag. Es wird keine Einigung ausgewiesen." });
    group.push(finalPost);
    for (const review of finalReviews) {
      const reply = reviewMessage(last, review, STAGE.final, "in");
      if (reply) (frozen ? finalPost.replies : group).push(reply);
    }
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
    for (const missing of list(report.fehlende_abschnitte)) group.push(message(last, "system", { text: `Fehlt: ${missing}` }));

    const chats = [{ id: "gruppe", name: "debAIte", kind: "group", section: "Gruppe",
      subtitle: `Du, ${actors.map((a) => a.name).join(", ")}, Prüfer`, messages: group }];
    for (const actor of actors) {
      for (const reviewer of Object.keys(REVIEWERS)) {
        chats.push({ id: pairKey(reviewer, actor.actor_id), name: `${REVIEWERS[reviewer]} und ${actor.name}`, kind: "private",
          section: "Privat", pair: [REVIEWERS[reviewer], actor.name], subtitle: "Vertraulich. Du liest mit.",
          messages: pairs[pairKey(reviewer, actor.actor_id)] });
      }
    }
    chats.push({ id: "meta", name: "Meta-Moderator", kind: "meta", section: "Fragen zur debAIte",
      subtitle: "Beantwortet deine Fragen aus dem Protokoll", messages: [] });
    return {
      header: { question: result.task.question, reference_date: result.task.reference_date,
        status: RESULT[result.status] || result.status, stop: STOP[result.stop_reason] || null },
      steps, chats,
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

  /* Messages of a chat up to and including a step: "Weiter" appends the next round. */
  const visible = (chat, stepIndex) => chat.messages.filter((m) => m.step <= stepIndex);

  return { buildChats, visible, labels: { STATUS, RESULT, ADMISSIBILITY } };
});
