"""The shared briefing blocks.  Text only — every builder here is pure.

The independence rule is a rule about *text*: `phase0_briefing()` takes no seat, no slot and no roster, so it
cannot leak an ID even by accident — byte-identical across every seat and matching no `Agent\\d+` anywhere.
"""

from __future__ import annotations

SANDBOX = """\
# Your workspace

Your working directory is the project, and an absolute path into it is your own private
copy of the project: a file another member created is not in your copy, and a file you
create is invisible to them. Directory surgery on the project is limited by the
overlay: `rm -rf` of a project directory fails with `EIO` and leaves it half-emptied,
`rename` of one gives `EXDEV`, and `mv` copies the tree, fails to delete the source and
leaves both copies. A directory you created yourself removes normally. `$TMPDIR`
(`.council-scratch` inside your working directory) has no such limits — use it freely.
An overlay errno is never a finding: it says what the sandbox forbids, not what the
project does. If you hit one, say what you were trying to do and move on.
"""

PROTOCOL = """\
# How this council works

You are one seat on a council of independent reviewers. The orchestrator mediates
everything: it owns the shared docket, shows you what the others said, and asks the
human what you cannot settle.

- Verify before you claim. A finding survives because peers with workspace access failed
  to knock it down, not because somebody asserted it. Cite the file, the line or the
  command output that shows it.
- Silence is never consent and never rejection: you are asked for a stance on every open
  item, and an answer you withhold is asked for. `abstain` is always valid, costs
  nothing, and neither endorses nor blocks — abstain rather than going quiet.
- A `disagree` without cited evidence changes nothing. To reject an item, refute it.
- Change your mind when shown better evidence; say so explicitly with a `disagree` or a
  refuting note. That is the job working, not a loss.
"""

ESCALATION = """\
# Asking the human

- `human_input` entries that name an item are escalation requests: say which item it
  blocks and what evidence would settle it without a human.
- `human_input` may also name no item at all — a question about the brief, the scope or
  the goal. That question is put to the human when the run would otherwise end; it never
  stops the run. To stop the council, stance `needs_human` on an item.
- A question you no longer need answered is dropped by restancing that item (an
  `abstain` does it), not by going quiet.
- Only an escalation several seats hold, or one the chair raises, stops the council
  mid-run to ask. Everything else is asked at the checkpoint the run reaches when it
  would otherwise end, so an unanswered question is never a way to avoid stancing.
- If a peer challenges your escalation with cited evidence, answer the challenge.
"""

ANSWER_SHAPE = """\
# Answer shape

Answer with a single JSON object, anywhere in your reply (prose around it is fine, and
is welcome). Field names are matched case- and separator-insensitively, so
`validationGate` and `validation_gate` are the same field.
"""

PLAN_EXTRA = """\
# Unanswered questions

An unanswered question the council judged **deferred** goes into the plan's assumptions
with the guess it stands for — never decided silently. An unanswered
**effective-blocking** question is a "do NOT guess": name it and stop.
"""


def _mode_text(mode: str):
    """The mode's own text, from `prompts/` — the one place prompt text lives."""
    from . import plan as plan_text
    from . import review as review_text

    return review_text if mode == "review" else plan_text


def goal_line(goal: str | None) -> str:
    return f"Goal: {goal}\n" if goal else "No goal was stated for this council.\n"


def phase0_briefing(*, mode: str, goal: str | None, project: str) -> str:
    """Byte-identical for every seat — no ID, no slot, no peer name.

    Nothing in this function takes a seat, a slot or a roster, which is what makes the
    byte-identical result a property of the code shape rather than of care taken while writing it.
    """
    text = _mode_text(mode)
    return "\n".join(
        [
            "# Phase 0 — independent pass",
            "",
            f"You are one seat on a council of coding agents convened over `{project}`.",
            goal_line(goal),
            text.PHASE0_TASK,
            PROTOCOL,
            SANDBOX,
            ESCALATION,
            ANSWER_SHAPE,
            text.PHASE0_SCHEMA,
        ]
    )


def round_briefing(*, mode: str, goal: str | None, project: str, me: str, docket: str, phase0_summary: str) -> str:
    """Self-contained (restart-safe) — identity, framing, the whole docket, the task."""
    text = _mode_text(mode)
    return "\n".join(
        [
            f"# Round deliberation — you are {me}",
            "",
            f"You are one seat on a council of coding agents deliberating over `{project}`.",
            f"You are {me}. The docket labels the seats `Agent1…`; your own entries are tagged "
            "`[yours]`, so a near-identical twin of your own claim still reads as somebody "
            "else's.",
            goal_line(goal),
            phase0_summary,
            text.ROUND_TASK,
            PROTOCOL,
            ESCALATION,
            PLAN_EXTRA if mode == "plan" else "",
            SANDBOX,
            ANSWER_SHAPE,
            text.ROUND_SCHEMA,
            "---",
            "## The docket",
            "",
            docket,
        ]
    )


