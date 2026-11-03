"""The markdown presenter: the document as the report file every run writes.

Blocks are separated by one blank line; prose is wrapped at 76 columns. Inline styling is the
markdown subset the report always used: `**bold**`, `` `code` ``, `_italic_`.
"""

from __future__ import annotations

from .doc import Block, Group, Heading, List, ListItem, Paragraph, Report, Text
from .text import wrap


def _inline(text: Text) -> str:
    out = []
    for part in text.parts:
        body = part.text
        if part.style == "bold":
            out.append(f"**{body}**")
        elif part.style == "code":
            out.append(f"`{body}`")
        elif part.style == "italic":
            out.append(f"_{body}_")
        else:
            out.append(body)
    return "".join(out)


def _item_lines(item: ListItem, depth: int = 0) -> list[str]:
    pad = "  " * depth
    lines = [f"{pad}- {_inline(item.text)}"]
    if item.tail is not None:
        lines += wrap(item.tail.plain(), indent=f"{pad}  ")
    if item.quote is not None:
        lines += [f"{pad}> {line}" for line in wrap(item.quote.plain(), indent="")]
    for sub in item.sub:
        lines += _item_lines(sub, depth + 1)
    return lines


def block_lines(block: Block) -> list[str]:
    if isinstance(block, Heading):
        return ["#" * block.level + " " + _inline(block.text)]
    if isinstance(block, Paragraph):
        return wrap(_inline(block.text), indent="") if block.wrapped else [_inline(block.text)]
    if isinstance(block, List):
        out = []
        for item in block.items:
            out += _item_lines(item)
        return out
    if isinstance(block, Group):
        out = []
        for inner in block.blocks:
            out += block_lines(inner)
        return out
    table = block
    head = "| " + " | ".join(_inline(cell) for cell in table.header) + " |"
    rows = ["| " + " | ".join(_inline(cell) for cell in row) + " |" for row in table.rows]
    return [head, "|" + "---|" * len(table.header)] + rows


def render_md(report: Report) -> str:
    lines = ["# " + _inline(report.title), ""]
    for label, value in report.meta:
        lines.append(f"- {label}: {_inline(value)}")
    for block in report.blocks:
        lines.append("")
        lines += block_lines(block)
    return "\n".join(lines).rstrip() + "\n"
