from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ModeAdapter(Protocol):
    @property
    def mode(self) -> str: ...

    @property
    def item_noun_plural(self) -> str: ...

    @property
    def detail_label(self) -> str: ...  # the heading an item's `detail` is printed under

    @property
    def id_prefix(self) -> str: ...  # "F" / "T"

    @property
    def report_name(self) -> str: ...  # `report.md` / `plan.md`

    def report_files(self, report_format: str) -> tuple[str, ...]: ...  # the report files one format writes

    @property
    def phase0_schema(self) -> str: ...  # the word the phase-0 count is reported in


@dataclass(frozen=True)
class BaseAdapter:
    mode: str = ""
    item_noun_plural: str = "items"
    detail_label: str = "what breaks"
    id_prefix: str = "I"
    report_name: str = "report.md"
    phase0_schema: str = "findings"

    @property
    def report_base(self) -> str:
        return self.report_name[: -len(".md")]

    def report_files(self, report_format: str) -> tuple[str, ...]:
        if report_format == "html":
            return (self.report_base + ".html",)
        if report_format == "both":
            return (self.report_base + ".md", self.report_base + ".html")
        return (self.report_base + ".md",)


REGISTRY: dict[str, ModeAdapter] = {}


def register(adapter: ModeAdapter) -> ModeAdapter:
    REGISTRY[adapter.mode] = adapter
    return adapter


def adapter_for(mode: str) -> ModeAdapter:
    return REGISTRY[mode]
