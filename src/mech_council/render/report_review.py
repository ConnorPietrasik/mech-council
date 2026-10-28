from __future__ import annotations

from ..deliberation.dispositions import derive
from ..deliberation.escalations import effective_class
from ..deliberation.state import AGREED, DISPUTED, HUMAN, REJECTED, UNHEARD, UNSEEN, Docket, Item, Question
from .context import ReportContext
from .text import caption, severity_key, wrap


def render(docket: Docket, context: ReportContext, *, run_dir: str = "") -> str:
    lines = context.header("Code review") + [""]
    lines += ["## Roster", ""] + context.roster_table() + [""]

    buckets = _bucket(docket)
    lines += _consensus(docket, buckets["agreed"], context)
    lines += _not_fully_vetted(docket, buckets["unheard"] + buckets["human"] + buckets["reopened"], context)
    lines += _disputed(docket, buckets["disputed"], context)
    lines += _rejected(docket, buckets["rejected"], context)
    lines += _never_vetted(docket, buckets["unseen"], context)
    lines += _sandbox_writes(context)
    lines += _sandbox_limits(docket, context)
    lines += _open_questions(docket, context)
    lines += _rounds(docket, context)
    lines += context.artifacts(run_dir)
    return "\n".join(lines).rstrip() + "\n"


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


def _consensus(docket: Docket, items: list[Item], context: ReportContext) -> list[str]:
    out = [f"## Consensus findings ({len(items)})", ""]
    if not items:
        return out + ["Nothing reached the majority bar with no reasoned objection.", ""]
    for item in _sorted(items):
        bar = docket.bar()
        acceptors = [a for a in docket.roster if (r := docket.stance(item.id, a)) and r.stance == "agree"]
        declined = [a for a in docket.roster if (r := docket.stance(item.id, a)) and r.declined]
        abstainers = [
            a for a in docket.roster if (r := docket.stance(item.id, a)) and not r.declined and r.stance in ("abstain", "needs_human")
        ]
        out.append(f"### {item.id} · {item.severity} · {item.claim}")
        if item.detail.strip():  # an empty detail must not print a stray line
            out += [f"  {line}" for line in wrap(item.detail)]
        if item.evidence:
            out.append(f"- evidence: {item.evidence}")
        out.append(
            f"- raised by: {_names(item.raised_by, context)}"
            + (f" (also reported by {_names(item.also_reported_by, context)})" if item.also_reported_by else "")
        )
        out.append(f"- accepted by ({len(acceptors)}/{docket.n}, bar {bar}): {_names(acceptors, context) or '(nobody stated agree)'}")
        if declined:
            out.append(f"- **declined** — the run asked and was not answered: {_names(declined, context)}")
        if abstainers:
            out.append(f"- not vetted by: {_names(abstainers, context)}")
        out.append(f"- closed: {item.disposition} in round {item.closed_round}")
        for question in _questions_on(docket, item, ("skipped", "unasked")):
            out.append(
                f"- caveat — {question.id} was "
                f"{'**skipped** by the human' if question.state == 'skipped' else 'left **unasked**'}: {question.text}"
            )
        for note in item.history:
            out.append(f"- history: {note}")
        for marker in item.markers:
            out.append(f"- `{marker}`")
        out.append("")
    return out


