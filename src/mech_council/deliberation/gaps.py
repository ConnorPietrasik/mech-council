from __future__ import annotations

from .state import Docket

LADDER_CEILING = 4  # a ceiling for the seat-round, never per cause
MECHANICAL = "mechanical"
DELIBERATE = "deliberate"

#: The gap ladder's cause table: the cause sets the length.
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


def ladder_plan(*, missing: list[str], missing_backing: bool, cause: str, asks_used: int, chunk: int = 12) -> list[list[str]]:
    pending = list(missing) + (["backing"] if missing_backing else [])
    budget = max(0, CEILING[route(cause)] - asks_used)
    if not pending or budget <= 0:
        return []
    chunk = max(1, chunk)
    groups = [pending[i : i + chunk] for i in range(0, len(pending), chunk)]
    return groups[:budget]
