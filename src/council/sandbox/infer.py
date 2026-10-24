"""§10's inference pass — one compiled alternation, run in the RPC pump and nowhere else.

Filtered to `isError` (pi sets it for every non-zero shell exit) and matched against the
text **tail**: successful output is never scanned, and the pass never runs in §9's
watchdog, so neither `watchdog_sec = 0` nor the gap between ticks can change what the run
reports (§13.9).  The console line says it is an inference, because that is what it is.
"""
from __future__ import annotations

import re

FULL = "sandbox_full"
LIMIT = "sandbox_limit_hit"

_FULL = re.compile(r"no space left on device|\benospc\b|cannot allocate disk blocks", re.I)
_LIMIT = re.compile(
    r"\beio\b|input/output error|\bexdev\b|invalid cross-?device link|"
    r"device or resource busy.*overlay|failed to rename|cannot remove directory",
    re.I)
#: §6.3's other marker: an item whose evidence *is* one of §10's overlay refusals.  The
#: table is §10's measured one — `EIO`, `EXDEV`, `EROFS`, `EMFILE`, `ENOSPC` and the
#: `rm -rf` that half-empties a directory — because those describe the sandbox, not the
#: project, and printing them as project evidence would be a fabricated finding.
_LIMIT_EVIDENCE = re.compile(
    r"read-only file system|\berofs\b|\beio\b|input/output error|\bexdev\b|"
    r"invalid cross-?device link|\bemfile\b|too many open files|\benospc\b|"
    r"no space left on device|failed to rename|cannot remove directory", re.I)

_TAIL = 2048


def evidence_is_limit(text: str) -> bool:
    """§6.3: does this evidence *describe* an overlay refusal rather than the project?"""
    return bool(_LIMIT_EVIDENCE.search(text or ""))


def scan(tool_result: dict) -> str | None:
    """`sandbox_full` / `sandbox_limit_hit` from one decoded `tool_execution_end`."""
    if not tool_result.get("isError"):
        return None                          # successful output is never scanned (§10)
    text = _tail(flatten_result(tool_result.get("result")), _TAIL)
    if not text:
        return None
    if _FULL.search(text):
        return FULL
    if _LIMIT.search(text):
        return LIMIT
    return None


def flatten_result(result: object) -> str:
    """pi's `result` is `{"content": [{"type": "text", "text": …}], …}`."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(str(block.get("text") or "") for block in content
                             if isinstance(block, dict))
        return str(result.get("text") or result.get("error") or "")
    if isinstance(result, list):
        return "\n".join(flatten_result(item) for item in result)
    return str(result or "")


def _tail(text: str, limit: int) -> str:
    return text[-limit:] if len(text) > limit else text


def cap_spent(sandbox_dir: str) -> bool:
    """§10's per-round `statvfs` backstop: a spent cap no member quoted the errno for."""
    try:
        info = os_statvfs(sandbox_dir)
    except OSError:
        return False
    return info is not None and info[0] * info[1] <= 64      # ≤ 64 KiB free of the cap


def os_statvfs(path: str):
    import os
    try:
        info = os.statvfs(path)
    except OSError:
        return None
    return (info.f_frsize, info.f_bavail)
