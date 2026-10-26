from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .chairwork import Chair
from .config import Council
from .console import Console

if TYPE_CHECKING:
    from .asks import Lanes


@dataclass
class Watchdog:
    council: Council
    lanes: Lanes
    chair: Chair
    console: Console
    task: asyncio.Task | None = None
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    ticks: int = 0
    deferred: int = 0

    def start(self) -> None:
        if self.council.watchdog_sec <= 0:
            self.console.say("watchdog disabled (watchdog_sec = 0): a wedged member will hang the run with no diagnosis")
            return
        self.task = asyncio.create_task(self._loop(), name="watchdog")

    async def stop(self) -> None:
        """Cancel the loop; never abort the chair's pass — a cancelled await mid-way would report `ChairUnusable` for a pass
        that was merely interrupted.
        """
        self.stopped.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as failure:
                # Cancelling is the normal end, but a bug inside the loop is not; it
                # would otherwise vanish with the task and the run would look clean.
                self.console.event("watchdog_failed", error=f"{type(failure).__name__}: {failure}", ticks=self.ticks)
                self.console.say(f"watchdog: the loop raised {type(failure).__name__}: {failure} — its ticks did not all complete")

    async def _loop(self) -> None:
        while not self.stopped.is_set():
            try:
                await asyncio.wait_for(self.stopped.wait(), timeout=self.council.watchdog_sec)
                return
            except TimeoutError:
                pass
            self.ticks += 1
            await self.pass_once()

    async def pass_once(self) -> dict | None:
        if self.lanes.chair_busy:
            # A pass in flight suppresses a new tick; it never queues one behind it.
            self.deferred += 1
            self.console.event("watchdog_deferred", pass_in_flight=True, deferred=self.deferred)
            return None
        rows = self.lanes.digest()
        if not rows:
            # Nothing is in flight: the run is at a checkpoint or between rounds, where the
            # non-blocking stdin rule (`humanio.py`) says the watchdog has nothing to say.
            return None
        parsed = await self.chair.digest(rows)
        if parsed is None:
            return None
        await self.enforce(parsed)
        return parsed

    async def enforce(self, parsed: dict) -> None:
        for decision in parsed.get("decisions") or []:
            # `parse_watchdog` normalises to agent/action/reason; the chair's own field
            # names (`agent` or `id`, `reason` or `note`) are its extraction's business.
            label = decision.get("agent") or decision.get("id") or ""
            action = decision.get("action") or ""
            reason = decision.get("reason") or decision.get("note") or ""
            if action == "keep":
                if reason:
                    self.console.event("watchdog_keep", member=label, note=reason)
                continue
            file_base = self._file_base(label)
            if file_base is None:
                # A pass naming an id that isn't a member applies to nobody.
                self.console.event("watchdog_unknown_agent", member=label)
                self.console.say(f"watchdog: the chair named {label!r}, which is not a member of this council — nobody is restarted")
                continue
            self.console.event("watchdog_restart", member=label, reason=reason)
            self.console.say(f"watchdog: restarting {label} ({reason or 'no reason given'})")
            await self._restart(file_base, reason or "watchdog restart")

    async def _restart(self, file_base: str, why: str) -> None:
        member = self.lanes.members.members.get(file_base)
        if member is None:
            return
        try:
            await member.rpc.abort()
        except Exception:
            pass  # abort, then the group kill, which needs no answer
        try:
            await self.lanes.members.restart(file_base, why=why)
        except Exception as failure:
            self.console.event("member_spawn_failed", member=file_base, error=str(failure))
            self.console.say(f"watchdog: {file_base} could not be restarted: {failure}")

    def _file_base(self, label: str) -> str | None:
        for spec in self.council.agents:
            if label in (spec.id, spec.file_base, spec.name):
                return spec.file_base
        return None
