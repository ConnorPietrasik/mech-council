from __future__ import annotations

from .base import BaseAdapter, register

REVIEW = register(
    BaseAdapter(
        mode="review",
        item_noun="finding",
        item_noun_plural="findings",
        id_prefix="F",
        consensus_kind="open_work",
        report_name="report.md",
        phase0_schema="findings",
        verdict_fields=(),
        docket_sections=("open", "closed", "questions"),
        report_sections=(
            "consensus",
            "not_fully_vetted",
            "disputed",
            "rejected",
            "never_vetted",
            "sandbox_writes",
            "open_questions",
            "rounds",
        ),
        synonyms={},
    )
)
