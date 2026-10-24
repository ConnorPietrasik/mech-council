"""Mode adapters hold **data and hooks only** — no parsing, no prompts, no arithmetic.

The registry is where a new mode is registered; config validation reads its literals
from `config.MODES`, which is derived from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ModeAdapter(Protocol):
    """Everything a mode contributes to the shared machinery.

    Every member is a read-only property: an adapter is a frozen record that the machinery
    reads and never writes, and a protocol member stated as a variable would ask for one
    you could assign through — which no adapter is.
    """

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

    # Hooks are injected by the engine, never imported by the adapter (the thin-adapter
    # rule's grep-test forbids a mode module touching subprocess/asyncio/tomllib or I/O).
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
