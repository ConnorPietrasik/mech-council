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

PHASE0_SCHEMA = """\
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
  "human_input": [{"question": "the decision, one line", "item": null, "blocking": false,
                   "context": "what you saw that leaves it open, and the cost of guessing",
                   "options": ["an answer you would accept", "another you would accept"]}]
}
```
"""

ROUND_TASK = """\
## Your task

DECIDE — stance every open decision point, with evidence.
CONVERGE — `backing` is mandatory and is exactly one of `AgentN`, `merged: <label>` (a
label you spelled out in a `revised_plan`), or `no_preference`. It is restated every
round; `abstain` is never a legal backing, and a `merged:` token you never wrote down is not
a plan. Two directions that hold through a round nothing else moved in stop the run and go to
the human to choose, so a backing you will not move is a question you are asking.
IMPROVE — new decision points, a revised plan of your own, questions and answers.
ESCALATE — `human_input` for what only the human can settle.

Sign off (`"signoff": true`) only once you have named a backing.
"""

ROUND_SCHEMA = """\
```json
{
  "summary": "what you did this round",
  "positions": [{"item": "T1", "stance": "agree|disagree|needs_human|abstain",
                 "note": "why", "evidence": "file:line or command output"}],
  "new_items": [{"claim": "the decision", "detail": "the answers it admits",
                 "evidence": "…"}],
  "backing": "Agent2",
  "revised_plan": {"name": "my-label", "stages": [{"state": "…", "gate": "…",
                                                  "scope": "…", "exceptions": []}]},
  "questions": [{"to": "Agent2", "question": "…"}],
  "answers": [{"to": "Agent3", "question": "Q1", "answer": "…"}],
  "human_input": [{"question": "the decision, one line", "item": "T1", "blocking": false,
                   "context": "what you saw that leaves it open, and the cost of guessing",
                   "options": ["an answer you would accept", "another you would accept"]}],
  "signoff": false
}
```
"""

# `common.round_briefing`'s per-mode slot: prose only plan's seats read.
EXTRA_BRIEFING = """\
# Unanswered questions

An unanswered question the council judged **deferred** goes into the plan's assumptions
with the guess it stands for — never decided silently. An unanswered
**effective-blocking** question is a "do NOT guess": name it and stop.
"""
MERGE_TASK = """\
Decision points raised since the last merge are listed below. Say which ask the **same
decision twice**; merging wording or scope is not merging, because it is the decision that
matches, not the sentence stating it.

- Two entries naming two forks that merely share a word are two decision points.
- Each entry prints the decision, then its `the answers it admits:` line and its `evidence:`
  line. Read all three — decisions that read alike often separate on an answer one admits and
  the other does not.
"""

COMPARE = """\
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
- You ask the human nothing from this pass. A conflict the seats cannot settle becomes their
  escalation, and the council's own split goes to the human on its own.
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

# The one ask the chair words: the run raises it, and asks only for the sentence.
DIRECTION = """\
# Say what is at stake between these directions

The council will not converge: two or more plans are still standing after a full round of trying,
and the human is going to pick one. You never plan anything yourself and you never pick.

- `question` — the choice in one line, in the terms the plans themselves use.
- `context` — what is at stake between these directions and what guessing wrong costs, in two
  lines.
- The directions below are listed by the ID each seat answers with; the run numbers them for the
  human and prints that seat's name over the ID. Write both lines as what is being chosen and not
  who is being chosen between — the human reads the numbered menu, never this listing. Quote the
  plans, invent nothing, and name no direction that is not listed.
"""

DIRECTION_SCHEMA = """\
```json
{"question": "the choice the human is making, one line",
 "context": "what is at stake between these directions, and the cost of guessing wrong"}
```
"""
