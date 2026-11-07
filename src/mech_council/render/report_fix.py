"""The fix run's report: the crews, where the findings came from, the gate, what was fixed and
committed, what was blocked and rolled back, and the final check."""

from __future__ import annotations

from ..deliberation.notes import item_notes
from ..deliberation.state import Docket
from ..deliberation.tree import Tree
from . import report_plan
from .context import ReportContext
from .doc import Block, Heading, List, ListItem, Paragraph, Report, T, Table, Text
from .markdown import render_md
from .text import caption


def build(docket: Docket, context: ReportContext, *, run_dir: str = "") -> Report:
    blocks: list[Block] = context.interrupted_blocks()
    blocks += _crews(docket, context)
    blocks += _source(docket)
    blocks += _gate(docket, context)
    blocks += _findings(docket, context)
    blocks += _landed(docket, context, run_dir)
    blocks += _final_check(docket)
    blocks += _unsent(docket, context)
    blocks += report_plan._human_input(docket, context)
    execution = docket.execution
    # only the findings with a detail section print their notes; a note about any other item stays in the ledger
    blocks += report_plan._notes(docket, context, under={rec.finding_id for rec in execution.findings} if execution is not None else set())
    blocks += context.artifacts(run_dir)
    return Report(title=T("Fix"), outcome=context.outcome, meta=context.meta(), blocks=tuple(blocks))


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


def _source(docket: Docket) -> list[Block]:
    execution = docket.execution
    blocks: list[Block] = [Heading(2, T("The findings"))]
    if execution is None or not execution.source:
        return blocks + [Paragraph(T("The run recorded no source for its findings."))]
    source = execution.source
    if source.get("how") == "council":
        detail = f"the review crew {source.get('council', '')!r} over the project, in {source.get('rounds', 0)} round(s)"
    else:
        detail = f"the review file {source.get('path', '')!r}"
        if source.get("council"):
            detail += f" (run of {source['council']}, {source['date']})"
    blocks.append(Paragraph(T(detail), wrapped=True))
    return blocks


def _gate(docket: Docket, context: ReportContext) -> list[Block]:
    execution = docket.execution
    blocks: list[Block] = [Heading(2, T("The gate"))]
    if execution is None or not execution.gate:
        return blocks + [Paragraph(T("No gate was taken: the findings came from a curated review file."))]
    rows: list[tuple[Text, Text, Text, Text, Text]] = []
    for qid, result in execution.gate.items():
        question = docket.questions.get(qid)
        finding = question.text if question is not None else qid
        state = "asked" if question is not None else "not asked"
        rows.append((T(qid), T(finding[:160]), T(result), T(state), T(question.answer or "—" if question is not None else "—")))
    blocks.append(Table(header=(T("q"), T("finding"), T("result"), T("state"), T("answer")), rows=tuple(rows)))
    return blocks


def _findings(docket: Docket, context: ReportContext) -> list[Block]:
    execution = docket.execution
    blocks: list[Block] = [Heading(2, T("Findings"))]
    if execution is None or not execution.findings:
        return blocks + [Paragraph(T("No findings were selected for the run."))]
    rows: list[tuple[Text, Text, Text, Text, Text, Text]] = []
    for rec in execution.findings:
        effort = []
        if rec.fix_passes:
            effort.append(f"{len(rec.fix_passes)} fix pass(es)")
        if rec.replans:
            effort.append(f"{len(rec.replans)} replan(s)")
        if rec.commit_sha:
            landing = f"committed {rec.commit_sha[:10]}"
        elif rec.status == "fixed":
            landing = "uncommitted"
        else:
            landing = "not committed"
        rows.append(
            (
                T(str(rec.index)),
                T((f"{rec.finding_id} · {rec.severity}", "code")),
                T(rec.claim[:120]),
                T(caption(rec.builder, context.captions) if rec.builder else "(none)"),
                T((rec.status, "bold")),
                T(landing + (f" · {', '.join(effort)}" if effort else "")),
            )
        )
    blocks.append(Table(header=(T("#"), T("finding"), T("claim"), T("builder"), T("status"), T("landing")), rows=tuple(rows)))
    for rec in execution.findings:
        blocks += _finding_detail(docket, rec, context)
    return blocks


