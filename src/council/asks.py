"""Every ask in the run is mediated through one of exactly two lanes.

Agent asks take a slot of the `max_concurrency` pool; the chair has its own single slot **outside** that cap.  Sharing
one pool is what makes the watchdog starvation-prone: with `max_concurrency` ≤ the number of wedged agents, every slot
is held by exactly the work the watchdog exists to interrupt — so a digest can never queue behind agent work here.

Both lanes own the same two rules: lenient extraction's two repair re-asks, and restart-and-re-ask — one
restart, one re-ask, a second failure ends the run — and both record in-flight activity so the chair's digest
has something to say.  A restart does not cancel a pending ask — it settles it with no message, which
is how the re-ask and the second-failure exit reach the run: a member's ask has exactly one outcome owner.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

from .config import Council
from .console import Console
from .extraction import extract
from .members import Members
from .pi_rpc import ABORT_TIMEOUT_S, Answer, ChildGone, ProtocolError
from .prompts import common

REPAIR_ASKS = 2  # the only repair count there is


class AskUnusable(Exception):
    """A member's **first** prompt settling on `stopReason: "error"` is a dead seat.

    The seat is unusable, not unhelpful: exit 1, naming it, with the provider/model and the round it last reached.  Keyed
    on the first prompt because the same test on a later ask is an ordinary ask failure on the restart-and-re-ask path.
    """

    def __init__(self, label: str, provider: str | None, model: str | None, detail: str, prompts: int) -> None:
        super().__init__(
            f"{label} is unusable ({provider or 'default provider'}/{model or 'pi default'}): {detail} after {prompts} prompt(s)"
        )
        self.label, self.provider, self.model = label, provider, model
        self.detail, self.prompts = detail, prompts


class AskLost(Exception):
    """Restart-and-re-ask's second failure: a fresh spawn and one re-ask both came back with nothing."""

    def __init__(self, label: str, provider: str | None, model: str | None, round_: int, detail: str) -> None:
        super().__init__(f"{label} failed in round {round_}: {detail}")
        self.label, self.provider, self.model = label, provider, model
        self.round, self.detail = round_, detail


@dataclass
class Outcome:
    """One mediated ask: the text, or the reason there is none, and what it cost."""

    text: str = ""
    failure: str | None = None  # set ⇒ restart-and-re-ask's second failure
    data: object = None
    errors: list[str] = field(default_factory=list)
    rejected: bool = False  # a message arrived and extraction refused it → a gap
    repairs: int = 0
    prompts: int = 0
    seconds: float = 0.0


def needs_repair(parsed: object) -> bool:
    """Lenient extraction's repairable half: no JSON at all, or fields that were present and invalid."""
    return bool(getattr(parsed, "invalid", False) or getattr(parsed, "errors", None))


