from __future__ import annotations

from .dispositions import derive
from .escalations import effective_class
from .state import ANSWERED, Docket


def signature(docket: Docket) -> tuple:
    items = []
    for item_id in docket.order:
        item = docket.items[item_id]
        stances = sorted(f"{agent}:{r.stance}{'|declined' if r.declined else ''}" for agent, r in docket.stances.get(item_id, {}).items())
        escalation_states = sorted(f"{esc.asker}:{esc.state}" for esc in docket.escalations.values() if esc.item_id == item_id)
        items.append(
            (
                item_id,
                item.disposition,
                item.closed_round,
                derive(docket, item_id) if not item.closed else item.disposition,
                tuple(stances),
                tuple(escalation_states),
            )
        )
    escalations = sorted(
        (
            esc.item_id,
            esc.asker,
            esc.state,
            effective_class(docket, docket.questions[esc.question_id]) if esc.question_id in docket.questions else "deferred",
        )
        for esc in docket.escalations.values()
    )
    answered_qa = sum(1 for qid in docket.qa_order if docket.qa[qid].answer)
    answered_human = sum(1 for qid in docket.question_order if docket.questions[qid].state == ANSWERED)
    parts = [tuple(items), tuple(sorted(docket.signoffs.items())), escalations, answered_qa, answered_human]
    if docket.mode == "plan":
        parts.append(tuple(sorted(docket.backing.items())))
    return tuple(parts)


def stalemate(docket: Docket, current: tuple, previous: tuple | None, checkpoint_moved: bool) -> bool:
    if docket.round <= 1 or previous is None or checkpoint_moved:
        return False
    return current == previous