def _not_fully_vetted(docket: Docket, items: list[Item], context: ReportContext) -> list[str]:
    out = [f"## Not fully vetted ({len(items)})", ""]
    if not items:
        return out + ["Nothing is short of the bar.", ""]
    for item in _sorted(items):
        state = derive(docket, item.id) if not item.closed else item.disposition
        out.append(f"### {item.id} · {item.severity} · {item.claim}  `[{state}]`")
        if item.evidence:
            out.append(f"- evidence: {item.evidence}")
        for agent in docket.roster:
            record = docket.stance(item.id, agent)
            if record is None:
                out.append(f"- {caption(agent, context.captions)}: (no stance yet)")
                continue
            tag = " `declined`" if record.declined else ""
            out.append(
                f"- {caption(agent, context.captions)}: {record.stance}"
                f" (round {record.round}){tag}" + (f" — {record.note}" if record.note else "")
            )
        not_vetted = [a for a in docket.roster if (r := docket.stance(item.id, a)) is None or r.stance != "agree"]
        out.append(f"- not vetted by: {_names(not_vetted, context)}")
        for escalation in docket.standing_escalations(item.id):
            question = docket.questions.get(escalation.question_id)
            if question:
                out.append(
                    f"- standing question {question.id} from "
                    f"{_names([escalation.asker], context)}: {question.text} "
                    f"({'effective-blocking' if effective_class(docket, question) == 'blocking' else 'deferred'})"
                )
        for note in item.history:
            out.append(f"- history: {note}")
        out.append("")
    return out


def _disputed(docket: Docket, items: list[Item], context: ReportContext) -> list[str]:
    out = [f"## Disputed findings ({len(items)})", ""]
    if not items:
        return out + ["No seat disagrees with another.", ""]
    for item in _sorted(items):
        out.append(f"### {item.id} · {item.severity} · {item.claim}")
        if item.evidence:
            out.append(f"- evidence: {item.evidence}")
        for agent in docket.roster:
            record = docket.stance(item.id, agent)
            if record is None:
                continue
            out.append(
                f"- {caption(agent, context.captions)} — **{record.stance}** "
                f"(round {record.round}): {record.note or '(no note)'}" + (f" | evidence: {record.evidence}" if record.evidence else "")
            )
        out.append("")
    return out


def _rejected(docket: Docket, items: list[Item], context: ReportContext) -> list[str]:
    out = [f"## Rejected findings ({len(items)})", ""]
    if not items:
        return out + ["Nothing was refuted.", ""]
    out.append("_A false alarm suppressed, not missed._")
    out.append("")
    for item in _sorted(items):
        out.append(f"### {item.id} · {item.severity} · {item.claim}")
        out.append(f"- closed rejected in round {item.closed_round}")
        for agent in docket.roster:
            record = docket.stance(item.id, agent)
            if record and record.stance == "disagree":
                out.append(
                    f"- killed by {caption(agent, context.captions)}: "
                    f"{record.note or '(no note)'}" + (f" | evidence: {record.evidence}" if record.evidence else "")
                )
        for note in item.history:
            out.append(f"- history: {note}")
        out.append("")
    return out


def _never_vetted(docket: Docket, items: list[Item], context: ReportContext) -> list[str]:
    out = [f"## Never vetted ({len(items)})", ""]
    if not items:
        return out + ["Every item reached every seat.", ""]
    for item in _sorted(items):
        records = {a: docket.stance(item.id, a) for a in docket.roster}
        missing = [a for a, record in records.items() if record is None]
        declined = [a for a, record in records.items() if record is not None and record.declined]
        why = (
            f"never seen by {_names(missing, context)}"
            if missing
            else (f"every seat declined to engage — {_names(declined, context)}" if declined else "no seat recorded a stance")
        )
        marks = "".join(f"  `{m}`" for m in item.markers)
        out.append(f"- **{item.id}** ({item.severity}) {item.claim} — {why}{marks}")
    out.append("")
    return out


def _sandbox_limits(docket: Docket, context: ReportContext) -> list[str]:
    hit = [item for item in docket.items.values() if "sandbox_limit" in item.markers]
    out = ["## Filesystem refusals" if context.in_place else "## Sandbox limits", ""]
    if not hit:
        empty = "No seat reported a refusal from the filesystem." if context.in_place else "No finding rested on an overlay refusal."
        return out + [empty, ""]
    for item in _sorted(hit):
        out.append(f"- **{item.id}** ({item.severity}) {item.claim}")
        for line in wrap(item.evidence or item.detail):
            out.append(f"  > {line}")
    out.append("")
    return out


