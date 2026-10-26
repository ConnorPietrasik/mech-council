from __future__ import annotations

import re

_CD = re.compile(r"^cd\s+(?P<target>\S+)\s*(?:&&|;)\s*")
_MAX = 220


def project_relative(text: str, project: str) -> str:
    if not project:
        return text
    return text.replace(project.rstrip("/") + "/", "").replace(project, ".")


def collapse_cd(command: str) -> str:
    match = _CD.match(command.strip())
    return command.strip()[match.end() :] if match else command.strip()


def middle_out(text: str, limit: int = _MAX) -> str:
    """Head and tail with the elision in the middle — the tail is the interesting part."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    head = max(1, (limit - 9) * 3 // 5)
    tail = limit - 9 - head
    return f"{text[:head]} … {text[-tail:] if tail > 0 else ''}"


def preview(args: object, *, project: str = "", tool_name: str = "", limit: int = _MAX) -> str:
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
