"""§10 step 6b + §4's launcher: `council --setup-seat --slot <file_base> -- <pi_cli> …`

This process **is** the member: it performs the five steps of 6b in itself, reports on the
spawn pipe, and then `execvp`s exactly what followed the `--` — no second process, no fork
between the mounts and the exec (§4).  Everything it needs beyond `--slot` arrives in
D12b's env, and every one of those variables is erased before the exec, so pi never sees
a path it could infer a mount from — and never sees the slot name it would key `slot<N>`
off (§1.2).
"""
from __future__ import annotations

import os
import sys

from .internal_env import (INTERNAL_ENV, LAUNCH_ENV, NS_REPORT_FAIL, NS_REPORT_OK,
                           PIPE_ENV)
from .sandbox import memberns


def _report(payload: str) -> None:
    raw = os.environ.get(PIPE_ENV)
    if not raw:
        return
    try:
        fd = int(raw)
        os.write(fd, payload.encode("utf-8", "replace") + b"\n")
        os.close(fd)
    except (ValueError, OSError):
        pass


def setup_seat_main(argv: list[str]) -> int:
    """The `--setup-seat` half of `__main__`: mount, report, exec.  Never returns on success."""
    slot, rest = _parse(argv)
    project = os.environ.get("COUNCIL_LAUNCH_PROJECT", "")
    runs_root = os.environ.get("COUNCIL_LAUNCH_RUNS_ROOT", "")
    slot_dir = os.environ.get("COUNCIL_LAUNCH_SLOT_DIR", "")
    if not (slot and project and runs_root and slot_dir and rest):
        _report(f"{NS_REPORT_FAIL} setup-seat wants --slot, a project, a runs root, "
                "a slot dir and a command after --")
        return 1
    try:
        namespace = memberns.setup_member_namespace(project=project, runs_dir=runs_root,
                                                   slot_dir=slot_dir)
    except memberns.SetupFailure as failure:
        # §10 step 8: the failing syscall and its errno, and this member never execs.
        _report(f"{NS_REPORT_FAIL} {failure.syscall} {failure.err}")
        if os.environ.get("COUNCIL_LAUNCH_DEBUG"):
            import traceback
            traceback.print_exc()
        return 1
    _report(f"{NS_REPORT_OK} {namespace}")
    for name in INTERNAL_ENV:
        os.environ.pop(name, None)
    program, *args = rest
    try:
        os.execvp(program, [program, *args])
    except OSError as exc:
        sys.stderr.write(f"council launcher: execvp({program!r}): {exc}\n")
        return 1
    return 1                                   # unreachable


def _parse(argv: list[str]) -> tuple[str, list[str]]:
    slot = ""
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--slot" and index + 1 < len(argv):
            slot = argv[index + 1]
            index += 2
            continue
        if token == "--":
            rest = argv[index + 1:]
            break
        index += 1
    return slot, rest
