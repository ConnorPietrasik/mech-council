"""§7.2 — `plan.md`.  Pure, byte-for-byte comparable, and never invents a synthesis."""
from __future__ import annotations

from ..deliberation.dispositions import derive
from ..deliberation.escalations import effective_class
from ..deliberation.state import (AGREED, CHAIR_ID, DECLINED, DISPUTED, Docket, HUMAN,
                                  Item, NO_PREFERENCE, Question, REJECTED, UNHEARD,
                                  UNSEEN)
from .context import ReportContext
from .text import caption, wrap

RULES = """\
## Rules for the implementer

- A **prescriptive** line ("do X") is a settled outcome: follow it.
- A **descriptive** line ("the code does X") is a claim about the tree: the code wins.
- An outcome you cannot actually produce is surfaced, never silently followed.
- Meet each stage's validation gate before starting the next one.
"""


def render(docket: Docket, context: ReportContext, *, run_dir: str = "",
           converged: str | None = None) -> str:
    lines = context.header("Implementation plan") + [""]
    lines += [RULES, ""]
    lines += ["## Roster", ""] + context.roster_table(extra="backing") + [""]
    lines += _chosen(docket, context, converged)
    lines += _stages(docket, context, converged)
    lines += _assumptions(docket, context)
    lines += _agreed_points(docket, context)
    lines += _decision_points(docket, context)
    lines += _human_input(docket, context)
    lines += _qa(docket, context)
    lines += _alternatives(docket, context)
    lines += _rounds(docket, context)
    lines += context.artifacts(run_dir)
    return "\n".join(lines).rstrip() + "\n"


def _chosen(docket: Docket, context: ReportContext, converged: str | None) -> list[str]:
    out = ["## Chosen direction", ""]
    if not converged:
        out.append("The council did not settle a direction. The candidates, side by side:")
        out.append("")
        for token in sorted({t for t in docket.backing.values() if t != NO_PREFERENCE}):
            backers = [a for a in docket.roster if docket.backing.get(a) == token]
            out.append(f"- `{token}` — backed by {_names(backers, context)}")
        shrug = [a for a in docket.roster if docket.backing.get(a) == NO_PREFERENCE]
        if shrug:
            out.append(f"- `no_preference` — {_names(shrug, context)}")
        never = [a for a in docket.roster if not docket.backing.get(a)]
        if never:
            out.append(f"- `declined` — {_names(never, context)}")
        out.append("")
        out.append("No direction is printed for you to follow: nobody chose one.")
        return out + [""]
    artifact, note = resolve_direction(docket, converged)
    out.append(f"- converged token: `{converged}`")
    if note:
        out.append(f"- **{note}**")
    out.append("")
    if artifact is None:
        out.append("(no plan artifact behind that token)")
        return out + [""]
    out.append(f"The plan to build: {artifact.get('name') or converged}")
    if artifact.get("text"):
        out += [f"  {line}" for line in wrap(artifact["text"])]
    out.append("")
    return out


def resolve_direction(docket: Docket, token: str) -> tuple[dict | None, str]:
    """§7.2 + plan D14: the token resolves to a real artifact, or to the fullest backer's
    plan **labeled as what it is** — never a synthesis, never a title match."""
    if token.startswith("merged:"):
        label = token.split(":", 1)[1].strip()
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
        return artifact, (f"the merge was never written down — this is "
                          f"{caption(agent, docket_captions(docket))}'s own plan, the "
                          "fullest one backing that token")
    agent = token
    approach = docket.approaches.get(agent)
    if approach is not None:
        return approach.artifact(), ""
    best = _fullest_backer(docket, token)
    return (best[1], "the named seat stated no plan") if best else (None, "no such plan")


def _fullest_backer(docket: Docket, token: str):
    """Among seats backing `token`: most stages; tie → lowest slot (plan D14)."""
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


def docket_captions(docket: Docket) -> dict[str, str]:
    return {}


def _stages(docket: Docket, context: ReportContext, converged: str | None) -> list[str]:
    artifact, _ = resolve_direction(docket, converged) if converged else (None, "")
    out = ["## Stages", ""]
    if not artifact or not artifact.get("stages"):
        return out + ["No stages were settled.", ""]
    for index, stage in enumerate(artifact["stages"], 1):
        out.append(f"### Stage {index}: {stage.get('state') or '(unnamed)'}")
        if stage.get("gate"):
            out.append(f"- gate: {stage['gate']}")
        if stage.get("scope"):
            out.append(f"- scope: {stage['scope']}")
        for exception in stage.get("exceptions") or []:
            out.append(f"- exception: {exception}")
    out.append("")
    return out


def _constraints_and_risks(docket: Docket, converged: str | None) -> dict[str, list[str]]:
    artifact, _ = resolve_direction(docket, converged) if converged else (None, "")
    source = artifact or {}
    agent = docket.approaches.get(converged or "")
    if agent and not source:
        source = agent.artifact()
    return {"constraints": list(source.get("constraints") or []),
            "risks": list(source.get("risks") or []),
            "assumptions": list(source.get("assumptions") or [])}


