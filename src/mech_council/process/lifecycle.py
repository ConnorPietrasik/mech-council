"""Two classes of descendant escape every group kill — a tool shell pi never cleaned up, and any descendant that called
setsid() and moved to a new pgid.  That is what the namespace-scoped sweep at the end of teardown is for; in place there is no
namespace to sweep, so those two classes are the one thing this cannot promise to clean up.
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
    """Close stdin → group SIGTERM → group SIGKILL.  Closing stdin is what starts pi's own shutdown, the only thing that
    reaches a tool shell pi detached into its own process group; SIGKILL-first is the bug that leaves them running.
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


def namespace_set(members: list[Member], *, own_namespace: int | None) -> set[int]:
    """`own_namespace` is None in place, where the members share this namespace and it is therefore the one
    scope the sweep must never be keyed on."""
    out: set[int] = set() if own_namespace is None else {own_namespace}
    for member in members:
        out.update(member.ns_records)
    return out


def sweep(namespaces: set[int], *, keep_fds: list[int], exclude: set[int] | None = None) -> SweepResult:
    """Kill every pid whose `/proc/<pid>/ns/mnt` is one this run created; where `/proc` is absent, nobody.  The comparison is
    against `keep_fds`, never numbers read back from `/proc`: from outside a namespace the readback can print the right
    inode for a namespace that is not the run's.
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
    """After the sweep: the held fd is the last handle on the run's namespaces once every member is gone."""
    for member in members:
        if member.ns_fd < 0:
            continue  # in place, there was never an fd to hold
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
    namespaces = namespace_set(members, own_namespace=own_namespace)
    # No namespace of its own means no sweep at all: the group kills above are the whole cleanup, and a
    # sweep keyed on the namespace we share with the operator would reach far past this run.
    result = sweep(namespaces, keep_fds=[member.ns_fd for member in members]) if namespaces else SweepResult()
    close_ns_fds(members)
    if on_line:
        on_line(survivor_line(result) if namespaces else NO_SWEEP_LINE)
    return result
