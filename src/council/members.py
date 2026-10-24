"""The member registry: who is alive, who restarted, which namespace each spawn reported.

Nothing here knows pi's dialect (§12.1.7) and nothing here knows the protocol (§12.1.3):
it spawns through §4's launcher form, checks the child with `get_state`, and disposes it
with §4's escalation.  A restart is a blank session on purpose (§4).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field

from .config import Council, MemberSpec
from .console import Console, MemberSinks, Transcripts
from .pi_rpc import ChildGone, ProtocolError
from .process import lifecycle, spawn
from .runjson import RunManifest
from .sandbox.infer import flatten_result
from .sandbox.setup import Sandbox


class MemberGone(Exception):
    """A member died or never spoke: §4's restart-and-re-ask owns it, its end is §3.3's."""


@dataclass
class Members:
    council: Council
    sandbox: Sandbox
    run_dir: str
    runs_dir: str
    console: Console
    manifest: RunManifest
    transcripts: Transcripts
    members: dict[str, spawn.Member] = field(default_factory=dict)   # file_base → live
    stopping: bool = False
    survivors: lifecycle.SweepResult = field(default_factory=lifecycle.SweepResult)

    # -- spawn ---------------------------------------------------------------

    def file_base_for(self, agent_id: str) -> str:
        """§1.2: `AgentN` is the protocol name; `file_base` is the filesystem one.  A member
        is asked by its ID and lives under its file_base, and only this translation joins
        them — which is also why an ID can never reach a path by accident."""
        for spec in self.council.agents:
            if spec.id == agent_id:
                return spec.file_base
        return agent_id

    def label_for(self, file_base: str) -> str:
        if file_base == self.council.chair.file_base:
            return "chair"
        for spec in self.council.agents:
            if spec.file_base == file_base:
                return spec.id or spec.file_base
        return file_base

    def spec_for(self, file_base: str) -> MemberSpec:
        if file_base == self.council.chair.file_base:
            return self.council.chair
        for spec in self.council.agents:
            if spec.file_base == file_base:
                return spec
        raise KeyError(file_base)

    def on_tool_result(self, file_base: str):
        def handle(record: dict) -> None:
            from .sandbox import infer
            verdict = infer.scan(record)
            if verdict is None:
                return
            self.console.event(verdict, member=file_base, id=self.members[file_base].id,
                               tool=record.get("toolName"), inferred=True)
            self.console.say(f"{verdict} (inferred from an erroring tool result) in "
                             f"{file_base}")
        return handle

    async def spawn_one(self, spec: MemberSpec) -> spawn.Member:
        sinks = MemberSinks(self.transcripts, spec.file_base)
        member = await spawn.spawn(
            file_base=spec.file_base, member_id=spec.id, spec=spec,
            pi_cli=self.council.pi_cli, project=self.council.project,
            runs_root=self.runs_dir, slot_dir=self.sandbox.slot_dir(spec.file_base),
            run_path=self.run_dir, untrusted=self.council.untrusted,
            transcript=sinks, on_tool_result=self.on_tool_result(spec.file_base))
        self.members[spec.file_base] = member
        self.manifest.record_namespace(spec.file_base, member.namespace)
        return member

    async def start_all(self) -> None:
        """§3.3: no probe, no handshake pass — the real spawn *is* the check."""
        specs = [*self.council.agents, self.council.chair]
        results = await asyncio.gather(*(self._start_one(spec) for spec in specs),
                                       return_exceptions=True)
        for spec, result in zip(specs, results):
            if isinstance(result, BaseException):
                raise MemberGone(f"{spec.file_base}: {result}") from result

    async def _start_one(self, spec: MemberSpec) -> None:
        label = spec.id or "chair"
        try:
            member = await self.spawn_one(spec)
        except spawn.LauncherSetupError as failure:
            self.console.say(f"{label} failed in §10 step 6b: {failure}")
            raise
        except OSError as failure:
            self.console.event("member_spawn_failed", member=label,
                               file_base=spec.file_base, error=str(failure))
            raise MemberGone(f"{label} could not be spawned: {failure}") from failure
        try:
            state = await member.rpc.get_state()
        except (ProtocolError, ChildGone, TimeoutError, OSError) as failure:
            await self.dispose_one(member)
            self.console.event("member_spawn_failed", member=label,
                               file_base=spec.file_base, error=str(failure))
            raise MemberGone(f"{label} did not answer get_state: {failure}") from failure
        warning = _stderr_warning(member)
        if warning:
            self.manifest.set_model_warning(spec.file_base, warning)
            self.console.event("council_unknown_model", member=label, warning=warning)
            self.console.say(f"{label}: {warning}")
        model = spec.model or "pi default"
        self.console.say(f"{label} up · {spec.provider or 'default provider'}/{model} "
                         f"· ns {member.namespace}")

    async def restart(self, file_base: str, *, why: str) -> spawn.Member:
        """§4/§9: abort → group-kill → fresh spawn.  Never once disposal has begun."""
        if self.stopping:
            raise MemberGone(f"{file_base}: restart dropped, the run is stopping")
        old = self.members.get(file_base)
        if old is not None:
            # The dead spawn's ns fd stays open until after the sweep (§4): restart does
            # not close it, so its inode remains in the sweep set.
            await lifecycle.dispose(old)
            self.members[file_base] = old            # keep the record; fds still held
        self.console.event("member_restart", member=file_base, reason=why)
        self.console.say(f"restarting {file_base} ({why})")
        spec = self.spec_for(file_base)
        member = await self.spawn_one(spec)
        member.ns_records = list(old.ns_records) + [member.namespace] if old else \
            [member.namespace]
        self.manifest.record_namespace(file_base, member.namespace)
        try:
            await member.rpc.get_state()
        except (ProtocolError, ChildGone, TimeoutError) as failure:
            raise MemberGone(f"{file_base} restarted but did not answer get_state: "
                             f"{failure}") from failure
        return member

    # -- dispose -------------------------------------------------------------

    async def dispose_all(self) -> lifecycle.SweepResult:
        """§3.2: dispose everything without restarting it, then the namespace sweep."""
        self.stopping = True
        held = list(self.members.values())
        # A restarted member leaves two records: the old spawn's ns fd is in `held`
        # because `restart` keeps it addressable until the sweep has run.
        self.console.say(f"disposing {len(self.council.agents) + 1} member(s)")
        self.survivors = await lifecycle.dispose_all(
            held, own_namespace=self.own_namespace(), on_line=self.console.say)
        self.transcripts.close()
        return self.survivors

    def own_namespace(self) -> int:
        from .sandbox.mount import namespace_id
        return namespace_id()

    async def dispose_one(self, member: spawn.Member) -> None:
        await lifecycle.dispose(member)


def _stderr_warning(member: spawn.Member) -> str:
    for chunk in member.stderr:
        if (warning := spawn.unknown_model_warning(chunk)):
            return warning
    return ""
