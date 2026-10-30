from __future__ import annotations

from .state import RETRACTED, STANDING, Docket, Note, note_key

NOTE_MAX_CHARS = 600
NOTE_MAX_PER_REPLY = 6


def raise_note(docket: Docket, *, by: str, text: str, item_id: str | None, round_: int) -> tuple[Note, bool]:
    """Record what a seat wants the human to know.

    A seat repeating itself updates its note; one it retracted is unsaid, so saying it again is a fresh claim and id.
    """
    key = note_key(text)
    for nid in docket.note_order:
        note = docket.human_notes[nid]
        if note.by == by and note.state == STANDING and note.key == key:
            note.updated_round = round_
            note.item_id = note.item_id or item_id
            return note, True
    note = Note(id=f"N{docket.next_note}", by=by, text=text, item_id=item_id, round=round_, updated_round=round_)
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


def unshown(docket: Docket) -> list[tuple[Note, str]]:
    """What the next checkpoint tells the human: each note once, each retraction once.

    A note retracted before it was printed is never printed.
    """
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