def _sandbox_writes(context: ReportContext) -> list[str]:
    if context.in_place:
        # No upper layer exists, so there is nothing to list: the project is what the members changed.
        return [
            "## Project writes",
            "",
            "Members worked in the project itself, and this run journaled nothing: what they left behind is `git status` away.",
            "",
        ]
    out = ["## Sandbox writes", ""]
    if not context.journal:
        return out + ["No member wrote anything.", ""]
    for member in context.journal:
        head = f"- `{member.get('file_base', '?')}`"
        if member.get("id"):
            head += f" ({member['id']})"
        head += f" — {member.get('bytes', 0)} bytes of the {context.sandbox_mib} MiB cap"
        out.append(head)
        for kind in ("created", "modified", "deleted"):
            paths = member.get(kind) or []
            if paths:
                out.append(f"  - {kind} ({len(paths)}): " + ", ".join(f"`{p}`" for p in paths))
    out.append("")
    return out


def _open_questions(docket: Docket, context: ReportContext) -> list[str]:
    questions = [docket.questions[i] for i in docket.question_order]
    out = [f"## Open questions for the human ({len(questions)})", ""]
    if not questions:
        return out + ["The council needed nothing from you.", ""]
    for question in questions:
        markers = _markers(docket, question)
        out.append(f"### {question.id} · {', '.join(markers) or question.state}")
        if question.item_id:
            item = docket.items.get(question.item_id)
            if item is not None:
                state = item.disposition if item.closed else derive(docket, item.id)
                section = _section_for(state)
                out.append(f'- item: **{item.id}** {item.claim} → `{state}` (reported under "{section}")')
        else:
            out.append("- stands on its own: no item")
        out.append(f"- asked by: {_names(question.askers, context)}" + (" (chair)" if question.chair_raised else ""))
        out.append(
            f"- raised: round {question.raised_round}"
            + (f", latest text update: round {question.updated_round}" if question.updated_round != question.raised_round else "")
        )
        out.append(
            f"- effective class: **{effective_class(docket, question)}**"
            + (" (the asker asked for the bar)" if question.blocking_requested else "")
        )
        out.append(f"- question as asked: {question.text}")
        for escalation in _escalations_for(docket, question):
            for challenge in escalation.challenges:
                out.append(
                    f"- challenge from {caption(challenge['by'], context.captions)} "
                    f"(round {challenge['round']}): {challenge.get('note') or ''}"
                    + (f" | evidence: {challenge['evidence']}" if challenge.get("evidence") else "")
                )
            if escalation.asker in question.askers:
                if escalation.defense:
                    out.append(f"- defense from {caption(escalation.asker, context.captions)}: {escalation.defense}")
                elif escalation.undefended:
                    out.append(f"- no defense: {caption(escalation.asker, context.captions)} did not answer the challenge")
        if question.recommendation:
            out.append(f"- asker's recommendation: {question.recommendation}")
        if question.answer:
            out.append(f"- **answer** (round {question.answer_round}): {question.answer}")
        out.append(
            f"- end state: **{question.state if question.state != 'asked' else 'standing'}**"
            + (f" ({', '.join(markers)})" if markers else "")
        )
        out.append("")
    return out


def _rounds(docket: Docket, context: ReportContext) -> list[str]:
    out = ["## Round-by-round verdicts", ""]
    for record in docket.rounds:
        out.append(f"### Round {record.round}")
        if record.summary:
            out.append(record.summary)
        out += [f"- {caption(agent, context.captions)}: {line}" for agent, line in record.lines]
        out.append("")
    return out


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
            if esc.state != "standing" and esc.state not in out:
                out.append(esc.state)
            if esc.undefended and "undefended" not in out:
                out.append("undefended")
    if question.state == "asked" and effective_class(docket, question) == "deferred":
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
