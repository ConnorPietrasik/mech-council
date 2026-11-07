from __future__ import annotations

from .base import BaseAdapter, register

FIX = register(
    BaseAdapter(
        mode="fix",
        item_noun_plural="findings",
        id_prefix="F",
        report_name="fix.md",
        phase0_schema="findings",
    )
)
