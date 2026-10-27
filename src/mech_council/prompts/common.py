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
An overlay errno is never an item you raise: it says what the sandbox forbids, not what the
project does. If you hit one, say what you were trying to do and move on.
"""

SHARED = """\
# Your workspace

Your working directory is the project, and it is the real one: your writes land in it and
stay there. The other seats work in this same tree at the same time, so a file or an edit
you did not make is a peer's work in progress — never a defect, and never yours to finish,
revert or reformat. Touch only what your own task touches, and leave the rest of the tree
as you found it. `$TMPDIR` is a directory of your own outside the project: put anything
half-finished there rather than in the tree. A filesystem refusal (`EACCES`, `EROFS`,
`ENOSPC`) says what your environment forbids, not what the project does: say what you were
trying to do and move on.
"""

PROTOCOL = """\
# How this council works

You are one seat on a council working toward the goal above. The orchestrator mediates
everything: it owns the shared docket, shows you what the others said, and asks the
human what you cannot settle.

- Verify before you claim. An item survives because peers with workspace access failed
  to knock it down, not because somebody asserted it. Cite the file, the line or the
  command output that shows it.
- Peers are numbers. You are shown `AgentN`, never a name: a claim carries its evidence or
  it carries nothing, and who reported it is never a reason to believe or to fold.
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

QUESTION_SHAPE = """\
# Asking well

Every `human_input` entry carries the decision, the context, and the answers you would
accept, and the human reads them at a checkpoint while the run is stopped:

- `question` — the decision in one line. Not a topic and not a restatement of the item you are
  stuck on: the thing you cannot decide from the code.
- `context` — what you looked at that leaves it open, and what guessing wrong costs. Never
  restate the question there; two lines is the limit and the second one is the stakes.
- `options` — two to four answers **you would accept**, each phrased AS the answer
  ("keep it 30 days"), never as a subject ("retention").

Options are your candidates, not the whole space, and they settle nothing: only the
human's own answer is authoritative. They are shown numbered so the human can take one
verbatim; answering in their own words stays open to them. If you truly cannot name an
acceptable answer, say why in `context` — that is a real result too.

The menu is yours: when you re-ask, what you send **replaces the candidates you offered
before**, so send the whole menu you stand behind now and never a fragment of it. A peer
asking the same question keeps its own candidates beside yours.
"""

ANSWER_SHAPE = """\
# Answer shape

Answer with a single JSON object at the start of your reply (prose after it is fine, and
is welcome). Field names are matched case- and separator-insensitively, so
`validationGate` and `validation_gate` are the same field.
"""


def _mode_text(mode: str):
    from . import plan as plan_text
    from . import review as review_text

    return review_text if mode == "review" else plan_text


def goal_line(goal: str | None) -> str:
    return f"Goal: {goal}\n" if goal else "No goal was stated for this council.\n"


def _workspace(in_place: bool) -> str:
    """The two workspaces differ on exactly one thing the seats can act on: whether a peer shares the tree."""
    return SHARED if in_place else SANDBOX


def phase0_briefing(*, mode: str, goal: str | None, project: str, in_place: bool = False) -> str:
    text = _mode_text(mode)
    return "\n".join(
        [
            "# Phase 0 — independent pass",
            "",
            f"You are one seat on a council of coding agents convened over `{project}`.",
            goal_line(goal),
            text.PHASE0_TASK,
            PROTOCOL,
            _workspace(in_place),
            ESCALATION,
            QUESTION_SHAPE,
            ANSWER_SHAPE,
            text.PHASE0_SCHEMA,
        ]
    )


def round_briefing(
    *,
    mode: str,
    goal: str | None,
    project: str,
    me: str,
    docket: str,
    phase0_summary: str,
    in_place: bool = False,
) -> str:
    text = _mode_text(mode)
    return "\n".join(
        [
            f"# Round deliberation — you are {me}",
            "",
            f"You are one seat on a council of coding agents deliberating over `{project}`.",
            f"You are {me}. The docket labels every seat by its numeric ID — peers read `AgentN` and you read `you` — and your own "
            "entries are tagged `[yours]`, so a near-identical twin of your own claim still reads as somebody else's. Judge a claim on "
            "its evidence: who raised it is not part of the evidence.",
            goal_line(goal),
            phase0_summary,
            text.ROUND_TASK,
            PROTOCOL,
            ESCALATION,
            text.EXTRA_BRIEFING,
            QUESTION_SHAPE,
            _workspace(in_place),
            ANSWER_SHAPE,
            text.ROUND_SCHEMA,
            "---",
            "## The docket",
            "",
            docket,
        ]
    )


def gap_ask(missing: list[str], *, reason: str, deliberate: bool) -> str:
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


def merge_schema(mode: str) -> str:
    """`chair.MERGE_SCHEMA`, with the example IDs in the prefix this mode's docket issues."""
    from ..modes import adapter_for
    from . import chair

    return chair.MERGE_SCHEMA.replace("{id}", adapter_for(mode).id_prefix)


def schema_hint(mode: str, *, phase: str) -> str:
    if phase == "phase0":
        return _mode_text(mode).PHASE0_SCHEMA
    if phase == "round":
        return _mode_text(mode).ROUND_SCHEMA
    if phase == "merge":
        return merge_schema(mode)
    if phase == "compare":
        # Plan mode's pass: review has no plans to map.
        from . import plan as plan_text

        return plan_text.COMPARISON_SCHEMA
    from . import chair

    # `digest` is what the watchdog calls its own phase; the shape it must repair to is the pass
    # schema.  An unknown phase is a bug here, not a reason to crash a member's ask.
    return chair.WATCHDOG_SCHEMA


def merge_task(mode: str, listing: str, goal: str | None = None) -> str:
    from . import chair

    return "\n".join(
        [
            chair.MERGE,
            goal_line(goal),
            _mode_text(mode).MERGE_TASK,
            ANSWER_SHAPE,
            merge_schema(mode),
            "---",
            "## The new items",
            "",
            listing,
        ]
    )


def comparison_task(listing: str, goal: str | None = None) -> str:
    """Plan mode's comparison pass, in plan's own prose — which is why it takes no mode."""
    from . import plan as plan_text

    return "\n".join(
        [
            plan_text.COMPARE,
            goal_line(goal),
            plan_text.COMPARISON_TASK,
            ANSWER_SHAPE,
            plan_text.COMPARISON_SCHEMA,
            "---",
            "## The plans",
            "",
            listing,
        ]
    )


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
