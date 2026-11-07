from __future__ import annotations

PHASE0_TASK = """\
## Your task

Investigate the project, then write the plan you would execute for the goal above.

A plan is a sequence of **gated stages**: the final state each stage leaves, the
validation gate that proves it, and the exceptions — the traps where the obvious
mechanical change is wrong (every one that exists, only those). Add cross-stage
constraints, risks, rejected alternatives, and assumptions.

No exact diffs, no line numbers, no predicted output. Write prescriptively ("do X"), not
descriptively ("the code does X"). Length tracks the task.
"""


def phase0_schema(human: str) -> str:
    stop = ', "blocking": false' if human == "blocking" else ""
    return (
        """\
```json
{
  "summary": "one paragraph",
  "approach": {
    "claim": "the plan in one line",
    "stages": [{"state": "what is true when this stage is done",
                "gate": "the check that proves it", "scope": "what it touches",
                "exceptions": ["where the obvious change is wrong"]}],
    "constraints": ["…"], "risks": ["…"], "assumptions": ["…"],
    "rejected_alternatives": ["…"]
  },
  "open_questions": ["…"],
  "human_input": [{"question": "the decision, one line", "item": null"""
        + stop
        + """,
                   "context": "what you saw that leaves it open, and the cost of guessing",
                   "options": ["an answer you would accept", "another you would accept"]}],
  "notes": ["what the human should know and need not answer", {"note": "T1: …", "item": "T1"}]
}
```
"""
    )


def round_task(human: str) -> str:
    split = {
        "blocking": (
            "a plan. Two directions that hold through a round nothing else moved in stop the run and go to\n"
            "the human to choose, so a backing you will not move is a question you are asking."
        ),
        "non_blocking": (
            "a plan. Two directions that hold through a round nothing else moved in go to the human to\n"
            "choose while the run keeps working, and the run ends on the split if the human never chooses,\n"
            "so a backing you will not move is a question you are asking."
        ),
        "none": (
            "a plan. Two directions that hold through a round nothing else moved in are the council's split:\n"
            "no one is there to choose, and the run ends on the split, so a backing you will not move is a\n"
            "question you are recording."
        ),
    }[human]
    escalate = {
        "blocking": "ESCALATE — `human_input` for what only the human can settle.",
        "non_blocking": "ESCALATE — `human_input` for what the council cannot settle among itself.",
        "none": "ESCALATE — `human_input` for what the council cannot settle among itself: it is recorded, not answered.",
    }[human]
    return f"""\
## Your task

DECIDE — stance every open decision point, with evidence.
CONVERGE — `backing` is mandatory and is exactly one of `AgentN`, `merged: <label>` (a
label you spelled out in a `revised_plan`), or `no_preference`. It is restated every
round; `abstain` is never a legal backing, and a `merged:` token you never wrote down is not
{split}
IMPROVE — new decision points, a revised plan of your own, questions and answers.
{escalate}

Sign off (`"signoff": true`) only once you have named a backing.
"""


def round_schema(human: str) -> str:
    stop = '", "blocking": false,' if human == "blocking" else '",'
    claim = ' "blocking_for": ["H4"],' if human == "blocking" else ""
    return (
        """\
```json
{
  "summary": "what you did this round",
  "positions": [{"item": "T1", "stance": "agree|disagree|needs_human|abstain",
                 "note": "why", "evidence": "file:line or command output"}],
  "new_items": [{"claim": "the decision", "detail": "the answers it admits",
                 "evidence": "…"}],
  "backing": "Agent2",
  "revised_plan": {"name": "my-label", "text": "the revised plan, in a few lines",
                   "stages": [{"state": "…", "gate": "…",
                               "scope": "…", "exceptions": []}]},
  "questions": [{"to": "Agent2", "question": "…"}],
  "answers": [{"to": "Agent3", "question": "Q1", "answer": "…"}],
  "human_input": [{"question": "the decision, one line", "item": "T1"""
        + stop
        + """
                   "context": "what you saw that leaves it open, and the cost of guessing",
                   "options": ["an answer you would accept", "another you would accept"]}],
  "second": ["H1"],"""
        + claim
        + """ "retract": ["H2"], "insist": ["H3"],
  "answer_question": [{"id": "H3", "answer": "what the workspace shows, so the human is not asked"}],
  "notes": ["what the human should know and need not answer", {"note": "T1: …", "item": "T1"}],
  "retract_note": [{"id": "N1", "reason": "what was wrong"}],
  "signoff": false
}
```
"""
    )


