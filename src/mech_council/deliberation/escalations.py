from __future__ import annotations

from typing import TypeGuard

from .state import (
    ANSWERED,
    ASKED,
    CHAIR_ID,
    DISAGREE,
    NEEDS_HUMAN,
    RETIRED,
    STANDING,
    WITHDRAWN,
    Docket,
    Escalation,
    Question,
    normalize_text,
)


def effective_class(docket: Docket, question: Question) -> str:
    # Nothing stops the run but a declared stop, and a declared stop needs a majority behind it.
    if question.direction_ask:
        return "blocking" if len(docket.directions()) > 1 else "deferred"
    if not showable(docket, question):
        # an ask the human is never shown cannot be the thing the run is waiting on
        return "deferred"
    return "blocking" if len(stop_holders(docket, question)) >= docket.bar() else "deferred"


def stop_holders(docket: Docket, question: Question) -> list[str]:
    """The seats backing this ask that also asked the council to wait for it."""
    return [a for a in question.blocked_by if a in docket.roster and a in question.askers]


def showable(docket: Docket, question: Question) -> bool:
    """Whether an ask has earned the human's keystrokes: the decision it asks about is still open, a second seat
    backs it, and no peer answer holds it back unless a seat that raised the ask itself insisted that answer was not
    enough.
    """
    if moot(docket, question):
        return False
    if question.direction_ask or not docket.seconding:
        return True
    if settlements(question) and not question.insisted:
        return False
    return len([a for a in question.askers if a in docket.roster]) >= 2


def settlements(question: Question) -> dict[str, str]:
    """The peer answers that hold an ask back: a seat asking the thing is no answer to it.

    A fold makes the seat that answered one of the asks it absorbs an asker of the survivor; the answer stays on the
    record either way, it only stops being a reason to keep the ask from the human.
    """
    return {seat: answer for seat, answer in question.peer_answers.items() if seat not in question.askers}


def moot(docket: Docket, question: Question) -> bool:
    """Whether the council has settled the very item this ask asks about — unless seats are declared to stop for it.

    The closure is the council's answer, so the ask is moot; a declared stop at the bar is the one claim that
    outranks a closure, because a majority saying the run cannot go on without the human is not settled by a vote.
    """
    if question.item_id is None or question.direction_ask:
        return False
    item = docket.items.get(question.item_id)
    return item is not None and item.closed and len(stop_holders(docket, question)) < docket.bar()


#: why an ask is kept from the human, in the word the ledger and the reports print
SETTLED, UNBACKED, MOOT = "settled", "unseconded", "moot"
HELD_BACK = (SETTLED, UNBACKED, MOOT)


def held_back(docket: Docket, question: Question) -> str:
    """Why an ask is not showable, or `""`: its item was settled, a seat settled it from the workspace, or no second
    seat backed it.
    """
    if showable(docket, question):
        return ""
    if moot(docket, question):
        return MOOT
    return SETTLED if settlements(question) and not question.insisted else UNBACKED


def _moved(question: Question | None) -> str:
    """The reason to hand back when a seat names an ask the chair folded away."""
    if question is not None and question.merged_into:
        return f"that ask is now {question.merged_into}: name {question.merged_into} instead"
    return ""


def _other_ledger(docket: Docket, qid: str, agent: str) -> str:
    """A note id in an ask field: whose note it is, and what this caller may do about it."""
    note = docket.human_notes.get(qid)
    if note is None:
        return ""
    if note.state != STANDING:
        return f"{qid} is a note {note.by} retracted in round {note.retracted_round}: it asks nothing"
    if note.by != agent:
        return f"{qid} is {note.by}'s note, not an ask: it asks and blocks nothing — say your own piece in `notes`"
    return f'{qid} is a note of yours, not an ask: retract it with `retract_note: ["{qid}"]`'


