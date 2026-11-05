from __future__ import annotations

from .base import BaseAdapter, register

IMPLEMENT = register(
    BaseAdapter(
        mode="implement",
        item_noun_plural="changes",
        id_prefix="C",
        report_name="implementation.md",
        phase0_schema="plan",
    )
)