def _finding_detail(docket: Docket, rec, context: ReportContext) -> list[Block]:
    blocks: list[Block] = []
    notes = item_notes(docket, rec.finding_id)
    if not rec.plan and not rec.changed and not rec.blocked_reasons and not notes:
        return blocks
    blocks.append(Heading(3, T(f"Finding {rec.index}: {rec.finding_id} — {rec.status}")))
    if rec.plan.get("state"):
        blocks.append(Paragraph(T("Plan: ", (rec.plan["state"], "code")), wrapped=True))
    if rec.plan.get("gate"):
        blocks.append(Paragraph(T("Gate: ", (rec.plan["gate"], "code")), wrapped=True))
    if rec.gate_rerun:
        blocks.append(Paragraph(T("The verify read the gate as: ", rec.gate_rerun), wrapped=True))
    if rec.verify_note:
        blocks.append(Paragraph(T(rec.verify_note), wrapped=True))
    if rec.changed:
        blocks.append(Paragraph(T("Changed: " + ", ".join(rec.changed)), wrapped=True))
    if notes:
        blocks.append(List(tuple(report_plan.note_line(note, context) for note in notes)))
    if rec.fix_passes:
        for fix_pass in rec.fix_passes:
            addressed = "; ".join(f"{line.get('id')}: {line.get('how')}" for line in fix_pass.get("addressed") or [])
            if addressed:
                blocks.append(Paragraph(T(f"fix pass {fix_pass['pass']} addressed: {addressed}"), wrapped=True))
    for replan in rec.replans:
        if replan.get("note"):
            blocks.append(Paragraph(T(f"replan {replan['pass']}: {replan['note']}"), wrapped=True))
    if rec.status == "blocked":
        # a held finding was never attempted: it has no pre-tree, so nothing was rolled back
        if rec.pre_tree:
            blocks.append(
                Paragraph(T("rolled back to the pre-finding tree; the tree and the commit history are as the finding found them"))
            )
        blocks.append(List(tuple(ListItem(T(reason)) for reason in rec.blocked_reasons) or (ListItem(T("(no reason recorded)")),)))
        if rec.diff_path:
            blocks.append(Paragraph(T("The failed fix's diff: ", (rec.diff_path, "code")), wrapped=True))
        for item_id in rec.review_findings:
            item = docket.items.get(item_id)
            if item is not None and not item.closed:
                blocks.append(Paragraph(T(f"{item_id} still stands: {item.claim}"), wrapped=True))
    if rec.commit_sha:
        blocks.append(Paragraph(T(f"Committed as {rec.commit_sha}"), wrapped=True))
    return blocks


def _landed(docket: Docket, context: ReportContext, run_dir: str) -> list[Block]:
    execution = docket.execution
    blocks: list[Block] = [Heading(2, T("What landed"))]
    if execution is None or not execution.findings:
        return blocks + [Paragraph(T("No fixes landed."))]
    if execution.baseline and run_dir:
        tree = Tree(context.project, run_dir)
        stat = tree.diff(execution.baseline, execution.final_tree or None, stat=True)
        if stat:
            blocks.append(Paragraph(T(stat, "code"), wrapped=True))
    if execution.commits:
        blocks.append(Heading(3, T("Commits")))
        rows = [(T(commit["sha"][:12]), T(commit["finding"]), T(commit["message"][:120])) for commit in execution.commits]
        blocks.append(Table(header=(T("sha"), T("finding"), T("message")), rows=tuple(rows)))
    else:
        if any(rec.status == "fixed" for rec in execution.findings):
            blocks.append(Paragraph(T("No commits: the fixes landed with commit = false.")))
        else:
            blocks.append(Paragraph(T("Nothing was committed: no finding passed.")))
    return blocks


def _final_check(docket: Docket) -> list[Block]:
    execution = docket.execution
    blocks: list[Block] = [Heading(2, T("The final check"))]
    if execution is None or not execution.final_check:
        return blocks + [Paragraph(T("The final check was not taken."))]
    check = execution.final_check
    verdict = "passed" if check.get("ok") else "failed"
    line = f"The whole-project check {verdict}."
    if check.get("ran"):
        line += f" Ran: {check['ran']}"
    blocks.append(Paragraph(T(line), wrapped=True))
    if check.get("evidence"):
        blocks.append(Paragraph(T(check["evidence"]), wrapped=True))
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
                        + (f" (finding {message.stage})" if message.stage else "")
                        + f": {message.text}"
                    )
                )
                for message in messages
            )
        )
    )
    return blocks
