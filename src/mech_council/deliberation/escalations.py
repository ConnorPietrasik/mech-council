from __future__ import annotations

from .state import ASKED, CHAIR_ID, DISAGREE, NEEDS_HUMAN, RETIRED, STANDING, WITHDRAWN, Docket, Escalation, Question, normalize_text


def effective_class(docket: Docket, question: Question) -> str:
    # Nothing a seat marks stops the run: an escalation does it only when enough seats hold it.
    if question.direction_ask:
        return "blocking" if len(docket.directions()) > 1 else "deferred"
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
    for qid in docket.question_order:
        existing = docket.questions[qid]
        if existing.state != ASKED:
            # Only an open question is the same question: filing onto an answered entry would hide the new ask.
            continue
        if existing.key == normalize_text(text):
            offer_options(existing, asker, options, replace=asker in existing.askers)
            if item_id is not None and existing.item_id is None:
                existing.item_id = item_id
                if blocking:
                    existing.blocking_requested = True
            if asker in existing.askers:
                if existing.text != text:
                    existing.text, existing.updated_round = text, round_
                    return existing, True
                return existing, False
            existing.askers.append(asker)
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
        blocking_requested=blocking,
        raised_round=round_,
        updated_round=round_,
    )
    docket.next_question += 1
    docket.questions[question.id] = question
    docket.question_order.append(question.id)
    return question, False


def raise_direction_ask(docket: Docket, *, text: str, context: str, options: list[str], round_: int) -> Question:
    """The council's split, put to the human once."""
    question = Question(
        id=f"H{docket.next_question}",
        text=text,
        context=context,
        menu=_menu(CHAIR_ID, options),
        askers=[CHAIR_ID],
        direction_ask=True,
        raised_round=round_,
        updated_round=round_,
    )
    docket.next_question += 1
    docket.questions[question.id] = question
    docket.question_order.append(question.id)
    return question


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
    menu_moved = offer_options(question, asker, options, replace=True)
    return changed or menu_moved


def offer_options(question: Question, asker: str, options: list[str] | None, *, replace: bool) -> bool:
    offered = _menu(asker, options)
    before = dict(question.menu)
    if replace and offered:
        question.menu = {key: entry for key, entry in question.menu.items() if entry[1] != asker or key in offered}
    for key, entry in offered.items():
        question.menu.setdefault(key, entry)
    return question.menu != before


def _menu(asker: str, options: list[str] | None) -> dict[str, tuple[str, str]]:
    menu: dict[str, tuple[str, str]] = {}
    for option in options or []:
        key = normalize_text(option)
        if key:
            menu.setdefault(key, (option, asker))
    return menu


def record_stance(
    docket: Docket, *, item_id: str, agent: str, stance: str, note: str = "", evidence: str = "", round_: int = 0
) -> list[tuple[str, dict]]:
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
        # An escalation raises its own question, so it reaches the checkpoint and the effective-blocking test.
        item = docket.items.get(item_id)
        text = (note or "").strip() or (f"{item.claim} — is a human decision needed here?" if item else ").")
        question, _ = raise_question(
            docket, text=text, asker=agent, item_id=item_id, blocking=True, round_=round_, context=(item.claim if item else "")
        )
        new.question_id = question.id
        events.append(("escalation_raised", {"item": item_id, "asker": agent, "round": round_, "id": question.id}))
    return events


def challenges(docket: Docket, item_id: str, judged_round: int, asker: str) -> list[dict]:
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
