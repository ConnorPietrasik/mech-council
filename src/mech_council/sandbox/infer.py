"""Inference answers two questions with two mechanisms, because the failure has two faces.  Quietly full: a write fails and the
tool result says `ENOSPC`, and nothing else about the view changes — reads and `ls` still succeed, so a seat that hits the cap
without saying so is invisible here.  Loudly denied: the seat quotes `EPERM`/`EACCES`/`EROFS`/`ENOTCONN` back, and that is
matched in its *own* prose, because the seat wrote the refusal down instead of a finding.
"""

from __future__ import annotations

import re

FULL = "sandbox_full"
LIMIT = "sandbox_limit_hit"

_FULL = re.compile(r"no space left on device|\benospc\b|cannot allocate disk blocks", re.IGNORECASE)
_LIMIT = re.compile(
    r"\beio\b|input/output error|\bexdev\b|invalid cross-?device link|"
    r"device or resource busy.*overlay|failed to rename|cannot remove directory",
    re.IGNORECASE,
)
#: The review report's other marker: an item whose evidence *is* one of the sandbox's overlay refusals.  The table is
#: the measured one — `EIO`, `EXDEV`, `EROFS`, `EMFILE`, `ENOSPC` and the `rm -rf` that half-empties a directory — because
#: those describe the sandbox, not the project, and printing them as project evidence would be a fabricated finding.
_LIMIT_EVIDENCE = re.compile(
    r"read-only file system|\berofs\b|\beio\b|input/output error|\bexdev\b|"
    r"invalid cross-?device link|\bemfile\b|too many open files|\benospc\b|"
    r"no space left on device|failed to rename|cannot remove directory",
    re.IGNORECASE,
)

_TAIL = 2048


def evidence_is_limit(text: str) -> bool:
    """Does this evidence *describe* an overlay refusal rather than the project?  The table is an errno *whitelist*, not a
    prefix match: `EOPNOTSUPP` means 'overlayfs cannot sit here' — an operator's mount mistake, not the cap — and a broad
    `E\\w{3,4}` would match `EBUSY` and `ENOSPC` anywhere in prose, which are project facts.
    """
    return bool(_LIMIT_EVIDENCE.search(text or ""))


def scan(tool_result: dict) -> str | None:
    if not tool_result.get("isError"):
        return None  # successful output is never scanned
    text = _tail(flatten_result(tool_result.get("result")), _TAIL)
    if not text:
        return None
    if _FULL.search(text):
        return FULL
    if _LIMIT.search(text):
        return LIMIT
    return None


def flatten_result(result: object) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(str(block.get("text") or "") for block in content if isinstance(block, dict))
        return str(result.get("text") or result.get("error") or "")
    if isinstance(result, list):
        return "\n".join(flatten_result(item) for item in result)
    return str(result or "")


def _tail(text: str, limit: int) -> str:
    return text[-limit:] if len(text) > limit else text