class Lanes:
    """The pool, the chair's slot, and the in-flight registry the watchdog reads."""

    def __init__(self, council: Council, members: Members, console: Console) -> None:
        self.council = council
        self.members = members
        self.console = console
        self.pool = asyncio.Semaphore(max(1, council.max_concurrency))
        self.chair_slot = asyncio.Semaphore(1)
        #: label → file_base while an ask is in flight; the *activity* itself belongs to
        #: the PiRpc that is reading the child, which is the only thing that sees events.
        self.in_flight: dict[str, str] = {}
        self.chair_busy = False
        self.prompts: dict[str, int] = {}  # prompts spent per member, for run.json

    # -- the two lanes ------------------------------------------------------------

    async def ask_agent(
        self, agent_id: str, message: str, *, round_: int, kind: str, parse_kw: dict, phase: str = "round", classify_unusable: bool = False
    ) -> Outcome:
        """A member ask: pool slot, restart-and-re-ask underneath, lenient extraction's repairs on top."""
        async with self.pool:
            return await self._mediated(
                agent_id,
                message,
                round_=round_,
                kind=kind,
                parse_kw=parse_kw,
                phase=phase,
                timeout=None,
                classify_unusable=classify_unusable,
            )

    async def ask_seats(
        self, batch, *, round_: int, kind: str, phase: str, parse_for, classify_unusable: bool = False
    ) -> dict[str, Outcome]:
        """A whole round, or one whole ladder pass, at the cap.

        `parse_for(member_id)` builds that member's extractor, because a verdict is validated against *its*
        author's known items.  One member's failure never abandons its siblings: every task is awaited
        either way, and a member that died mid-ask gets the restart-and-re-ask that is its own to receive.
        """

        async def guarded(agent, message):
            try:
                return await self.ask_agent(
                    agent, message, round_=round_, kind=kind, parse_kw=parse_for(agent), phase=phase, classify_unusable=classify_unusable
                )
            except (AskUnusable, AskLost) as failure:
                return Outcome(text=str(failure), failure=str(failure))

        tasks = [asyncio.create_task(guarded(agent, message)) for agent, message in batch]
        done = await asyncio.gather(*tasks)
        return {b[0]: outcome for b, outcome in zip(batch, done)}

    async def ask_chair(
        self,
        message: str,
        *,
        round_: int,
        phase: str,
        kind: str,
        timeout: float,
        parse_kw: dict | None = None,
        classify_unusable: bool = False,
    ) -> Outcome:
        """The chair's own slot, outside `max_concurrency`, never queued behind agent work."""
        async with self.chair_slot:
            self.chair_busy = True
            try:
                return await self._mediated(
                    self.council.chair.file_base,
                    message,
                    round_=round_,
                    kind=kind,
                    parse_kw=parse_kw or {},
                    phase=phase,
                    timeout=timeout,
                    classify_unusable=classify_unusable,
                )
            finally:
                self.chair_busy = False

    async def restart_member(self, file_base: str, *, why: str) -> None:
        await self.members.restart(file_base, why=why)

    # -- the digest's rows --------------------------------------------------------

    def digest(self) -> list[dict]:
        """Every in-flight member: last tool, previewed args, time since the last event.

        The rows read the PiRpc's own activity, because that is the object the read pump updates.  A copy made when
        the ask started would freeze `events` at zero and hand the chair a digest in which every member looks wedged.
        """
        from .render import preview

        rows = []
        for label, file_base in sorted(self.in_flight.items()):
            member = self.members.members.get(file_base)
            activity = getattr(getattr(member, "rpc", None), "activity", None)
            if activity is None:
                continue
            rows.append(
                {
                    "agent": label,
                    "elapsed": round(time.monotonic() - activity.started, 1),
                    "idle": round(activity.age(), 1),
                    "events": activity.events,
                    "tool_name": activity.last_tool,
                    "last_event": activity.last_event_name,
                    "last_tool": preview.preview(activity.last_tool_args, project=self.council.project, tool_name=activity.last_tool),
                }
            )
        return rows

    @property
    def has_in_flight(self) -> bool:
        return bool(self.in_flight)

    # -- one mediated ask ---------------------------------------------------------

    async def _mediated(
        self,
        agent_id: str,
        message: str,
        *,
        round_: int,
        kind: str,
        parse_kw: dict,
        phase: str,
        timeout: float | None,
        classify_unusable: bool,
    ) -> Outcome:
        """One ask, and the one place `AgentN` becomes a file_base: the two vocabularies meet at the process boundary and nowhere else."""
        file_base = self.members.file_base_for(agent_id)
        label = self.members.label_for(file_base)
        outcome = Outcome()
        started = time.monotonic()
        text = await self._prompt_with_restart(
            file_base, message, label=label, round_=round_, timeout=timeout, outcome=outcome, classify_unusable=classify_unusable
        )
        # Every completed ask prints with its duration, so a slow seat is visible while the run is live rather than only in a post-mortem.
        self.console.say(f"{label}: {kind} answered in {time.monotonic() - started:.1f}s (round {round_})")
        parsed = extract(kind, text, **parse_kw)
        while needs_repair(parsed) and outcome.repairs < REPAIR_ASKS:
            outcome.repairs += 1
            hint = "; ".join(getattr(parsed, "errors", []) or []) or "the answer did not match the schema"
            outcome.errors = list(getattr(parsed, "errors", []) or [])
            self.console.event("verdict_rejected", member=label, round=round_, reason=hint, repair=outcome.repairs, phase=phase)
            self.console.say(f"{label}: answer refused ({hint}) — repair {outcome.repairs}/{REPAIR_ASKS}")
            text = await self._prompt_with_restart(
                file_base,
                common.repair_ask(self.council.mode, phase, hint, text),
                label=label,
                round_=round_,
                timeout=timeout,
                outcome=outcome,
                classify_unusable=False,
            )
            parsed = extract(kind, text, **parse_kw)
        outcome.text = text
        if needs_repair(parsed):
            # The third failure rejects the *message*, and the gap ladder gaps on everything.
            outcome.errors = list(getattr(parsed, "errors", []) or []) or ["no JSON object in the answer"]
            outcome.rejected = True
            self.console.event(
                "verdict_rejected", member=label, round=round_, reason=outcome.errors[0], repair=outcome.repairs, final=True, phase=phase
            )
            self.console.say(f"{label}: message rejected after {outcome.repairs} repair re-ask(s)")
            return outcome
        outcome.data = parsed
        return outcome

    async def _prompt_with_restart(
        self, file_base: str, message: str, *, label: str, round_: int, timeout: float | None, outcome: Outcome, classify_unusable: bool
    ) -> str:
        """One prompt, with restart-and-re-ask-once underneath it.

        "No message arrived" and "a message that settled on `stopReason: error`" take the same
        road, because restart-and-re-ask is a rule about the ask, not about the child.  The one
        exception is the unusable-seat test, which spends exactly one prompt and ends the run.
        """
        spec = self.members.spec_for(file_base)
        started = time.monotonic()
        detail = "no answer"
        for attempt in (0, 1):
            first_prompt = self.prompts.get(file_base, 0) == 0
            answer = None
            outcome.prompts += 1  # a prompt that came back with nothing was spent
            try:
                answer = await self._prompt(file_base, message, timeout=timeout, label=label)
                if answer.errored:
                    detail = answer.error_message or f"stopReason: {answer.stop_reason or 'error'}"
                else:
                    outcome.seconds += time.monotonic() - started
                    return answer.text
                if classify_unusable and first_prompt:
                    raise AskUnusable(label, spec.provider, spec.model, detail, self.prompts.get(file_base, 0))
            except (ChildGone, ProtocolError, TimeoutError, OSError) as error:
                detail = str(error) or type(error).__name__
            if attempt == 1:
                raise AskLost(label, spec.provider, spec.model, round_, f"{detail} (after a restart and one re-ask)")
            self.console.event("ask_failed", member=label, round=round_, error=detail)
            self.console.say(f"{label}: ask failed ({detail}) — one restart and one re-ask")
            try:
                await self.members.restart(file_base, why=detail)
            except Exception as restart_error:
                raise AskLost(
                    label, spec.provider, spec.model, round_, f"{detail}; the restart failed too: {restart_error}"
                ) from restart_error
        raise AskLost(label, spec.provider, spec.model, round_, detail)  # unreachable

    async def _prompt(self, file_base: str, message: str, *, timeout: float | None, label: str) -> Answer:
        member = self.members.members.get(file_base)
        if member is None:
            raise ChildGone(f"{label}: no live child")
        self.in_flight[label] = file_base
        try:
            return await member.rpc.ask(message, timeout=timeout)
        finally:
            self.in_flight.pop(label, None)
            self.prompts[file_base] = self.prompts.get(file_base, 0) + 1

    # -- no un-awaited aborts -----------------------------------------------------

    async def abort_all(self) -> None:
        for member in list(self.members.members.values()):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(member.rpc.abort(), timeout=ABORT_TIMEOUT_S + 1)
