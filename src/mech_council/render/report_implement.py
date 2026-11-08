"""The implement run's report: the crews, the adopted plan, the stages, what landed, and the asks."""

from __future__ import annotations

import os
import subprocess

from ..deliberation.implement import blind_note
from ..deliberation.notes import item_notes
from ..deliberation.state import Docket, Execution, StageRecord
from ..deliberation.tree import snapshot_env
from . import report_plan
from .context import ReportContext
from .doc import Block, Heading, List, ListItem, Paragraph, Report, T, Table, Text
from .markdown import render_md
from .text import caption

_DIFFSTAT_CAP = 4000
_USER_TEXT_CAP = 4000

_HOW = {
    "human": "the human chose it",
    "consensus": "every planner backed it",
    "majority": "a majority of the planners backed it",
    "chair": "the run's chair picked it",
    "structured": "the run's chair structured it into the gated stages",
}
_APPROVAL = {
    "granted": "The human approved the build",
    "recorded": "The build was recorded, not waited on",
    "declined": "The human declined to proceed: no code was written",
}


def build(docket: Docket, context: ReportContext, *, run_dir: str = "") -> Report:
    blocks: list[Block] = context.interrupted_blocks()
    blocks += _crews(docket, context)
    blocks += _adopted(docket, context)
    blocks += _stages(docket, context)
    blocks += _landed(context, run_dir)
    blocks += _decision_points(docket, context)
    blocks += _unsent(docket, context)
    blocks += report_plan._human_input(docket, context)
    blocks += report_plan._notes(docket, context, under=set(docket.order))
    blocks += context.artifacts(run_dir)
    return Report(title=T("Implementation"), outcome=context.outcome, meta=context.meta(), blocks=tuple(blocks))


def render(docket: Docket, context: ReportContext, *, run_dir: str = "") -> str:
    return render_md(build(docket, context, run_dir=run_dir))


def _crews(docket: Docket, context: ReportContext) -> list[Block]:
    execution = docket.execution
    blocks: list[Block] = [Heading(2, T("Crews"))]
    if not execution or not execution.crews:
        return blocks
    rows: list[tuple[Text, Text, Text, Text, Text]] = []
    for crew in execution.crews:
        seats = ", ".join(f"{name} ({agent_id})" for agent_id, name in crew["seats"])
        rows.append(
            (
                T((crew["label"], "bold")),
                T(crew["council"]),
                T(crew["mode"]),
                T(seats),
                T(crew["chair"]["name"]),
            )
        )
    blocks.append(Table(header=(T("crew"), T("council"), T("mode"), T("seats"), T("chair")), rows=tuple(rows)))
    return blocks


def _adopted(docket: Docket, context: ReportContext) -> list[Block]:
    execution = docket.execution
    blocks: list[Block] = [Heading(2, T("Adopted plan"))]
    if execution is None or not execution.adopted:
        return blocks + [Paragraph(T("No plan was adopted: the run ended before the build."))]
    who = caption(execution.adopted, context.captions)
    how = _HOW.get(execution.adopted_how, execution.adopted_how)
    approval = _APPROVAL.get(execution.approval, execution.approval or "Approval was not taken")
    blocks.append(Paragraph(T(("Direction: ", "bold"), (who, "code"), f" — {how}. {approval}.")))
    if execution.plan_text:
        blocks.append(Heading(3, T("The plan")))
        blocks.append(Paragraph(T(execution.plan_text), wrapped=True))
    if execution.assumptions:
        blocks.append(Heading(3, T("Carried assumptions")))
        blocks.append(List(tuple(ListItem(T(a)) for a in execution.assumptions)))
    return blocks


def _stages(docket: Docket, context: ReportContext) -> list[Block]:
    execution = docket.execution
    blocks: list[Block] = [Heading(2, T("Stages"))]
    if execution is None or not execution.stages:
        return blocks + [Paragraph(T("No stages were built."))]
    rows: list[tuple[Text, Text, Text, Text, Text, Text, Text]] = []
    for rec in execution.stages:
        effort = []
        if rec.fix_passes:
            effort.append(f"{len(rec.fix_passes)} fix pass(es)")
        if rec.replans:
            effort.append(f"{len(rec.replans)} replan(s)")
        if rec.prose_pass:
            effort.append("prose pass")
        rows.append(
            (
                T(str(rec.index)),
                T(rec.state or "(unnamed)"),
                T((rec.gate or "(no gate)", "code")),
                T(caption(rec.builder, context.captions) if rec.builder else "(none)"),
                T(rec.kind),
                T((rec.status, "bold")),
                T(", ".join(effort) or "—"),
            )
        )
    blocks.append(Table(header=(T("#"), T("final state"), T("gate"), T("builder"), T("kind"), T("status"), T("effort")), rows=tuple(rows)))
    for rec in execution.stages:
        if rec.user_text:
            blocks += _user_text(context.project, rec)
    blocked = [rec for rec in execution.stages if rec.status == "blocked"]
    for rec in blocked:
        blocks.append(Heading(3, T(f"Stage {rec.index}: blocked")))
        blocks.append(List(tuple(ListItem(T(reason)) for reason in rec.blocked_reasons) or (ListItem(T("(no reason recorded)")),)))
        if rec.open_items:
            blocks.append(Paragraph(T("Left open by the builder: " + ", ".join(rec.open_items))))
        for replan in rec.replans:
            if replan.get("note"):
                blocks.append(Paragraph(T(f"replan {replan['pass']}: {replan['note']}"), wrapped=True))
    passed = [rec for rec in execution.stages if rec.status == "passed"]
    for rec in passed:
        if rec.review_blind:
            blocks.append(Paragraph(T(blind_note(rec))))
        if rec.open_items:
            blocks.append(Paragraph(T(f"stage {rec.index} passed with the builder leaving open: " + ", ".join(rec.open_items))))
    return blocks


