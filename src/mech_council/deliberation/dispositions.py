from __future__ import annotations

from .state import (
    ABSTAIN,
    AGREE,
    AGREED,
    DISAGREE,
    DISPUTED,
    HUMAN,
    NEEDS_HUMAN,
    OPEN,
    REJECTED,
    SPLIT,
    UNHEARD,
    UNSEEN,
    Docket,
    Item,
    Stance,
)

#: a closed item's challenge budget: how many times it may be reopened in its life
MAX_REOPENS = 2
#: a disputed episode's budget: the closure passes an objection may stand before the bar rules on it
DISPUTE_ROUNDS = 3


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
    # a bare dissent is noted, not blocking: only a reasoned objection holds the bar
    disputes = [s for s in objections(docket, item_id) if s.reasoned]
    if agrees and disputes:
        return DISPUTED
    complete = all(c is not None for c in counted)
    if complete and len(agrees) >= docket.bar():
        return AGREED
    if complete and not agrees and disputes:
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
            if item.disposition == SPLIT:
                continue  # a recorded split is terminal: the council does not relitigate it
            challenge = _reopen_trigger(docket, item, judged_round) if reopen else None
            if challenge is None:
                _record_closed_notes(docket, item, judged_round)
                continue
            prior_disposition, prior_round = item.disposition, item.closed_round
            asker, reason = challenge
            if item.reopen_count >= MAX_REOPENS:
                item.history.append(
                    f"challenge in round {judged_round} by {asker} ({reason}) refused: the item was already reopened "
                    f"{item.reopen_count} times; the closure stands"
                )
                _record_closed_notes(docket, item, judged_round)
                continue
            _reopen(docket, item, judged_round, asker, reason)
            events.append(
                (
                    "item_reopened",
                    {
                        "id": item_id,
                        "prior_disposition": prior_disposition,
                        "prior_round": prior_round,
                        "challenger": asker,
                        "reason": reason,
                        "round": judged_round,
                    },
                )
            )
            continue
        state = derive(docket, item_id)
        if state == DISPUTED:
            if item.disputed_since == 0:
                item.disputed_since = judged_round
            elif judged_round - item.disputed_since >= DISPUTE_ROUNDS:
                events.append(_freeze(docket, item, judged_round))
            continue
        item.disputed_since = 0
        if state in (AGREED, REJECTED):
            item.disposition, item.closed_round = state, judged_round
            item.history.append(f"{state} in round {judged_round}")
            events.append(("item_closed", {"id": item_id, "disposition": state, "round": judged_round}))
    return events


def _reopen(docket: Docket, item: Item, judged_round: int, asker: str, reason: str) -> None:
    """Reopen without a wipe: the closure's supporters keep their stances, so the item re-derives
    as the dispute the challenge claims — never back to nobody having seen it."""
    prior_disposition, prior_round = item.disposition, item.closed_round
    kept = {
        agent: record
        for agent, record in docket.stances[item.id].items()
        if record.round == judged_round or (not record.declined and _supports(record.stance, prior_disposition))
    }
    docket.stances[item.id] = kept
    item.disposition, item.closed_round = OPEN, 0
    item.disputed_since = 0
    item.reopen_count += 1
    item.history.append(f"{prior_disposition} in round {prior_round}, reopened in round {judged_round} by {asker} ({reason})")
    # a needs_human challenge is a hold raised this round: it is why the item reopens, so it stands —
    # the stale escalations of the closed episode go
    for key in [k for k, esc in docket.escalations.items() if k[0] == item.id and esc.raised_round != judged_round]:
        del docket.escalations[key]


def _supports(stance: str, prior_disposition: str) -> bool:
    """Whether a stance stood with the closure it survived."""
    if prior_disposition == AGREED:
        return stance == AGREE
    return stance in (DISAGREE, ABSTAIN)


def _freeze(docket: Docket, item: Item, judged_round: int) -> tuple[str, dict]:
    """End a disputed episode on its budget: the bar rules — over it, or as the recorded split."""
    agrees = support(docket, item.id)
    disputes = len([s for s in objections(docket, item.id) if s.reasoned])
    since = item.disputed_since
    if agrees >= docket.bar():
        item.disposition, item.closed_round = AGREED, judged_round
        item.history.append(
            f"agreed in round {judged_round}: the refutation stood {DISPUTE_ROUNDS} rounds and the bar held "
            f"({agrees} agree, {disputes} reasoned disagree, disputed since round {since})"
        )
    else:
        item.disposition, item.closed_round = SPLIT, judged_round
        item.history.append(
            f"split in round {judged_round}: {agrees} agree, {disputes} reasoned disagree, "
            f"disputed since round {since} — the council records the split"
        )
    item.disputed_since = 0
    return "item_closed", {"id": item.id, "disposition": item.disposition, "round": judged_round}


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