def extra_briefing(human: str) -> str:
    """The `common.round_briefing` slot; plan-specific prose."""
    stop = ' An unanswered\n**effective-blocking** question is a "do NOT guess": name it and stop.' if human == "blocking" else ""
    return f"""\
# Unanswered questions

An unanswered question the council judged **deferred** goes into the plan's assumptions
with the guess it stands for — never decided silently.{stop}
"""


MERGE_TASK = """\
New decision points are listed under **Entries to group**. The decision points still open on the docket are
listed under **Already on the docket**: a new entry asking one of those asks the same decision twice, and it
folds into it. Say which entries ask the **same decision twice**; merging wording or scope is not merging,
because it is the decision that matches, not the sentence stating it.

- Never fold two entries from **Already on the docket** together: those were grouped once already.
- Two entries naming two forks that merely share a word are two decision points.
- Each entry prints the decision, then its `the answers it admits:` line and its `evidence:`
  line. Read all three — decisions that read alike often separate on an answer one admits and
  the other does not.
"""


def compare(human: str) -> str:
    split = {
        "blocking": (
            "- A conflict the seats cannot settle becomes their\n  escalation, and the council's own split goes to the human on its own."
        ),
        "non_blocking": (
            "- A conflict the seats cannot settle becomes their\n"
            "  escalation, and the council's own split goes to the human on its own — the run"
            " keeps working until then."
        ),
        "none": (
            "- A conflict the seats cannot settle becomes their\n"
            "  escalation, and the council's own split is recorded on its own: no one is"
            " there to choose, and the run ends on it."
        ),
    }[human]
    return f"""\
# Compare the plans, do not write one

You are the chair of a planning council. You compare the planners' independent plans and
build the decision map; you never plan anything yourself.

- `agreed`: points the plans share, with the agents sharing them.
- `conflicts`: one entry per **same decision answered differently**, with each agent's
  position. Do not invent conflicts between plans that merely differ in wording or scope;
  a conflict is the same decision answered differently.
- `uncovered`: what the council's goal needs and no plan covers.
- Judge plans by consequence: mechanism is fair game, naming and phrasing are not worth a
  round.
{split}
- Echo the agent IDs exactly; make none up.
"""


COMPARISON_TASK = "The plans are below, one per seat, in their own words. Map them.\n"

COMPARISON_SCHEMA = """\
```json
{
  "agreed": [{"point": "…", "agents": ["Agent1", "Agent2"]}],
  "conflicts": [{"claim": "the decision in one line",
                 "positions": [{"agent": "Agent1", "claim": "its answer"},
                               {"agent": "Agent2", "claim": "its answer"}]}],
  "uncovered": ["…"]
}
```
"""


# The chair-worded ask: the run raises it; the reply is a sentence.
def direction(human: str) -> str:
    pick = {
        "blocking": "and the human is going to pick one. You never plan anything yourself and you never pick.",
        "non_blocking": (
            "and the human is going to pick one while the run keeps working; if they never do, the"
            " run ends on the split. You never plan anything yourself and you never pick."
        ),
        "none": (
            "and no one is there to pick: the split is recorded for the report, and the run ends on"
            " it. You never plan anything yourself and you never pick."
        ),
    }[human]
    menu = (
        "the human reads the numbered menu, never this listing."
        if human != "none"
        else "the numbered menu is recorded for the report, never this listing."
    )
    return f"""\
# Say what is at stake between these directions

The council will not converge: two or more plans are still standing after a full round of trying,
{pick}

- `question` — the choice in one line, in the terms the plans themselves use.
- `context` — what is at stake between these directions and what guessing wrong costs, in two
  lines.
- The directions below are listed by the ID each seat answers with; the run numbers them for the
  human and prints that seat's name over the ID. Write both lines as what is being chosen and not
  who is being chosen between — {menu} Quote the
  plans, invent nothing, and name no direction that is not listed.
"""


def direction_schema(human: str) -> str:
    who = "the human is making" if human != "none" else "the report records"
    return f"""\
```json
{{"question": "the choice {who}, one line",
 "context": "what is at stake between these directions, and the cost of guessing wrong"}}
```
"""
