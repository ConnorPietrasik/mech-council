"""§5.1's derivation and §5.2.3's closure/reopen.  Pure (§12.1.10).

The evaluation order is the *line* in §5.1, not the table's row order:
`human → disputed → agreed → rejected → unheard → unseen`.
"""
from __future__ import annotations

from .state import (AGREED, AGREE, DISAGREE, DISPUTED, Docket, HUMAN, Item,
                    NEEDS_HUMAN, OPEN, REJECTED, STANDING, UNHEARD, UNSEEN, Stance)


def support(docket: Docket, item_id: str) -> int:
    """§5.1's `agree` count over current stances (the denominator is frozen `N`)."""
    return sum(1 for agent in docket.roster
               if docket.effective_stance(item_id, agent) == AGREE)


def objections(docket: Docket, item_id: str) -> list[Stance]:
    """Current `disagree` stances; a `declined` record never objects (§5.2.1)."""
    return [s for s in (docket.stance(item_id, a) for a in docket.roster)
            if s is not None and not s.declined and s.stance == DISAGREE]


def derive(docket: Docket, item_id: str) -> str:
    """The disposition of an **open** item, from current stances and nothing else."""
    item = docket.items[item_id]
    if item.closed:                       # §5.2.3: closure is stored, never re-derived
        return item.disposition
    if docket.standing_escalations(item_id):
        return HUMAN
    counted = [docket.effective_stance(item_id, a) for a in docket.roster]
    agrees = [c for c in counted if c == AGREE]
    disputes = [c for c in counted if c == DISAGREE]
    if agrees and disputes:
        return DISPUTED
    complete = all(c is not None for c in counted)
    if complete and len(agrees) >= docket.bar() and not disputes:
        return AGREED
    if complete and not agrees and any(s.reasoned for s in objections(docket, item_id)):
        return REJECTED
    if complete:
        return UNHEARD
    return UNSEEN


def closure_pass(docket: Docket, judged_round: int) -> list[tuple[str, dict]]:
    """§5.2.3: close what reached `agreed`/`rejected`; reopen what a challenge undoes.

    Returns `(event_name, fields)` records for the console/events layer.  Reopening
    clears every stance record for the item, `declined` included (§6.2), so the ladder
    applies again and the item is `unseen` this round.
    """
    events: list[tuple[str, dict]] = []
    for item_id in list(docket.order):
        item = docket.items[item_id]
        if item.closed:
            challenge = _reopen_trigger(docket, item, judged_round)
            if challenge is None:
                _record_closed_notes(docket, item, judged_round)
                continue
            prior_disposition, prior_round = item.disposition, item.closed_round
            asker, reason = challenge
            item.disposition, item.closed_round = OPEN, 0
            docket.stances[item_id] = {}
            item.history.append(f"{prior_disposition} in round {prior_round}, reopened in "
                                f"round {judged_round} by {asker} ({reason})")
            for key in [k for k, esc in docket.escalations.items() if k[0] == item_id]:
                del docket.escalations[key]
            events.append(("item_reopened", {"id": item_id, "prior_disposition": prior_disposition,
                                             "prior_round": prior_round, "challenger": asker,
                                             "round": judged_round}))
            continue
        state = derive(docket, item_id)
        if state in (AGREED, REJECTED):
            item.disposition, item.closed_round = state, judged_round
            item.history.append(f"{state} in round {judged_round}")
            events.append(("item_closed", {"id": item_id, "disposition": state,
                                           "round": judged_round}))
    return events


def _reopen_trigger(docket: Docket, item: Item, judged_round: int):
    """§5.2.3's reopen conditions, judged on what this round actually brought in."""
    for agent in docket.roster:
        record = docket.stance(item.id, agent)
        if record is None or record.declined or record.round != judged_round:
            continue
        if record.stance == NEEDS_HUMAN:
            return (agent, "needs_human")
        if record.stance == AGREE and item.disposition == REJECTED:
            return (agent, "agree")
        if record.stance == DISAGREE and item.disposition == AGREED and record.reasoned:
            return (agent, "cited evidence")
    return None


def _record_closed_notes(docket: Docket, item: Item, judged_round: int) -> None:
    """§5.2.3/§5.2.4: an unreasoned disagree on a closed item is a note, never a reopen."""
    for agent in docket.roster:
        record = docket.stance(item.id, agent)
        if record is None or record.declined or record.round != judged_round:
            continue
        if item.disposition == AGREED and record.stance == DISAGREE and not record.reasoned:
            note = f"unreasoned disagree from {agent} in round {judged_round}"
            if note not in item.history:
                item.history.append(note)
