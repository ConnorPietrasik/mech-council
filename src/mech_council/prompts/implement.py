"""The implement mode's prose: the builder's passes and the run's chair structuring.

The implement council's own seats never deliberate — they build — so the mode's verdict-shaped
slots hand back the builder's work shape; the borrowed plan and review crews keep their own prose.
"""

from __future__ import annotations

PHASE0_TASK = ""


def phase0_schema(human: str = "blocking") -> str:
    return work_schema()


def round_task(human: str = "blocking") -> str:
    return ""


def round_schema(human: str = "blocking") -> str:
    return work_schema()


def extra_briefing(human: str = "blocking") -> str:
    return ""


MERGE_TASK = ""

BUILDER_WORKSPACE = """\
# Your workspace

Your working directory is the project, and it is the real one: this run is in place and the
writes are the deliverable. You hold the baton for this pass — right now you are the only seat
editing the tree, and the review crew only reviews, never edits. Touch only
what your task touches, and leave the rest of the tree as you found it. The run never commits:
the human reviews the whole diff when it is done. `$TMPDIR` is a directory of your own outside
the project: put anything half-finished there.
"""

PROSE_WORKSPACE = """\
# Your workspace

Your working directory is the project, and it is the real one. You hold the baton for this
pass — you are the only seat briefed to edit the tree, and the only files you touch are the
user-visible surfaces listed below. Do not change behaviour; rewrite the text, keep the
code.
"""

SAY_TO = """\
`saying to a peer` — `say_to: [{"to": "Agent2", "text": "…"}]` is a fire-and-forget message
to a seat by its ID from the roster: it lands at that seat's next prompt, printed under
"Messages for you". Use it for what a peer should know and nothing more — it settles nothing
and is not an ask.
"""

#: appended to the plan phase's goal: the final stage must prove the whole project still stands
PLAN_GOAL_TAIL = (
    "Final-stage rule: the plan's final stage must gate on the project's full check — its "
    "test suite, linters and type-checker — so a passing run proves the whole project still stands."
)


def work_schema() -> str:
    return """\
```json
{
  "summary": "what you did, in a few lines",
  "gate_command": "what you ran (or checked) to satisfy the gate",
  "gate_output": "its last lines, as it printed them",
  "changed": ["the paths you created or modified"],
  "user_text": ["the paths of the files holding user-visible text you touched — help strings, docs, error messages; paths, not the text"],
  "open_issues": ["what is left open, if anything"],
  "human_input": [{"question": "the decision, one line", "item": null, "blocking": false,
                   "context": "what you saw that leaves it open, and the cost of guessing",
                   "options": ["an answer you would accept", "another you would accept"]}],
  "notes": ["what the human should know and need not answer", {"note": "C1: …", "item": "C1"}],
  "say_to": [{"to": "Agent2", "text": "…"}],
  "addressed": [{"id": "C1", "how": "what you did about it"}]
}
```
`addressed` is for fix passes only: one line per item you addressed. A `notes` entry about a docket item opens with the
item's id and carries it as `item`, so the report files the note under that item.
"""


def replan_schema() -> str:
    return """\
```json
{"note": "what you changed and why it is smaller",
 "stages": [{"state": "what is true when this stage is done",
             "gate": "the check that proves it", "scope": "what it touches",
             "exceptions": ["where the obvious change is wrong"]}]}
```
"""


def structure_schema() -> str:
    return """\
```json
{"stages": [{"state": "what is true when this stage is done",
             "gate": "the check that proves it — a command, or a statement of what must be true; the review crew runs and verifies it",
             "scope": "what it touches",
             "exceptions": ["where the obvious change is wrong"],
             "kind": "code|prose"}]}
```
`kind` is `prose` only when the stage's entire artifact is user-visible text — a migration
guide, release notes; a stage that also touches code stays `code`.
"""


def stage_block(index: int, total: int, stage: dict) -> str:
    lines = [f"## Stage {index} of {total}: {stage.get('state') or '(unnamed)'}"]
    if stage.get("gate"):
        lines.append(f"- gate: {stage['gate']} — when this is true the stage is done")
    if stage.get("scope"):
        lines.append(f"- scope: {stage['scope']}")
    for exception in stage.get("exceptions") or []:
        lines.append(f"- exception: {exception}")
    return "\n".join(lines)


def seat_roster(seats: list[tuple[str, str]]) -> str:
    """The run's seats by ID, each with a note: how a seat addresses a peer in `say_to`."""
    lines = ["## The seats in this run", ""]
    lines += [f"- {seat_id} ({note})" if note else f"- {seat_id}" for seat_id, note in seats]
    return "\n".join(lines)


def messages_section(messages: list[dict]) -> str:
    """The `say_to` lines queued for this seat, printed at its next prompt."""
    lines = ["## Messages for you", ""]
    lines += [f"- from {message['from_']}: {message['text']}" for message in messages]
    return "\n".join(lines)


