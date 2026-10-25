"""The one facade the loop uses to reach prompt text.

The engine never builds a string: it asks for a briefing and gets one.  Keeping the vocabulary here means
`modes/` stays data, the engine stays pure, and every word a member reads is greppable in `prompts/`.

**No agent reads a name through here.**  `Brief` holds no roster names at all — it takes none in its constructor
and passes none down — so a seat reads a peer as `AgentN` and cannot weight a seat called "Senior" over one called
"Newbie".  Names are the human's: `report_*`, the checkpoint and the console caption IDs from their own tables.
"""

from __future__ import annotations

from ..render import docket as docket_render
from . import common


class Brief:
    """Every message a member reads is built here, and only here."""

    def __init__(self, *, mode: str, project: str, max_items: int = 12) -> None:
        self.mode = mode
        self.project = project
        self.max_items = max_items

    # -- member asks -----------------------------------------------------------

    def phase0(self, *, mode: str, goal: str | None, project: str) -> str:
        return common.phase0_briefing(mode=mode or self.mode, goal=goal, project=project or self.project)

    def round(self, *, mode: str, goal: str | None, project: str, me: str, docket: str, phase0_summary: str) -> str:
        summary = f"## Your phase 0 summary\n\n{phase0_summary}\n" if phase0_summary else ""
        return common.round_briefing(
            mode=mode or self.mode, goal=goal, project=project or self.project, me=me, docket=docket, phase0_summary=summary
        )

    def docket(self, docket, viewer: str) -> str:
        """The docket for one seat: IDs, never names."""
        return docket_render.render(docket, viewer)

    def gap(self, chunk: list[str], *, deliberate: bool, reason: str) -> str:
        return common.gap_ask(chunk, reason=reason, deliberate=deliberate)

    def defense(self, item_id: str, challenge: dict, question: str) -> str:
        return common.defense_ask(item_id, challenge, question)

    def explain(self, question: str, followup: str) -> str:
        return common.explain_ask(question, followup)

    # -- chair asks ------------------------------------------------------------

    def merge(self, *, mode: str, listing: str) -> str:
        return common.merge_task(mode or self.mode, listing)

    def comparison(self, *, mode: str, listing: str) -> str:
        return common.comparison_task(mode or self.mode, listing)

    def watchdog(self, rows: list[dict], *, mode: str = "") -> str:
        return common.watchdog_task(render_digest(rows))


def render_digest(rows: list[dict]) -> str:
    """The chair's digest, as text: member id, last tool, previewed args, time since last event."""
    lines = []
    for row in rows:
        lines.append(
            f"- {row['agent']}: {row.get('tool_name') or 'no tool call yet'} · "
            f"{row['idle']:.0f}s since the last event · {row['elapsed']:.0f}s in this ask · "
            f"{row['events']} event(s)\n"
            f"    args: {row['last_tool'] or '(none)'}"
        )
    return "\n".join(lines)
