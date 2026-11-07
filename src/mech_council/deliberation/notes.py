from __future__ import annotations

from .state import RETRACTED, STANDING, Docket, Item, Note, note_key

NOTE_MAX_CHARS = 600
NOTE_MAX_PER_REPLY = 6
NOTE_KINDS = ("info", "correction")


def raise_note(docket: Docket, *, by: str, text: str, item_id: str | None, round_: int, kind: str = "info") -> tuple[Note, bool]:
    """Record what a seat wants the human to know.

    A seat repeating itself updates its note; one it retracted is a fresh claim and id.
    A correction names the item it corrects; without one it is no correction.
    """
    kind = kind if kind in NOTE_KINDS else "info"
    if kind == "correction" and item_id is None:
        kind = "info"
    key = note_key(text)
    for nid in docket.note_order:
        note = docket.human_notes[nid]
        if note.by == by and note.state == STANDING and note.key == key:
            note.updated_round = round_
            note.item_id = note.item_id or item_id
            if kind == "correction" and note.item_id is not None:
                note.kind = "correction"
            return note, True
    note = Note(id=f"N{docket.next_note}", by=by, text=text, kind=kind, item_id=item_id, round=round_, updated_round=round_)
    docket.next_note += 1
    docket.human_notes[note.id] = note
    docket.note_order.append(note.id)
    return note, False


def retract_note(docket: Docket, *, nid: str, agent: str, round_: int, reason: str = "") -> tuple[Note | None, str]:
    """Only the author retracts a note: a peer disputes it with an item."""
    note = docket.human_notes.get(nid)
    if note is None:
        if ask := docket.questions.get(nid):
            who = ", ".join(ask.askers)
            if who and agent in ask.askers:
                return None, f'{nid} is an ask you are in: drop it with `retract: ["{nid}"]`'
            return None, f"{nid} is an ask of {who or 'the chair'}, not a note: only its askers drop it"
        return None, "no standing note with that id"
    if note.state != STANDING:
        return None, "no standing note with that id"
    if note.by != agent:
        return None, f"{note.by} wrote that note: only {note.by} retracts it — dispute it with an item and evidence"
    note.state, note.retracted_round, note.updated_round = RETRACTED, round_, round_
    note.retract_reason = reason.strip()[:NOTE_MAX_CHARS]
    return note, ""


def item_named(text: str, items: dict[str, Item]) -> str | None:
    """The docket item a note names up front, when its first word is one."""
    words = text.split(None, 1)
    if not words:
        return None
    first = words[0].strip(".:;,!?()[]\"'`-")
    return first if first in items else None


def resolve_note_item(docket: Docket, entry: dict) -> str | None:
    """The item a note links to: its `item` field when that is a docket item, else the item its text opens with."""
    item_id = entry.get("item")
    if item_id and item_id not in docket.items:
        item_id = None
    if item_id is None:
        item_id = item_named(entry["text"], docket.items)
    return item_id


def unshown(docket: Docket) -> list[tuple[Note, str]]:
    """What the next checkpoint tells the human: each note once, each retraction once."""
    out: list[tuple[Note, str]] = []
    for nid in docket.note_order:
        note = docket.human_notes[nid]
        if note.shown_round is None:
            if note.state == STANDING:
                out.append((note, "note"))
        elif note.state == RETRACTED and note.retraction_shown_round is None:
            out.append((note, "retraction"))
    return out


def mark_shown(shown: list[tuple[Note, str]], round_: int) -> None:
    """Mark what this checkpoint printed, so it never prints again."""
    for note, kind in shown:
        if kind == "retraction":
            note.retraction_shown_round = round_
        else:
            note.shown_round = round_


def report_notes(docket: Docket) -> list[Note]:
    """Every note except one retracted before the human ever read it."""
    return [
        note
        for note in (docket.human_notes[nid] for nid in docket.note_order)
        if not (note.state == RETRACTED and note.shown_round is None)
    ]


def item_notes(docket: Docket, item_id: str) -> list[Note]:
    """The ledger's notes filed under one docket item."""
    return [note for note in report_notes(docket) if note.item_id == item_id]