def _open_ask(docket: Docket, qid: str, agent: str) -> tuple[Question | None, str]:
    question = docket.questions.get(qid)
    if question is None:
        return None, _other_ledger(docket, qid, agent) or "no open question with that id"
    if moved := _moved(question):
        return question, moved
    if question.state != ASKED:
        return None, "no open question with that id"
    if question.direction_ask:
        # the camps own this ask, not a seat
        return question, "the split ask belongs to the camps, not to a seat"
    return question, ""


def item_ask(docket: Docket, item_id: str) -> Question | None:
    """The open ask that already stands for this item, if one does: one item, one decision, one ask."""
    for question in docket.questions_for_item(item_id):
        if question.state == ASKED and not question.direction_ask:
            return question
    return None


def _their_words(question: Question, asker: str) -> bool:
    """Whether the ask carries this seat's words: they were the first to raise it."""
    return not question.raised_by or question.raised_by[0] == asker


def _note_wording(question: Question, asker: str, text: str) -> None:
    """Record the words a seat asked with when another seat's wording is the one the human reads."""
    question.also_asked.setdefault(asker, text)


def second(docket: Docket, *, qid: str, agent: str, round_: int) -> tuple[Question | None, str]:
    """Back a peer's ask. Backing is not raising: a seconder cannot call an answer to it not enough."""
    question, reason = _open_ask(docket, qid, agent)
    if question is None or reason:
        return question, reason
    if agent in question.askers:
        return question, "you already ask it"
    question.askers.append(agent)
    question.updated_round = round_
    return question, ""


def ask_to_stop(docket: Docket, question: Question, agent: str) -> bool:
    """Record that this seat wants the run to wait on the ask. True when that is new."""
    if question.direction_ask or agent not in question.askers or agent in question.blocked_by:
        return False
    question.blocked_by.append(agent)
    return True


def stop_for(docket: Docket, *, qid: str, agent: str, round_: int) -> tuple[Question | None, str]:
    """Back an ask *and* declare that the run waits for the answer."""
    question, reason = _open_ask(docket, qid, agent)
    if question is None or reason:
        return question, reason
    if agent not in question.askers:
        question.askers.append(agent)
        question.updated_round = round_
    ask_to_stop(docket, question, agent)
    return question, ""


def answer_by_seat(docket: Docket, *, qid: str, agent: str, text: str, round_: int = 0) -> tuple[Question | None, str]:
    """Settle a peer's ask from the workspace. The asker's own guess is not an answer."""
    question, reason = _open_ask(docket, qid, agent)
    if question is None or reason:
        return question, reason
    if agent in question.askers:
        return question, "you ask it: retract it if you no longer need the answer"
    if not text.strip():
        return question, "an empty answer changes nothing"
    question.peer_answers[agent] = text.strip()[:1200]
    question.peer_answer_round = max(question.peer_answer_round, round_)
    return question, ""


def insist(docket: Docket, *, qid: str, agent: str, round_: int, seen: int) -> tuple[Question | None, str]:
    """Say a peer's answer was not enough, for a seat that raised the ask.

    `seen` is the latest round of peer answers this pass shows the seat: an insistence rebuts those and nothing later,
    so an answer that landed in the round a verdict was written in is not one that verdict can have read.
    """
    question, reason = _open_ask(docket, qid, agent)
    if question is None or reason:
        return question, reason
    if agent not in question.raised_by:
        return question, "only a seat that raised it can say an answer was not enough: `second` it if you would ask it too"
    if not settlements(question):
        return question, "no seat answered that ask, so there is nothing to call not enough"
    if question.peer_answer_round > seen:
        return question, f"the answer landed in round {question.peer_answer_round}, which this pass does not show"
    if question.insisted:
        return question, "you already said that answer was not enough"
    question.insisted, question.updated_round = True, round_
    return question, ""


