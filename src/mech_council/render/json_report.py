"""The JSON presenter: the run's docket, for a program to read back.

The items with their stances, severity and disposition, the questions and what became of them,
the notes and messages, and the mode's own record — a plan run's approaches and backing, an
implement run's spine.
"""

from __future__ import annotations

import dataclasses
import json

from ..deliberation.dispositions import derive
from ..deliberation.escalations import effective_class, showable
from ..deliberation.state import RETIRED, Docket, Note
from .context import ReportContext

#: the shape of this document; bump when a key's meaning changes
VERSION = 1

#: the document's name, in its own `format` key
FORMAT = "council-report"


def render(docket: Docket | None, context: ReportContext, *, reason: str = "") -> str:
    """The docket as one JSON document; the run's reason alone when it recorded nothing."""
    return json.dumps(build(docket, context, reason=reason), indent=2, ensure_ascii=False, default=str) + "\n"


def fallback(outcome: str, error: str) -> str:
    """The document the renderer owes when even the run's own block could not be built."""
    document = {"format": FORMAT, "version": VERSION, "run": {"outcome": outcome}, "error": error}
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def build(docket: Docket | None, context: ReportContext, *, reason: str = "") -> dict:
    out: dict = {
        "format": FORMAT,
        "version": VERSION,
        "run": {
            "council": context.council,
            "mode": context.mode,
            "project": context.project,
            "config": context.config_path,
            "goal": context.goal,
            "outcome": context.outcome,
            "rounds": context.rounds,
            "date": context.date,
            "human": context.human,
        },
        "roster": [{"id": row.id, "name": row.name, "model": row.model} for row in context.roster],
        "chair": {"name": context.chair_name, "model": context.chair_model},
        "items": [],
        "questions": [],
        "notes": [],
        "messages": [],
        "qa": [],
        "escalations": [],
        "rounds": [],
        "phase0_counts": {},
    }
    if docket is None or docket.blank:
        if reason:
            out["reason"] = reason
        return out
    if context.aborted_reason:
        out["run"]["aborted_reason"] = context.aborted_reason
    out["items"] = [_item(docket, item_id) for item_id in docket.order]
    out["questions"] = [_question(docket, qid) for qid in docket.question_order]
    out["notes"] = [_note(docket.human_notes[nid]) for nid in docket.note_order]
    out["messages"] = [dataclasses.asdict(message) for message in docket.human_messages]
    out["qa"] = [dataclasses.asdict(record) for record in (docket.qa[qid] for qid in docket.qa_order)]
    out["escalations"] = [
        {
            "item": item_id,
            "asker": asker,
            "state": e.state,
            "question": e.question_id,
            "raised_round": e.raised_round,
            "challenges": e.challenges,
            "defense": e.defense,
            "defended_round": e.defended_round,
        }
        for (item_id, asker), e in docket.escalations.items()
    ]
    out["rounds"] = [
        {"round": record.round, "summary": record.summary, "lines": [list(line) for line in record.lines]} for record in docket.rounds
    ]
    out["phase0_counts"] = dict(docket.phase0_counts)
    if docket.mode == "plan":
        out["approaches"] = {agent: dataclasses.asdict(approach) for agent, approach in docket.approaches.items()}
        out["agreed_points"] = list(docket.agreed_points)
        out["backing"] = dict(docket.backing)
    if docket.execution is not None:
        out["execution"] = dataclasses.asdict(docket.execution)
    return out


def _item(docket: Docket, item_id: str) -> dict:
    item = docket.items[item_id]
    return {
        "id": item.id,
        "claim": item.claim,
        "detail": item.detail,
        "evidence": item.evidence,
        "severity": item.severity,
        "disposition": item.disposition,
        "state": derive(docket, item_id),
        "raised_by": list(item.raised_by),
        "also_reported_by": list(item.also_reported_by),
        "opened_round": item.opened_round,
        "closed_round": item.closed_round,
        "history": list(item.history),
        "markers": list(item.markers),
        "stances": {
            agent: {
                "stance": record.stance,
                "note": record.note,
                "evidence": record.evidence,
                "round": record.round,
                "declined": record.declined,
            }
            for agent, record in docket.stances[item_id].items()
        },
    }


def _question(docket: Docket, qid: str) -> dict:
    q = docket.questions[qid]
    return {
        "id": q.id,
        "askers": list(q.askers),
        "blocked_by": list(q.blocked_by),
        "item": q.item_id,
        "raised_round": q.raised_round,
        "question": q.text,
        "context": q.context,
        "options": list(q.options),
        "effective": effective_class(docket, q),
        "showable": showable(docket, q),
        "peer_answers": dict(q.peer_answers),
        "insisted": q.insisted,
        "retracted_by": q.retracted_by,
        "retracted_round": q.retracted_round,
        "moot_with": q.moot_with,
        "state": q.state,
        "answer": q.answer,
        "answer_round": q.answer_round,
        "answer_option": q.answer_option,
        "answer_option_text": q.answer_option_text,
        "recommendation": q.recommendation,
        "markers": list(q.markers),
        "merged_into": q.merged_into,
        "direction_ask": q.direction_ask,
        "retired_escalations": [list(key) for key, e in docket.escalations.items() if e.question_id == q.id and e.state == RETIRED],
    }


def _note(note: Note) -> dict:
    return {
        "id": note.id,
        "seat": note.by,
        "item": note.item_id,
        "raised_round": note.round,
        "note": note.text,
        "state": note.state,
        "shown_round": note.shown_round,
        "retracted_round": note.retracted_round,
        "retract_reason": note.retract_reason,
        "retraction_shown_round": note.retraction_shown_round,
    }
