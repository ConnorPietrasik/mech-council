from __future__ import annotations

import os
import sys

from .internal_env import INTERNAL_ENV, NS_REPORT_FAIL, NS_REPORT_OK, PIPE_ENV
from .sandbox import memberns


def _report(payload: str) -> None:
    raw = os.environ.get(PIPE_ENV)
    if not raw:
        return
    try:
        fd = int(raw)
        os.write(fd, payload.encode("utf-8", "replace") + b"\n")
        os.close(fd)
    except ValueError, OSError:
        pass


def setup_seat_main(argv: list[str]) -> int:
    slot, rest = _parse(argv)
    project = os.environ.get("COUNCIL_LAUNCH_PROJECT", "")
    runs_root = os.environ.get("COUNCIL_LAUNCH_RUNS_ROOT", "")
    slot_dir = os.environ.get("COUNCIL_LAUNCH_SLOT_DIR", "")
    if not (slot and project and runs_root and slot_dir and rest):
        _report(f"{NS_REPORT_FAIL} setup-seat wants --slot, a project, a runs root, a slot dir and a command after --")
        return 1
    try:
        namespace = memberns.setup_member_namespace(project=project, runs_dir=runs_root, slot_dir=slot_dir)
    except memberns.SetupFailure as failure:
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
    return 1


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
            rest = argv[index + 1 :]
            break
        index += 1
    return slot, rest
