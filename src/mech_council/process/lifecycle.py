from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass, field

from ..sandbox.mount import DISPOSE_SWEEP_AFTER_S, TERM_GRACE_S
from .spawn import Member

STDIN_CLOSE_GRACE_S = 3.0
#: The hurried disposal's bound on one step: a member's notice of its closed stdin, then the reap after the kill.
HURRY_STEP_S = 0.5


@dataclass
class SweepResult:
    pids: list[int] = field(default_factory=list)
    namespaces: list[int] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.pids


def send(pgid: int, sig: int, *, own_pgid: int) -> bool:
    if pgid < 2 or pgid == own_pgid:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError, PermissionError:
        return False


async def dispose(member: Member, *, own_pgid: int | None = None) -> None:
    """Close stdin, then group SIGTERM, then group SIGKILL.  Closing stdin is what reaches a tool shell pi
    detached into its own process group, so it comes first.
    """
    own_pgid = own_pgid if own_pgid is not None else os.getpgid(0)
    await _bounded(STDIN_CLOSE_GRACE_S, (_settle(member),))
    if member.proc.returncode is None:
        send(member.pgid, signal.SIGTERM, own_pgid=own_pgid)
        try:
            async with asyncio.timeout(TERM_GRACE_S):
                await member.proc.wait()
        except TimeoutError:
            send(member.pgid, signal.SIGKILL, own_pgid=own_pgid)
            with contextlib.suppress(Exception):
                await member.proc.wait()
    with contextlib.suppress(Exception):
        await member.rpc.stop()
    if member.stderr_task is not None:
        member.stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await member.stderr_task


def namespace_set(members: list[Member], *, own_namespace: int | None) -> set[int]:
    out: set[int] = set() if own_namespace is None else {own_namespace}
    for member in members:
        out.update(member.ns_records)
    return out


async def kill_all(members: list[Member], *, own_namespace: int | None, on_line=None) -> SweepResult:
    """The hurried disposal: closed stdins, then group SIGKILL for what is left, then the sweep."""
    await _bounded(HURRY_STEP_S, (_settle(member) for member in members))
    own_pgid = os.getpgid(0)
    for member in list(members):
        if member.proc.returncode is None:
            send(member.pgid, signal.SIGKILL, own_pgid=own_pgid)
    await _bounded(HURRY_STEP_S, (_reap(member) for member in members))
    return _sweep_and_report(members, own_namespace=own_namespace, on_line=on_line)


async def _bounded(seconds: float, steps: Iterable[Awaitable[None]]) -> None:
    """Do these at once, giving up on them after `seconds`."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await asyncio.gather(*steps)


async def _settle(member: Member) -> None:
    """The closed stdin, then the member's own way out — the only one that reaches what it detached on its own."""
    with contextlib.suppress(Exception):
        await member.rpc.close_stdin()
    await _await_exit(member)


async def _reap(member: Member) -> None:
    await _await_exit(member)
    with contextlib.suppress(Exception):
        await member.rpc.stop()
    if member.stderr_task is not None:
        member.stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await member.stderr_task


async def _await_exit(member: Member) -> None:
    """Wait for the member's own exit without awaiting its transport, which `close_stdin` has already half closed."""
    while member.proc.returncode is None:
        await asyncio.sleep(0.05)


def sweep(namespaces: set[int], *, keep_fds: list[int], exclude: set[int] | None = None) -> SweepResult:
    """Kill every pid whose `/proc/<pid>/ns/mnt` is one this run created; where `/proc` is absent, nobody.
    The comparison is against `keep_fds`, never an inode read back from `/proc`.
    """
    result = SweepResult()
    exclude = (exclude or set()) | {os.getpid()}
    ours = set(namespaces)
    for fd in keep_fds:
        try:
            ours.add(os.fstat(fd).st_ino)
        except OSError:
            continue
    if not ours:
        return result
    try:
        entries = os.listdir("/proc")
    except OSError:
        return result
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude or pid < 2:
            continue
        try:
            inode = os.stat(f"/proc/{pid}/ns/mnt").st_ino
        except OSError:
            continue
        if inode not in ours:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            continue
        result.pids.append(pid)
        if inode not in result.namespaces:
            result.namespaces.append(inode)
    return result


def survivor_line(result: SweepResult) -> str:
    if result.empty:
        return "no survivors: every namespace this run created is empty"
    joined = ", ".join(f"ns {ns}" for ns in sorted(result.namespaces))
    return f"killed {len(result.pids)} survivor(s) {sorted(result.pids)} in the namespace(s) this run created ({joined})"


NO_SWEEP_LINE = "no namespace sweep: in place the members share this namespace, so only their own process groups were signalled"


def close_ns_fds(members: list[Member]) -> None:
    for member in members:
        if member.ns_fd < 0:
            continue
        with contextlib.suppress(OSError):
            os.close(member.ns_fd)


async def dispose_all(
    members: list[Member], *, own_namespace: int | None, on_line=None, budget: float = DISPOSE_SWEEP_AFTER_S
) -> SweepResult:
    own_pgid = os.getpgid(0)
    try:
        async with asyncio.timeout(budget):
            for member in list(members):
                await dispose(member, own_pgid=own_pgid)
    except TimeoutError:
        if on_line:
            on_line("dispose never settled; sweeping by namespace")
        for member in members:
            send(member.pgid, signal.SIGKILL, own_pgid=own_pgid)
    await asyncio.sleep(0)
    return _sweep_and_report(members, own_namespace=own_namespace, on_line=on_line)


def _sweep_and_report(members: list[Member], *, own_namespace: int | None, on_line=None) -> SweepResult:
    # in place the members share this namespace, so there is nothing to sweep without reaching past the run
    namespaces = namespace_set(members, own_namespace=own_namespace)
    result = sweep(namespaces, keep_fds=[member.ns_fd for member in members]) if namespaces else SweepResult()
    close_ns_fds(members)
    if on_line:
        on_line(survivor_line(result) if namespaces else NO_SWEEP_LINE)
    return result
