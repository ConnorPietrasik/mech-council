"""§5.2.7's termination checks — both orders, and plan's direction rule.  Pure.

The same function serves the provisional pre-checkpoint evaluation (§8's wait rule) and
the real post-checkpoint check; only the state handed to it differs.  `go` means "keep
going"; anything else is an outcome (§2).
"""
from __future__ import annotations

from .dispositions import derive
from .escalations import effective_class
from .state import (ANSWERED, ASKED, CONSENSUS, DECLINED, DISPUTED, Docket, GO,
                    NEEDS_OUTCOME, NO_PREFERENCE, SKIPPED, UNSEEN)


def unsettled_questions(docket: Docket) -> list:
    """§8's **ask set**: every question not yet put that its asker still holds.

    An item escalation counts while it is **standing**; a chair-raised or item-less
    question counts until it is answered or declined.  A `withdrawn`, `retired` or
    `demoted` question is never printed or waited on again.
    """
    out = []
    for qid in docket.question_order:
        question = docket.questions[qid]
        if question.state != ASKED:
            continue
        if question.item_id is not None and not question.chair_raised:
            holders = [e for e in docket.standing_escalations(question.item_id)
                       if e.question_id == question.id or e.asker in question.askers]
            if not holders:
                continue
        out.append(question)
    return out


def blocking_questions(docket: Docket) -> list:
    """§5.2.8's effective-blocking subset of the ask set — the only ones that may wait."""
    return [q for q in unsettled_questions(docket)
            if effective_class(docket, q) == "blocking"]


def declined_blocking(docket: Docket) -> list:
    """§5.2.7's "asked and declined", plan D6: the check consults the **question record**.

    A skip retires what it covers (§8), so the escalation state is gone by now; the
    question still forced the outcome, which is exactly why §5.2.7 reads "still standing,
    or asked and declined" rather than "still standing".
    """
    return [q for q in (docket.questions[i] for i in docket.question_order)
            if q.state == SKIPPED and effective_class(docket, q) == "blocking"]


def direction_rule(docket: Docket) -> tuple[bool, str | None, str]:
    """§5.2.7's three conditions, checked in order.  `(consensus, token, reason)`."""
    unsigned = [a for a in docket.roster if a not in docket.signoffs]
    if unsigned:
        return False, None, "sign-off missing: " + ",".join(unsigned)
    unnamed = [a for a in docket.roster if docket.backing.get(a) in (None, DECLINED)]
    if unnamed:
        return False, None, "backing not named by: " + ",".join(unnamed)
    named = {docket.backing[a] for a in docket.roster}
    real = {t for t in named if t != NO_PREFERENCE}
    if len(real) > 1:
        return False, None, ("seats name different directions: "
                             + ",".join(sorted(named)))
    if not real:
        return False, None, "no seat named a direction (no_preference never causes consensus)"
    return True, next(iter(real)), "every seat backs one direction or holds no preference"


def evaluate(docket: Docket) -> tuple[str, str]:
    """§5.2.7's termination order, first match wins — and the order differs by mode.

    *Review:* sign-off → disputed/unseen → an unanswered **or asked-and-declined**
    effective-blocking question → consensus.
    *Plan:* sign-off → that same question check, which **precedes** the dispute checks →
    disputed/unseen → the direction rule.

    There is no separate "an escalation stands" row on purpose: §5.1 says a `human` item
    forces `needs_human` **only** through §5.2.8's effective-blocking test, and every
    escalation carries a question (§5.2.8), so the question check *is* the escalation
    check.  That is also why no escalation can stand when `consensus` is returned: §8's
    terminal checkpoint has just put the whole ask set to the human.
    """
    unsigned = [a for a in docket.roster if a not in docket.signoffs]
    if unsigned:
        return GO, "not everyone signed off: " + ",".join(unsigned)
    if docket.mode == "plan":
        if answer := _open_effective_blocking(docket):
            return NEEDS_OUTCOME, answer
    states = {item_id: derive(docket, item_id) for item_id in docket.order
              if not docket.items[item_id].closed}
    moving = sorted(i for i, s in states.items() if s in (DISPUTED, UNSEEN))
    if moving:
        return GO, ("disputed or unseen items: " if docket.mode == "review"
                    else "disputed or unseen topics: ") + ",".join(moving)
    if answer := _open_effective_blocking(docket):
        return NEEDS_OUTCOME, answer
    if docket.mode == "plan":
        agreed, token, reason = direction_rule(docket)
        if agreed:
            return CONSENSUS, f"{reason} ({token})"
        return GO, reason
    return CONSENSUS, "no disputed, unseen or unanswered effective-blocking work"


def _open_effective_blocking(docket: Docket) -> str | None:
    """§5.2.7's one question row: still standing, **or** asked and declined (§8)."""
    if blocked := blocking_questions(docket):
        return "unanswered effective-blocking question: " + ",".join(q.id for q in blocked)
    if declined := declined_blocking(docket):
        return ("effective-blocking question asked and declined: "
                + ",".join(q.id for q in declined))
    return None


def answered_since(docket: Docket, round_: int) -> bool:
    """§5.2.7: an answer buys a full round; a checkpoint where everything was declined
    settles nothing further."""
    return any(q.state == ANSWERED and q.answer_round >= round_ for q in docket.questions.values())
