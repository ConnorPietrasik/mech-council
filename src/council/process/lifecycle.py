"""The dispose path and the namespace-scoped sweep; the exit mechanics every exit path owes.

Testable without pi: every function here takes a pid/pgid/namespace, not a model. Two classes of
descendant escape every group kill — a tool shell whose pi never ran its cleanup, and any descendant
that `setsid`s — and the sweep is what catches them, by mount namespace rather than by process tree.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from dataclasses import dataclass, field

from ..sandbox.mount import DISPOSE_SWEEP_AFTER_S, TERM_GRACE_S
from .spawn import Member

STDIN_CLOSE_GRACE_S = 3.0


@dataclass
class SweepResult:
    """`run.json`'s `survivor_pids`/`survivor_ns`: the sweep's whole honest report."""

    pids: list[int] = field(default_factory=list)
    namespaces: list[int] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.pids


def send(pgid: int, sig: int, *, own_pgid: int) -> bool:
    """Absolute guards: never below 2, never our own group, never a failed lookup."""
    if pgid < 2 or pgid == own_pgid:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError, PermissionError:
        return False


async def dispose(member: Member, *, own_pgid: int | None = None) -> None:
    """Close stdin → group SIGTERM → group SIGKILL.  Signals are the escalation, not the move.

    pi spawns every tool shell detached in its own group, so our `killpg` never reaches a tool shell and pi's own shutdown
    must: closing stdin is what starts that shutdown, and it is why SIGKILL-first is the bug that leaves tool shells running.
    """
    own_pgid = own_pgid if own_pgid is not None else os.getpgid(0)
    with contextlib.suppress(Exception):
        await member.rpc.close_stdin()
    deadline = time.monotonic() + STDIN_CLOSE_GRACE_S
    while time.monotonic() < deadline and member.proc.returncode is None:
        await asyncio.sleep(0.05)
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


def namespace_set(members: list[Member], *, own_namespace: int) -> set[int]:
    """The orchestrator's own namespace plus every one whose fd is still held.

    Holding the reference is what makes the key sound: a namespace we hold cannot be freed, so no unrelated
    namespace can have taken its inode number — membership really does mean "ours", with no depth or pid cap.
    """
    out = {own_namespace}
    for member in members:
        out.update(member.ns_records)
    return out


def sweep(namespaces: set[int], *, keep_fds: list[int], exclude: set[int] | None = None) -> SweepResult:
    """Kill every pid whose `/proc/<pid>/ns/mnt` is one this run created.

    Where `/proc` is absent the sweep is unavailable and says so by returning nothing.

    The comparison is against `keep_fds` — the descriptors this run opened on its own namespaces — and not against the
    numbers read back from `/proc`. Reading `/proc/<pid>/ns/mnt` from *outside* the namespace in question reports
    the reader's own namespace: measured here, a parent scanning a mapped child's entry sees the parent's inode, so a
    set of *reported* ids matches nothing and the sweep reports "no survivors" over a member that is still running.  `fstat`
    on a held fd is the namespace the run actually owns, which is also why the fd is held until after this.
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
    """The sweep's own line, built from `run.json`'s two fields and nothing else: name the *scope* that died.

    "killed 3 processes" hides the only fact that matters — whether they were ours.
    """
    if result.empty:
        return "no survivors: every namespace this run created is empty"
    joined = ", ".join(f"ns {ns}" for ns in sorted(result.namespaces))
    return f"killed {len(result.pids)} survivor(s) {sorted(result.pids)} in the namespace(s) this run created ({joined})"


def close_ns_fds(members: list[Member]) -> None:
    """Closed after the sweep, at the very end of teardown — never before."""
    for member in members:
        with contextlib.suppress(OSError):
            os.close(member.ns_fd)


async def dispose_all(members: list[Member], *, own_namespace: int, on_line=None, budget: float = DISPOSE_SWEEP_AFTER_S) -> SweepResult:
    """Dispose every member, then sweep the namespaces for whatever escaped the group kill."""
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
    result = sweep(namespace_set(members, own_namespace=own_namespace), keep_fds=[member.ns_fd for member in members])
    close_ns_fds(members)
    if on_line:
        on_line(survivor_line(result))
    return result
