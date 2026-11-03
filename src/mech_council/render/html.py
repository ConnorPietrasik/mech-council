"""The HTML presenter: the report as one self-contained page.

No scripts, no network, no external assets — the file is the report. Every piece of text is
escaped per segment, so model-written content can never carry markup into the page.
"""

from __future__ import annotations

import html as _html
import re

from .doc import Block, Group, Heading, List, ListItem, Paragraph, Report, Text

#: the badge colour per outcome; anything else reads as muted
OUTCOME_CLASS = {
    "consensus": "ok",
    "unvetted": "info",
    "needs_human": "warn",
    "stalemate": "warn",
    "round_limit_reached": "muted",
    "aborted": "bad",
}

_SLUG = re.compile(r"[^a-z0-9]+")


def _inline(text: Text) -> str:
    out = []
    for part in text.parts:
        if not part.text:
            continue
        body = _html.escape(part.text, quote=False)
        if part.style == "bold":
            out.append(f"<strong>{body}</strong>")
        elif part.style == "code":
            out.append(f"<code>{body}</code>")
        elif part.style == "italic":
            out.append(f"<em>{body}</em>")
        else:
            out.append(body)
    return "".join(out)


def _slug(text: str, taken: dict[str, int]) -> str:
    base = _SLUG.sub("-", text.lower()).strip("-") or "section"
    count = taken.get(base, 0)
    taken[base] = count + 1
    return base if count == 0 else f"{base}-{count}"


def _item_html(item: ListItem) -> str:
    out = [f"<li>{_inline(item.text)}"]
    if item.tail is not None:
        out.append(f'<p class="tail">{_inline(item.tail)}</p>')
    if item.quote is not None:
        out.append(f"<blockquote><p>{_inline(item.quote)}</p></blockquote>")
    if item.sub:
        out.append("<ul>" + "".join(_item_html(sub) for sub in item.sub) + "</ul>")
    out.append("</li>")
    return "".join(out)


def _block_html(block: Block, taken: dict[str, int], toc: list[tuple[str, str]]) -> str:
    if isinstance(block, Heading):
        tag = "h2" if block.level <= 2 else "h3"
        if tag == "h2":
            anchor = _slug(block.text.plain(), taken)
            toc.append((anchor, block.text.plain()))
            return f'<h2 id="{anchor}">{_inline(block.text)}</h2>'
        return f"<h3>{_inline(block.text)}</h3>"
    if isinstance(block, Paragraph):
        return f"<p>{_inline(block.text)}</p>"
    if isinstance(block, List):
        return "<ul>" + "".join(_item_html(item) for item in block.items) + "</ul>"
    if isinstance(block, Group):
        return "".join(_block_html(inner, taken, toc) for inner in block.blocks)
    table = block
    head = "".join(f"<th>{_inline(cell)}</th>" for cell in table.header)
    body = "".join("<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>" for row in table.rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _meta_html(meta: list[tuple[str, Text]]) -> str:
    rows = "".join(f"<dt>{_html.escape(label, quote=False)}</dt><dd>{_inline(value)}</dd>" for label, value in meta)
    return f'<dl class="meta">{rows}</dl>'


def _toc_html(toc: list[tuple[str, str]]) -> str:
    if not toc:
        return ""
    items = "".join(f'<li><a href="#{anchor}">{_html.escape(title, quote=False)}</a></li>' for anchor, title in toc)
    return f'<details class="toc" open><summary>contents</summary><ol>{items}</ol></details>'


def render_html(report: Report, page_title: str) -> str:
    taken: dict[str, int] = {}
    toc: list[tuple[str, str]] = []
    body = "".join(_block_html(block, taken, toc) for block in report.blocks)
    badge = OUTCOME_CLASS.get(report.outcome, "muted")
    title = _html.escape(page_title, quote=False)
    badge_label = _html.escape(report.outcome or "no outcome", quote=False)
    heading = f'<h1>{_inline(report.title)}<span class="badge {badge}">{badge_label}</span></h1>'
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title} — {_html.escape(report.outcome or 'no outcome', quote=False)}</title>\n"
        f"<style>\n{_CSS}\n</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="page">\n'
        f'<header class="masthead">\n{heading}\n{_meta_html(report.meta)}\n</header>\n'
        f"{_toc_html(toc)}\n"
        f"<main>\n{body}\n</main>\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


