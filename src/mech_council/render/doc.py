"""The report's document: the content every presenter formats.

A report is a title, the run's metadata, and a flat list of blocks. Inline text is a list of
styled segments, so a document never carries one format's markup — the presenters apply their
own styling and their own escaping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Style = Literal["plain", "bold", "code", "italic"]


@dataclass(frozen=True)
class Seg:
    text: str
    style: Style = "plain"


@dataclass(frozen=True)
class Text:
    parts: tuple[Seg, ...] = ()

    def plain(self) -> str:
        return "".join(part.text for part in self.parts)


Part = str | tuple[str, Style] | Text


def T(*parts: Part) -> Text:
    """A bare string is plain text; a `(text, style)` pair is styled; a Text is spliced in whole."""
    segs: list[Seg] = []
    for part in parts:
        if isinstance(part, Text):
            segs += list(part.parts)
        elif isinstance(part, str):
            segs.append(Seg(part))
        else:
            segs.append(Seg(part[0], part[1]))
    return Text(tuple(segs))


@dataclass(frozen=True)
class Heading:
    level: int
    text: Text


@dataclass(frozen=True)
class Paragraph:
    text: Text
    #: wrap at 76 columns (markdown) — for prose; a single line otherwise
    wrapped: bool = False


@dataclass(frozen=True)
class ListItem:
    text: Text
    sub: tuple[ListItem, ...] = ()
    #: prose that hangs under the item, e.g. an assumption's "assumed: …"
    tail: Text | None = None
    #: quoted evidence under the item, e.g. a sandbox refusal
    quote: Text | None = None


@dataclass(frozen=True)
class List:
    items: tuple[ListItem, ...]


@dataclass(frozen=True)
class Table:
    header: tuple[Text, ...]
    rows: tuple[tuple[Text, ...], ...] = ()


@dataclass(frozen=True)
class Group:
    """Blocks that print as one run — no blank line between them (markdown)."""

    blocks: tuple[Block, ...]


Block = Heading | Paragraph | List | Table | Group


@dataclass(frozen=True)
class Report:
    title: Text
    #: what the run ended as: the outcome name, for the page's badge
    outcome: str
    #: the run's metadata, label to value: the page's header card, the report's header lines
    meta: list[tuple[str, Text]] = field(default_factory=list)
    blocks: tuple[Block, ...] = ()
