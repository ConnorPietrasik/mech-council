from __future__ import annotations

from .base import BaseAdapter, register

REVIEW = register(
    BaseAdapter(
        mode="review",
        item_noun_plural="findings",
        id_prefix="F",
        report_name="report.md",
        phase0_schema="findings",
    )
)
