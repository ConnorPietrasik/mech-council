"""§5.2.8's escalation ledger: states, challenges/defenses, and the effective class.

Pure.  An escalation is its asker's to drop and open to challenge — nothing else in the
design ends one, and no seat ends another seat's escalation by outvoting it (§14.6).
"""
from __future__ import annotations

from .state import (ASKED, CHAIR_ID, DISAGREE, Docket, Escalation, NEEDS_HUMAN, Question,
                    RETIRED, STANDING, WITHDRAWN, normalize_text)


def effective_class(docket: Docket, question: Question) -> str:
    """§5.2.8: `blocking` is requested; the **run** grants it.

    Granted when the chair raised the question, or when the count of seats whose latest
    stated stance on its item is `needs_human` is ≥ ⌊N/2⌋+1.  A question naming no item
    is never effective-blocking however many seats raise it: to stop the council, name
    the item.
    """
    if question.chair_raised:
        return "blocking"
    if question.item_id is None:
        return "deferred"
    holders = sum(1 for agent in docket.roster
                  if docket.stated_stance(question.item_id, agent) == NEEDS_HUMAN)
    return "blocking" if holders >= docket.bar() else "deferred"


def raise_question(docket: Docket, *, text: str, asker: str, item_id: str | None,
                   blocking: bool, round_: int, context: str = "",
                   options: list[str] | None = None) -> tuple[Question, bool]:
    """§8: deduplicated by normalized text — the same question from anyone is one entry.

    The asker owns the text, so a later round may replace it (latest asker round wins);
    a co-asker never rewrites it.  Returns `(question, updated)`.
    """
    for qid in docket.question_order:
        existing = docket.questions[qid]
        if existing.key == normalize_text(text):
            if asker in existing.askers:
                # The asker owns the text: latest asker round wins, `H` id stable.
                if existing.text != text:
                    existing.text, existing.updated_round = text, round_
                    return existing, True
                return existing, False
            existing.askers.append(asker)      # a co-asker never rewrites it
            if blocking:
                existing.blocking_requested = True
            return existing, False
    question = Question(id=f"H{docket.next_question}", text=text, context=context,
                        options=list(options or []), item_id=item_id, askers=[asker],
                        chair_raised=(asker == CHAIR_ID), blocking_requested=blocking,
                        raised_round=round_, updated_round=round_)
    docket.next_question += 1
    docket.questions[question.id] = question
    docket.question_order.append(question.id)
    return question, False


def record_stance(docket: Docket, *, item_id: str, agent: str, stance: str, note: str = "",
                  evidence: str = "", round_: int = 0) -> list[tuple[str, dict]]:
    """File a stance and apply §5.2.8's state rules to it.

    A standing escalation ends only when its **asker sends** another stance; a
    `declined` record the run wrote withdraws nothing.
    """
    from .state import Stance
    record = Stance(stance=stance, note=note, evidence=evidence, round=round_)
    docket.stances.setdefault(item_id, {})[agent] = record
    events: list[tuple[str, dict]] = []
    escalation = docket.escalations.get((item_id, agent))
    if escalation is not None and escalation.state == STANDING and stance != NEEDS_HUMAN:
        escalation.state = WITHDRAWN
        escalation.end_round = round_
        events.append(("escalation_withdrawn", {"item": item_id, "asker": agent,
                                                "round": round_,
                                                "cause": f"sent {stance}"}))
    if stance == NEEDS_HUMAN and (escalation is None or escalation.state != STANDING):
        new = Escalation(item_id=item_id, asker=agent, raised_round=round_)
        docket.escalations[(item_id, agent)] = new
        # §5.2.8: an escalation *is* a §8 question, phrased by the asker, so it reaches the
        # checkpoint and §5.2.7's effective-blocking test.  Without this link a `needs_human`
        # would stand forever unseen and consensus would be declared over it.
        item = docket.items.get(item_id)
        text = (note or "").strip() or (
            f"{item.claim} — is a human decision needed here?" if item else").")
        question, _ = raise_question(docket, text=text, asker=agent, item_id=item_id,
                                     blocking=True, round_=round_,
                                     context=(item.claim if item else ""))
        new.question_id = question.id
        events.append(("escalation_raised", {"item": item_id, "asker": agent,
                                             "round": round_, "id": question.id}))
    return events


def challenges(docket: Docket, item_id: str, judged_round: int,
               asker: str) -> list[dict]:
    """§5.2.8's challenge: a reasoned `disagree` or refuting note from a **non-asking** seat."""
    out = []
    for other in docket.roster:
        if other == asker:
            continue
        record = docket.stance(item_id, other)
        if (record and not record.declined and record.stance == DISAGREE
                and record.reasoned and record.round == judged_round):
            out.append({"by": other, "round": judged_round, "note": record.note,
                        "evidence": record.evidence, "defended": False})
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
    """A chair-raised question ends `retired` and nothing else (§5.2.8)."""
    return question.chair_raised and question.state == ASKED
