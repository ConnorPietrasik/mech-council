from __future__ import annotations

from .base import BaseAdapter, register

PLAN = register(
    BaseAdapter(
        mode="plan",
        item_noun_plural="decision points",
        detail_label="the answers it admits",
        id_prefix="T",
        report_name="plan.md",
        phase0_schema="plan",
        companion="report.md",
    )
)
