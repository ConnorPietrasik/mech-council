from __future__ import annotations

from ..deliberation.dispositions import derive
from ..deliberation.escalations import NO_HUMAN, SETTLED, UNBACKED, effective_class, held_back, stop_holders
from ..deliberation.notes import item_notes, report_notes
from ..deliberation.state import (
    ABSTAIN,
    AGREE,
    AGREED,
    ASKED,
    DISAGREE,
    DISPUTED,
    HUMAN,
    NEEDS_HUMAN,
    REJECTED,
    RETRACTED,
    SKIPPED,
    STANDING,
    UNHEARD,
    UNSEEN,
    Docket,
    Item,
    Question,
)
from ..sandbox.journal import printable
from .context import ReportContext
from .doc import Block, Group, Heading, List, ListItem, Paragraph, Part, Report, T
from .markdown import render_md
from .report_plan import note_line
from .text import caption, severity_key


def build(docket: Docket, context: ReportContext, *, run_dir: str = "") -> Report:
    blocks: list[Block] = context.interrupted_blocks()
    blocks += [Heading(2, T("Roster")), context.roster_table()]

    buckets = _bucket(docket)
    blocks += _consensus(docket, buckets["agreed"], context)
    blocks += _not_fully_vetted(docket, buckets["unheard"] + buckets["human"] + buckets["reopened"], context)
    blocks += _disputed(docket, buckets["disputed"], context)
    blocks += _rejected(docket, buckets["rejected"], context)
    blocks += _never_vetted(docket, buckets["unseen"], context)
    blocks += _sandbox_writes(context)
    blocks += _sandbox_limits(docket, context)
    blocks += _notes(docket, context)
    blocks += _open_questions(docket, context)
    blocks += _rounds(docket, context)
    blocks += context.artifacts(run_dir)
    return Report(title=T("Code review"), outcome=context.outcome, meta=context.meta(), blocks=tuple(blocks))


def render(docket: Docket, context: ReportContext, *, run_dir: str = "") -> str:
    return render_md(build(docket, context, run_dir=run_dir))


def _bucket(docket: Docket) -> dict[str, list[Item]]:
    out: dict[str, list[Item]] = {"agreed": [], "unheard": [], "disputed": [], "rejected": [], "unseen": [], "human": [], "reopened": []}
    for item in docket.items.values():
        if item.closed:
            out["agreed" if item.disposition == AGREED else "rejected"].append(item)
            continue
        state = derive(docket, item.id)
        if state in (AGREED, REJECTED):  # derived but not yet closed: report the state
            out["agreed" if state == AGREED else "rejected"].append(item)
            continue
        if item.history and not item.closed:
            out["reopened"].append(item)
            continue
        out.setdefault(state, []).append(item)
    return out


def _sorted(items: list[Item]) -> list[Item]:
    return sorted(items, key=lambda i: (severity_key(i.severity), i.id))


