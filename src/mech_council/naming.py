"""`file_base` = ``slot<NN>[-<slugified name>]`` for a seat, ``chair`` for the chair."""

from __future__ import annotations

import re

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _NON_SLUG.sub("-", (text or "").casefold()).strip("-")


def seat_file_base(slot: int, name: str = "") -> str:
    slug = slugify(name)
    return f"slot{slot:02d}-{slug}" if slug else f"slot{slot:02d}"


def chair_file_base() -> str:
    return "chair"