def work_task(
    *,
    goal: str,
    stage: dict,
    index: int,
    total: int,
    plan_text: str = "",
    assumptions: list[str] | None = None,
    messages: list[dict] | None = None,
    seats: list[tuple[str, str]] | None = None,
    human: str = "blocking",
) -> str:
    """The builder's work ask: implement the stage, satisfy its gate, report."""
    from . import common

    parts = [
        "# Your work — one stage of the adopted plan",
        "",
        "You are the builder: the seat holding the baton. Implement the stage's final state,",
        "satisfy its gate, and report what you did.",
        "",
        f"Goal: {goal}",
    ]
    if plan_text:
        parts += ["", "## The plan you are executing", "", plan_text]
    if assumptions:
        parts += ["", "## The plan's open decision points", ""]
        parts += [f"- {a}" for a in assumptions]
        parts.append("Where one bites you, surface it in `human_input`.")
    parts += ["", stage_block(index, total, stage)]
    if seats:
        parts += ["", seat_roster(seats)]
    if messages:
        parts += ["", messages_section(messages)]
    steps = [f"1. Implement stage {index} until its final state is reached."]
    if stage.get("gate"):
        steps.append(
            "2. Satisfy its gate — run it, or verify its condition — and let it pass; a gate you cannot make pass is an `open_issue`, "
            "not a lie. The review crew re-verifies it independently."
        )
    steps.append(f"{len(steps) + 1}. Report with the work shape.")
    parts += [
        "",
        "## Do this",
        "",
        *steps,
        "",
        common.escalation(human),
        SAY_TO,
        common.ANSWER_SHAPE,
        work_schema(),
        BUILDER_WORKSPACE,
    ]
    return "\n".join(parts)


def fix_task(
    *,
    goal: str,
    stage: dict,
    index: int,
    total: int,
    pass_number: int,
    items: list[dict],
    gate_output: str = "",
    messages: list[dict] | None = None,
    seats: list[tuple[str, str]] | None = None,
    human: str = "blocking",
) -> str:
    """A repair-ladder fix pass: the standing findings, addressed one by one."""
    from . import common

    parts = [
        "# Fix pass — stage is not done",
        "",
        f"You built stage {index} of {total}; the review crew found what is still wrong. This is fix pass "
        f"{pass_number}. Address the items, satisfy the gate again, and report.",
        "",
        f"Goal: {goal}",
        "",
        stage_block(index, total, stage),
    ]
    if gate_output:
        parts += ["", "The last gate verdict:", "```", gate_output, "```"]
    parts += ["", "## The findings"]
    for item in items:
        line = f"- {item['id']}: {item['claim']}"
        if item.get("evidence"):
            line += f" — {item['evidence']}"
        parts.append(line)
    if seats:
        parts += ["", seat_roster(seats)]
    if messages:
        parts += ["", messages_section(messages)]
    steps = ["1. Address every finding — or show, with cited evidence, that it is wrong."]
    if stage.get("gate"):
        steps.append("2. Satisfy the stage's gate again — re-run it, or re-verify its condition; it must pass.")
    steps.append(f"{len(steps) + 1}. Report with the work shape: `addressed` names each item and what you did about it.")
    parts += [
        "",
        "## Do this",
        "",
        *steps,
        "",
        common.escalation(human),
        SAY_TO,
        common.ANSWER_SHAPE,
        work_schema(),
        BUILDER_WORKSPACE,
    ]
    return "\n".join(parts)


def replan_task(
    *,
    goal: str,
    stage: dict,
    index: int,
    total: int,
    pass_number: int,
    items: list[dict],
    gate_output: str = "",
    messages: list[dict] | None = None,
    seats: list[tuple[str, str]] | None = None,
) -> str:
    """A repair-ladder replan: a revised, smaller breakdown of the stuck stage."""
    from . import common

    parts = [
        "# Replan — the stage is stuck",
        "",
        f"Stage {index} of {total} is not converging: {pass_number} replan. You will implement nothing now — "
        "report a revised, smaller breakdown of this stage that the next pass can land.",
        "",
        f"Goal: {goal}",
        "",
        stage_block(index, total, stage),
    ]
    if items:
        parts += ["", "## What the review crew still finds"]
        for item in items:
            line = f"- {item['id']}: {item['claim']}"
            if item.get("evidence"):
                line += f" — {item['evidence']}"
            parts.append(line)
    if gate_output:
        parts += ["", "The last gate verdict:", "```", gate_output, "```"]
    if seats:
        parts += ["", seat_roster(seats)]
    if messages:
        parts += ["", messages_section(messages)]
    parts += [
        "",
        "## Report",
        "",
        "A revised breakdown of this same stage: fewer, smaller stages, each with a gate it can actually pass.",
        "Say in `note` what you changed and why it is smaller.",
        "",
        common.ANSWER_SHAPE,
        replan_schema(),
        BUILDER_WORKSPACE,
    ]
    return "\n".join(parts)