def _consensus(docket: Docket, items: list[Item], context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T(f"Consensus findings ({len(items)})"))]
    if not items:
        return blocks + [Paragraph(T("Nothing reached the majority bar with no reasoned objection."))]
    for item in _sorted(items):
        bar = docket.bar()
        acceptors = [a for a in docket.roster if (r := docket.stance(item.id, a)) and r.stance == AGREE]
        declined = [a for a in docket.roster if (r := docket.stance(item.id, a)) and r.declined]
        abstainers = [
            a for a in docket.roster if (r := docket.stance(item.id, a)) and not r.declined and r.stance in (ABSTAIN, NEEDS_HUMAN)
        ]
        group: list[Block] = [Heading(3, T(f"{item.id} · {item.severity} · {item.claim}"))]
        if item.detail.strip():  # an empty detail must not print a stray line
            group.append(Paragraph(T(item.detail), wrapped=True))
        out: list[ListItem] = []
        if item.evidence:
            out.append(ListItem(T("evidence: ", item.evidence)))
        out.append(
            ListItem(
                T(
                    "raised by: ",
                    _names(item.raised_by, context),
                    *([T(" (also reported by ", _names(item.also_reported_by, context), ")")] if item.also_reported_by else []),
                )
            )
        )
        bar_text = _names(acceptors, context) or "(nobody stated agree)"
        out.append(ListItem(T(f"accepted by ({len(acceptors)}/{docket.n}, bar {bar}): ", bar_text)))
        if declined:
            out.append(ListItem(T(("declined", "bold"), f" — the run asked and was not answered: {_names(declined, context)}")))
        if abstainers:
            out.append(ListItem(T("not vetted by: ", _names(abstainers, context))))
        out.append(ListItem(T(f"closed: {item.disposition} in round {item.closed_round}")))
        for question in _questions_on(docket, item, (SKIPPED, "unasked", "unanswered", NO_HUMAN)):
            said = (
                T(("skipped", "bold"), " by the human")
                if question.state == SKIPPED
                else T("raised, but this run had no human to answer it")
                if NO_HUMAN in question.markers
                else T("asked and left ", ("unanswered", "bold"))
                if "unanswered" in question.markers
                else T("left ", ("unasked", "bold"))
            )
            out.append(ListItem(T("caveat — ", question.id, " was ", said, f": {question.text}")))
        for note in item.history:
            out.append(ListItem(T("history: ", note)))
        for item_note in item_notes(docket, item.id):
            out.append(note_line(item_note, context))
        for marker in item.markers:
            out.append(ListItem(T((marker, "code"))))
        group.append(List(tuple(out)))
        blocks.append(Group(tuple(group)))
    return blocks


def _not_fully_vetted(docket: Docket, items: list[Item], context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T(f"Not fully vetted ({len(items)})"))]
    if not items:
        return blocks + [Paragraph(T("Nothing is short of the bar."))]
    for item in _sorted(items):
        state = derive(docket, item.id) if not item.closed else item.disposition
        group: list[Block] = [Heading(3, T(f"{item.id} · {item.severity} · {item.claim}  ", (f"[{state}]", "code")))]
        out: list[ListItem] = []
        if item.evidence:
            out.append(ListItem(T("evidence: ", item.evidence)))
        for agent in docket.roster:
            record = docket.stance(item.id, agent)
            if record is None:
                out.append(ListItem(T(f"{caption(agent, context.captions)}: (no stance yet)")))
                continue
            tag = T(" ", ("declined", "code")) if record.declined else T()
            who = f"{caption(agent, context.captions)}: {record.stance} (round {record.round})"
            out.append(ListItem(T(who, tag, *([T(f" — {record.note}")] if record.note else []))))
        not_vetted = [a for a in docket.roster if (r := docket.stance(item.id, a)) is None or r.stance != AGREE]
        out.append(ListItem(T("not vetted by: ", _names(not_vetted, context))))
        for escalation in docket.standing_escalations(item.id):
            question = docket.questions.get(escalation.question_id)
            if question:
                out.append(
                    ListItem(
                        T(
                            f"standing question {question.id} from ",
                            _names([escalation.asker], context),
                            f": {question.text} ",
                            ("(effective-blocking)" if effective_class(docket, question) == "blocking" else "(deferred)"),
                        )
                    )
                )
        for note in item.history:
            out.append(ListItem(T("history: ", note)))
        for item_note in item_notes(docket, item.id):
            out.append(note_line(item_note, context))
        group.append(List(tuple(out)))
        blocks.append(Group(tuple(group)))
    return blocks


def _disputed(docket: Docket, items: list[Item], context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T(f"Disputed findings ({len(items)})"))]
    if not items:
        return blocks + [Paragraph(T("No seat disagrees with another."))]
    for item in _sorted(items):
        group: list[Block] = [Heading(3, T(f"{item.id} · {item.severity} · {item.claim}"))]
        out: list[ListItem] = []
        if item.evidence:
            out.append(ListItem(T("evidence: ", item.evidence)))
        for agent in docket.roster:
            record = docket.stance(item.id, agent)
            if record is None:
                continue
            out.append(
                ListItem(
                    T(
                        f"{caption(agent, context.captions)} — ",
                        (record.stance, "bold"),
                        f" (round {record.round}): {record.note or '(no note)'}",
                        *([T(f" | evidence: {record.evidence}")] if record.evidence else []),
                    )
                )
            )
        for item_note in item_notes(docket, item.id):
            out.append(note_line(item_note, context))
        group.append(List(tuple(out)))
        blocks.append(Group(tuple(group)))
    return blocks


