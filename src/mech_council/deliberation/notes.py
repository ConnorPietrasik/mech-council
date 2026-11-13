from __future__ import annotations

from typing import TypeGuard

from .state import MAX_FIELD_CHARS, RETRACTED, STANDING, Docket, Note, note_key, one_line

NOTE_MAX_PER_REPLY = 6
NOTE_KINDS = ("info", "correction")


def raise_note(docket: Docket, *, by: str, text: str, item_id: str | None, round_: int, kind: str = "info") -> tuple[Note, bool]:
    """Record what a seat wants the human to know.

    A seat repeating itself updates its note; one it retracted or the chair folded is a fresh claim and id.
    A correction names the item it corrects; without one it is no correction.
    """
    kind = kind if kind in NOTE_KINDS else "info"
    if kind == "correction" and item_id is None:
        kind = "info"
    text = one_line(text)
    key = note_key(text)
    for nid in docket.note_order:
        note = docket.human_notes[nid]
        if note.by == by and note.state == STANDING and note.folded_into is None and note.key == key:
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
    if note.folded_into is not None:
        return None, f"{nid} folded into {note.folded_into}: the human reads the survivor"
    if note.by != agent:
        return None, f"{note.by} wrote that note: only {note.by} retracts it — dispute it with an item and evidence"
    note.state, note.retracted_round, note.updated_round = RETRACTED, round_, round_
    note.retract_reason = one_line(reason)[:MAX_FIELD_CHARS]
    return note, ""


def item_named(text: str, docket: Docket) -> str | None:
    """The docket item a note names up front, when its first word is one: a folded one reads as its survivor."""
    words = text.split(None, 1)
    if not words:
        return None
    first = words[0].strip(".:;,!?()[]\"'`-")
    if first not in docket.items and first not in docket.item_aliases:
        return None
    return docket.resolve_item(first)


def resolve_note_item(docket: Docket, entry: dict) -> str | None:
    """The item a note links to: its `item` field when that is a docket item, else the item its text opens with."""
    item_id = entry.get("item")
    if item_id:
        item_id = docket.resolve_item(item_id)
    if item_id and item_id not in docket.items:
        item_id = None
    if item_id is None:
        item_id = item_named(entry["text"], docket)
    return item_id


def unshown(docket: Docket) -> list[tuple[Note, str]]:
    """What the next checkpoint tells the human: each note once, each retraction once."""
    out: list[tuple[Note, str]] = []
    for nid in docket.note_order:
        note = docket.human_notes[nid]
        if folded_away(docket, note):
            continue
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
    """Every note except one retracted before the human ever read it, and one folded into a note that still stands."""
    return [
        note
        for note in (docket.human_notes[nid] for nid in docket.note_order)
        if not (note.state == RETRACTED and note.shown_round is None) and not folded_away(docket, note)
    ]


def folded_away(docket: Docket, note: Note) -> bool:
    """Folded into a note that still stands: the human reads the survivor, and this one beside it."""
    if note.folded_into is None:
        return False
    survivor = docket.human_notes.get(note.folded_into)
    return survivor is not None and survivor.state == STANDING


def _note_foldable(note: Note | None) -> TypeGuard[Note]:
    """Only a standing note the human has not read, and no fold has taken, can fold."""
    return note is not None and note.state == STANDING and note.shown_round is None and note.folded_into is None


def merge_notes(docket: Docket, *, groups: list[dict], round_: int) -> tuple[list[tuple[str, dict]], str]:
    """Fold the notes the chair says are one note: the note raised first survives whichever one the
    chair names; a group naming one note twice, or one note in two groups, folds nothing.
    """
    claimed: set[str] = set()
    usable: list[tuple[Note, list[Note], str]] = []
    for group in groups:
        ids = [group["canonical"], *(group.get("duplicates") or [])]
        if len(set(ids)) != len(ids):
            return [], f"{ids[0]} is named twice in one group"
        if reused := claimed & set(ids):
            return [], f"{sorted(reused)[0]} is reused across groups"
        claimed.update(ids)
        notes = [one for one in (docket.human_notes.get(nid) for nid in ids) if _note_foldable(one)]
        if len(notes) > 1 and len({one.kind for one in notes}) == 1:
            survivor = min(notes, key=lambda one: docket.note_order.index(one.id))
            usable.append((survivor, [one for one in notes if one is not survivor], group["canonical"]))
    events: list[tuple[str, dict]] = []
    for survivor, duplicates, chair_named in usable:
        for duplicate in duplicates:
            _fold_note(survivor, duplicate)
            events.append(
                ("note_merged", {"id": duplicate.id, "into": survivor.id, "by": survivor.by, "chair_named": chair_named, "round": round_})
            )
    return events, ""


def _fold_note(canonical: Note, duplicate: Note) -> None:
    """One note absorbs another: the human reads the survivor, and the duplicate's seat is named beside it."""
    if duplicate.by != canonical.by and duplicate.by not in canonical.also_noted_by:
        canonical.also_noted_by.append(duplicate.by)
    if duplicate.item_id and canonical.item_id is None:
        canonical.item_id = duplicate.item_id
    duplicate.folded_into = canonical.id


def item_notes(docket: Docket, item_id: str) -> list[Note]:
    """The ledger's notes filed under one docket item."""
    return [note for note in report_notes(docket) if note.item_id == item_id]
