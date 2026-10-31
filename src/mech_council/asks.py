from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

from .config import Council
from .console import Console
from .extraction import extract
from .members import Members
from .pi_rpc import Answer, ChildGone, ProtocolError
from .prompts import common

REPAIR_ASKS = 2
#: All members drop their queues at once, under one budget: a wedged one costs the exit one timeout, not N.
ABORT_ALL_BUDGET_S = 10.0


def round_note(round_: int | None) -> str:
    return "" if round_ is None else f" (round {round_})"


def round_field(round_: int | None) -> dict[str, int]:
    return {} if round_ is None else {"round": round_}


class AskUnusable(Exception):
    def __init__(self, label: str, provider: str | None, model: str | None, detail: str, prompts: int) -> None:
        super().__init__(
            f"{label} is unusable ({provider or 'default provider'}/{model or 'pi default'}): {detail} after {prompts} prompt(s)"
        )
        self.label, self.provider, self.model = label, provider, model
        self.detail, self.prompts = detail, prompts


class AskLost(Exception):
    def __init__(self, label: str, provider: str | None, model: str | None, round_: int | None, detail: str) -> None:
        super().__init__(f"{label} failed{round_note(round_)}: {detail}")
        self.label, self.provider, self.model = label, provider, model
        self.round, self.detail = round_, detail


@dataclass
class Outcome:
    text: str = ""
    failure: str | None = None  # a batch sets it; a single ask raises instead
    data: object = None
    errors: list[str] = field(default_factory=list)
    rejected: bool = False  # extraction refused the message
    repairs: int = 0
    prompts: int = 0
    seconds: float = 0.0


def needs_repair(parsed: object) -> bool:
    return bool(getattr(parsed, "invalid", False) or getattr(parsed, "errors", None))


class Lanes:
    def __init__(self, council: Council, members: Members, console: Console) -> None:
        self.council = council
        self.members = members
        self.console = console
        self.pool = asyncio.Semaphore(max(1, council.max_concurrency))
        self.chair_slot = asyncio.Semaphore(1)
        #: label → file_base while an ask is in flight
        self.in_flight: dict[str, str] = {}
        self.chair_busy = False
        self.prompts: dict[str, int] = {}  # prompts spent per member

    async def ask_agent(
        self, agent_id: str, message: str, *, round_: int, kind: str, parse_kw: dict, phase: str = "round", classify_unusable: bool = False
    ) -> Outcome:
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
        round_: int | None,
        phase: str,
        kind: str,
        timeout: float,
        parse_kw: dict | None = None,
        classify_unusable: bool = False,
    ) -> Outcome:
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

    def digest(self) -> list[dict]:
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

    async def _mediated(
        self,
        agent_id: str,
        message: str,
        *,
        round_: int | None,
        kind: str,
        parse_kw: dict,
        phase: str,
        timeout: float | None,
        classify_unusable: bool,
    ) -> Outcome:
        file_base = self.members.file_base_for(agent_id)
        label = self.members.label_for(file_base)
        who = self.members.display_for(file_base)
        outcome = Outcome()
        started = time.monotonic()
        text = await self._prompt_with_restart(
            file_base, message, label=label, round_=round_, timeout=timeout, outcome=outcome, classify_unusable=classify_unusable
        )
        self.console.say(f"{who}: {kind} answered in {time.monotonic() - started:.1f}s{round_note(round_)}")
        parsed = extract(kind, text, **parse_kw)
        while needs_repair(parsed) and outcome.repairs < REPAIR_ASKS:
            outcome.repairs += 1
            hint = "; ".join(getattr(parsed, "errors", []) or []) or "the answer did not match the schema"
            outcome.errors = list(getattr(parsed, "errors", []) or [])
            self.console.event("verdict_rejected", member=label, **round_field(round_), reason=hint, repair=outcome.repairs, phase=phase)
            self.console.say(f"{who}: answer refused ({hint}) — repair {outcome.repairs}/{REPAIR_ASKS}")
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
            outcome.errors = list(getattr(parsed, "errors", []) or []) or ["no JSON object in the answer"]
            outcome.rejected = True
            self.console.event(
                "verdict_rejected",
                member=label,
                **round_field(round_),
                reason=outcome.errors[0],
                repair=outcome.repairs,
                final=True,
                phase=phase,
            )
            self.console.say(f"{who}: message rejected after {outcome.repairs} repair re-ask(s)")
            return outcome
        outcome.data = parsed
        return outcome

    async def _prompt_with_restart(
        self,
        file_base: str,
        message: str,
        *,
        label: str,
        round_: int | None,
        timeout: float | None,
        outcome: Outcome,
        classify_unusable: bool,
    ) -> str:
        spec = self.members.spec_for(file_base)
        who = self.members.display_for(file_base)
        started = time.monotonic()
        detail = "no answer"
        for attempt in (0, 1):
            first_prompt = self.prompts.get(file_base, 0) == 0
            answer = None
            outcome.prompts += 1
            try:
                answer = await self._prompt(file_base, message, timeout=timeout, label=label)
                if answer.errored:
                    detail = answer.error_message or f"stopReason: {answer.stop_reason or 'error'}"
                else:
                    outcome.seconds += time.monotonic() - started
                    return answer.text
                if classify_unusable and first_prompt:
                    raise AskUnusable(who, spec.provider, spec.model, detail, self.prompts.get(file_base, 0))
            except (ChildGone, ProtocolError, TimeoutError, OSError) as error:
                detail = str(error) or type(error).__name__
            if attempt == 1:
                raise AskLost(who, spec.provider, spec.model, round_, f"{detail} (after a restart and one re-ask)")
            self.console.event("ask_failed", member=label, **round_field(round_), error=detail)
            self.console.say(f"{who}: ask failed ({detail}) — one restart and one re-ask")
            try:
                await self.members.restart(file_base, why=detail)
            except Exception as restart_error:
                raise AskLost(
                    who, spec.provider, spec.model, round_, f"{detail}; the restart failed too: {restart_error}"
                ) from restart_error
        raise AskLost(who, spec.provider, spec.model, round_, detail)

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

    async def abort_all(self) -> None:
        async def drop(member) -> None:
            with contextlib.suppress(Exception):
                await member.rpc.abort()

        members = list(self.members.members.values())
        if not members:
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(ABORT_ALL_BUDGET_S):
                await asyncio.gather(*(drop(member) for member in members))
