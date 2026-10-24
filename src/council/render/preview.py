"""The chair's one shared tool-call preview formatter — previews and nothing else.

Middle-out truncation (a long `npm test` tail matters more than its head), project-relative paths, and
collapsed `cd … && …` prefixes.  The sandbox's `sandbox_full`/`sandbox_limit_hit` inference never
runs here: it lives in the RPC pump, so a watchdog that is off or between ticks cannot blind the run.
"""

from __future__ import annotations

import re

_CD = re.compile(r"^cd\s+(?P<target>\S+)\s*(?:&&|;)\s*")
_MAX = 220


def project_relative(text: str, project: str) -> str:
    """Strip the project prefix — the seat's cwd — so previews read like the tree."""
    if not project:
        return text
    return text.replace(project.rstrip("/") + "/", "").replace(project, ".")


def collapse_cd(command: str) -> str:
    """`cd /x/y && make` → `make` (the cwd is the project; the prefix is noise)."""
    match = _CD.match(command.strip())
    return command.strip() if not match else match.group(0)[match.end() - match.end() :] or command.strip()[match.end() :]


def middle_out(text: str, limit: int = _MAX) -> str:
    """Head and tail with the elision in the middle — the tail is the interesting part."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    head = max(1, (limit - 9) * 3 // 5)
    tail = limit - 9 - head
    return f"{text[:head]} … {text[-tail:] if tail > 0 else ''}"


def preview(args: object, *, project: str = "", tool_name: str = "", limit: int = _MAX) -> str:
    """One line describing a tool call, for the watchdog digest and for logs."""
    if isinstance(args, dict):
        if tool_name in ("bash", "shell", "exec"):
            text = str(args.get("command") or args.get("cmd") or "")
            text = collapse_cd(project_relative(text, project))
        else:
            wanted = ("path", "file", "file_path", "pattern", "query", "command", "dir")
            parts = [f"{key}={args[key]}" for key in wanted if key in args]
            text = " ".join(parts) or ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
            text = project_relative(text, project)
    else:
        text = project_relative(str(args or ""), project)
    return middle_out(text, limit)
