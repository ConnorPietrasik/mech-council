from __future__ import annotations

from .state import AGREE, AGREED, DISAGREE, DISPUTED, HUMAN, NEEDS_HUMAN, OPEN, REJECTED, UNHEARD, UNSEEN, Docket, Item, Stance


def support(docket: Docket, item_id: str) -> int:
    return sum(1 for agent in docket.roster if docket.effective_stance(item_id, agent) == AGREE)


def objections(docket: Docket, item_id: str) -> list[Stance]:
    return [s for s in (docket.stance(item_id, a) for a in docket.roster) if s is not None and not s.declined and s.stance == DISAGREE]


def derive(docket: Docket, item_id: str) -> str:
    item = docket.items[item_id]
    if item.closed:
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


def closure_pass(docket: Docket, judged_round: int, *, reopen: bool = True) -> list[tuple[str, dict]]:
    """Close what the seats settled. `reopen=False` is the report's pass: it closes, and never reopens."""
    events: list[tuple[str, dict]] = []
    for item_id in list(docket.order):
        item = docket.items[item_id]
        if item.closed:
            challenge = _reopen_trigger(docket, item, judged_round) if reopen else None
            if challenge is None:
                _record_closed_notes(docket, item, judged_round)
                continue
            prior_disposition, prior_round = item.disposition, item.closed_round
            asker, reason = challenge
            item.disposition, item.closed_round = OPEN, 0
            docket.stances[item_id] = {}
            item.history.append(f"{prior_disposition} in round {prior_round}, reopened in round {judged_round} by {asker} ({reason})")
            for key in [k for k, esc in docket.escalations.items() if k[0] == item_id]:
                del docket.escalations[key]
            events.append(
                (
                    "item_reopened",
                    {
                        "id": item_id,
                        "prior_disposition": prior_disposition,
                        "prior_round": prior_round,
                        "challenger": asker,
                        "round": judged_round,
                    },
                )
            )
            continue
        state = derive(docket, item_id)
        if state in (AGREED, REJECTED):
            item.disposition, item.closed_round = state, judged_round
            item.history.append(f"{state} in round {judged_round}")
            events.append(("item_closed", {"id": item_id, "disposition": state, "round": judged_round}))
    return events


def _reopen_trigger(docket: Docket, item: Item, judged_round: int):
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
    for agent in docket.roster:
        record = docket.stance(item.id, agent)
        if record is None or record.declined or record.round != judged_round:
            continue
        if item.disposition == AGREED and record.stance == DISAGREE and not record.reasoned:
            note = f"unreasoned disagree from {agent} in round {judged_round}"
            if note not in item.history:
                item.history.append(note)
