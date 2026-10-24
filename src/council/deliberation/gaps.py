"""The gap ladder — the pure part: what is missing, what caused it, how long the ladder is.

The engine owns the asks; this module owns the arithmetic, so the cause
table is a lookup and not an `if` chain scattered through the loop.
"""

from __future__ import annotations

from .state import Docket

LADDER_CEILING = 4  # a ceiling for the seat-round, never per cause
MECHANICAL = "mechanical"
DELIBERATE = "deliberate"

#: The gap ladder's cause table: the cause sets the length.
CEILING = {MECHANICAL: LADDER_CEILING, DELIBERATE: 1}


def missing_items(docket: Docket, agent: str, shown: list[str]) -> list[str]:
    """Open items this seat was shown and returned nothing for.

    An `abstain` is a stance and a `declined` record carries forward, so neither is
    a gap; closed items are never gaps either — the shown set here is open-only.
    """
    out = []
    for item_id in shown:
        record = docket.stance(item_id, agent)
        if record is None:
            out.append(item_id)
    return out


def missing_backing(docket: Docket, agent: str) -> bool:
    """A missing or unmappable `backing` is a gap; `no_preference` never is."""
    if docket.mode != "plan":
        return False
    return not docket.backing.get(agent)


def route(cause: str) -> str:
    return MECHANICAL if cause == MECHANICAL else DELIBERATE


def ladder_plan(*, missing: list[str], missing_backing: bool, cause: str, asks_used: int, chunk: int = 12) -> list[list[str]]:
    """The asks a ladder still owes, each naming only what is still missing for that seat.

    A docket too large for one reply is **chunked** across successive asks of the same ladder, still at most one
    ask per item or backing, and the seat-round ceiling is never refilled by a restart or a watchdog decision.
    """
    pending = list(missing) + (["backing"] if missing_backing else [])
    budget = max(0, CEILING[route(cause)] - asks_used)
    if not pending or budget <= 0:
        return []
    chunk = max(1, chunk)
    groups = [pending[i : i + chunk] for i in range(0, len(pending), chunk)]
    return groups[:budget]


def declined_records(docket: Docket, agent: str) -> list[str]:
    return [item_id for item_id, stances in docket.stances.items() if (record := stances.get(agent)) is not None and record.declined]