def _rejected(docket: Docket, items: list[Item], context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T(f"Rejected findings ({len(items)})"))]
    if not items:
        return blocks + [Paragraph(T("Nothing was refuted."))]
    blocks.append(Paragraph(T(("A false alarm suppressed, not missed.", "italic"))))
    for item in _sorted(items):
        group: list[Block] = [Heading(3, T(f"{item.id} · {item.severity} · {item.claim}"))]
        out: list[ListItem] = [ListItem(T(f"closed rejected in round {item.closed_round}"))]
        for agent in docket.roster:
            record = docket.stance(item.id, agent)
            if record and record.stance == DISAGREE:
                out.append(
                    ListItem(
                        T(
                            f"killed by {caption(agent, context.captions)}: ",
                            record.note or "(no note)",
                            *([T(f" | evidence: {record.evidence}")] if record.evidence else []),
                        )
                    )
                )
        for note in item.history:
            out.append(ListItem(T("history: ", note)))
        for item_note in item_notes(docket, item.id):
            out.append(note_line(item_note, context))
        group.append(List(tuple(out)))
        blocks.append(Group(tuple(group)))
    return blocks


def _never_vetted(docket: Docket, items: list[Item], context: ReportContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, T(f"Never vetted ({len(items)})"))]
    if not items:
        return blocks + [Paragraph(T("Every item reached every seat."))]
    items_list: list[ListItem] = []
    for item in _sorted(items):
        records = {a: docket.stance(item.id, a) for a in docket.roster}
        missing = [a for a, record in records.items() if record is None]
        declined = [a for a, record in records.items() if record is not None and record.declined]
        why = (
            f"never seen by {_names(missing, context)}"
            if missing
            else (f"every seat declined to engage — {_names(declined, context)}" if declined else "no seat recorded a stance")
        )
        marker_parts: list[Part] = [part for m in item.markers for part in ("  ", (m, "code"))]
        items_list.append(
            ListItem(
                T((item.id, "bold"), f" ({item.severity}) {item.claim} — {why}", *marker_parts),
                sub=tuple(note_line(item_note, context) for item_note in item_notes(docket, item.id)),
            )
        )
    blocks.append(List(tuple(items_list)))
    return blocks


def _sandbox_writes(context: ReportContext) -> list[Block]:
    if context.in_place:
        return [
            Heading(2, T("Project writes")),
            Paragraph(
                T(
                    "Members worked in the project itself, and this run journaled nothing: what they left behind is ",
                    ("git status", "code"),
                    " away.",
                )
            ),
        ]
    blocks: list[Block] = [Heading(2, T("Sandbox writes"))]
    if not context.journal:
        return blocks + [Paragraph(T("No member wrote anything."))]
    items: list[ListItem] = []
    for member in context.journal:
        parts: list[Part] = [(str(member.get("file_base", "?")), "code")]
        if member.get("id"):
            parts.append(f" ({member['id']})")
        parts.append(f" — {member.get('bytes', 0)} bytes of the {context.sandbox_mib} MiB cap")
        subs: list[ListItem] = []
        for kind in ("created", "modified", "deleted"):
            paths = member.get(kind) or []
            if paths:
                subs.append(ListItem(T(*[kind, f" ({len(paths)}): "] + _joined_code(printable(p) for p in paths))))
        items.append(ListItem(T(*parts), sub=tuple(subs)))
    blocks.append(List(tuple(items)))
    return blocks


def _joined_code(shown) -> list[Part]:
    """Code spans joined by plain ", "."""
    parts: list[Part] = []
    for index, text in enumerate(shown):
        if index:
            parts.append(", ")
        parts.append((text, "code"))
    return parts


