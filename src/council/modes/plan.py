"""The plan adapter: data only."""

from __future__ import annotations

from .base import BaseAdapter, register

PLAN = register(
    BaseAdapter(
        mode="plan",
        item_noun="decision point",
        item_noun_plural="decision points",
        id_prefix="T",
        consensus_kind="direction",
        report_name="plan.md",
        phase0_schema="plan",
        verdict_fields=("revised_plan", "backing"),
        docket_sections=("approaches", "agreed_points", "open", "closed", "questions", "backing"),
        report_sections=(
            "rules",
            "chosen_direction",
            "stages",
            "constraints",
            "risks",
            "assumptions",
            "agreed_points",
            "decision_points",
            "human_input",
            "qa",
            "alternatives",
            "rounds",
        ),
        synonyms={},
    )
)
