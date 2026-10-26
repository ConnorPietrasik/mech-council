from __future__ import annotations

from .config import Council
from .console import Console
from .deliberation.state import CHAIR_ID
from .prompts.brief import Brief
from .sandbox.mount import WATCHDOG_PASS_TIMEOUT_S


class ChairUnusable(Exception):
    def __init__(self, phase: str, round_: int, detail: str) -> None:
        super().__init__(f"the chair's {phase} pass failed in round {round_}: {detail}")
        self.phase, self.round, self.detail = phase, round_, detail


class Chair:
    def __init__(self, council: Council, lanes, console: Console, brief: Brief) -> None:
        self.council = council
        self.lanes = lanes
        self.console = console
        self.brief = brief
        self.passes = 0

    # -- publication passes ------------------------------------------------------

    async def merge(self, listing: str, *, round_: int) -> dict:
        message = self.brief.merge(mode=self.council.mode, listing=listing)
        return await self._publication(message, kind="merge", phase="merge", round_=round_)

    async def compare(self, listing: str, *, round_: int) -> dict:
        message = self.brief.comparison(mode=self.council.mode, listing=listing)
        return await self._publication(message, kind="compare", phase="compare", round_=round_)

    async def _publication(self, message: str, *, kind: str, phase: str, round_: int) -> dict:
        refusal = "no JSON object"
        for attempt in (0, 1):
            outcome = await self.lanes.ask_chair(
                message,
                round_=round_,
                phase=phase,
                kind=kind,
                timeout=self.council.chair_timeout_sec,
                classify_unusable=(attempt == 0 and round_ == 0),
            )
            if not outcome.rejected and outcome.data is not None:
                return outcome.data.data
            refusal = "; ".join(outcome.errors) or "no JSON object"
            if attempt == 0:
                self.console.event("chair_unreadable", phase=phase, round=round_, reason=refusal, restart=True)
                self.console.say(f"chair {phase} pass unreadable ({refusal}) — a digest, then a restart, then one re-run")
                await self.digest(self.lanes.digest())
                await self.lanes.restart_member(CHAIR_ID, why=f"unreadable {phase} pass")
        raise ChairUnusable(phase, round_, f"{refusal} — unreadable after repair re-asks, a digest, a restart and one re-run")

    # -- the digest ---------------------------------------------------------

    async def digest(self, rows: list[dict]) -> dict | None:
        if not rows:
            return None
        self.passes += 1
        self.console.event(
            "watchdog_pass",
            pass_number=self.passes,
            agents=[row["agent"] for row in rows],
            tools=[row.get("tool_name") or "" for row in rows],
            elapsed=[row["elapsed"] for row in rows],
            idle=[row["idle"] for row in rows],
        )
        self.console.say(_digest_line(rows))
        message = self.brief.watchdog(rows, mode=self.council.mode)
        try:
            outcome = await self.lanes.ask_chair(
                message, round_=self.passes, phase="digest", kind="watchdog", timeout=WATCHDOG_PASS_TIMEOUT_S
            )
        except Exception as failure:
            # The pass itself failed.  That says nothing about any member.
            self.console.event("watchdog_pass_failed", pass_number=self.passes, error=str(failure))
            return None
        if outcome.rejected or outcome.data is None:
            self.console.event("watchdog_keep_all", pass_number=self.passes, reason="; ".join(outcome.errors) or "no JSON object")
            self.console.say("watchdog: the chair's pass was unreadable after both repair re-asks — every agent is kept")
            return None
        return outcome.data.data


def _digest_line(rows: list[dict]) -> str:
    parts = "; ".join(f"{row['agent']} {row.get('tool_name') or 'no tool'} {row['idle']:.0f}s idle" for row in rows)
    return f"watchdog: chair pass over {parts}"
