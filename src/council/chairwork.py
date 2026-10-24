"""§12.1.8 — the chair's own rules, which only a caller can apply.

The lane cannot tell a digest from a publication pass, so these rules live here:

* one slot, outside `max_concurrency`, so a digest can never queue behind agent work;
* a publication pass is a member ask like any other — §5.3's two repair re-asks, then §9's
  digest, then §4's restart and **one** re-run, then §3.3's exit naming the round and phase
  (§12.1.11: an unreadable merge plan is an *outcome*, never an exit code);
* a **digest** unreadable after both repair re-asks is `keep_all`: it never blocks anything,
  and a round with no chair pass is exactly the round the watchdog exists for;
* a digest is bounded by `WATCHDOG_PASS_TIMEOUT_S` — a constant, never a config key, and a
  different value from the merge pass's timeout;
* the chair holds no roster ID and appears in no rendered docket: `chair` is not a value of
  any `id` field (§9).
"""
from __future__ import annotations

from .config import Council
from .console import Console
from .deliberation.state import CHAIR_ID
from .prompts.brief import Brief
from .sandbox.mount import WATCHDOG_PASS_TIMEOUT_S

REPAIR_ASKS = 2               # §5.3, shared by both lanes


class ChairUnusable(Exception):
    """§3.3 / §6.1: the publication pass failed twice, and the run ends naming the phase."""

    def __init__(self, phase: str, round_: int, detail: str) -> None:
        super().__init__(f"the chair's {phase} pass failed in round {round_}: {detail}")
        self.phase, self.round, self.detail = phase, round_, detail


class Chair:
    """Every chair ask in the run goes through here, and only here."""

    def __init__(self, council: Council, lanes, console: Console, brief: Brief) -> None:
        self.council = council
        self.lanes = lanes
        self.console = console
        self.brief = brief
        self.passes = 0

    # -- publication passes (§6.1, §7.1) ----------------------------------------

    async def merge(self, listing: str, *, round_: int) -> dict:
        """`{groups: [...], questions: [...]}`, or `refused` with the reason (§6.1)."""
        message = self.brief.merge(mode=self.council.mode, listing=listing)
        return await self._publication(message, kind="merge", phase="merge",
                                       round_=round_)

    async def compare(self, listing: str, *, round_: int) -> dict:
        """§7.1 step 2.  Unreadable twice and the run ends: there is no docket without it."""
        message = self.brief.comparison(mode=self.council.mode, listing=listing)
        return await self._publication(message, kind="compare", phase="compare",
                                       round_=round_)

    async def _publication(self, message: str, *, kind: str, phase: str,
                           round_: int) -> dict:
        """Two attempts, the second only after a digest and §4's restart."""
        refusal = "no JSON object"
        for attempt in (0, 1):
            outcome = await self.lanes.ask_chair(
                message, round_=round_, phase=phase, kind=kind,
                timeout=self.council.chair_timeout_sec,
                classify_unusable=(attempt == 0 and round_ == 0))
            if not outcome.rejected and outcome.data is not None:
                return outcome.data.data
            refusal = "; ".join(outcome.errors) or "no JSON object"
            if attempt == 0:
                self.console.event("chair_unreadable", phase=phase, round=round_,
                                   reason=refusal, restart=True)
                self.console.say(f"chair {phase} pass unreadable ({refusal}) — a digest, "
                                 "then §4's restart, then one re-run")
                await self.digest(self.lanes.digest())
                await self.lanes.restart_member(CHAIR_ID, why=f"unreadable {phase} pass")
        raise ChairUnusable(phase, round_, f"{refusal} — unreadable after repair re-asks, "
                            "a digest, §4's restart and one re-run")

    # -- the digest (§9) ---------------------------------------------------------

    async def digest(self, rows: list[dict]) -> dict | None:
        """One pass over `rows`.  `None` means *keep everyone* — the watchdog's own floor."""
        if not rows:
            return None
        self.passes += 1
        self.console.event("watchdog_pass", pass_number=self.passes,
                           agents=[row["agent"] for row in rows],
                           tools=[row.get("tool_name") or "" for row in rows],
                           elapsed=[row["elapsed"] for row in rows],
                           idle=[row["idle"] for row in rows])
        self.console.say(_digest_line(rows))
        message = self.brief.watchdog(rows, mode=self.council.mode)
        try:
            outcome = await self.lanes.ask_chair(
                message, round_=self.passes, phase="digest", kind="watchdog",
                timeout=WATCHDOG_PASS_TIMEOUT_S)
        except Exception as failure:
            # The pass itself failed.  That says nothing about any member.
            self.console.event("watchdog_pass_failed", pass_number=self.passes,
                               error=str(failure))
            return None
        if outcome.rejected or outcome.data is None:
            self.console.event("watchdog_keep_all", pass_number=self.passes,
                               reason="; ".join(outcome.errors) or "no JSON object")
            self.console.say("watchdog: the chair's pass was unreadable after both repair "
                             "re-asks — every agent is kept")
            return None
        return outcome.data.data


def _digest_line(rows: list[dict]) -> str:
    parts = "; ".join(f"{row['agent']} {row.get('tool_name') or 'no tool'} "
                      f"{row['idle']:.0f}s idle" for row in rows)
    return f"watchdog: chair pass over {parts}"
