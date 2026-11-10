from __future__ import annotations

PHASE0_TASK = """\
## Your task

Investigate the project, then report what you would fix or improve according to the goal.

- An empty findings list is a valid answer. Do not invent work to justify the run.
- Every finding needs evidence you actually looked at.
- Severity: `blocker` (breaks now), `major` (real defect or real cost), `minor`
  (worth doing), `trivial` (a nit).
"""


def phase0_schema(human: str) -> str:
    stop = ', "blocking": false' if human == "blocking" else ""
    return (
        """\
```json
{
  "summary": "one paragraph: what you looked at and what you concluded",
  "findings": [
    {"claim": "one line, falsifiable", "detail": "what breaks and when",
     "evidence": "file:line / command output — what you actually saw",
     "severity": "blocker|major|minor|trivial"}
  ],
  "open_questions": ["a question only the human can answer"],
  "human_input": [{"question": "the decision, one line", "item": null"""
        + stop
        + """,
                   "context": "what you saw that leaves it open, and the cost of guessing",
                   "options": ["an answer you would accept", "another you would accept"]}],
  "notes": ["what the human should know and need not answer"]
}
```
"""
    )


def round_task(human: str) -> str:
    """Review's round task: it does not vary with the human's posture."""
    return """\
## Your task

For **every open item** in the docket: verify it against the code yourself, then stance
it — `agree`, `disagree` (with cited evidence), `needs_human`, or `abstain`. Leaving an
item out is a gap and you will be asked for it.

Then, if you have more: add new findings, answer another agent's question, ask one of
your own, raise a question for the human. Sign off (`"signoff": true`) only when you
would build what this council has agreed.
"""


def round_schema(human: str) -> str:
    stop = '", "blocking": true,' if human == "blocking" else '",'
    claim = ' "blocking_for": ["H4"],' if human == "blocking" else ""
    return (
        """\
```json
{
  "summary": "what you did this round",
  "positions": [{"item": "F1", "stance": "agree|disagree|needs_human|abstain",
                 "note": "why", "evidence": "file:line or command output"}],
  "new_items": [{"claim": "…", "detail": "…", "evidence": "…", "severity": "minor"}],
  "questions": [{"to": "Agent2", "question": "…"}],
  "answers": [{"to": "Agent3", "question": "Q2", "answer": "…"}],
  "human_input": [{"question": "the decision, one line", "item": "F1"""
        + stop
        + """
                   "context": "what you saw that leaves it open, and the cost of guessing",
                   "options": ["an answer you would accept", "another you would accept"]}],
  "second": ["H1"],"""
        + claim
        + """ "retract": ["H2"], "insist": ["H3"],
  "answer_question": [{"id": "H3", "answer": "what the workspace shows, so the human is not asked"}],
  "open_questions": ["…"],
  "notes": ["what the human should know and need not answer", {"note": "F1: …", "item": "F1"}],
  "retract_note": [{"id": "N1", "reason": "what was wrong"}],
  "signoff": false
}
```
"""
    )


# the `common.round_briefing` slot; review adds nothing.
def extra_briefing(human: str) -> str:
    return ""


MERGE_TASK = """\
Findings the chair still owes a fold on are listed under **Entries to group**: what the round raised, and what
its last plan did not fold. The findings still open on the docket are listed under
**Already on the docket**: an entry that reports one of those is that finding reported twice, and it
folds into it. Say which entries report the **same defect**.

- Never fold two entries from **Already on the docket** together: those were grouped once already.
- Forbid merging partial overlaps: two findings that share a symptom but name different causes
  are two findings.
- Each finding prints its claim, then its `what breaks:` line and its `evidence:` line. Read
  all three — claims that read alike often separate on the cause the middle line names.
"""
