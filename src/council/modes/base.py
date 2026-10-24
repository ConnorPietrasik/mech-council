"""§12.1.4: mode adapters hold **data and hooks only** — no parsing, no prompts, no arithmetic.

The registry is where a new mode is registered; config validation reads its literals from
`config.MODES`, which is derived from here (plan §7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


class ModeAdapter(Protocol):
    """Everything a mode contributes to the shared machinery."""

    mode: str
    item_noun: str                       # "finding" / "decision point"
    item_noun_plural: str
    id_prefix: str                       # "F" / "T"
    consensus_kind: str                  # "open_work" / "direction"
    report_name: str                     # §3.4: `report.md` / `plan.md`
    docket_sections: tuple[str, ...]
    report_sections: tuple[str, ...]


@dataclass(frozen=True)
class BaseAdapter:
    mode: str = ""
    item_noun: str = "item"
    item_noun_plural: str = "items"
    id_prefix: str = "I"
    consensus_kind: str = "open_work"
    report_name: str = "report.md"
    phase0_schema: str = "findings"
    verdict_fields: tuple[str, ...] = ()
    docket_sections: tuple[str, ...] = ()
    report_sections: tuple[str, ...] = ()
    synonyms: dict = field(default_factory=dict)

    # Hooks are injected by the engine, never imported by the adapter (§12.1.4's
    # grep-test forbids a mode module touching subprocess/asyncio/tomllib or I/O).
    def seed_sections(self) -> tuple[str, ...]:
        return self.report_sections


REGISTRY: dict[str, ModeAdapter] = {}


def register(adapter: ModeAdapter) -> ModeAdapter:
    REGISTRY[adapter.mode] = adapter
    return adapter


def adapter_for(mode: str) -> ModeAdapter:
    return REGISTRY[mode]


def modes() -> tuple[str, ...]:
    return tuple(REGISTRY)
