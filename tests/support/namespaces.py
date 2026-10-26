"""The one fork/unshare/map handshake, in one place.

It lives here rather than in `test_sandbox.py` so a family that needs only a *namespace* — the sweep in
`test_process.py` — still runs when the sandbox family's overlay ground is unusable: importing the helper
from a module that skips at import time would skip the importer's whole family with it.
"""

from __future__ import annotations

import json
import os
import socket

from mech_council.sandbox import mount as mount_mod


def in_mapped_child(body, *args, mount_namespace=True):
    """Fork, unshare, let the **parent** write the map, then run `body(*args)` inside."""
    parent, child_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    os.set_inheritable(parent.fileno(), False)
    pid = os.fork()
    if pid == 0:
        parent.close()
        try:
            flags = mount_mod.CLONE_NEWUSER | (mount_mod.CLONE_NEWNS if mount_namespace else 0)
            os.unshare(flags)
            child_end.sendall(b"r")
            if child_end.recv(1) != b"m":
                os._exit(3)
            result = body(*args)
            child_end.sendall(b"|" + json.dumps(result, default=str).encode())
            child_end.close()
            os._exit(0)
        except BaseException as error:  # reported, not lost
            import traceback

            detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            try:
                child_end.sendall(b"!" + detail.encode()[-900:])
            except Exception:
                pass
            os._exit(1)
    child_end.close()
    uid, gid = os.getuid(), os.getgid()
    assert parent.recv(1) == b"r", "the child never reached the mapping handshake"
    for name, value in (("setgroups", "deny"), ("uid_map", f"0 {uid} 1"), ("gid_map", f"0 {gid} 1")):
        with open(f"/proc/{pid}/{name}", "w", encoding="utf-8") as handle:
            handle.write(value)  # written from outside: the only place it can be
    parent.sendall(b"m")
    payload = _drain(parent)
    parent.close()
    _, status = os.waitpid(pid, 0)
    if payload.startswith(b"!"):
        raise AssertionError(f"the sandboxed child failed:\n{payload[1:].decode()}")
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, f"the child did not exit cleanly: {status}"
    return json.loads(payload[1:] or b"null")


def _drain(sock) -> bytes:
    payload = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return payload
        payload += chunk
