"""The implement run's rosters: the builder's own council plus the borrowed planner and reviewer crews.

A crew is a council of its own, spawned and disposed at phase boundaries. Seats wear the run's
global `AgentN` ids; `CrewLanes` spans the live crews' `Lanes`.
"""

from __future__ import annotations

import asyncio

from .asks import Lanes, Outcome
from .chairwork import Chair
from .config import Council
from .console import Console, Transcripts
from .members import MemberGone, Members
from .process import spawn
from .prompts.brief import Brief
from .runjson import RunManifest
from .sandbox.setup import Seat


class Crew:
    """One roster of the run: its council, live members, lanes and chair passes."""

    def __init__(
        self,
        label: str,
        council: Council,
        *,
        sandbox: Seat,
        run_dir: str,
        runs_dir: str,
        console: Console,
        manifest: RunManifest,
        transcripts: Transcripts,
    ) -> None:
        self.label = label
        self.council = council
        self.sandbox = sandbox
        self.run_dir = run_dir
        self.runs_dir = runs_dir
        self.console = console
        self.manifest = manifest
        self.transcripts = transcripts
        self.members: Members | None = None
        self.lanes: Lanes | None = None
        self.chair: Chair | None = None
        self.brief: Brief | None = None
        self.prompts: dict[str, int] | None = None  # the last lanes' counts: a disposed crew forgets them otherwise
        self.live = False
        self.disposed = False

    @property
    def live_members(self) -> dict[str, spawn.Member]:
        return self.members.members if self.members else {}

    def prepare(self) -> None:
        """The scratch slots the crew's seats will want, before any of them spawns."""
        for base in self.council.member_file_bases:
            self.sandbox.prepare_member(base)

    async def start(self, seats: bool = True) -> None:
        """Spawn the crew's members — the chair always, the seats unless `seats` is off."""
        self.members = Members(
            council=self.council,
            sandbox=self.sandbox,
            run_dir=self.run_dir,
            runs_dir=self.runs_dir,
            console=self.console,
            manifest=self.manifest,
            transcripts=self.transcripts,
        )
        self.lanes = Lanes(self.council, self.members, self.console)
        self.brief = Brief(mode=self.council.mode, project=self.council.project, in_place=self.council.in_place, human=self.council.human)
        self.chair = Chair(self.council, self.lanes, self.console, self.brief)
        specs = [self.council.chair, *(self.council.agents if seats else [])]
        results = await asyncio.gather(*(self._start_one(spec) for spec in specs), return_exceptions=True)
        for spec, result in zip(specs, results):
            if isinstance(result, BaseException):
                await self.dispose()
                raise result
        self.live = True

    async def _start_one(self, spec) -> None:
        """One seat or chair, wrapped the way `start_all` wraps it: the run's abort paths expect MemberGone."""
        assert self.members is not None
        try:
            await self.members._start_one(spec)
        except MemberGone, asyncio.CancelledError:
            raise
        except BaseException as failure:
            raise MemberGone(f"{spec.name}: {failure}") from failure

    def seat(self, agent_id: str):
        """The live member under a global seat id, or None."""
        for spec in self.council.agents:
            if spec.id == agent_id:
                return self.members.members.get(spec.file_base) if self.members else None
        return None

    async def start_seats(self) -> None:
        """A crew started chair-only: its seats join now, at the execution phase."""
        assert self.members is not None
        for spec in self.council.agents:
            await self._start_one(spec)

    async def message(self, key: str, message: str, *, round_: int, phase: str):
        assert self.lanes is not None
        return await self.lanes.message(key, message, round_=round_, phase=phase)

    def crew_for_file_base(self, file_base: str) -> Crew | None:
        return self if file_base in self.live_members else None

    def ask_seats(self, batch, **kw):
        assert self.lanes is not None
        return self.lanes.ask_seats(batch, **kw)

    async def ask_seat(self, agent_id: str, message: str, **kw):
        assert self.lanes is not None
        return self.lanes.ask_agent(agent_id, message, **kw)

    async def dispose(self, *, fast: bool = False):
        from .process import lifecycle

        result = lifecycle.SweepResult()
        if self.members is not None:
            result = await self.members.dispose_fast() if fast else await self.members.dispose_all()
        if self.lanes is not None:
            self.prompts = self.lanes.prompts
        self.live = False
        self.disposed = True
        self.members = None
        self.lanes = None
        self.chair = None
        return result