def retract(docket: Docket, *, qid: str, agent: str, round_: int) -> tuple[Question | None, str]:
    """Drop one asker: the ask survives until the last one walks away. An answered one goes moot, never void."""
    question = docket.questions.get(qid)
    if question is None:
        return None, _other_ledger(docket, qid, agent) or "no open question with that id"
    if moved := _moved(question):
        return question, moved
    if question.state not in (ASKED, ANSWERED):
        return None, "no open question with that id"
    if question.direction_ask:
        return question, "the split ask belongs to the camps, not to a seat"
    if agent not in question.askers:
        return question, f"{agent} does not ask it"
    question.askers = [a for a in question.askers if a != agent]
    question.blocked_by = [a for a in question.blocked_by if a != agent]
    if question.askers:
        return question, ""
    if question.state == ANSWERED:
        if "moot" not in question.markers:
            question.markers.append("moot")
        question.retracted_by = agent
        return question, ""
    question.state, question.retracted_round, question.retracted_by = WITHDRAWN, round_, agent
    return question, ""


def _foldable(question: Question | None) -> TypeGuard[Question]:
    """Only an open ask of a seat's own can be folded into another."""
    return question is not None and question.state == ASKED and not question.direction_ask


def merge_asks(docket: Docket, *, groups: list[dict], round_: int) -> tuple[list[tuple[str, dict]], str]:
    """Fold the asks the chair says are one ask: askers, declared stops, peer answers and menus all move.

    The ask raised first survives whichever one the chair names. A group naming one ask twice, or one ask in two
    groups, is a plan that says nothing at all: nothing folds.
    """
    claimed: set[str] = set()
    usable: list[tuple[Question, list[Question], str]] = []
    for group in groups:
        ids = [group["canonical"], *(group.get("duplicates") or [])]
        if len(set(ids)) != len(ids):
            return [], f"{ids[0]} is named twice in one group"
        if reused := claimed & set(ids):
            return [], f"{sorted(reused)[0]} is reused across groups"
        canonical = docket.questions.get(group["canonical"])
        if not _foldable(canonical):
            continue  # an id that is not an open ask is skipped, as an unknown item id is
        claimed.update(ids)
        one_ask = [one for one in (canonical, *(docket.questions.get(qid) for qid in group.get("duplicates") or [])) if _foldable(one)]
        if len(one_ask) > 1:
            survivor = min(one_ask, key=lambda one: docket.question_order.index(one.id))
            usable.append((survivor, [one for one in one_ask if one is not survivor], canonical.id))
    events: list[tuple[str, dict]] = []
    for survivor, duplicates, chair_named in usable:
        for duplicate in duplicates:
            _fold_ask(docket, survivor, duplicate, round_)
            events.append(
                (
                    "question_merged",
                    {"id": duplicate.id, "into": survivor.id, "askers": list(survivor.askers), "chair_named": chair_named, "round": round_},
                )
            )
    return events, ""


def _fold_ask(docket: Docket, canonical: Question, duplicate: Question, round_: int) -> None:
    """One ask absorbs another: the seats behind it now stand behind the one that survives."""
    for agent in duplicate.askers:
        if agent not in canonical.askers:
            canonical.askers.append(agent)
    for agent in duplicate.raised_by:
        if agent not in canonical.raised_by:
            canonical.raised_by.append(agent)
    for agent in duplicate.blocked_by:
        if agent not in canonical.blocked_by:
            canonical.blocked_by.append(agent)
    for agent, answer in duplicate.peer_answers.items():
        canonical.peer_answers.setdefault(agent, answer)
    for key, entry in duplicate.menu.items():
        canonical.menu.setdefault(key, entry)
    canonical.insisted = canonical.insisted or duplicate.insisted
    canonical.peer_answer_round = max(canonical.peer_answer_round, duplicate.peer_answer_round)
    canonical.raised_round = min(canonical.raised_round, duplicate.raised_round)
    canonical.updated_round = max(canonical.updated_round, duplicate.updated_round)
    if duplicate.item_id and canonical.item_id is None:
        canonical.item_id = duplicate.item_id
    if duplicate.context and duplicate.context != canonical.context:
        canonical.context = f"{canonical.context} | {duplicate.context}" if canonical.context else duplicate.context
    if duplicate.text != canonical.text:
        for agent in duplicate.raised_by or duplicate.askers:
            canonical.also_asked.setdefault(agent, duplicate.text)
    for agent, said in duplicate.also_asked.items():
        if said != canonical.text:
            canonical.also_asked.setdefault(agent, said)
    for escalation in docket.escalations.values():
        if escalation.question_id == duplicate.id:
            escalation.question_id = canonical.id
    duplicate.askers, duplicate.blocked_by, duplicate.peer_answers = [], [], {}
    if "merged" not in duplicate.markers:
        duplicate.markers.append("merged")
    duplicate.state, duplicate.merged_into, duplicate.retracted_round = WITHDRAWN, canonical.id, round_


