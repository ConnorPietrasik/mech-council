from __future__ import annotations

# Prose every mode's chair shares: the merge discipline and the watchdog's brief.  What differs by
# mode lives with the mode, in `review.py` and `plan.py`.

MERGE = """\
# Deduplicate, do not investigate

You are the council's chair. You never investigate the project yourself and never add an entry
of your own: your job is to see which entries below are one entry reported twice, and to say
so.

- Conservative beats tidy: when unsure, keep them apart.
- Every entry below carries an ID. Echo those IDs exactly; make none up.
- If two entries force a product decision neither can settle, raise it in `human_input`:
  `context` is the one line showing why neither entry settles it, and `options` are the
  alternatives **as the seats themselves put them** — quote the entries in front of you and
  invent nothing. The run stops on what you raise, so make it answerable by typing a number.
- Anything you raise stops the run to ask; a seat's question only stops if a majority votes
  to raise.
"""

WATCHDOG = """\
# Watchdog

Some seats have work in flight. For each one below you get how long it has been running,
how long since its last event, how many events it has produced, and a preview of its last
tool call.

Be conservative: long tool output is legitimate, a big test run is legitimate. Restart
only a seat that is clearly stuck. **If unsure, keep.**

Echo each seat's ID exactly as given (`AgentN`) and answer with one decision per seat.
"""

#: Both modes deduplicate by the rules above, so one schema serves both; `merge_schema` stamps `{id}`
#: with the ID prefix that mode's docket hands out.
MERGE_SCHEMA = """\
```json
{"groups": [{"canonical": "{id}1", "duplicates": ["{id}4", "{id}7"]}],
 "human_input": [{"question": "…", "context": "why neither entry settles it",
                  "options": ["the alternative as one seat put it", "…"], "blocking": false}]}
```
A group means: keep `canonical`, and record `duplicates` as other seats reporting it.
Only list a group when the duplicates are one entry reported twice; omit everything
that stands alone.
"""

WATCHDOG_SCHEMA = """\
```json
{"decisions": [{"agent": "Agent1", "action": "keep", "reason": "…"}],
 "human_input": []}
```
`action` is `keep` or `restart`, exactly once per agent listed.
"""


def digest_header(elapsed_note: str) -> str:
    return f"# Seats with work in flight\n\n{elapsed_note}\n"
