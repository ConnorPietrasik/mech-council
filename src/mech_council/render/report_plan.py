from __future__ import annotations

from ..deliberation.dispositions import derive
from ..deliberation.escalations import HELD_BACK, NO_HUMAN, NOT_WAITED, SETTLED, UNBACKED, effective_class, held_back
from ..deliberation.notes import item_notes, report_notes
from ..deliberation.state import (
    ANSWERED,
    ASKED,
    MERGED,
    NO_PREFERENCE,
    REJECTED,
    RETRACTED,
    SKIPPED,
    WITHDRAWN,
    Docket,
    Note,
    Question,
)
from .context import ReportContext
from .doc import Block, Group, Heading, List, ListItem, Paragraph, Part, Report, T
from .markdown import render_md
from .text import caption

RULES: tuple[ListItem, ...] = (
    ListItem(T("A ", ("prescriptive", "bold"), ' line ("do X") is a settled outcome: follow it.')),
    ListItem(T("A ", ("descriptive", "bold"), ' line ("the code does X") is a claim about the tree: the code wins.')),
    ListItem(T("An outcome you cannot actually produce is surfaced, never silently followed.")),
    ListItem(T("Meet each stage's validation gate before starting the next one.")),
)


def build(docket: Docket, context: ReportContext, *, run_dir: str = "", converged: str | None = None) -> Report:
    blocks: list[Block] = context.interrupted_blocks()
    blocks += [Heading(2, T("Rules for the implementer")), List(RULES)]
    blocks += [Heading(2, T("Roster")), context.roster_table(extra="backing")]
    blocks += _chosen(docket, context, converged)
    blocks += _stages(docket, context, converged)
    blocks += _assumptions(docket, context)
    blocks += _agreed_points(docket, context)
    blocks += _decision_points(docket, context)
    blocks += folded_ids(docket)
    blocks += _human_input(docket, context)
    blocks += _notes(docket, context, under=set(docket.items))
    blocks += _qa(docket, context)
    blocks += _alternatives(docket, context)
    blocks += _rounds(docket, context)
    blocks += context.artifacts(run_dir)
    return Report(title=T("Implementation plan"), outcome=context.outcome, meta=context.meta(), blocks=tuple(blocks))


def render(docket: Docket, context: ReportContext, *, run_dir: str = "", converged: str | None = None) -> str:
    return render_md(build(docket, context, run_dir=run_dir, converged=converged))


def _chosen(docket: Docket, context: ReportContext, converged: str | None) -> list[Block]:
    blocks: list[Block] = [Heading(2, T("Chosen direction"))]
    if not converged:
        blocks.append(Paragraph(T("The council did not settle a direction. The candidates, side by side:")))
        items: list[ListItem] = []
        for token in sorted({t for t in docket.backing.values() if t != NO_PREFERENCE}):
            backers = [a for a in docket.roster if docket.backing.get(a) == token]
            items.append(ListItem(T((token, "code"), f" — backed by {_names(backers, context)}")))
        shrug = [a for a in docket.roster if docket.backing.get(a) == NO_PREFERENCE]
        if shrug:
            items.append(ListItem(T(("no_preference", "code"), f" — {_names(shrug, context)}")))
        never = [a for a in docket.roster if not docket.backing.get(a)]
        if never:
            items.append(ListItem(T(("declined", "code"), f" — {_names(never, context)}")))
        ask = docket.direction_ask()
        if ask is not None:
            if context.human == "none":
                sub = (ListItem(T(f"answer: {ask.answer}")),) if ask.answer else ()
                items.append(
                    ListItem(T(f"the council's split was recorded, with no human on the run to choose ({ask.id}): ", ask.text), sub=sub)
                )
            else:
                sub = (
                    ListItem(T(f"answer: {ask.answer}"))
                    if ask.answer
                    else ListItem(T(f"{'declined' if ask.state == SKIPPED else 'never answered'}")),
                )
                items.append(ListItem(T(f"the human was asked ({ask.id}): ", ask.text), sub=sub))
        blocks.append(List(tuple(items)))
        blocks.append(Paragraph(T("No direction is printed for you to follow: nobody chose one.")))
        return blocks
    artifact, note = resolve_direction(docket, converged, context.captions)
    token_items: list[ListItem] = [ListItem(T("converged token: ", (converged, "code")))]
    if context.interrupted is not None:
        cut_note = f"{context.interrupted.name} ended the run with this direction holding the majority, not settled"
        token_items.append(ListItem(T((cut_note, "bold"))))
    if note:
        token_items.append(ListItem(T((note, "bold"))))
    blocks.append(List(tuple(token_items)))
    if artifact is None:
        blocks.append(Paragraph(T("(no plan artifact behind that token)")))
        return blocks
    tail: list[Block] = [Paragraph(T(f"The plan to build: {artifact.get('name') or converged}"))]
    if artifact.get("text"):
        tail.append(Paragraph(T(artifact["text"]), wrapped=True))
    blocks.append(Group(tuple(tail)))
    return blocks


