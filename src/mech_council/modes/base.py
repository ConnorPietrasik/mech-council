from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ModeAdapter(Protocol):
    @property
    def mode(self) -> str: ...

    @property
    def item_noun(self) -> str: ...  # "finding" / "decision point"

    @property
    def item_noun_plural(self) -> str: ...

    @property
    def id_prefix(self) -> str: ...  # "F" / "T"

    @property
    def consensus_kind(self) -> str: ...  # "open_work" / "direction"

    @property
    def report_name(self) -> str: ...  # `report.md` / `plan.md`

    @property
    def phase0_schema(self) -> str: ...  # the word the phase-0 count is reported in

    @property
    def verdict_fields(self) -> tuple[str, ...]: ...

    @property
    def docket_sections(self) -> tuple[str, ...]: ...

    @property
    def report_sections(self) -> tuple[str, ...]: ...

    def seed_sections(self) -> tuple[str, ...]: ...


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

    # Hooks are injected by the engine, never imported by the adapter: `modes/` stays data.
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
