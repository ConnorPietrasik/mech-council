from __future__ import annotations

MERGE = """\
# Deduplicate, do not investigate

You are the council's chair. You never investigate the project yourself and never add an entry
of your own: your job is to see which entries below are one entry reported twice, and to say
so.

- Conservative beats tidy: when unsure, keep them apart.
- Every entry below carries an ID. Echo those IDs exactly; make none up, and never name an entry that is
  not listed: the listing is every entry you are judging.
- Name the entry raised **first** as `canonical`: its claim is what the human reads, and the others are
  recorded beside it. The ids ascend with time, so the lowest one in a group is the earliest.
"""


def ask_merge(human: str) -> str:
    once = "the human is asked once" if human != "none" else "one ask stands for the decision"
    return f"""\
# Fold the asks that want the same decision

You are the council's chair. Below are the open asks seats have raised for the human. Two seats
often want one decision and word it differently; your job is to say which asks are one ask, so
{once} and a second seat counting toward it is visible.

- Conservative beats tidy: when unsure, keep them apart.
- Asks listed on the same item (`H2 (on F1)`) are the likeliest to be one ask: an item is one
  decision, and two seats each asking the human about it separately is what this fold is for.
- Name the ask raised **first** as `canonical`: its words are what the human reads, and the others
  are recorded beside them. The ids ascend with time, so the lowest one in a group is the earliest.
- Echo the `H` IDs below exactly; make none up, and never name an ask that is not listed.
- You answer nothing here, and you raise nothing of your own: an ask you cannot trace to a seat
  does not exist.
"""


def note_merge() -> str:
    return """\
# Fold the notes that say the same thing

You are the council's chair. Below are notes the seats filed for the human. Seats often tell the
human the same thing separately; your job is to say which notes are one note, so the human reads
it once and the other seats are named beside it.

- Conservative beats tidy: when unsure, keep them apart.
- A note that corrects an item is a different kind from a plain note: they are one note only
  when they say the same thing and are of one kind.
- Name the note raised **first** as `canonical`: its words are what the human reads, and the others
  are recorded beside it. The ids ascend with time, so the lowest one in a group is the earliest.
- Echo the `N` IDs below exactly; make none up, and never name a note that is not listed.
- You add nothing here: a note you cannot trace to a seat does not exist.
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

MERGE_SCHEMA = """\
```json
{"groups": [{"canonical": "{id}1", "duplicates": ["{id}4", "{id}7"]}]}
```
A group means: keep `canonical`, and record `duplicates` as other seats reporting it.
Only list a group when the duplicates are one entry reported twice; omit everything
that stands alone.
"""

ASK_MERGE_SCHEMA = """\
```json
{"groups": [{"canonical": "H1", "duplicates": ["H2", "H5"]}]}
```
A group means: keep `canonical` — the ask raised first, whose words the human reads — and the
`duplicates` are that same decision asked again in other words: every seat that asked one now asks
the other. Only list a group when the asks want the same decision; an answer that would settle one
must settle the rest. Omit every ask that stands alone, and send `{"groups": []}` when nothing folds.
"""

NOTE_MERGE_SCHEMA = """\
```json
{"groups": [{"canonical": "N1", "duplicates": ["N4", "N7"]}]}
```
A group means: keep `canonical` — the note raised first, whose words the human reads — and the
`duplicates` are the same note filed by other seats: the human reads it once, with those seats
named beside it. Only list a group when the notes say the same thing; omit every note that stands
alone, and send `{"groups": []}` when nothing folds.
"""

WATCHDOG_SCHEMA = """\
```json
{"decisions": [{"agent": "Agent1", "action": "keep", "reason": "…"}]}
```
`action` is `keep` or `restart`, exactly once per agent listed.
"""


def digest_header(elapsed_note: str) -> str:
    return f"# Seats with work in flight\n\n{elapsed_note}\n"
