from __future__ import annotations

from ..deliberation.dispositions import derive
from ..deliberation.escalations import effective_class
from ..deliberation.state import ANSWERED, ASKED, NO_PREFERENCE, Docket, Question
from ..modes.base import adapter_for


def render(docket: Docket, viewer: str, *, extra_sections: list[tuple[str, str]] | None = None) -> str:
    adapter = adapter_for(docket.mode)
    noun = adapter.item_noun_plural
    out: list[str] = [f"Docket for {viewer} — round {docket.round}", ""]

    for title, body in extra_sections or []:
        out += [f"## {title}", body, ""]

    open_items = docket.open_items()
    out.append(f"## Open {noun} ({len(open_items)})")
    if not open_items:
        out.append("(none)")
    for item in open_items:
        state = derive(docket, item.id)
        # A seat's own entries are tagged, so self-agreement is not mistaken for independent confirmation.
        yours = " [yours]" if viewer in item.raised_by else ""
        out.append(f"### {item.id} [{state}]{yours} {item.claim}")
        if item.detail:
            out.append(f"    {item.detail}")
        if item.evidence:
            out.append(f"    evidence: {item.evidence}")
        out.append(f"    severity: {item.severity} · raised by {_who(item.raised_by, viewer)}")
        if item.also_reported_by:
            out.append(f"    also reported by {_who(item.also_reported_by, viewer)}")
        for agent in docket.roster:
            record = docket.stance(item.id, agent)
            if record is None:
                out.append(f"    {_who([agent], viewer)}: (no stance yet)")
                continue
            flags = []
            if record.declined:
                flags.append("declined")
            if agent == viewer:
                flags.append("yours")
            suffix = f" [{','.join(flags)}]" if flags else ""
            line = f"    {_who([agent], viewer)}: {record.stance} (round {record.round}){suffix}"
            if record.note:
                line += f" — {record.note}"
            if record.evidence:
                line += f" | evidence: {record.evidence}"
            out.append(line)
        missing = [a for a in docket.roster if docket.stance(item.id, a) is None]
        if missing:
            out.append("    no current stance: " + _who(missing, viewer))
        for escalation in docket.standing_escalations(item.id):
            question = docket.questions.get(escalation.question_id)
            if question is None:
                continue
            out.append(
                f"    needs_human from {_who([escalation.asker], viewer)}: "
                f"{question.text} "
                f"({'effective-blocking' if effective_class(docket, question) == 'blocking' else 'deferred'})"
            )
        out.append("")

    closed = docket.closed_items()
    if closed:
        out.append(f"## Settled {noun} — challenge them by stancing, never by silence ({len(closed)})")
        for item in closed:
            out.append(f"- {item.id} [{item.disposition}, round {item.closed_round}] {item.claim}")
            for entry in item.history:
                out.append(f"  - {entry}")
        out.append("")

    open_asks, answered_asks = _ask_sections(docket)
    if open_asks:
        out.append(f"## Questions for the human ({len(open_asks)})")
        for question in open_asks:
            out.append(_question_line(docket, question, viewer))
        out.append("")
    if answered_asks:
        out.append(f"## Human answers — plan against these, never your own guess ({len(answered_asks)})")
        for question in answered_asks:
            out.append(_question_line(docket, question, viewer))
        out.append("")

    answered = [qid for qid in docket.qa_order if docket.qa[qid].answer]
    unanswered = [qid for qid in docket.qa_order if not docket.qa[qid].answer]
    if answered or unanswered:
        out.append("## Agent questions")
        for qid in unanswered + answered:
            qa = docket.qa[qid]
            out.append(f"- {qa.id} {_who([qa.asker], viewer)} → {_who([qa.to], viewer)}: {qa.text}")
            if qa.answer:
                out.append(f"  answer from {_who([qa.to], viewer)}: {qa.answer}")
        out.append("")

    if docket.mode == "plan":
        out += _plan_sections(docket, viewer)
    return "\n".join(out).rstrip() + "\n"


def _question_line(docket: Docket, question: Question, viewer: str) -> str:
    where = f" (on {question.item_id})" if question.item_id else " (no item)"
    state = f"answered, round {question.answer_round}" if question.state == ANSWERED else effective_class(docket, question)
    line = f"- {question.id}{where}, {state}: {question.text}"
    if question.askers:
        line += f"\n  asked by {_who(question.askers, viewer)}"
    if question.state == ANSWERED and question.answer:
        line += f"\n  ANSWER (authoritative): {question.answer}"
    elif question.state == ASKED:
        if state == "blocking":
            line += "\n  unanswered and effective-blocking: do NOT guess. Say what you need and stop."
        else:
            line += "\n  unanswered and deferred: write it into your assumptions."
    if question.recommendation:
        line += f"\n  asker's recommendation: {question.recommendation}"
    return line


def _ask_sections(docket: Docket) -> tuple[list[Question], list[Question]]:
    """The open asks and the human's answered ones, kept apart."""
    from ..deliberation.termination import unsettled_questions

    answered = [q for q in (docket.questions[i] for i in docket.question_order) if q.state == ANSWERED and q.answer]
    return unsettled_questions(docket), answered


def _plan_sections(docket: Docket, viewer: str) -> list[str]:
    out = ["## Approaches"]
    for agent in docket.roster:
        approach = docket.approaches.get(agent)
        if approach is None:
            continue
        artifact = approach.artifact()
        out.append(f"- {_who([agent], viewer)}: {artifact.get('name') or 'the original plan'}")
        for stage in artifact.get("stages") or []:
            out.append(f"  - {stage.get('state', '')}" + (f" (gate: {stage['gate']})" if stage.get("gate") else ""))
    out.append("")
    out += _backing_section(docket, viewer)
    return out


def _backing_section(docket: Docket, viewer: str) -> list[str]:
    if not docket.backing:
        return []
    out = ["## Backing now"]
    for agent in docket.roster:
        token = docket.backing.get(agent)
        shown = "no preference" if token == NO_PREFERENCE else (token or "(none yet)")
        out.append(f"- {_who([agent], viewer)}: {shown}")
    out.append("")
    return out


def _who(ids: list[str], viewer: str) -> str:
    return ", ".join("you" if i == viewer else i for i in ids) or "(nobody)"
