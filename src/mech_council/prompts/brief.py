from __future__ import annotations

from ..render import docket as docket_render
from . import common


class Brief:
    def __init__(self, *, mode: str, project: str, max_items: int = 12, in_place: bool = False) -> None:
        self.mode = mode
        self.project = project
        self.max_items = max_items
        self.in_place = in_place

    def phase0(self, *, mode: str, goal: str | None, project: str) -> str:
        return common.phase0_briefing(mode=mode or self.mode, goal=goal, project=project or self.project, in_place=self.in_place)

    def round(self, *, mode: str, goal: str | None, project: str, me: str, docket: str, phase0_summary: str) -> str:
        summary = f"## Your phase 0 summary\n\n{phase0_summary}\n" if phase0_summary else ""
        return common.round_briefing(
            mode=mode or self.mode,
            goal=goal,
            project=project or self.project,
            me=me,
            docket=docket,
            phase0_summary=summary,
            in_place=self.in_place,
        )

    def docket(self, docket, viewer: str) -> str:
        return docket_render.render(docket, viewer)

    def gap(self, chunk: list[str], *, deliberate: bool, reason: str) -> str:
        return common.gap_ask(chunk, reason=reason, deliberate=deliberate)

    def defense(self, item_id: str, challenge: dict, question: str) -> str:
        return common.defense_ask(item_id, challenge, question)

    def peer_pass(self, listing: str) -> str:
        return common.peer_pass_task(listing, self.mode)

    def merge(self, *, mode: str, listing: str, goal: str | None = None) -> str:
        return common.merge_task(mode or self.mode, listing, goal)

    def ask_merge(self, listing: str) -> str:
        return common.ask_merge_task(listing)

    def comparison(self, *, listing: str, goal: str | None = None) -> str:
        return common.comparison_task(listing, goal)

    def direction(self, *, listing: str, goal: str | None = None) -> str:
        return common.direction_task(listing, goal)

    def watchdog(self, rows: list[dict], *, mode: str = "") -> str:
        return common.watchdog_task(render_digest(rows))


def render_digest(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        lines.append(
            f"- {row['agent']}: {row.get('tool_name') or 'no tool call yet'} · "
            f"{row['idle']:.0f}s since the last event · {row['elapsed']:.0f}s in this ask · "
            f"{row['events']} event(s)\n"
            f"    args: {row['last_tool'] or '(none)'}"
        )
    return "\n".join(lines)
