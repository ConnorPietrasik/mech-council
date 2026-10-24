"""§5.2.6's round signature — "stalemate = no movement", computed from data only."""
from __future__ import annotations

from .dispositions import derive
from .escalations import effective_class
from .state import ASKED, ANSWERED, Docket, STANDING


def signature(docket: Docket) -> tuple:
    """§5.2.6, mode-generic (plan D18): the only plan-only input is the backing map.

    A withdrawal, a demotion, an effective-class change and a stance turning `declined`
    are each movement; so is every answer, which is why the signature is recorded
    *before* the checkpoint.
    """
    items = []
    for item_id in docket.order:
        item = docket.items[item_id]
        stances = sorted(
            f"{agent}:{r.stance}{'|declined' if r.declined else ''}"
            for agent, r in docket.stances.get(item_id, {}).items())
        escalation_states = sorted(
            f"{esc.asker}:{esc.state}" for esc in docket.escalations.values()
            if esc.item_id == item_id)
        items.append((item_id, item.disposition, item.closed_round,
                      derive(docket, item_id) if not item.closed else item.disposition,
                      tuple(stances), tuple(escalation_states)))
    escalations = sorted(
        (esc.item_id, esc.asker, esc.state,
         effective_class(docket, docket.questions[esc.question_id])
         if esc.question_id in docket.questions else "deferred")
        for esc in docket.escalations.values())
    answered_qa = sum(1 for qid in docket.qa_order if docket.qa[qid].answer)
    answered_human = sum(1 for qid in docket.question_order
                         if docket.questions[qid].state == ANSWERED)
    parts = [tuple(items), tuple(sorted(docket.signoffs.items())), escalations,
             answered_qa, answered_human]
    if docket.mode == "plan":
        parts.append(tuple(sorted(docket.backing.items())))
    return tuple(parts)


def stalemate(docket: Docket, current: tuple, previous: tuple | None,
              checkpoint_moved: bool) -> bool:
    """§5.2.6: identical consecutive signatures after round 1, and no checkpoint movement."""
    if docket.round <= 1 or previous is None or checkpoint_moved:
        return False
    return current == previous