def raise_question(
    docket: Docket,
    *,
    text: str,
    asker: str,
    item_id: str | None,
    round_: int,
    context: str = "",
    options: list[str] | None = None,
    blocking: bool = False,
) -> tuple[Question, bool]:
    """Raise or merge an ask. `blocking` is this seat declaring it wants the run to wait on it."""
    question, updated = _raise_or_merge(docket, text=text, asker=asker, item_id=item_id, round_=round_, context=context, options=options)
    if blocking:
        ask_to_stop(docket, question, asker)
    return question, updated


def _raise_or_merge(
    docket: Docket,
    *,
    text: str,
    asker: str,
    item_id: str | None,
    round_: int,
    context: str,
    options: list[str] | None,
    hold: bool = False,
) -> tuple[Question, bool]:
    """An ask the item hold raises joins the ask that already stands for that item, whoever worded it first."""
    standing = item_ask(docket, item_id) if hold and item_id else None
    for qid in docket.question_order:
        existing = docket.questions[qid]
        if existing.state != ASKED:
            # Only an open question is the same question: filing onto an answered entry would hide the new ask.
            continue
        if existing is standing or existing.key == normalize_text(text):
            return _join_ask(existing, asker=asker, text=text, item_id=item_id, options=options, round_=round_)
    question = Question(
        id=f"H{docket.next_question}",
        text=text,
        context=context,
        menu=_menu(asker, options),
        item_id=item_id,
        askers=[asker],
        raised_by=[asker],
        raised_round=round_,
        updated_round=round_,
    )
    docket.next_question += 1
    docket.questions[question.id] = question
    docket.question_order.append(question.id)
    return question, False


def _join_ask(
    existing: Question, *, asker: str, text: str, item_id: str | None, options: list[str] | None, round_: int
) -> tuple[Question, bool]:
    """Stand behind an ask that is already open: its words survive, and a seat asking fresh words raised it too."""
    offer_options(existing, asker, options, replace=asker in existing.askers)
    if item_id is not None and existing.item_id is None:
        existing.item_id = item_id
    if asker in existing.askers:
        if existing.text != text and _their_words(existing, asker):
            existing.text, existing.updated_round = text, round_
            return existing, True
        return existing, False
    existing.askers.append(asker)
    existing.raised_by.append(asker)
    existing.updated_round = round_
    if existing.text != text:
        _note_wording(existing, asker, text)
    return existing, False


def raise_direction_ask(docket: Docket, *, text: str, context: str, options: list[str], round_: int) -> Question:
    """The council's split, put to the human once."""
    question = Question(
        id=f"H{docket.next_question}",
        text=text,
        context=context,
        menu=_menu(CHAIR_ID, options),
        askers=[CHAIR_ID],
        raised_by=[CHAIR_ID],
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
    round_: int = 0,
) -> bool:
    """A seat restating an ask on the docket: only the seat whose words these are may reword them."""
    changed = False
    mine = _their_words(question, asker)
    if mine:
        if text and question.text != text:
            question.text, question.updated_round = text, round_
            changed = True
        if context and question.context != context:
            question.context = context
            changed = True
    elif text and question.text != text:
        # a seat that shares the ask does not take the words out of the other seat's mouth
        before = dict(question.also_asked)
        _note_wording(question, asker, text)
        changed = changed or question.also_asked != before
    if item_id is not None and question.item_id is None:
        question.item_id = item_id
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


