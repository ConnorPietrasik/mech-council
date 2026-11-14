from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from .chairwork import Chair
from .config import Council
from .console import Console


class LanesLike(Protocol):
    """What the watchdog reads of the lanes: a busy chair, a digest, and the restart route."""

    @property
    def chair_busy(self) -> bool: ...

    def digest(self) -> list[dict]: ...

    def resolve(self, label: str) -> str | None: ...

    def display_for(self, file_base: str) -> str: ...

    async def abort_member(self, file_base: str) -> None: ...

    async def restart_member(self, file_base: str, *, why: str) -> None: ...


@dataclass
class Watchdog:
    council: Council
    lanes: LanesLike
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
        """Cancel the loop; never abort the chair's pass mid-flight."""
        self.stopped.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as failure:
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
            self.deferred += 1
            self.console.event("watchdog_deferred", pass_in_flight=True, deferred=self.deferred)
            return None
        rows = self.lanes.digest()
        if not rows:
            return None
        parsed = await self.chair.digest(rows)
        if parsed is None:
            return None
        await self.enforce(parsed)
        return parsed

    async def enforce(self, parsed: dict) -> None:
        for decision in parsed.get("decisions") or []:
            label = decision.get("agent") or decision.get("id") or ""
            action = decision.get("action") or ""
            reason = decision.get("reason") or decision.get("note") or ""
            if action == "restart":
                file_base = self.lanes.resolve(label)
                if file_base is None:
                    self.console.event("watchdog_unknown_agent", member=label)
                    self.console.say(f"watchdog: the chair named {label!r}, which is not a member of this run — nobody is restarted")
                    continue
                self.console.event("watchdog_restart", member=label, reason=reason)
                self.console.say(f"watchdog: restarting {self.lanes.display_for(file_base)} ({reason or 'no reason given'})")
                await self._restart(file_base, reason or "watchdog restart")
            elif action == "keep":
                if reason:
                    self.console.event("watchdog_keep", member=label, note=reason)
            else:
                # a restart wipes the seat's session, so only a spelled-out restart may spend it
                self.console.event("watchdog_unknown_action", member=label, action=action)
                self.console.say(f"watchdog: {action or 'nothing'} is not keep or restart for {label!r} — the seat is kept")

    async def _restart(self, file_base: str, why: str) -> None:
        try:
            await self.lanes.abort_member(file_base)
        except Exception:
            pass
        try:
            await self.lanes.restart_member(file_base, why=why)
        except Exception as failure:
            self.console.event("member_spawn_failed", member=file_base, error=str(failure))
            self.console.say(f"watchdog: {self.lanes.display_for(file_base)} could not be restarted: {failure}")
