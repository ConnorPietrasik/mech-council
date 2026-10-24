"""§12.1.5 — per-viewer docket rendering, written once.

The shared skeleton (items, stances with their round and `declined` flag, who holds no
current stance, closed one-liners **with their IDs**, §8's open human-input requests with
their effective class) is here; a mode adds *sections*, never a second renderer.
"""
from __future__ import annotations

from ..deliberation.dispositions import derive, support
from ..deliberation.escalations import effective_class
from ..deliberation.state import (ANSWERED, ASKED, CHAIR_ID, Docket, NEEDS_HUMAN,
                                  NO_PREFERENCE, Question)
from ..modes.base import adapter_for
from .text import caption

CLOSED_LINE = "closed"


def render(docket: Docket, viewer: str, *, names: dict[str, str] | None = None,
           extra_sections: list[tuple[str, str]] | None = None) -> str:
    """The docket as `viewer` sees it: `you` for itself, `[yours]` on its own entries."""
    names = names or {}
    adapter = adapter_for(docket.mode)
    noun = adapter.item_noun_plural
    out: list[str] = [f"Docket for {viewer} — round {docket.round}", ""]

    for title, body in (extra_sections or []):
        out += [f"## {title}", body, ""]

    open_items = docket.open_items()
    out.append(f"## Open {noun} ({len(open_items)})")
    if not open_items:
        out.append("(none)")
    for item in open_items:
        state = derive(docket, item.id)
        # §5.4: a seat's own entries are tagged, so self-agreement is not mistaken for
        # independent confirmation.
        yours = " [yours]" if viewer in item.raised_by else ""
        out.append(f"### {item.id} [{state}]{yours} {item.claim}")
        if item.detail:
            out.append(f"    {item.detail}")
        if item.evidence:
            out.append(f"    evidence: {item.evidence}")
        out.append(f"    severity: {item.severity} · raised by "
                   f"{_who(item.raised_by, viewer, names)}")
        if item.also_reported_by:
            out.append(f"    also reported by {_who(item.also_reported_by, viewer, names)}")
        for agent in docket.roster:
            record = docket.stance(item.id, agent)
            if record is None:
                out.append(f"    {_who([agent], viewer, names)}: (no stance yet)")
                continue
            flags = []
            if record.declined:
                flags.append("declined")
            if agent == viewer:
                flags.append("yours")
            suffix = f" [{','.join(flags)}]" if flags else ""
            line = (f"    {_who([agent], viewer, names)}: {record.stance} "
                    f"(round {record.round}){suffix}")
            if record.note:
                line += f" — {record.note}"
            if record.evidence:
                line += f" | evidence: {record.evidence}"
            out.append(line)
        missing = [a for a in docket.roster if docket.stance(item.id, a) is None]
        if missing:
            out.append("    no current stance: " + _who(missing, viewer, names))
        for escalation in docket.standing_escalations(item.id):
            question = docket.questions.get(escalation.question_id)
            if question is None:
                continue
            out.append(f"    needs_human from {_who([escalation.asker], viewer, names)}: "
                       f"{question.text} "
                       f"({'effective-blocking' if effective_class(docket, question) == 'blocking' else 'deferred'})")
        out.append("")

    closed = docket.closed_items()
    if closed:
        out.append(f"## Settled {noun} — challenge them by stancing, never by silence "
                   f"({len(closed)})")
        for item in closed:
            out.append(f"- {item.id} [{item.disposition}, round {item.closed_round}] "
                       f"{item.claim}")
            for entry in item.history:
                out.append(f"  - {entry}")
        out.append("")

    questions = _visible_questions(docket)
    if questions:
        out.append("## Questions for the human")
        for question in questions:
            out.append(_question_line(docket, question, viewer, names))
        out.append("")

    answered = [qid for qid in docket.qa_order if docket.qa[qid].answer]
    unanswered = [qid for qid in docket.qa_order if not docket.qa[qid].answer]
    if answered or unanswered:
        out.append("## Agent questions")
        for qid in unanswered + answered:
            qa = docket.qa[qid]
            out.append(f"- {qa.id} {_who([qa.asker], viewer, names)} → "
                       f"{_who([qa.to], viewer, names)}: {qa.text}")
            if qa.answer:
                out.append(f"  answer from {_who([qa.to], viewer, names)}: {qa.answer}")
        out.append("")

    if docket.mode == "plan":
        out += _plan_sections(docket, viewer, names)
    return "\n".join(out).rstrip() + "\n"


def _question_line(docket: Docket, question: Question, viewer: str,
                   names: dict[str, str]) -> str:
    where = f" (on {question.item_id})" if question.item_id else " (no item)"
    state = effective_class(docket, question)
    line = f"- {question.id}{where}, {state}: {question.text}"
    if question.askers:
        line += f"\n  asked by {_who(question.askers, viewer, names)}"
    if question.state == ANSWERED and question.answer:
        line += f"\n  ANSWER (authoritative): {question.answer}"
    elif question.state == ASKED:
        if state == "blocking":
            line += ("\n  unanswered and effective-blocking: do NOT guess. Say what you "
                     "need and stop.")
        else:
            line += "\n  unanswered and deferred: write it into your assumptions."
    if question.recommendation:
        line += f"\n  asker's recommendation: {question.recommendation}"
    return line


def _visible_questions(docket: Docket) -> list[Question]:
    """Open requests only — a withdrawn/retired/demoted escalation is never shown (§8)."""
    from ..deliberation.termination import unsettled_questions
    return unsettled_questions(docket)


def _plan_sections(docket: Docket, viewer: str, names: dict[str, str]) -> list[str]:
    out = ["## Approaches"]
    for agent in docket.roster:
        approach = docket.approaches.get(agent)
        if approach is None:
            continue
        artifact = approach.artifact()
        tag = " [yours]" if agent == viewer else ""
        out.append(f"- {agent}{tag}: {artifact.get('name') or 'the original plan'}")
        for stage in artifact.get("stages") or []:
            out.append(f"  - {stage.get('state', '')}"
                       + (f" (gate: {stage['gate']})" if stage.get("gate") else ""))
    out.append("")
    agreed = [q for q in docket.questions.values() if False]
    out += _backing_section(docket, viewer, names)
    return out


def _backing_section(docket: Docket, viewer: str, names: dict[str, str]) -> list[str]:
    if not docket.backing:
        return []
    out = ["## Backing now"]
    for agent in docket.roster:
        token = docket.backing.get(agent)
        shown = "no preference" if token == NO_PREFERENCE else (token or "(none yet)")
        out.append(f"- {_who([agent], viewer, names)}: {shown}"
                   + (" [yours]" if agent == viewer else ""))
    out.append("")
    return out


def _who(ids: list[str], viewer: str, names: dict[str, str]) -> str:
    """§5.4's `you` rewrite: the viewer's own ID reads `you` and nothing else changes."""
    return ", ".join("you" if i == viewer else caption(i, names) for i in ids) or "(nobody)"
