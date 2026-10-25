"""The escalation ledger: states, challenges/defenses, and the effective class.

Pure.  An escalation is its asker's to drop and open to challenge — nothing else
in the model ends one, and no seat ends another seat's escalation by outvoting it.
"""

from __future__ import annotations

from .state import ASKED, CHAIR_ID, DISAGREE, NEEDS_HUMAN, RETIRED, STANDING, WITHDRAWN, Docket, Escalation, Question, normalize_text


def effective_class(docket: Docket, question: Question) -> str:
    """`blocking` is requested; the **run** grants it.

    Granted when the chair raised the question, or when the count of seats whose latest stated stance on its item is `needs_human` is
    ≥ ⌊N/2⌋+1.  A question naming no item is never effective-blocking however many seats raise it: to stop the council, name the item.
    """
    if question.chair_raised:
        return "blocking"
    if question.item_id is None:
        return "deferred"
    holders = sum(1 for agent in docket.roster if docket.stated_stance(question.item_id, agent) == NEEDS_HUMAN)
    return "blocking" if holders >= docket.bar() else "deferred"


def raise_question(
    docket: Docket,
    *,
    text: str,
    asker: str,
    item_id: str | None,
    blocking: bool,
    round_: int,
    context: str = "",
    options: list[str] | None = None,
) -> tuple[Question, bool]:
    """Deduplicated by normalized text **among the questions still open** — the same question from anyone is one entry.

    The asker owns the **text** (latest asker round wins) and a co-asker never rewrites it; each asker owns **its slice
    of the menu** (`offer_options`), so re-asking retires that asker's stale candidates and leaves a co-asker's alone.
    An entry that **names an item** wins the item: the question adopts one it was created without, and never loses it.
    Returns `(question, updated)`.
    """
    for qid in docket.question_order:
        existing = docket.questions[qid]
        if existing.state != ASKED:
            # **Only an open question is the same question.**  An answered or declined entry is a decision the human made
            # about the decision in front of them then.  Filing a fresh ask onto it would take the fresh ask out of the
            # human's sight for good — `unsettled_questions` reads `ASKED` and nothing else, and `effective_class` judges
            # a question by the item it carries, which is the *old* ask's item — so the new escalation could neither be
            # asked nor counted, and the run would close `consensus` over a majority escalation it never put to anybody.
            # The cost of this rule is a seat that repeats itself being asked again, and that is the cheaper error: the
            # human skips a redundant question in one keystroke, and no keystroke recovers a decision never put to them.
            continue
        if existing.key == normalize_text(text):
            offer_options(existing, asker, options, replace=asker in existing.askers)
            if item_id is not None and existing.item_id is None:
                # An item-less entry is a *request*; naming an item is an escalation, and an escalation is the stronger
                # act over the same words.  A seat follows the brief's own advice — state the decision in one line — and
                # re-sends that line when it escalates, so leaving the deduped entry item-less would strand it: `effective_class`
                # returns `deferred` for an item-less question before it ever counts stance holders, and not even a
                # unanimous escalation would stop the run.
                existing.item_id = item_id
                if blocking:
                    existing.blocking_requested = True
            if asker in existing.askers:
                # The asker owns the text: latest asker round wins, `H` id stable.
                if existing.text != text:
                    existing.text, existing.updated_round = text, round_
                    return existing, True
                return existing, False
            existing.askers.append(asker)  # a co-asker never rewrites it
            if blocking:
                existing.blocking_requested = True
            return existing, False
    question = Question(
        id=f"H{docket.next_question}",
        text=text,
        context=context,
        menu=_menu(asker, options),
        item_id=item_id,
        askers=[asker],
        chair_raised=(asker == CHAIR_ID),
        blocking_requested=blocking,
        raised_round=round_,
        updated_round=round_,
    )
    docket.next_question += 1
    docket.questions[question.id] = question
    docket.question_order.append(question.id)
    return question, False


def refresh_question(
    question: Question,
    *,
    asker: str,
    text: str,
    item_id: str | None = None,
    context: str = "",
    options: list[str] | None = None,
    blocking: bool = False,
    round_: int = 0,
) -> bool:
    """The asker restates **its own open question**: the entry is updated in place, never forked.

    An escalation owns exactly one question — `escalation.question_id` is what the checkpoint asks — so a second open
    entry over the same decision is a question the human is asked *twice*, and answering one retires the escalation and
    silently drops the other.  A refreshed phrasing is therefore an update, not a new ask.

    The fields move by the same rules `raise_question` uses, so a re-ask cannot mean two things depending on whether the
    wording happened to survive dedup: the asker owns the text and the context of its own question (latest round wins)
    and **re-offers its own menu** rather than adding to it, and an item is adopted and never lost.  Nothing counts as
    changed that did not: a verbatim re-send retires nothing and moves nothing, and says so.  Returns whether anything
    actually changed.
    """
    changed = False
    if text and question.text != text:
        question.text, question.updated_round = text, round_
        changed = True
    if context and question.context != context:
        question.context = context
        changed = True
    if item_id is not None and question.item_id is None:
        question.item_id = item_id
        changed = True
    if blocking and not question.blocking_requested:
        question.blocking_requested = True
        changed = True
    menu_moved = offer_options(question, asker, options, replace=True)  # never short-circuited: a rephrasing still carries a menu
    return changed or menu_moved