def resolve_direction(docket: Docket, token: str, captions: dict[str, str]) -> tuple[dict | None, str]:
    if token.startswith(MERGED):
        label = token.partition(MERGED)[2].strip()
        for agent in docket.roster:
            approach = docket.approaches.get(agent)
            if approach is None:
                continue
            for revision in reversed(approach.revisions):
                if revision.get("name") == label:
                    return revision, ""
        best = _fullest_backer(docket, token)
        if best is None:
            return None, "the merge was never written down and no seat backs it"
        agent, artifact = best
        return artifact, (
            f"the merge was never written down — this is {caption(agent, captions)}'s own plan, the fullest one backing that token"
        )
    agent = token
    approach = docket.approaches.get(agent)
    if approach is not None:
        return approach.artifact(), ""
    best = _fullest_backer(docket, token)
    return (best[1], "the named seat stated no plan") if best else (None, "no such plan")


def _fullest_backer(docket: Docket, token: str):
    best = None
    for agent in docket.roster:
        if docket.backing.get(agent) != token:
            continue
        approach = docket.approaches.get(agent)
        if approach is None:
            continue
        artifact = approach.artifact()
        stages = len(artifact.get("stages") or [])
        if best is None or stages > best[2]:
            best = (agent, artifact, stages)
    return (best[0], best[1]) if best else None


def _stages(docket: Docket, context: ReportContext, converged: str | None) -> list[Block]:
    artifact, _ = resolve_direction(docket, converged, context.captions) if converged else (None, "")
    blocks: list[Block] = [Heading(2, T("Stages"))]
    if not artifact or not artifact.get("stages"):
        return blocks + [Paragraph(T("No stages were settled."))]
    stage_blocks: list[Block] = []
    for index, stage in enumerate(artifact["stages"], 1):
        stage_blocks.append(Heading(3, T(f"Stage {index}: {stage.get('state') or '(unnamed)'}")))
        out: list[ListItem] = []
        if stage.get("gate"):
            out.append(ListItem(T("gate: ", stage["gate"])))
        if stage.get("scope"):
            out.append(ListItem(T("scope: ", stage["scope"])))
        for exception in stage.get("exceptions") or []:
            out.append(ListItem(T("exception: ", exception)))
        if out:
            stage_blocks.append(List(tuple(out)))
    blocks.append(Group(tuple(stage_blocks)))
    return blocks


def _assumptions(docket: Docket, context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T("Assumptions"))]
    questions = [docket.questions[i] for i in docket.question_order if _touches_direction(docket, docket.questions[i])]
    if not questions:
        return blocks + [Paragraph(T("None the council had to make: every open question was answered."))]
    items: list[ListItem] = []
    for question in questions:
        kind = _assumption_kind(docket, question)
        items.append(
            ListItem(
                T((kind, "bold"), f" ({question.id}, {effective_class(docket, question)}): ", question.text),
                tail=T(f"assumed: {question.answer or 'whatever the seats did in its place'}"),
            )
        )
    blocks.append(List(tuple(items)))
    return blocks


def _assumption_kind(docket: Docket, question: Question) -> str:
    """Why this ask became an assumption, as the docket phrases it."""
    held = held_back(docket, question) if question.state == ASKED else next((m for m in question.markers if m in HELD_BACK), "")
    if NO_HUMAN in question.markers:
        return "no human on this run"
    if "unasked" in question.markers:
        return "unasked"
    if "unanswered" in question.markers:
        return "unanswered"
    if held:
        return held
    if question.state == SKIPPED:
        return "skipped"
    if any(e.undefended for e in _escalations(docket, question)):
        return "undefended"
    return "deferred"


def _agreed_points(docket: Docket, context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T("Agreed points"))]
    if not docket.agreed_points:
        return blocks + [Paragraph(T("The chair's comparison recorded none."))]
    items: list[ListItem] = []
    for point in docket.agreed_points:
        who = _names(point.get("agents") or [], context)
        tail = f" — {who}" if who != "(nobody)" else " — the chair named no seats"
        items.append(ListItem(T(f"{point.get('point')}", tail)))
    blocks.append(List(tuple(items)))
    return blocks