def _assumptions(docket: Docket, context: ReportContext) -> list[str]:
    """§7.2: every deferred / skipped / unasked / undefended question touching the
    direction is a named assumption spelling the guess it stands for."""
    out = ["## Assumptions", ""]
    questions = [docket.questions[i] for i in docket.question_order
                 if _touches_direction(docket, docket.questions[i])]
    if not questions:
        out.append("None the council had to make: every open question was answered.")
        return out + [""]
    for question in questions:
        kind = "unasked" if "unasked" in question.markers else \
            "skipped" if question.state == "skipped" else \
            "undefended" if any(e.undefended for e in _escalations(docket, question)) else \
            "deferred"
        out.append(f"- **{kind}** ({question.id}, {effective_class(docket, question)}): "
                   f"{question.text}")
        out.append(f"  assumed: {question.answer or 'whatever the seats did in its place'}")
    out.append("")
    return out


def _agreed_points(docket: Docket, context: ReportContext) -> list[str]:
    out = ["## Agreed points", ""]
    if not docket.agreed_points:
        return out + ["The chair's comparison recorded none.", ""]
    for point in docket.agreed_points:
        who = _names(point.get("agents") or [], context)
        # The chair's schema asks for the sharing seats; when it named none, saying
        # "(nobody)" would read as a finding about the council rather than a missing field.
        tail = f" — {who}" if who != "(nobody)" else " — the chair named no seats"
        out.append(f"- {point.get('point')}{tail}")
    out.append("")
    return out


def _decision_points(docket: Docket, context: ReportContext) -> list[str]:
    out = [f"## Decision points ({len(docket.order)})", ""]
    if not docket.order:
        return out + ["None were raised.", ""]
    for item in docket.items.values():
        state = item.disposition if item.closed else derive(docket, item.id)
        out.append(f"### {item.id} `[{state}]` {item.claim}")
        if item.closed:
            out.append(f"- closed {item.disposition} in round {item.closed_round}")
        out.append(f"- raised by: {_names(item.raised_by, context)}")
        if item.detail.strip():        # an empty detail must not print a stray line
            out += [f"  {line}" for line in wrap(item.detail)]
        for agent in docket.roster:
            record = docket.stance(item.id, agent)
            if record is None:
                continue
            tag = " `declined`" if record.declined else ""
            out.append(f"- {caption(agent, context.captions)}: {record.stance}"
                       f" (round {record.round}){tag}"
                       + (f" — {record.note}" if record.note else ""))
        for note in item.history:
            out.append(f"- history: {note}")
        out.append("")
    return out


def _human_input(docket: Docket, context: ReportContext) -> list[str]:
    questions = [docket.questions[i] for i in docket.question_order]
    out = ["## Human input", ""]
    if not questions:
        return out + ["None was asked for.", ""]
    for question in questions:
        state = question.state if question.state != "asked" else "standing"
        out.append(f"- {question.id} ({state}) {_names(question.askers, context)}: "
                   f"{question.text}")
        if question.answer:
            out.append(f"  - authoritative answer (round {question.answer_round}): "
                       f"{question.answer}")
        else:
            out.append(f"  - assumed in its place: whatever the council did with it — "
                       f"never settled")
    out.append("")
    return out


def _qa(docket: Docket, context: ReportContext) -> list[str]:
    out = ["## Planner Q&A", ""]
    if not docket.qa_order:
        return out + ["None asked.", ""]
    for qid in docket.qa_order:
        qa = docket.qa[qid]
        out.append(f"- {qa.id} {_names([qa.asker], context)} → {_names([qa.to], context)}: "
                   f"{qa.text}")
        if qa.answer:
            out.append(f"  - answer: {qa.answer}")
    out.append("")
    return out


def _alternatives(docket: Docket, context: ReportContext) -> list[str]:
    out = ["## Alternatives on the table", ""]
    if not docket.approaches:
        return out + ["No plans were put on the table.", ""]
    for agent in docket.roster:
        approach = docket.approaches.get(agent)
        if approach is None:
            continue
        original = approach.original or "(no text)"
        out.append(f"- {caption(agent, context.captions)}: {original.splitlines()[0] if original else ''}")
        for stage in approach.stages:
            out.append(f"  - {stage.get('state', '')}")
        for alternative in approach.rejected_alternatives:
            out.append(f"  - rejected: {alternative}")
        for revision in approach.revisions:
            out.append(f"  - revision `{revision.get('name') or '?'}` "
                       f"(round {revision.get('round')}): "
                       f"{len(revision.get('stages') or [])} stages")
    out.append("")
    return out


def _rounds(docket: Docket, context: ReportContext) -> list[str]:
    out = ["## Round-by-round verdicts", ""]
    for record in docket.rounds:
        out.append(f"### Round {record.round}")
        if record.summary:
            out.append(record.summary)
        out += [f"- {line}" for line in record.lines]
        out.append("")
    return out


def _touches_direction(docket: Docket, question: Question) -> bool:
    """A question touches the chosen direction when it names no item (so it frames the
    whole plan) or names one that is not closed rejected."""
    if question.state == "answered":
        return False
    if question.item_id is None:
        return True
    item = docket.items.get(question.item_id)
    return item is None or item.disposition != REJECTED


def _escalations(docket: Docket, question: Question):
    return [e for e in docket.escalations.values() if e.question_id == question.id]


def _names(ids: list[str], context: ReportContext) -> str:
    return ", ".join(caption(i, context.captions) for i in ids) or "(nobody)"
