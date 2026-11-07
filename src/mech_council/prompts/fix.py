"""The fix mode's prose: the builder's plan and work passes for one finding, the verifier's
scope pass, and the run's final check. The builder never commits: the run does, per finding."""

from __future__ import annotations

from .implement import messages_section, seat_roster, work_schema

FIX_WORKSPACE = """\
# Your workspace

Your working directory is the project, and it is the real one: this run is in place and the
writes are the deliverable. You hold the baton for this pass — you are the only seat editing the
tree, and the review crew only reviews. Touch only what this finding touches, and leave the rest
of the tree as you found it. The run commits each verified fix as its own commit, and a fix that
fails verification is rolled back to the tree it started from: do not commit, and assume anything
half-finished in the tree is lost when a fix is rolled back. `$TMPDIR` is a directory of your own
outside the project: put anything half-finished there.
"""

SAY_TO = """\
`saying to a peer` — `say_to: [{"to": "Agent2", "text": "…"}]` is a fire-and-forget message
to a seat by its ID from the roster: it lands at that seat's next prompt, printed under
"Messages for you". Use it for what a peer should know and nothing more — it settles nothing
and is not an ask.
"""


def plan_schema() -> str:
    return """\
```json
{"stages": [{"state": "what is true when the fix is done",
             "gate": "the check that proves it — a command, or a statement of what must be true; the verifier re-runs it",
             "scope": "the files the fix touches",
             "exceptions": ["where the obvious fix is wrong"]}]}
```
Exactly one stage: this one finding's fix, nothing else.
"""


def stage_block(index: int, total: int, stage: dict) -> str:
    lines = [f"## The plan for finding {index} of {total}: {stage.get('state') or '(unnamed)'}"]
    if stage.get("gate"):
        lines.append(f"- gate: {stage['gate']} — when this is true the fix is done")
    if stage.get("scope"):
        lines.append(f"- scope: {stage['scope']}")
    for exception in stage.get("exceptions") or []:
        lines.append(f"- exception: {exception}")
    return "\n".join(lines)


def step_block(index: int, total: int, step: dict) -> str:
    lines = [f"## Step {index} of {total}: {step.get('state') or '(unnamed)'}"]
    if step.get("gate"):
        lines.append(f"- gate: {step['gate']} — when this is true the step is done")
    if step.get("scope"):
        lines.append(f"- scope: {step['scope']}")
    for exception in step.get("exceptions") or []:
        lines.append(f"- exception: {exception}")
    return "\n".join(lines)


def finding_block(finding: dict) -> str:
    lines = [f"## The finding — {finding['id']} ({finding['severity']})"]
    lines.append(f"claim: {finding['claim']}")
    if finding.get("detail"):
        lines.append(f"what breaks: {finding['detail']}")
    if finding.get("evidence"):
        lines.append(f"evidence: {finding['evidence']}")
    return "\n".join(lines)


def plan_task(
    *,
    finding: dict,
    index: int,
    total: int,
    fixed_so_far: list[str] | None = None,
    seats: list[tuple[str, str]] | None = None,
    messages: list[dict] | None = None,
    human: str = "blocking",
) -> str:
    """The builder's plan pass: one finding, one stage, a gate the verifier can re-run."""
    from . import common

    parts = [
        "# Plan the fix — one finding",
        "",
        "You are the builder: the seat holding the baton. Plan the fix for this one finding — the final state,",
        "the gate that proves it, the files it touches. You will implement it on the next pass.",
        "",
        finding_block(finding),
        f"Finding {index} of {total} on the run's list; the fixes already committed are the tree you start from.",
    ]
    if fixed_so_far:
        parts += ["", "## The fixes already committed"]
        parts += [f"- {line}" for line in fixed_so_far]
    if seats:
        parts += ["", seat_roster(seats)]
    if messages:
        parts += ["", messages_section(messages)]
    parts += [
        "",
        "## Do this",
        "",
        "1. Plan the smallest fix that closes this finding — no drive-by changes, no refactor the finding does not ask for.",
        "2. Name a gate the verifier can re-run: a command, or a statement of what must be true.",
        "3. Name the scope: the files the fix touches. A path that is not in the scope is out of bounds.",
        "4. Reply with the plan shape — one stage.",
        "",
        common.escalation(human),
        SAY_TO,
        common.ANSWER_SHAPE,
        plan_schema(),
        FIX_WORKSPACE,
    ]
    return "\n".join(parts)


def fix_work_task(
    *,
    finding: dict,
    plan: dict,
    index: int,
    total: int,
    messages: list[dict] | None = None,
    seats: list[tuple[str, str]] | None = None,
    human: str = "blocking",
) -> str:
    """The builder's work pass: implement the planned fix, satisfy the gate, report."""
    from . import common

    parts = [
        "# Implement the fix — one finding",
        "",
        "You are the builder: the seat holding the baton. Implement the planned fix, satisfy its gate,",
        "and report what you did.",
        "",
        finding_block(finding),
        "",
        stage_block(index, total, plan),
    ]
    if seats:
        parts += ["", seat_roster(seats)]
    if messages:
        parts += ["", messages_section(messages)]
    steps = ["1. Implement the fix until the plan's final state is reached."]
    if plan.get("gate"):
        steps.append(
            "2. Satisfy the gate — run it, or verify its condition — and let it pass; a gate you cannot make pass is an `open_issue`, "
            "not a lie. The verifier re-verifies it independently."
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
        FIX_WORKSPACE,
    ]
    return "\n".join(parts)


