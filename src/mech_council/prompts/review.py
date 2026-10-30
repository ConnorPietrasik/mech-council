from __future__ import annotations

PHASE0_TASK = """\
## Your task

Investigate the project with whatever tools you have — read the code, run the tests,
grep, look at git history. Then report what you would fix or improve.

- An empty findings list is a valid answer. Do not invent work to justify the run.
- Every finding needs evidence you actually looked at.
- Severity: `blocker` (breaks now), `major` (real defect or real cost), `minor`
  (worth doing), `trivial` (a nit).
"""

PHASE0_SCHEMA = """\
```json
{
  "summary": "one paragraph: what you looked at and what you concluded",
  "findings": [
    {"claim": "one line, falsifiable", "detail": "what breaks and when",
     "evidence": "file:line / command output — what you actually saw",
     "severity": "blocker|major|minor|trivial"}
  ],
  "open_questions": ["a question only the human can answer"],
  "human_input": [{"question": "the decision, one line", "item": null, "blocking": false,
                   "context": "what you saw that leaves it open, and the cost of guessing",
                   "options": ["an answer you would accept", "another you would accept"]}]
}
```
"""

ROUND_TASK = """\
## Your task

For **every open item** in the docket: verify it against the code yourself, then stance
it — `agree`, `disagree` (with cited evidence), `needs_human`, or `abstain`. Leaving an
item out is a gap and you will be asked for it.

Then, if you have more: add new findings, answer another agent's question, ask one of
your own, raise a question for the human. Sign off (`"signoff": true`) only when you
would build what this council has agreed.
"""

ROUND_SCHEMA = """\
```json
{
  "summary": "what you did this round",
  "positions": [{"item": "F1", "stance": "agree|disagree|needs_human|abstain",
                 "note": "why", "evidence": "file:line or command output"}],
  "new_items": [{"claim": "…", "detail": "…", "evidence": "…", "severity": "minor"}],
  "questions": [{"to": "Agent2", "question": "…"}],
  "answers": [{"to": "Agent3", "question": "Q2", "answer": "…"}],
  "human_input": [{"question": "the decision, one line", "item": "F1", "blocking": true,
                   "context": "what you saw that leaves it open, and the cost of guessing",
                   "options": ["an answer you would accept", "another you would accept"]}],
  "second": ["H1"], "blocking_for": ["H4"], "retract": ["H2"], "insist": ["H3"],
  "answer_question": [{"id": "H3", "answer": "what the workspace shows, so the human is not asked"}],
  "open_questions": ["…"],
  "signoff": false
}
```
"""

# `common.round_briefing`'s per-mode slot: review needs nothing extra.
EXTRA_BRIEFING = ""
MERGE_TASK = """\
Findings raised since the last merge are listed below. Say which report the **same defect**.

- Forbid merging partial overlaps: two findings that share a symptom but name different causes
  are two findings.
- Each finding prints its claim, then its `what breaks:` line and its `evidence:` line. Read
  all three — claims that read alike often separate on the cause the middle line names.
"""