def _decision_points(docket: Docket, context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T(f"Decision points ({len(docket.order)})"))]
    if not docket.order:
        return blocks + [Paragraph(T("None were raised."))]
    for item in docket.items.values():
        state = item.disposition if item.closed else derive(docket, item.id)
        group: list[Block] = [Heading(3, T(f"{item.id} ", (f"[{state}]", "code"), f" {item.claim}"))]
        before: list[ListItem] = []
        if item.closed:
            before.append(ListItem(T(f"closed {item.disposition} in round {item.closed_round}")))
        before.append(
            ListItem(
                T(
                    "raised by: ",
                    _names(item.raised_by, context),
                    *([T(f" (also reported by {_names(item.also_reported_by, context)})")] if item.also_reported_by else []),
                )
            )
        )
        group.append(List(tuple(before)))
        if item.detail.strip():  # an empty detail must not print a stray line
            group.append(Paragraph(T(item.detail), wrapped=True))
        after: list[ListItem] = []
        for agent in docket.roster:
            record = docket.stance(item.id, agent)
            if record is None:
                continue
            tag = T(" ", ("declined", "code")) if record.declined else T()
            who = f"{caption(agent, context.captions)}: {record.stance} (round {record.round})"
            after.append(ListItem(T(who, tag, *([T(f" — {record.note}")] if record.note else []))))
        for note in item.history:
            after.append(ListItem(T("history: ", note)))
        for item_note in item_notes(docket, item.id):
            after.append(note_line(item_note, context))
        if after:
            group.append(List(tuple(after)))
        blocks.append(Group(tuple(group)))
    return blocks


def folded_ids(docket: Docket) -> list[Block]:
    """The ids the chair folded away: what a stale reference in the record means now."""
    if not docket.item_aliases:
        return []
    items = tuple(ListItem(T(f"{old} → {docket.resolve_item(new)}")) for old, new in docket.item_aliases.items())
    return [Heading(2, T(f"Folded ids — name the survivor, not the folded one ({len(docket.item_aliases)})")), List(items)]


def _human_input(docket: Docket, context: ReportContext) -> list[Block]:
    questions = [docket.questions[i] for i in docket.question_order]
    blocks: list[Block] = [Heading(2, T("Human input"))]
    if not questions:
        return blocks + [Paragraph(T("None was asked for."))]
    items: list[ListItem] = []
    for question in questions:
        if question.state == ASKED and NO_HUMAN in question.markers:
            # a no-human run never put the ask in front of anyone: "standing" would say the run is waiting
            state = "never asked"
        elif question.state == ASKED and NOT_WAITED in question.markers:
            # the run's own gate on a run that never waits: "standing" would say the run is waiting
            state = "not waited on"
        elif question.state == ASKED and UNBACKED in question.markers:
            # the council itself kept the ask back: "standing" would say the run is waiting on it
            state = "unseconded"
        elif question.state == ASKED and SETTLED in question.markers:
            state = "settled"
        elif question.state != ASKED:
            state = question.state
        else:
            state = "standing"
        subs: list[ListItem] = []
        for seat, said in question.also_asked.items():
            subs.append(ListItem(T(f"asked as well by {_names([seat], context)}: ", said)))
        if question.retracted_by:
            subs.append(ListItem(T(f"retracted by {_names([question.retracted_by], context)} in round {question.retracted_round}")))
        if question.merged_into:
            subs.append(ListItem(T(f"folded into {question.merged_into}: the same ask in other words")))
        for seat, answer in question.peer_answers.items():
            said = "answered it and asks it still" if seat in question.askers else "answered instead of asking"
            subs.append(ListItem(T(f"{said}: ", _names([seat], context), f": {answer}")))
        if question.answer:
            subs.append(ListItem(T(f"authoritative answer (round {question.answer_round}): ", question.answer)))
        elif question.moot_with:
            subs.append(ListItem(T(f"moot with {question.moot_with}: the council settled that item, so nothing was assumed in its place")))
        elif NO_HUMAN in question.markers:
            subs.append(ListItem(T("raised, but this run had no human to answer it")))
        elif NOT_WAITED in question.markers:
            subs.append(ListItem(T("the run recorded it and kept going; it never waited on this one")))
        elif UNBACKED in question.markers:
            subs.append(ListItem(T("no second seat backed it: the council never put this to you — it is a result, not a question you owe")))
        elif SETTLED in question.markers:
            subs.append(ListItem(T("a seat answered it: the council took that answer as settlement, so this was never put to you")))
        else:
            subs.append(ListItem(T("assumed in its place: whatever the council did with it — never settled")))
        items.append(ListItem(T(f"{question.id} ({state}) ", _names(question.askers, context), f": {question.text}"), sub=tuple(subs)))
    blocks.append(List(tuple(items)))
    return blocks


