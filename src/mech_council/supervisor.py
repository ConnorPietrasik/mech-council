from __future__ import annotations

import contextlib
import os
import signal
import sys

from .sandbox import mount


def _die_with_the_supervisor() -> None:
    """SIGTERM here when the supervisor dies: the run takes its own exit path instead of orphaning members."""
    with contextlib.suppress(Exception):
        mount.prctl(mount.PR_SET_PDEATHSIG, signal.SIGTERM)


def _write_map(pid: int) -> None:
    """The row is `0 <uid> 1`, not `<uid> <uid> 1`: the latter maps the caller's own uid, so the child's
    numeric root owns nothing in the tree and its first `mount(2)` is `EPERM`.
    """
    uid, gid = os.getuid(), os.getgid()
    with open(f"/proc/{pid}/setgroups", "w", encoding="utf-8") as handle:
        handle.write("deny")
    for name, value in (("uid_map", f"0 {uid} 1"), ("gid_map", f"0 {gid} 1")):
        with open(f"/proc/{pid}/{name}", "w", encoding="utf-8") as handle:
            handle.write(value)


def spawn_orchestrator(body, *, name: str = "orchestrator", in_place: bool = False) -> int:
    ack_read, ack_write = os.pipe()  # child → parent: "I am inside"
    go_read, go_write = os.pipe()  # parent → child: "you are mapped"
    # a fork copies unflushed buffers, and both processes would write what the parent left in them
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    pid = os.fork()
    if pid == 0:
        return _child(ack_write, go_read, body, in_place=in_place)
    os.close(ack_write)
    os.close(go_read)
    return _supervisor(pid, ack_read, go_write, name, in_place=in_place)


def _child(ack_write: int, go_read: int, body, *, in_place: bool) -> int:
    # the orchestrator stays in the terminal's group: out of it, its first stdin read is a background-group
    # read, which the terminal answers with SIGTTIN
    if not in_place:
        try:
            mount.unshare(mount.CLONE_NEWUSER | mount.CLONE_NEWNS)
        except OSError as failure:
            from .cli import downgrade_hint

            sys.stderr.write(f"unshare(CLONE_NEWUSER|CLONE_NEWNS): {os.strerror(failure.errno or 0)}.{downgrade_hint()}\n")
            os.close(ack_write)
            os.close(go_read)
            return 1
    os.write(ack_write, b"r")
    os.close(ack_write)
    released = os.read(go_read, 1)
    os.close(go_read)
    if released != b"m":
        sys.stderr.write("released without a uid/gid map\n")
        return 1
    _die_with_the_supervisor()  # after the credential changes, which clear it
    try:
        return int(body())
    except KeyboardInterrupt:  # pragma: no cover — the handler path owns this
        return 130


def _supervisor(pid: int, ack_read: int, go_write: int, name: str, *, in_place: bool = False) -> int:
    # forward signals rather than dying here and orphaning the orchestrator with its mounts
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda number, _frame: _relay(pid, number))
    if os.read(ack_read, 1) != b"r":
        os.close(ack_read)
        os.close(go_write)
        sys.stderr.write(
            f"{name} never reported entering its namespace\n" if not in_place else f"{name} never came up: the child died before it acked\n"
        )
        return 1
    os.close(ack_read)
    if not in_place:
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
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def _relay(pid: int, signum: int = signal.SIGTERM) -> None:
    # a keystroke reaches the orchestrator twice, from the terminal and from here, and `interrupt.Brake`
    # counts presses inside one second as the one press it is
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        pass
