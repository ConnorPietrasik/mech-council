from __future__ import annotations

from .state import Docket

LADDER_CEILING = 4
MECHANICAL = "mechanical"
DELIBERATE = "deliberate"

#: asks a seat gets in one seat-round: the cause sets the length
CEILING = {MECHANICAL: LADDER_CEILING, DELIBERATE: 1}


def missing_items(docket: Docket, agent: str, shown: list[str]) -> list[str]:
    out = []
    for item_id in shown:
        record = docket.stance(item_id, agent)
        if record is None:
            out.append(item_id)
    return out


def missing_backing(docket: Docket, agent: str) -> bool:
    if docket.mode != "plan":
        return False
    return not docket.backing.get(agent)


def route(cause: str) -> str:
    return MECHANICAL if cause == MECHANICAL else DELIBERATE


def ladder_plan(*, missing: list[str], cause: str, asks_used: int, chunk: int = 12) -> list[list[str]]:
    """The chunks the asks this seat has left cover, `chunk` items each. `missing` carries the `backing` pseudo-item."""
    budget = max(0, CEILING[route(cause)] - asks_used)
    if not missing or budget <= 0:
        return []
    chunk = max(1, chunk)
    groups = [missing[i : i + chunk] for i in range(0, len(missing), chunk)]
    return groups[:budget]