def prose_task(
    *,
    goal: str,
    stage: dict,
    index: int,
    total: int,
    surfaces: list[tuple[str, str]],
    gate_output: str = "",
    messages: list[dict] | None = None,
    seats: list[tuple[str, str]] | None = None,
) -> str:
    """The prose pass: the builder's user-visible text, rewritten by the prose seat."""
    from . import common

    parts = [
        "# The prose pass — the stage's user-visible text",
        "",
        f"The builder's last pass on stage {index} of {total} named the user-visible surfaces it touched. "
        "You are the prose seat: rewrite those surfaces per your writing guidelines.",
        "",
        f"Goal: {goal}",
        "",
        "## The surfaces, as they stand",
    ]
    for path, content in surfaces:
        parts += ["", f"### {path}", "```", content, "```"]
    if gate_output:
        parts += ["", f"Gate: {stage.get('gate') or '(no gate named)'}", "Its last output:", "```", gate_output, "```"]
    if seats:
        parts += ["", seat_roster(seats)]
    if messages:
        parts += ["", messages_section(messages)]
    steps = ["1. Rewrite only the user-visible text of the listed surfaces."]
    if stage.get("gate"):
        steps.append("2. Satisfy the stage's gate again — re-run it, or re-verify its condition; it must still pass.")
    steps.append(f"{len(steps) + 1}. Report with the work shape: `changed` names the surfaces you rewrote.")
    parts += [
        "",
        "## Do this",
        "",
        *steps,
        "",
        SAY_TO,
        common.ANSWER_SHAPE,
        work_schema(),
        PROSE_WORKSPACE,
    ]
    return "\n".join(parts)


def pick_task(goal: str, directions: list[tuple[str, str]]) -> str:
    """The run's chair, picking the direction when the planners' rules did not adopt one."""
    from . import common

    listed = [f"- {token}: {excerpt}" for token, excerpt in directions]
    return "\n".join(
        [
            "# Pick the direction",
            "",
            "You are the chair of an implement run. The planners did not converge, the human chose none, and no",
            "direction holds a majority. Pick the one the run will build — exactly one of the seats' directions,",
            "named by its token. You do not invent a plan.",
            "",
            f"Goal: {goal}",
            "",
            "## The directions",
            "",
            *listed,
            "",
            common.ANSWER_SHAPE,
            '```json\n{"backing": "Agent1"}\n```\n',
        ]
    )


def chair_gate_task(*, gate: str, state: str, project: str) -> str:
    """The run's chair, verifying a stage's gate when the review crew's verdicts did not read."""
    from . import common

    return "\n".join(
        [
            "# Verify the stage's gate",
            "",
            f"You are the chair of an implement run over `{project}`. The stage's final state is: {state}",
            f"Its gate is: {gate}",
            "",
            "Run the gate — or verify its condition by other means — yourself, in the project, and report the",
            "verdict. Do not edit anything: you only check.",
            "",
            common.ANSWER_SHAPE,
            '```json\n{"ok": true, "ran": "what you ran or checked", "evidence": "its last lines"}\n```\n',
        ]
    )


def structure_task(document: str, goal: str) -> str:
    """The run's chair, structuring a `--plan` document into the gated stages the run builds."""
    from . import common

    return "\n".join(
        [
            "# Structure the plan",
            "",
            "You are the chair of an implement run. The human gave a plan document instead of convening the",
            "planners; structure it into the gated stages the run builds, in the document's order. Keep the",
            "document's scope. A gate is the check that proves the stage's final state — a command, or a",
            "statement of what must be true; the review crew runs and verifies it. Give a stage that lacks",
            "one a gate, and say so where you had to invent it.",
            "",
            f"Goal: {goal}",
            "",
            common.ANSWER_SHAPE,
            structure_schema(),
            "---",
            "## The plan document",
            "",
            document,
        ]
    )


def classify_task(goal: str, stages: list[dict]) -> str:
    """The run's chair, classifying the adopted plan's stages as code or prose."""
    from . import common

    listed = []
    for index, stage in enumerate(stages, 1):
        line = f"{index}. {stage.get('state') or '(unnamed)'}"
        if stage.get("gate"):
            line += f" (gate: {stage['gate']})"
        if stage.get("scope"):
            line += f" — {stage['scope']}"
        listed.append(line)
    return "\n".join(
        [
            "# Classify the stages",
            "",
            "You are the chair of an implement run. The council adopted the plan below; classify each stage's",
            "`kind`, in order: `prose` when the stage's entire artifact is user-visible text — a migration",
            "guide, release notes — `code` otherwise. A stage that also touches user-visible text stays",
            "`code`: its text is a prose pass, not the stage's artifact.",
            "",
            f"Goal: {goal}",
            "",
            "## The adopted stages",
            "",
            *listed,
            "",
            "Reply with the stages in the same order, each carrying its `kind`.",
            "",
            common.ANSWER_SHAPE,
            structure_schema(),
        ]
    )