def _user_text(project: str, rec: StageRecord) -> list[Block]:
    """The stage's user-visible surfaces, as they stand when the report is written."""
    blocks: list[Block] = [Heading(3, T(f"Stage {rec.index}: user-visible text"))]
    for path in rec.user_text:
        full = os.path.join(project, path)
        try:
            with open(full, encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            content = "(the file is no longer there)"
        if len(content) > _USER_TEXT_CAP:
            content = content[:_USER_TEXT_CAP] + "…"
        blocks.append(Paragraph(T((path, "code"))))
        blocks.append(Paragraph(T(content), wrapped=True))
    return blocks


def _stat_line(line: str) -> str:
    """A `git diff --stat` file line without its column padding: `path | changes`, the + - bar dropped.
    A line that is not a file line — the summary line or a blank one — comes back empty."""
    text = line.strip()
    path, sep, rest = text.rpartition(" | ")
    words = rest.split()
    count = words[0] if words else ""
    if sep and count.isdigit():
        return f"{path.strip()} | {count}"
    return text if sep else ""


def _landed(context: ReportContext, run_dir: str) -> list[Block]:
    execution = context.docket_execution if isinstance(context.docket_execution, Execution) else None
    blocks: list[Block] = [Heading(2, T("What landed"))]
    if execution is None or not execution.stages:
        return blocks + [Paragraph(T("No code was written."))]
    baseline = execution.baseline
    if baseline:
        args = ["git", "-C", context.project, "diff", "--stat", baseline]
        if execution.final_tree:  # the tree the run ended on: new files included, the worktree not read
            args.append(execution.final_tree)
        # the run's trees live in the run directory's private object store, not the repo's
        env = {**os.environ, **snapshot_env(run_dir)} if run_dir else None
        try:
            proc = subprocess.run(args, check=False, capture_output=True, text=True, timeout=60, env=env)
            # one code line: inline code is single-line, and the stat's padding and + - bar read as noise
            stat = " · ".join(part for part in (_stat_line(line) for line in (proc.stdout or "").splitlines()) if part)[:_DIFFSTAT_CAP]
            if proc.returncode == 0 and stat:
                blocks.append(Paragraph(T((stat, "code"))))
                return blocks
        except OSError, subprocess.TimeoutExpired:
            pass
    changed = sorted({path for rec in execution.stages for path in rec.changed})
    if changed:
        blocks.append(Paragraph(T("Changed by the run: " + ", ".join(changed)), wrapped=True))
    else:
        blocks.append(Paragraph(T("The run reports no changed paths.")))
    return blocks


def _unsent(docket: Docket, context: ReportContext) -> list[Block]:
    """The say_to lines never delivered: the target's next prompt never came."""
    messages = docket.peer_messages
    if not messages:
        return []
    blocks: list[Block] = [Heading(2, T("Unsent messages"))]
    blocks.append(
        List(
            tuple(
                ListItem(
                    T(
                        f"{caption(message.from_, context.captions)} to {caption(message.to, context.captions)}"
                        + (f" (stage {message.stage})" if message.stage else "")
                        + f": {message.text}"
                    )
                )
                for message in messages
            )
        )
    )
    return blocks


def _decision_points(docket: Docket, context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T("Decision points"))]
    if not docket.order:
        return blocks + [Paragraph(T("None were raised."))]
    addressed: dict[str, str] = {}
    execution = docket.execution
    if execution is not None:
        for rec in execution.stages:
            for fix_pass in rec.fix_passes:
                for line in fix_pass.get("addressed") or []:
                    addressed[str(line.get("id") or "")] = str(line.get("how") or "")
    items: list[ListItem] = []
    for item_id in docket.order:
        item = docket.items[item_id]
        state = "closed: " + item.disposition if item.closed else "still standing"
        subs: list[ListItem] = []
        if item.evidence:
            subs.append(ListItem(T((f"evidence: {item.evidence}", "code"))))
        if item.also_reported_by:
            subs.append(ListItem(T("re-confirmed by " + ", ".join(caption(a, context.captions) for a in item.also_reported_by))))
        how = addressed.get(item.id)
        if how:
            subs.append(ListItem(T(f"the builder addressed it: {how}")))
        for note in item_notes(docket, item.id):
            subs.append(report_plan.note_line(note, context))
        items.append(ListItem(T(f"{item_id} [{item.severity}] {item.claim} — {state}"), sub=tuple(subs)))
    blocks.append(List(tuple(items)))
    return blocks
