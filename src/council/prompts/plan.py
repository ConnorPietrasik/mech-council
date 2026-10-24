"""Plan-mode prompt text.  Text is data, and it all lives here."""

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
  "human_input": [{"question": "…", "item": null, "blocking": false}]
}
```
"""

ROUND_TASK = """\
## Your task

DECIDE — stance every open decision point, with evidence.
CONVERGE — `backing` is mandatory and is exactly one of `AgentN`, `merged: <label>` (a
label you spelled out in a `revised_plan`), or `no_preference`. It is restated every
round; `abstain` is never legal here, and a `merged:` token you never wrote down is not
a plan.
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
  "human_input": [{"question": "…", "item": "T1", "blocking": false}],
  "signoff": false
}
```
"""

EXTRA_BRIEFING = ""
MERGE_TASK = (
    "Decision points raised since the last merge are listed below. Say which "
    "are the same decision asked twice — merging wording or scope is not "
    "merging."
)