def _sandbox_limits(docket: Docket, context: ReportContext) -> list[Block]:
    hit = [item for item in docket.items.values() if "sandbox_limit" in item.markers]
    blocks: list[Block] = [Heading(2, T("Filesystem refusals" if context.in_place else "Sandbox limits"))]
    if not hit:
        empty = "No seat reported a refusal from the filesystem." if context.in_place else "No finding rested on an overlay refusal."
        return blocks + [Paragraph(T(empty))]
    items: list[ListItem] = []
    for item in _sorted(hit):
        items.append(ListItem(T((item.id, "bold"), f" ({item.severity}) {item.claim}"), quote=T(item.evidence or item.detail)))
    blocks.append(List(tuple(items)))
    return blocks


def _notes(docket: Docket, context: ReportContext) -> list[Block]:
    """The notes that belong to no finding; a note filed under a finding prints with that finding."""
    ledger = report_notes(docket)
    filed_ids = {note.id for note in ledger if note.item_id and note.item_id in docket.items}
    notes = [note for note in ledger if note.id not in filed_ids]
    blocks: list[Block] = [Heading(2, T(f"Notes for the human ({len(notes)})"))]
    if filed_ids:
        blocks.append(Paragraph(T(f"Notes about an item print with it — {len(filed_ids)} filed.")))
    if not notes:
        if not filed_ids:
            blocks.append(Paragraph(T("Nothing the council wanted you to know without asking.")))
        return blocks
    for note in notes:
        group: list[Block] = [Heading(3, T(f"{note.id} · {'retracted' if note.state == RETRACTED else 'standing'} · {note.text}"))]
        out: list[ListItem] = [ListItem(T("from: ", _names([note.by], context)))]
        if note.also_noted_by:
            out.append(ListItem(T("also noted by: ", _names(note.also_noted_by, context))))
        if note.item_id:
            item = docket.items.get(note.item_id)
            out.append(ListItem(T("about: ", (note.item_id, "bold"), *([f" {item.claim}"]) if item is not None else [note.item_id])))
        if note.shown_round is None:
            out.append(ListItem(T("report only: the run ended before showing it")))
        else:
            out.append(ListItem(T(f"shown to you: round {note.shown_round}")))
        if note.state == RETRACTED:
            reason = f": {note.retract_reason}" if note.retract_reason else None
            out.append(ListItem(T(f"retracted in round {note.retracted_round}", *([reason] if reason else []))))
            if note.shown_round is not None:
                out.append(ListItem(T(("you were shown this before it was retracted", "bold"))))
        group.append(List(tuple(out)))
        blocks.append(Group(tuple(group)))
    return blocks