def gap_ask(missing: list[str], *, reason: str, deliberate: bool) -> str:
    """Each ladder ask names only what is still missing for that seat."""
    line = (
        "You were shown these open items and returned nothing for them: "
        f"{', '.join(missing)}.\n\n"
        "Reply with the same JSON shape, containing **only** the missing entries"
    )
    if "backing" in missing:
        line += " and the missing `backing` (exactly `AgentN`, `merged: <label>` or `no_preference`)"
    if deliberate:
        line += ". `abstain` is a valid answer for any item: it costs nothing, and it neither endorses nor blocks."
    else:
        line += ". Answer every one of them."
    return line + f"\n\nWhy I am asking again: {reason}\n"


def repair_ask(mode: str, phase: str, error: str, previous: str) -> str:
    """Lenient extraction's repair re-ask: the error, the schema hint, and the truncated previous answer."""
    return "\n".join(
        [
            "Your last answer could not be read as structured output.",
            f"The problem: {error}",
            "",
            "Reply again with the same task's JSON shape, nothing else changed:",
            schema_hint(mode, phase=phase),
            "Your previous answer, truncated:",
            (previous or "")[:4000],
        ]
    )


def schema_hint(mode: str, *, phase: str) -> str:
    if phase == "phase0":
        return _mode_text(mode).PHASE0_SCHEMA
    if phase == "round":
        return _mode_text(mode).ROUND_SCHEMA
    from . import chair

    # `digest` is what the watchdog calls its own phase; the shape it must repair to is the
    # pass schema.  An unknown phase is a bug here, not a reason to crash a member's ask.
    return {
        "merge": chair.MERGE_SCHEMA,
        "compare": chair.COMPARISON_SCHEMA,
        "watchdog": chair.WATCHDOG_SCHEMA,
        "digest": chair.WATCHDOG_SCHEMA,
    }.get(phase, chair.WATCHDOG_SCHEMA)


def merge_task(mode: str, listing: str) -> str:
    """The chair's merge pass, both modes: one pass, the mode's own framing."""
    from . import chair

    task = chair.MERGE_TASK_REVIEW if mode == "review" else chair.MERGE_TASK_PLAN
    return "\n".join([chair.MERGE, task, ANSWER_SHAPE, chair.MERGE_SCHEMA, "---", "## The new items", "", listing])


def comparison_task(mode: str, listing: str) -> str:
    from . import chair

    return "\n".join([chair.COMPARE, chair.COMPARISON_TASK, ANSWER_SHAPE, chair.COMPARISON_SCHEMA, "---", "## The plans", "", listing])


def watchdog_task(digest: str) -> str:
    from . import chair

    return "\n".join(
        [
            chair.WATCHDOG,
            chair.digest_header("Every seat listed has an ask in flight right now."),
            ANSWER_SHAPE,
            chair.WATCHDOG_SCHEMA,
            "---",
            "## The seats",
            "",
            digest,
        ]
    )


def explain_ask(question: str, followup: str) -> str:
    """The human asks the asker to explain itself.  The reply is a recommendation."""
    return "\n".join(
        [
            "The human reviewing your question has a follow-up before answering.",
            f"Your question was: {question}",
            f"They ask: {followup}",
            "",
            "Answer with the tradeoffs and **a recommendation**. You may re-read the "
            "workspace. Your reply is shown beside the question as your own text: it changes "
            "no stance and settles nothing — the human still decides.",
        ]
    )


def defense_ask(item_id: str, challenge: dict, question: str) -> str:
    """The one defense ask, earned by a challenge with cited evidence."""
    return "\n".join(
        [
            f"Your `needs_human` on {item_id} was challenged with cited evidence.",
            f"Challenge from {challenge.get('by', 'a peer')}:",
            f"  note: {challenge.get('note') or '(none)'}",
            f"  evidence: {challenge.get('evidence') or '(none)'}",
            f"Your question was: {question}",
            "",
            "Answer the challenge. Restate `needs_human` and address the cited evidence — "
            "optionally with refreshed question text — or the escalation is demoted and the "
            "item derives from the remaining stances. Repeating the question without "
            "touching the evidence demotes it.",
        ]
    )
