from __future__ import annotations

import os
import signal
import sys

from .sandbox import mount


def _write_map(pid: int) -> None:
    """The row is `0 <uid> 1`, not `<uid> <uid> 1`, and that difference is not cosmetic: `<uid> <uid> 1` maps the caller's own
    uid, so the child's numeric root owns nothing in the tree, and its *first* `mount(2)` — the private remount of `/` — is
    `EPERM`.

    The parent can still write it after the child unshared: `setgroups`/`uid_map`/`gid_map` are CAP_SETUID-gated writability
    plus a capability check on the *writer*, and the parent never dropped anything.
    """
    uid, gid = os.getuid(), os.getgid()
    with open(f"/proc/{pid}/setgroups", "w", encoding="utf-8") as handle:
        handle.write("deny")
    for name, value in (("uid_map", f"0 {uid} 1"), ("gid_map", f"0 {gid} 1")):
        with open(f"/proc/{pid}/{name}", "w", encoding="utf-8") as handle:
            handle.write(value)


def spawn_orchestrator(body, *, name: str = "orchestrator") -> int:
    ack_read, ack_write = os.pipe()  # child → parent: "I am inside"
    go_read, go_write = os.pipe()  # parent → child: "you are mapped"
    pid = os.fork()
    if pid == 0:
        return _child(ack_write, go_read, body)
    os.close(ack_write)
    os.close(go_read)
    return _supervisor(pid, ack_read, go_write, name)


def _child(ack_write: int, go_read: int, body) -> int:
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
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        pass
