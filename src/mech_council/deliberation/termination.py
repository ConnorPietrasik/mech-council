from __future__ import annotations

from .dispositions import derive
from .escalations import effective_class
from .state import ASKED, CONSENSUS, DECLINED, DISPUTED, GO, NEEDS_OUTCOME, NO_PREFERENCE, SKIPPED, UNSEEN, Docket


def unsettled_questions(docket: Docket) -> list:
    out = []
    for qid in docket.question_order:
        question = docket.questions[qid]
        if question.state != ASKED:
            continue
        if question.item_id is not None and not question.chair_raised:
            holders = [
                e for e in docket.standing_escalations(question.item_id) if e.question_id == question.id or e.asker in question.askers
            ]
            if not holders:
                continue
        out.append(question)
    return out


def blocking_questions(docket: Docket) -> list:
    return [q for q in unsettled_questions(docket) if effective_class(docket, q) == "blocking"]


def declined_blocking(docket: Docket) -> list:
    return [
        q for q in (docket.questions[i] for i in docket.question_order) if q.state == SKIPPED and effective_class(docket, q) == "blocking"
    ]


def direction_rule(docket: Docket) -> tuple[bool, str | None, str]:
    unsigned = [a for a in docket.roster if a not in docket.signoffs]
    if unsigned:
        return False, None, "sign-off missing: " + ",".join(unsigned)
    unnamed = [a for a in docket.roster if docket.backing.get(a) in (None, DECLINED)]
    if unnamed:
        return False, None, "backing not named by: " + ",".join(unnamed)
    named = {docket.backing[a] for a in docket.roster}
    real = {t for t in named if t != NO_PREFERENCE}
    if len(real) > 1:
        return False, None, ("seats name different directions: " + ",".join(sorted(named)))
    if not real:
        return False, None, "no seat named a direction (no_preference never causes consensus)"
    return True, next(iter(real)), "every seat backs one direction or holds no preference"


def evaluate(docket: Docket) -> tuple[str, str]:
    unsigned = [a for a in docket.roster if a not in docket.signoffs]
    if unsigned:
        return GO, "not everyone signed off: " + ",".join(unsigned)
    if docket.mode == "plan":
        if answer := _open_effective_blocking(docket):
            return NEEDS_OUTCOME, answer
    states = {item_id: derive(docket, item_id) for item_id in docket.order if not docket.items[item_id].closed}
    moving = sorted(i for i, s in states.items() if s in (DISPUTED, UNSEEN))
    if moving:
        return GO, ("disputed or unseen items: " if docket.mode == "review" else "disputed or unseen topics: ") + ",".join(moving)
    if answer := _open_effective_blocking(docket):
        return NEEDS_OUTCOME, answer
    if docket.mode == "plan":
        agreed, token, reason = direction_rule(docket)
        if agreed:
            return CONSENSUS, f"{reason} ({token})"
        return GO, reason
    return CONSENSUS, "no disputed, unseen or unanswered effective-blocking work"


def _open_effective_blocking(docket: Docket) -> str | None:
    if blocked := blocking_questions(docket):
        return "unanswered effective-blocking question: " + ",".join(q.id for q in blocked)
    if declined := declined_blocking(docket):
        return "effective-blocking question asked and declined: " + ",".join(q.id for q in declined)
    return None
