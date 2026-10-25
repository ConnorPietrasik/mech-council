"""The supervisor: the one process that runs no loop and holds no state — the one event loop's only exception.

Fork, map the child 1:1, forward two signals, relay the status.  The supervisor
never calls `mount(2)`; the orchestrator owns everything from the `unshare` onwards.

An unprivileged process cannot write its own `uid_map`: after `unshare` its euid is the unmapped overflowid and it
holds no `CAP_SIG`/`CAP_SETUID` in the parent namespace, so the write is `EPERM` for every map shape.  A process
**outside** the new namespace writes the same file with no privileges at all — which is why the shape is two processes.
"""

from __future__ import annotations

import os
import signal
import sys

from .sandbox import mount


def _write_map(pid: int) -> None:
    """The uid/gid map: `setgroups=deny`, then a 1:1 `uid_map`/`gid_map` — **us
    to namespace root**, which is what a one-row map means here.

    The row is `0 <uid> 1`, not `<uid> <uid> 1`.  That difference is not cosmetic: with an identity row the
    process is uid 1000 *inside* a namespace whose root is unmapped, so it is not root there, and the
    kernel drops the capability sets on every `execve` — every launcher the run spawns would arrive unable
    to `unshare(CLONE_NEWNS)`.  Mapped to root inside, capabilities survive the exec, and files created
    inside still land owned by our real uid outside, which is the same trick every rootless container plays.
    """
    uid, gid = os.getuid(), os.getgid()
    with open(f"/proc/{pid}/setgroups", "w", encoding="utf-8") as handle:
        handle.write("deny")
    for name, value in (("uid_map", f"0 {uid} 1"), ("gid_map", f"0 {gid} 1")):
        with open(f"/proc/{pid}/{name}", "w", encoding="utf-8") as handle:
            handle.write(value)


def spawn_orchestrator(body, *, name: str = "orchestrator") -> int:
    """Fork, map the child, release it, forward signals, exit with its status.

    `body()` runs in the child, already inside the run's user+mount
    namespace and already mapped, and returns the exit code the shell sees.
    """
    ack_read, ack_write = os.pipe()  # child → parent: "I am inside"
    go_read, go_write = os.pipe()  # parent → child: "you are mapped"
    pid = os.fork()
    if pid == 0:
        return _child(ack_write, go_read, body)
    os.close(ack_write)
    os.close(go_read)
    return _supervisor(pid, ack_read, go_write, name)


def _child(ack_write: int, go_read: int, body) -> int:
    """The orchestrator: unshare, wait to be mapped, then everything from the private remount on — the one loop's owner."""
    try:
        mount.unshare(mount.CLONE_NEWUSER | mount.CLONE_NEWNS)
    except OSError as failure:
        sys.stderr.write(f"unshare(CLONE_NEWUSER|CLONE_NEWNS): {os.strerror(failure.errno or 0)}\n")
        os.close(ack_write)
        os.close(go_read)
        return 1
    os.write(ack_write, b"r")  # "inside, waiting to be mapped"
    os.close(ack_write)
    released = os.read(go_read, 1)
    os.close(go_read)
    if released != b"m":
        sys.stderr.write("released without a uid/gid map\n")
        return 1
    try:
        return int(body())
    except KeyboardInterrupt:  # pragma: no cover — the handler path owns this
        return 130


def _supervisor(pid: int, ack_read: int, go_write: int, name: str) -> int:
    # A signal aimed at *this* pid would otherwise die right here, orphaning the
    # orchestrator with its mounts.  The supervisor forwards and keeps waiting.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda number, _frame: _relay(pid, number))
    if os.read(ack_read, 1) != b"r":
        os.close(ack_read)
        os.close(go_write)
        sys.stderr.write(f"{name} never reported entering its namespace\n")
        return 1
    os.close(ack_read)
    try:
        _write_map(pid)
    except OSError as failure:
        sys.stderr.write(f"the uid/gid map for {name}: {os.strerror(failure.errno or 0)}\n")
        os.close(go_write)
        _relay(pid)
        return 1
    os.write(go_write, b"m")
    os.close(go_write)
    while True:
        try:
            _, status = os.waitpid(pid, 0)
        except InterruptedError:
            continue
        except ChildProcessError:
            return 1
        break
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)  # 130 / 143 reach the shell unchanged
    return os.WEXITSTATUS(status)


def _relay(pid: int, signum: int = signal.SIGTERM) -> None:
    """Forward the signal the shell actually sent, so the run leaves on the same code."""
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        pass


def write_map(pid: int) -> None:
    """Exposed for the sandbox family's helper, which must be mapped **by its parent**."""
    _write_map(pid)