def offer_options(question: Question, asker: str, options: list[str] | None, *, replace: bool) -> bool:
    """File the candidates an asker offers with its entry; **each asker owns its own slice of the menu**.

    `replace` says this asker is re-asking rather than asking for the first time — it already stands behind the
    question's text — so what it offers now is its *revised* menu: its own earlier candidates come off, and a co-asker's
    stay, because that seat has not re-asked and still stands behind them.  Without provenance there is no way to tell
    those two apart, and a menu that only ever grows asks the human to take a candidate its author abandoned — verbatim,
    as an authoritative answer.  A candidate the asker repeats keeps its number, so a menu the human read last round does
    not shuffle under them.

    An entry offering **nothing** replaces nothing: re-asking without a menu is not a revision, and a menu the human was
    shown must not vanish because the model went terse.  Returns whether the menu changed.
    """
    offered = _menu(asker, options)
    before = dict(question.menu)
    if replace and offered:
        question.menu = {key: entry for key, entry in question.menu.items() if entry[1] != asker or key in offered}
    for key, entry in offered.items():
        question.menu.setdefault(key, entry)  # first wording wins, whoever offered it
    return question.menu != before


def _menu(asker: str, options: list[str] | None) -> dict[str, tuple[str, str]]:
    """One asker's options, keyed the way the question keys itself so the same candidate is one line."""
    menu: dict[str, tuple[str, str]] = {}
    for option in options or []:
        key = normalize_text(option)
        if key:
            menu.setdefault(key, (option, asker))
    return menu


def record_stance(
    docket: Docket, *, item_id: str, agent: str, stance: str, note: str = "", evidence: str = "", round_: int = 0
) -> list[tuple[str, dict]]:
    """File a stance and apply the escalation state rules to it.

    A standing escalation ends only when its **asker sends** another stance; a `declined` record the run wrote withdraws nothing.
    """
    from .state import Stance

    record = Stance(stance=stance, note=note, evidence=evidence, round=round_)
    docket.stances.setdefault(item_id, {})[agent] = record
    events: list[tuple[str, dict]] = []
    escalation = docket.escalations.get((item_id, agent))
    if escalation is not None and escalation.state == STANDING and stance != NEEDS_HUMAN:
        escalation.state = WITHDRAWN
        escalation.end_round = round_
        events.append(("escalation_withdrawn", {"item": item_id, "asker": agent, "round": round_, "cause": f"sent {stance}"}))
    if stance == NEEDS_HUMAN and (escalation is None or escalation.state != STANDING):
        new = Escalation(item_id=item_id, asker=agent, raised_round=round_)
        docket.escalations[(item_id, agent)] = new
        # An escalation *is* a checkpoint question, phrased by the asker, so it reaches the checkpoint and the effective-blocking
        # test.  Without this link a `needs_human` would stand forever unseen and consensus would be declared over it.
        item = docket.items.get(item_id)
        text = (note or "").strip() or (f"{item.claim} — is a human decision needed here?" if item else ").")
        question, _ = raise_question(
            docket, text=text, asker=agent, item_id=item_id, blocking=True, round_=round_, context=(item.claim if item else "")
        )
        new.question_id = question.id
        events.append(("escalation_raised", {"item": item_id, "asker": agent, "round": round_, "id": question.id}))
    return events


def challenges(docket: Docket, item_id: str, judged_round: int, asker: str) -> list[dict]:
    """A challenge: a reasoned `disagree` or refuting note from a **non-asking** seat."""
    out = []
    for other in docket.roster:
        if other == asker:
            continue
        record = docket.stance(item_id, other)
        if record and not record.declined and record.stance == DISAGREE and record.reasoned and record.round == judged_round:
            out.append({"by": other, "round": judged_round, "note": record.note, "evidence": record.evidence, "defended": False})
    return out


def demote(escalation: Escalation, round_: int) -> None:
    from .state import DEMOTED

    escalation.state = DEMOTED
    escalation.end_round = round_
    escalation.undefended = True


def retire(escalation: Escalation, round_: int) -> None:
    escalation.state = RETIRED
    escalation.end_round = round_


def chair_question_open(docket: Docket, question: Question) -> bool:
    """A chair-raised question ends `retired` and nothing else."""
    return question.chair_raised and question.state == ASKED