def _open_questions(docket: Docket, context: ReportContext) -> list[Block]:
    questions = [docket.questions[i] for i in docket.question_order]
    title = "Open questions for the human" if context.human == "blocking" else "Questions the council raised"
    blocks: list[Block] = [Heading(2, T(f"{title} ({len(questions)})"))]
    if not questions:
        return blocks + [Paragraph(T("The council needed nothing from you."))]
    for question in questions:
        markers = _markers(docket, question)
        group: list[Block] = [Heading(3, T(f"{question.id} · {', '.join(markers) or question.state}"))]
        out: list[ListItem] = []
        if question.item_id:
            item = docket.items.get(question.item_id)
            if item is not None:
                state = item.disposition if item.closed else derive(docket, item.id)
                section = _section_for(state)
                out.append(ListItem(T("item: ", (item.id, "bold"), f" {item.claim} → ", (state, "code"), f' (reported under "{section}")')))
        else:
            out.append(ListItem(T("stands on its own: no item")))
        out.append(ListItem(T("asked by: ", _names(question.askers, context))))
        if question.blocked_by:
            out.append(
                ListItem(
                    T(
                        "asked to stop the run for it: ",
                        _names(question.blocked_by, context),
                        f" ({len(stop_holders(docket, question))}/{docket.bar()} at the bar)",
                    )
                )
            )
        if question.retracted_by:
            out.append(ListItem(T(f"retracted by {_names([question.retracted_by], context)} in round {question.retracted_round}")))
        if question.merged_into:
            out.append(ListItem(T(f"folded into {question.merged_into}: one ask now asked by the seats behind both")))
        if question.moot_with:
            settled = docket.items.get(question.moot_with)
            said = f"{question.moot_with} as {settled.disposition}" if settled is not None and settled.closed else question.moot_with
            out.append(ListItem(T(f"moot: the council settled {said}, so this was never put to you")))
        for seat, answer in question.peer_answers.items():
            said = "answered it and asks it still" if seat in question.askers else "answered instead of asking"
            out.append(ListItem(T(f"{said}: ", _names([seat], context), f": {answer}")))
        update = f", latest text update: round {question.updated_round}" if question.updated_round != question.raised_round else None
        out.append(ListItem(T("raised: round ", str(question.raised_round), *([update] if update else []))))
        out.append(ListItem(T("effective class: ", (effective_class(docket, question), "bold"))))
        out.append(ListItem(T("question as asked: ", question.text)))
        for seat, said in question.also_asked.items():
            out.append(ListItem(T(f"asked as well by {caption(seat, context.captions)}: ", said)))
        for escalation in _escalations_for(docket, question):
            for challenge in escalation.challenges:
                out.append(
                    ListItem(
                        T(
                            f"challenge from {caption(challenge['by'], context.captions)} ",
                            f"(round {challenge['round']}): {challenge.get('note') or ''}",
                            *([T(f" | evidence: {challenge['evidence']}")] if challenge.get("evidence") else []),
                        )
                    )
                )
            if escalation.asker in question.askers:
                if escalation.defense:
                    out.append(ListItem(T(f"defense from {caption(escalation.asker, context.captions)}: ", escalation.defense)))
                elif escalation.undefended:
                    out.append(ListItem(T(f"no defense: {caption(escalation.asker, context.captions)} did not answer the challenge")))
        if question.recommendation:
            out.append(ListItem(T("asker's recommendation: ", question.recommendation)))
        if question.answer:
            out.append(ListItem(T(("answer", "bold"), f" (round {question.answer_round}): {question.answer}")))
        marker_suffix = f" ({', '.join(markers)})" if markers else None
        state_text = question.state if question.state != ASKED else "standing"
        out.append(ListItem(T("end state: ", (state_text, "bold"), *([marker_suffix] if marker_suffix else []))))
        if UNBACKED in markers:
            out.append(
                ListItem(T(("no second seat", "bold"), ": the council never put this to you — it is a result, not a question you owe"))
            )
        elif SETTLED in markers:
            out.append(
                ListItem(T(("a seat answered it", "bold"), ": the council took that answer as settlement, so this was never put to you"))
            )
        group.append(List(tuple(out)))
        blocks.append(Group(tuple(group)))
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


def _questions_on(docket: Docket, item: Item, states: tuple[str, ...]) -> list[Question]:
    out = []
    for question in docket.questions_for_item(item.id):
        if question.state in states or any(m in question.markers for m in states):
            out.append(question)
    return out


def _escalations_for(docket: Docket, question: Question):
    return [esc for esc in docket.escalations.values() if esc.question_id == question.id or esc.item_id == question.item_id]


def _markers(docket: Docket, question: Question) -> list[str]:
    out = list(question.markers)
    for esc in _escalations_for(docket, question):
        if esc.item_id == question.item_id and esc.asker in question.askers:
            if esc.state != STANDING and esc.state not in out:
                out.append(esc.state)
            if esc.undefended and "undefended" not in out:
                out.append("undefended")
    if question.state == ASKED and (held := held_back(docket, question)) and held not in out:
        out.append(held)
    if question.state == ASKED and effective_class(docket, question) == "deferred":
        if "deferred" not in out:
            out.append("deferred")
    return out


def _section_for(state: str) -> str:
    return {
        AGREED: "Consensus findings",
        REJECTED: "Rejected findings",
        UNHEARD: "Not fully vetted",
        UNSEEN: "Never vetted",
        DISPUTED: "Disputed findings",
        HUMAN: "Not fully vetted",
    }.get(state, state)


def _names(ids: list[str], context: ReportContext) -> str:
    return ", ".join(caption(i, context.captions) for i in ids) or "(nobody)"
