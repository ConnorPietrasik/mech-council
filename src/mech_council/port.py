from __future__ import annotations

from dataclasses import dataclass, field

from .asks import AskLost, AskUnusable, Lanes
from .chairwork import Chair, ChairUnusable
from .config import Council
from .console import Console
from .deliberation.engine import Abort, RawAsk
from .deliberation.state import CHAIR_ID, Docket
from .humaninput import Checkpoint
from .prompts.brief import Brief


@dataclass
class Port:
    council: Council
    lanes: Lanes
    chair: Chair
    brief: Brief
    console: Console
    checkpoint_run: Checkpoint
    docket: Docket
    names: dict[str, str] = field(default_factory=dict)

    # -- member asks -------------------------------------------------------------

    async def ask_seats(self, batch, *, round_: int, phase: str = "round", classify_unusable: bool = False) -> dict[str, RawAsk]:
        known = set(self.docket.items)
        closed = {i for i in known if self.docket.items[i].closed}

        def parse_for(agent: str) -> dict:
            return {"mode": self.council.mode, "me": agent, "known_items": known, "closed_items": closed}

        outcomes = await self.lanes.ask_seats(
            batch, round_=round_, kind="verdict", phase=phase, parse_for=parse_for, classify_unusable=classify_unusable
        )
        return {agent: _raw(outcome) for agent, outcome in outcomes.items()}

    async def ask_seat(self, agent: str, message: str, *, round_: int, phase: str = "round") -> RawAsk:
        known = set(self.docket.items)
        closed = {i for i in known if self.docket.items[i].closed}
        try:
            outcome = await self.lanes.ask_agent(
                agent,
                message,
                round_=round_,
                kind="verdict",
                phase=phase,
                parse_kw={"mode": self.council.mode, "me": agent, "known_items": known, "closed_items": closed},
            )
        except (AskUnusable, AskLost) as failure:
            return RawAsk(text=str(failure), failure=str(failure))
        return _raw(outcome)

    # -- chair asks (chairwork owns the lane rules) ---------------------------------

    async def chair_merge(self, listing: str, *, round_: int) -> dict:
        try:
            return await self.chair.merge(listing, round_=round_)
        except ChairUnusable as failure:
            raise Abort(str(failure), member=CHAIR_ID, round_=round_) from failure

    async def chair_compare(self, listing: str, *, round_: int) -> dict:
        try:
            return await self.chair.compare(listing, round_=round_)
        except ChairUnusable as failure:
            raise Abort(str(failure), member=CHAIR_ID, round_=round_) from failure

    async def chair_direction(self, listing: str, *, round_: int) -> dict:
        try:
            return await self.chair.direction(listing, round_=round_)
        except ChairUnusable as failure:
            raise Abort(str(failure), member=CHAIR_ID, round_=round_) from failure

    # -- the human ------------------------------------------------------------

    async def checkpoint(self, ask_set, *, round_: int, terminal: bool, listing: list, waiting: bool):
        return await self.checkpoint_run.run(listing, ask_set=ask_set, round_=round_, terminal=terminal, waiting=waiting)


def _raw(outcome) -> RawAsk:
    return RawAsk(text=outcome.text, failure=getattr(outcome, "failure", None), rejected=bool(outcome.rejected))