_CSS = """\
:root {
  --fg: #1f2328; --muted: #656d76; --bg: #ffffff; --card: #f6f8fa; --line: #d0d7de;
  --accent: #0969da;
  --ok: #1a7f37; --ok-bg: #dafbe1; --info: #0969da; --info-bg: #ddf4ff;
  --warn: #9a6700; --warn-bg: #fff8c5; --bad: #cf222e; --bad-bg: #ffebe9; --muted-bg: #eaeef2;
}
@media (prefers-color-scheme: dark) {
  :root {
    --fg: #e6edf3; --muted: #8b949e; --bg: #0d1117; --card: #161b22; --line: #30363d;
    --accent: #58a6ff;
    --ok: #3fb950; --ok-bg: #033a1d; --info: #58a6ff; --info-bg: #0c2d6b;
    --warn: #d29922; --warn-bg: #3a2e00; --bad: #f85149; --bad-bg: #490202; --muted-bg: #21262d;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }
.page { max-width: 48rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
.masthead { border: 1px solid var(--line); border-radius: 10px; background: var(--card); padding: 1.25rem 1.5rem 1.35rem; }
h1 { font-size: 1.4rem; line-height: 1.3; margin: 0 0 .9rem; }
h1 code { font-weight: 600; }
.badge { display: inline-block; margin-left: .5rem; padding: .05rem .6rem; border-radius: 999px; font-size: .78rem; font-weight: 600; }
.badge { vertical-align: middle; }
.badge.ok { color: var(--ok); background: var(--ok-bg); }
.badge.info { color: var(--info); background: var(--info-bg); }
.badge.warn { color: var(--warn); background: var(--warn-bg); }
.badge.bad { color: var(--bad); background: var(--bad-bg); }
.badge.muted { color: var(--muted); background: var(--muted-bg); }
.meta { display: grid; grid-template-columns: max-content 1fr; gap: .15rem 1rem; margin: 0; font-size: .92rem; }
.meta dt { color: var(--muted); }
.meta dd { margin: 0; overflow-wrap: anywhere; }
.toc { margin: 1rem 0 1.5rem; font-size: .92rem; }
.toc summary { cursor: pointer; color: var(--muted); }
.toc ol { columns: 2; column-gap: 2rem; margin: .4rem 0 0; padding-left: 1.3rem; }
.toc a { color: var(--accent); text-decoration: none; }
.toc a:hover { text-decoration: underline; }
h2 { font-size: 1.18rem; margin: 2rem 0 .6rem; padding-bottom: .3rem; border-bottom: 1px solid var(--line); }
h3 { font-size: 1.02rem; margin: 1.4rem 0 .4rem; }
p { margin: .5rem 0; overflow-wrap: anywhere; }
ul { margin: .4rem 0; padding-left: 1.4rem; }
li { margin: .18rem 0; }
li p.tail { margin: .25rem 0 0; color: var(--muted); }
blockquote { margin: .5rem 0; padding: .3rem .8rem; border-left: 3px solid var(--line); color: var(--muted); overflow-wrap: anywhere; }
blockquote p { margin: .2rem 0; }
table { border-collapse: collapse; width: 100%; margin: .6rem 0; font-size: .92rem; }
th, td { border: 1px solid var(--line); padding: .35rem .6rem; text-align: left; vertical-align: top; }
th { background: var(--card); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .88em; background: var(--card); }
code { border: 1px solid var(--line); border-radius: 4px; padding: 0 .3em; overflow-wrap: anywhere; }
strong { font-weight: 650; }
@media print {
  body { background: #fff; color: #000; }
  .toc { display: none; }
  .masthead { border-color: #999; background: none; }
  h2, h3 { break-after: avoid; }
  table, blockquote, .masthead { break-inside: avoid; }
}
"""
