"""§4's identity naming: `file_base` and its slug.  Pure; one owner (§plan §2).

`file_base` = ``slot<N>[-<slugified name>]`` for a seat and ``chair`` for the chair.
Spawn, the launcher, the sandbox slot directories, `run.json` and the renderers all
read it from here — it must not live under `render/`, because half of its readers
are process code.
"""
from __future__ import annotations

import re

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lower-case, every run of non-alphanumerics becomes one ``-``, ends trimmed."""
    return _NON_SLUG.sub("-", (text or "").casefold()).strip("-")


def seat_file_base(slot: int, name: str = "") -> str:
    """§4: ``slot<N>`` plus the slugified seat name when there is one to slug."""
    slug = slugify(name)
    return f"slot{slot}-{slug}" if slug else f"slot{slot}"


def chair_file_base() -> str:
    return "chair"