def release_stops(docket: Docket, *, item_id: str, agent: str) -> None:
    """A seat that lets go of the item lets go of the stop it declared on that item's asks."""
    for question in docket.questions_for_item(item_id):
        if question.state == ASKED and agent in question.blocked_by:
            question.blocked_by = [a for a in question.blocked_by if a != agent]


def drop_ask(docket: Docket, escalation: Escalation, agent: str, round_: int) -> list[tuple[str, dict]]:
    """The ask this seat's hold raised goes with the hold; one it raised on its own, holding nothing, is untouched."""
    if not escalation.question_id:
        return []
    question, reason = retract(docket, qid=escalation.question_id, agent=agent, round_=round_)
    if question is None or reason:
        return []
    return [
        (
            "question_retracted",
            {
                "id": question.id,
                "seat": agent,
                "round": round_,
                "state": question.state,
                "still_asking": list(question.askers),
                "cause": "released the hold",
            },
        )
    ]


def withdraw_escalation(docket: Docket, *, item_id: str, agent: str, round_: int, cause: str) -> list[tuple[str, dict]]:
    escalation = docket.escalations.get((item_id, agent))
    if escalation is None or escalation.state != STANDING:
        return []
    escalation.state = WITHDRAWN
    escalation.end_round = round_
    release_stops(docket, item_id=item_id, agent=agent)
    events = [("escalation_withdrawn", {"item": item_id, "asker": agent, "round": round_, "cause": cause})]
    events += drop_ask(docket, escalation, agent, round_)
    return events


def moot_item_asks(docket: Docket, *, item_id: str, disposition: str, round_: int) -> list[tuple[str, dict]]:
    """Retire the asks this closure answers: `moot` says which item settled under them.

    A blocking ask is left standing, and a below-bar stop dies with the ask it was declared on.
    """
    events: list[tuple[str, dict]] = []
    for question in docket.questions_for_item(item_id):
        if question.state != ASKED or not moot(docket, question):
            continue
        question.state, question.moot_with, question.retracted_round = WITHDRAWN, item_id, round_
        question.blocked_by = []  # a stop dies with the ask it was declared on
        if "moot" not in question.markers:
            question.markers.append("moot")
        events.append(
            (
                "question_mooted",
                {
                    "id": question.id,
                    "item": item_id,
                    "disposition": disposition,
                    "round": round_,
                    "askers": list(question.askers),
                },
            )
        )
    return events


def record_stance(
    docket: Docket, *, item_id: str, agent: str, stance: str, note: str = "", evidence: str = "", round_: int = 0
) -> list[tuple[str, dict]]:
    from .state import Stance

    record = Stance(stance=stance, note=note, evidence=evidence, round=round_)
    docket.stances.setdefault(item_id, {})[agent] = record
    events: list[tuple[str, dict]] = []
    if stance != NEEDS_HUMAN:
        events += withdraw_escalation(docket, item_id=item_id, agent=agent, round_=round_, cause=f"sent {stance}")
    escalation = docket.escalations.get((item_id, agent))
    if stance == NEEDS_HUMAN and (escalation is None or escalation.state != STANDING):
        new = Escalation(item_id=item_id, asker=agent, raised_round=round_)
        docket.escalations[(item_id, agent)] = new
        # An escalation raises its own question, so it reaches the checkpoint and the report.
        item = docket.items.get(item_id)
        text = (note or "").strip() or (f"{item.claim} — is a human decision needed here?" if item else ").")
        question, _ = _raise_or_merge(
            docket, text=text, asker=agent, item_id=item_id, round_=round_, context=(item.claim if item else ""), options=None, hold=True
        )
        new.question_id = question.id
        events.append(
            (
                "escalation_raised",
                {
                    "item": item_id,
                    "asker": agent,
                    "round": round_,
                    "id": question.id,
                    "asked_with": [one for one in question.raised_by if one != agent],
                },
            )
        )
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
