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

- `human_input` entries ask. Naming an item says what the ask is about; it does **not**
  escalate it, hold it open, or stop the run. Only the stance `needs_human` says you will not
  sign that item off, and that is the act a peer can challenge you to defend.
- An ask is a proposal to the council, not a message to the human. It reaches them only once
  a **second seat backs it** with `second: ["H1"]`; an unbacked ask is reported and never
  asked. Back the ones you would act on and leave the rest alone.
- A peer who wants the same decision will word it differently. Two of you holding **one item** raise
  one ask together — each of you the other's second seat, without either saying so — and the chair
  folds asks that want one answer into the ask raised first. So word yours such that the answer you
  need is the only answer that fits — a sharp ask folded into a vague one stops nothing.
- Every ask carries an id (`H1`) on the docket, and most of them belong to the council, not
  to the human: `answer_question` answers a peer's ask from the workspace, `insist: ["H1"]` says a
  peer's answer to one of yours was not enough, and `retract: ["H1"]` drops one you raised and no
  longer need. Say so — never go quiet.
- Dropping an ask releases nothing but itself: an item you hold with `needs_human` goes only by
  **stancing that item again** as one you now accept or abstain from. And the reverse: letting go of
  the item drops the ask your hold raised, so re-ask it if you still need the answer. The council
  **settling the item** answers an ask about it the same way — a declared stop is the one claim that
  outranks a closure — so ask over no item what the closure left open.
- A peer's answer holds an ask back — they are telling the council the workspace settles it. Only a
  seat that **raised** the ask can call that answer not enough, with `insist: ["H1"]` in a round
  **after** the answer lands: an answer that arrived in the round you are answering in is not one you
  have read, so it is not one you can refuse. Backing an ask another seat raised cannot un-answer it,
  and your own answer stops counting the moment you ask that ask too — what you wrote is kept, it just
  stops keeping it from the human. Insisting still takes a second seat.
- Nothing stops the run but a **declared stop**: `blocking: true` on an ask you raise, or
  `blocking_for: ["H1"]` on a peer's. A majority of the council has to want that stop before
  the council waits, so claim it only where you genuinely cannot do the work without the
  answer. A taste call, a name, a colour is not it — ask, or decide it yourself. A stop is sticky:
  sending `blocking: false` back releases nothing, and there is no other way to give one up than
  `retract` the ask or let go of the item it was declared on.
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
asking the same question keeps its own candidates beside yours, and asking it twice is also
the second seat that gets the ask put to the human.

`blocking` is a separate claim from the ask: it says the council must *wait* for the answer,
and it takes a majority making the same claim to mean anything.
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


def peer_pass_task(listing: str, mode: str) -> str:
    return "\n".join(
        [
            "# Before the council ends: the asks that will not reach the human",
            "",
            "None of the asks below reaches the human as things stand, and the listing says why for each: no second",
            "seat backed it, a seat answered it from the workspace, or the council settled the item it asked about.",
            "For each one, do whichever is true:",
            "",
            "- you can settle it from the workspace → `answer_question`: its id and your answer;",
            "- you want the human's word on it too → `second`: its id. Only a peer can back an ask, so `second` is not yours",
            "  on one you raised; `blocking_for`: its id is the claim that the council should wait for the answer, and that",
            "  one you may declare on an ask of your own — it takes a majority declaring the same stop for it to bite;",
            "- you raised it, a seat answered it, and that answer misses the point → `insist`: its id — the only way an",
            "  answered ask comes back to the human;",
            "- you raised it and no longer need it → `retract`: its id.",
            "",
            "An ask mooted by a settled item comes back only on a declared stop at the bar: the closure is the council's",
            "answer about that item.",
            "",
            "This pass lets go and calls one answer not enough. Findings, sign-offs, new asks and `human_input` belong to",
            "a round and are read by nobody here; `positions` are read for one thing — if a peer's answer settles an item",
            "you are holding with `needs_human`, stance it as you now see it and that hold goes. You cannot",
            "open a hold, take another seat's, or keep one by stancing `needs_human` again. Say nothing",
            "about an ask you have nothing on.",
            "",
            ANSWER_SHAPE,
            schema_hint(mode, phase="round"),
            "---",
            "## The asks",
            "",
            listing,
        ]
    )


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


#: the passes where a seat owes a verdict-shaped reply, so a repair re-ask quotes the round schema back to it
SEAT_PHASES = ("round", "gap", "defense", "explain", "seconding")


def schema_hint(mode: str, *, phase: str) -> str:
    if phase == "phase0":
        return _mode_text(mode).PHASE0_SCHEMA
    if phase in SEAT_PHASES:
        return _mode_text(mode).ROUND_SCHEMA
    if phase == "merge":
        return merge_schema(mode)
    if phase == "asks":
        from . import chair as chair_text

        return chair_text.ASK_MERGE_SCHEMA
    if phase == "compare":
        from . import plan as plan_text

        return plan_text.COMPARISON_SCHEMA
    if phase == "direction":
        from . import plan as plan_text

        return plan_text.DIRECTION_SCHEMA
    from . import chair

    # an unknown phase is a bug here, not a reason to crash a member's ask
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


def ask_merge_task(listing: str) -> str:
    """The chair's ask fold: ids are `H1`-shaped in every mode, so no id prefix goes in."""
    from . import chair

    return "\n".join(
        [
            chair.ASK_MERGE,
            ANSWER_SHAPE,
            chair.ASK_MERGE_SCHEMA,
            "---",
            "## The open asks",
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


def direction_task(listing: str, goal: str | None = None) -> str:
    """Plan mode's one chair-worded ask: the directions themselves are the run's, so the reply is a sentence."""
    from . import plan as plan_text

    return "\n".join(
        [
            plan_text.DIRECTION,
            goal_line(goal),
            ANSWER_SHAPE,
            plan_text.DIRECTION_SCHEMA,
            "---",
            "## The directions still standing",
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