def fix_fix_task(
    *,
    finding: dict,
    plan: dict,
    index: int,
    total: int,
    pass_number: int,
    items: list[dict],
    gate_output: str = "",
    revised: list[dict] | None = None,
    messages: list[dict] | None = None,
    seats: list[tuple[str, str]] | None = None,
    human: str = "blocking",
) -> str:
    """A repair-ladder fix pass: the verifier's standing findings, addressed one by one. With a
    `revised` breakdown, a replan's steps land in full in this pass; the finding's own gate still
    closes it — a step's gate is the way there, not the finish line."""
    from . import common

    if revised:
        parts = [
            "# Fix pass — the revised breakdown",
            "",
            f"You implemented the fix for finding {index} of {total}; it was not converging, so you replanned it into a",
            f"smaller breakdown. This is fix pass {pass_number}. Implement the whole breakdown, address the items, and",
            "satisfy the finding's gate again: the steps' gates are the way there, the finding's gate is the finish line.",
            "",
            finding_block(finding),
            "",
            stage_block(index, total, plan),
            "",
            "## The revised breakdown",
            "",
        ]
        parts += [step_block(i, len(revised), step) for i, step in enumerate(revised, 1)]
    else:
        parts = [
            "# Fix pass — the finding is not done",
            "",
            f"You implemented the fix for finding {index} of {total}; the verifier found what is still wrong. This is fix pass "
            f"{pass_number}. Address the items, satisfy the gate again, and report.",
            "",
            finding_block(finding),
            "",
            stage_block(index, total, plan),
        ]
    if gate_output:
        parts += ["", "The last gate verdict:", "```", gate_output, "```"]
    parts += ["", "## The findings still standing"]
    for item in items:
        line = f"- {item['id']}: {item['claim']}"
        if item.get("evidence"):
            line += f" — {item['evidence']}"
        parts.append(line)
    if seats:
        parts += ["", seat_roster(seats)]
    if messages:
        parts += ["", messages_section(messages)]
    steps = []
    if revised:
        steps.append("1. Implement the revised breakdown — every step, in order — until the plan's final state is reached.")
    steps.append(f"{len(steps) + 1}. Address every finding — or show, with cited evidence, that it is wrong.")
    if plan.get("gate"):
        gate = "the finding's gate" if revised else "the gate"
        steps.append(f"{len(steps) + 1}. Satisfy {gate} again — re-run it, or re-verify its condition; it must pass.")
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
        FIX_WORKSPACE,
    ]
    return "\n".join(parts)


def fix_replan_task(
    *,
    finding: dict,
    plan: dict,
    index: int,
    total: int,
    pass_number: int,
    items: list[dict],
    gate_output: str = "",
    messages: list[dict] | None = None,
    seats: list[tuple[str, str]] | None = None,
) -> str:
    """A repair-ladder replan: a revised, smaller breakdown of the stuck finding's fix."""
    from . import common

    parts = [
        "# Replan — the fix is stuck",
        "",
        f"Finding {index} of {total} is not converging: {pass_number} replan. You will implement nothing now — "
        "report a revised, smaller breakdown of this fix that the next pass can land.",
        "",
        finding_block(finding),
        "",
        stage_block(index, total, plan),
    ]
    if items:
        parts += ["", "## What the verifier still finds"]
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
        "A revised breakdown of this same fix: fewer, smaller steps, each with a gate it can actually pass.",
        "Say in `note` what you changed and why it is smaller.",
        "",
        common.ANSWER_SHAPE,
        """\
```json
{"note": "what you changed and why it is smaller",
 "stages": [{"state": "what is true when this step is done",
             "gate": "the check that proves it", "scope": "what it touches",
             "exceptions": ["where the obvious fix is wrong"]}]}
```
""",
        FIX_WORKSPACE,
    ]
    return "\n".join(parts)


def chair_gate_task(*, gate: str, state: str, project: str) -> str:
    """The run's chair verifying a finding's gate: the crew's verdicts did not read, or there is no crew."""
    from . import common

    return "\n".join(
        [
            "# Verify the gate — one finding's fix",
            "",
            f"The builder says the fix for one finding is done, in `{project}`. Verify this gate yourself — run it, or verify its",
            "condition by other means — and report the verdict. Do not edit anything: you only check.",
            "",
            f"Gate: {gate}",
            f"The fix is done when: {state}",
            "",
            common.ANSWER_SHAPE,
            '```json\n{"ok": true, "ran": "what you ran or checked", "evidence": "its last lines"}\n```\n',
        ]
    )


def final_check_task(project: str) -> str:
    """The whole-project check over the finished tree: the fix mode's final-stage rule."""
    from . import common

    return "\n".join(
        [
            "# The final check — the whole project",
            "",
            f"The fix run over `{project}` is done. Run the project's full check — its test suite, linters and",
            "type-checker — over the finished tree, and report the verdict. Do not edit anything: you only check.",
            "",
            common.ANSWER_SHAPE,
            '```json\n{"ok": true, "ran": "what you ran", "evidence": "its last lines"}\n```\n',
        ]
    )