def _notes(docket: Docket, context: ReportContext, *, under: set[str]) -> list[Block]:
    """The ledger minus the notes that print under their item in this report."""
    notes = report_notes(docket)
    filed = {note.id for note in notes if note.item_id in under}
    flat = [note for note in notes if note.id not in filed]
    blocks: list[Block] = [Heading(2, T(f"Notes for the human ({len(flat)})"))]
    if filed:
        blocks.append(Paragraph(T(f"Notes about an item print with it — {len(filed)} filed.")))
    if not flat:
        if not filed:
            blocks.append(Paragraph(T("Nothing was said to you that needed no answer.")))
        return blocks
    items: list[ListItem] = []
    for note in flat:
        where = f" (on {note.item_id})" if note.item_id else ""
        state = "retracted" if note.state == RETRACTED else "standing"
        shown = f"shown to you in round {note.shown_round}" if note.shown_round is not None else "report only: no checkpoint showed it"
        subs: list[ListItem] = [ListItem(T(shown))]
        if note.state == RETRACTED:
            reason = f": {note.retract_reason}" if note.retract_reason else ""
            after = ", after you had been shown it" if note.shown_round is not None else ""
            subs.append(ListItem(T((f"retracted in round {note.retracted_round}", "bold"), after, reason)))
        items.append(ListItem(T(f"{note.id} ({state}){where} ", _names([note.by], context), f": {note.text}"), sub=tuple(subs)))
    blocks.append(List(tuple(items)))
    return blocks


def note_line(note: Note, context: ReportContext) -> ListItem:
    """A note filed under an item: the one line the item's section prints for it."""
    who = _names([note.by], context)
    label = f"correction, {who}" if note.kind == "correction" else who
    if note.also_noted_by:
        label += f", also noted by {_names(note.also_noted_by, context)}"
    parts: list[Part] = ["note: ", (note.id, "bold"), f" ({label}"]
    if note.state == RETRACTED:
        reason = f": {note.retract_reason}" if note.retract_reason else ""
        parts.append(f", retracted in round {note.retracted_round}{reason}")
    parts.append(f"): {note.text}")
    if note.shown_round is None:
        parts.append(" (report only: no checkpoint showed it)")
    return ListItem(T(*parts))


def _qa(docket: Docket, context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T("Planner Q&A"))]
    if not docket.qa_order:
        return blocks + [Paragraph(T("None asked."))]
    items: list[ListItem] = []
    for qid in docket.qa_order:
        qa = docket.qa[qid]
        sub = (ListItem(T(f"answer: {qa.answer}")),) if qa.answer else ()
        items.append(ListItem(T(f"{qa.id} {_names([qa.asker], context)} → {_names([qa.to], context)}: {qa.text}"), sub=sub))
    blocks.append(List(tuple(items)))
    return blocks


def _alternatives(docket: Docket, context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T("Alternatives on the table"))]
    if not docket.approaches:
        return blocks + [Paragraph(T("No plans were put on the table."))]
    items: list[ListItem] = []
    for agent in docket.roster:
        approach = docket.approaches.get(agent)
        if approach is None:
            continue
        original = approach.original or "(no text)"
        subs: list[ListItem] = [ListItem(T(stage.get("state", ""))) for stage in approach.stages]
        subs += [ListItem(T("rejected: ", alternative)) for alternative in approach.rejected_alternatives]
        subs += [
            ListItem(
                T(
                    "revision ",
                    (str(revision.get("name") or "?"), "code"),
                    f" (round {revision.get('round')}): {len(revision.get('stages') or [])} stages",
                )
            )
            for revision in approach.revisions
        ]
        items.append(ListItem(T(f"{caption(agent, context.captions)}: ", original.splitlines()[0] if original else ""), sub=tuple(subs)))
    blocks.append(List(tuple(items)))
    return blocks


def _rounds(docket: Docket, context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T("Round-by-round verdicts"))]
    for record in docket.rounds:
        group: list[Block] = [Heading(3, T(f"Round {record.round}"))]
        if record.summary:
            group.append(Paragraph(T(record.summary)))
        group.append(List(tuple(ListItem(T(f"{caption(agent, context.captions)}: {line}")) for agent, line in record.lines)))
        blocks.append(Group(tuple(group)))
    return blocks


def _touches_direction(docket: Docket, question: Question) -> bool:
    if question.direction_ask:
        return False  # the split itself, printed under "Chosen direction", never one of the plan's assumptions
    if question.state in (ANSWERED, WITHDRAWN):
        return False
    if question.item_id is None:
        return True
    item = docket.items.get(question.item_id)
    return item is None or item.disposition != REJECTED


def _escalations(docket: Docket, question: Question):
    return [e for e in docket.escalations.values() if e.question_id == question.id]


def _names(ids: list[str], context: ReportContext) -> str:
    return ", ".join(caption(i, context.captions) for i in ids) or "(nobody)"