class Crews:
    """The run's crews in crew order: the planner, the builder, the reviewer."""

    def __init__(
        self,
        councils: list[tuple[str, Council]],
        *,
        sandbox: Seat,
        run_dir: str,
        runs_dir: str,
        console: Console,
        manifest: RunManifest,
        transcripts: Transcripts,
    ) -> None:
        self.crews: dict[str, Crew] = {}
        self.order: list[str] = []
        for label, council in councils:
            crew = Crew(
                label,
                council,
                sandbox=sandbox,
                run_dir=run_dir,
                runs_dir=runs_dir,
                console=console,
                manifest=manifest,
                transcripts=transcripts,
            )
            self.crews[label] = crew
            self.order.append(label)
        self.builder = self.crews.get("builder")
        self.planner = self.crews.get("planner")
        self.reviewer = self.crews.get("reviewer")

    def __getitem__(self, label: str) -> Crew:
        return self.crews[label]

    def get(self, label: str) -> Crew | None:
        return self.crews.get(label)

    @property
    def live(self) -> list[Crew]:
        return [self.crews[label] for label in self.order if self.crews[label].live]

    def crew_of_file_base(self, file_base: str) -> Crew | None:
        for label in self.order:
            crew = self.crews[label].crew_for_file_base(file_base)
            if crew is not None:
                return crew
        return None

    def names(self) -> dict[str, str]:
        """Every roster key to the name a human reads: the seats by global id, each crew's chair by file_base."""
        out: dict[str, str] = {}
        for label in self.order:
            council = self.crews[label].council
            for spec in council.agents:
                if spec.id is not None:
                    out[spec.id] = spec.name
            out[council.chair.file_base] = council.chair.name
        return out

    async def send_message(self, key: str, message: str, *, round_: int, phase: str) -> Outcome:
        """The human's direct message, routed across the run: a crew's chair by file_base, a seat by its global id.

        The checkpoint is shown every crew's names, so the target may sit in a crew not up yet or already
        disposed: refusing is the cheap path, waking a crew is not.
        """
        who = self.names().get(key, key)
        for label in self.order:
            crew = self.crews[label]
            council = crew.council
            if key == council.chair.file_base:
                if crew.live:
                    return await crew.message(key, message, round_=round_, phase=phase)
                if crew.disposed:
                    return Outcome(text="", failure=f"{who} is not in this run right now")
                return Outcome(text="", failure=f"{who} is not up yet: try again at the next checkpoint")
            for spec in council.agents:
                if spec.id != key:
                    continue
                if crew.live and crew.seat(key) is not None:
                    return await crew.message(key, message, round_=round_, phase=phase)
                if crew.disposed:
                    return Outcome(text="", failure=f"{who} is not in this run right now")
                return Outcome(text="", failure=f"{who} is not up yet: try again at the next checkpoint")
        return Outcome(text="", failure=f"{who} is not in this run right now")

    async def dispose_all(self, *, fast: bool = False):
        from .process import lifecycle

        combined = lifecycle.SweepResult()
        for label in reversed(self.order):
            if self.crews[label].live:
                result = await self.crews[label].dispose(fast=fast)
                combined.pids = sorted(set(combined.pids) | set(result.pids))
                combined.namespaces = sorted(set(combined.namespaces) | set(result.namespaces))
        return combined

    def lanes(self) -> "CrewLanes":
        return CrewLanes(self)


class CrewLanes:
    """The watchdog's view over the live crews: one digest, one chair, restarts routed home."""

    def __init__(self, crews: Crews) -> None:
        self.crews = crews

    @property
    def chair_busy(self) -> bool:
        return any(crew.lanes.chair_busy for crew in self.crews.live if crew.lanes is not None)

    def digest(self) -> list[dict]:
        rows: list[dict] = []
        for crew in self.crews.live:
            if crew.lanes is not None:
                for row in crew.lanes.digest():
                    # the run's chair reads one roster over every crew: label each row the seat's global ID
                    agent = crew.lanes.label_for(row["file_base"])
                    if agent == "chair" and crew.label != "builder":
                        # a borrowed crew's chair would otherwise read as the run's own `chair`
                        agent = f"chair ({crew.label})"
                    rows.append({**row, "agent": agent})
        return rows

    def resolve(self, label: str) -> str | None:
        for crew in self.crews.live:
            if crew.lanes is not None and (file_base := crew.lanes.resolve(label)) is not None:
                return file_base
        return None

    def display_for(self, file_base: str) -> str:
        crew = self.crews.crew_of_file_base(file_base)
        if crew is None or crew.members is None:
            return file_base
        if file_base == crew.council.chair.file_base:
            return crew.council.chair.name
        return crew.members.display_for(file_base)

    async def abort_member(self, file_base: str) -> None:
        crew = self.crews.crew_of_file_base(file_base)
        if crew is not None and crew.lanes is not None:
            await crew.lanes.abort_member(file_base)

    async def restart_member(self, file_base: str, *, why: str) -> None:
        crew = self.crews.crew_of_file_base(file_base)
        if crew is not None and crew.lanes is not None:
            await crew.lanes.restart_member(file_base, why=why)

    async def abort_all(self) -> None:
        await asyncio.gather(*(crew.lanes.abort_all() for crew in self.crews.live if crew.lanes is not None), return_exceptions=True)
