from __future__ import annotations

import re
from collections.abc import Sequence

from .deliberation.engine import CheckpointOutcome
from .deliberation.escalations import effective_class, stop_holders
from .deliberation.state import Docket, Note, Question

#: The id and the separator are both tolerant.
_ANSWER = re.compile(r"^\s*(h\d+)\s*[:=]\s*(.*)$", re.DOTALL | re.IGNORECASE)
_EXPLAIN = re.compile(r"^\s*(?:explain|ask)\s+(h\d+)\s*(?:[:=]\s*(.*))?$", re.DOTALL | re.IGNORECASE)
_SKIP_ALL = re.compile(r"^\s*(skip(?:\s+all)?|none)\s*$", re.IGNORECASE)
#: `skip H1`, `skip h1 h2`: the whole line has to be ids
_SKIP_IDS = re.compile(r"^\s*(?:skip|none)\s+((?:h\d+[\s,]+)*h\d+)[\s,]*$", re.IGNORECASE)
#: a line that opens like a command gets the grammar said back to it
_COMMAND_HEAD = re.compile(r"^\s*(?:(?:skip|none)\b|(?:explain|ask)\s+h\d+)", re.IGNORECASE)
#: `N1:` reads like `H1:`, and would answer whichever ask happens to be open
_NOTE_LINE = re.compile(r"^\s*(?:note\s+)?n\d+\s*[:=]", re.IGNORECASE)
#: a bare number takes that option, and only when the question offered that many
_SELECTION = re.compile(r"^\s*(\d{1,2})\s*$")

_GRAMMAR = (
    "that is not the grammar: `skip` declines every open question, `skip H1` declines one (`skip H1 H2` two), and "
    "`explain H1: <question>` asks the seat that raised H1 — to answer in your own words, write `H1: <text>`"
)


class Line:
    __slots__ = ("id", "ids", "kind", "text")

    def __init__(self, kind: str, id: str = "", text: str = "", ids: tuple[str, ...] = ()) -> None:
        self.kind, self.id, self.text, self.ids = kind, id, text, tuple(ids)


def parse_line(text: str) -> Line:
    """`H1: text` answers · `H1: 2` takes option 2 · `explain H1`/`ask H1: …` asks the asker · `skip` all, `skip H1` one.

    A bare line answers the first still-unanswered question.
    """
    if not (text or "").strip():
        return Line("end")
    if match := _SKIP_IDS.match(text):
        ids = tuple(found.upper() for found in re.findall(r"h\d+", match.group(1), flags=re.IGNORECASE))
        return Line("skip", text=text.strip(), ids=ids)
    if _SKIP_ALL.match(text):
        return Line("skip", text=text.strip())
    if match := _ANSWER.match(text):
        return Line("answer", id=match.group(1).upper(), text=match.group(2).strip())
    if match := _EXPLAIN.match(text):
        return Line("explain", id=match.group(1).upper(), text=(match.group(2) or "").strip() or "Why are you asking this?")
    if _COMMAND_HEAD.match(text):
        return Line("error", text=_GRAMMAR)
    if _NOTE_LINE.match(text):
        return Line("error", text="a note needs no answer and nothing you type retracts one; `H1: <text>` answers an open question")
    return Line("answer", id="", text=text.strip())


def _direction_line(option: str, docket: Docket, names: dict[str, str]) -> str:
    """A direction menu line in the names the human knows."""
    from .render.text import direction_label

    token = next((t for t in docket.directions() if option.startswith(f"{t}: ")), "")
    if not token:
        return option
    return f"{direction_label(token, names)}: {option[len(token) + 2 :]}"


def _spoken(question: Question, names: dict[str, str]) -> tuple[str, str]:
    """Protocol ids in model-written text → the names the human knows."""
    from .render.text import spoken

    if not question.direction_ask:
        return question.text, question.context
    return spoken(question.text, names), spoken(question.context, names)


def render_ask_set(docket: Docket, listing: list[Question], *, names: dict[str, str], waiting: bool) -> str:
    from .render.text import name_of

    if not listing:
        return "the council has no question for you right now"
    lines = ["the council needs you" if waiting else "open questions (the run keeps working):"]
    for question in listing:
        where = f" on {question.item_id}" if question.item_id else " (the council will not converge)" if question.direction_ask else ""
        askers = ", ".join(name_of(a, names) for a in question.askers) or "the chair"
        text, context = _spoken(question, names)
        lines.append(f"  {question.id}{where} [{effective_class(docket, question)}] asked by {askers} (round {question.raised_round})")
        lines.append(f"    {text}")
        if context:
            lines.append(f"    context: {context}")
        for seat, said in question.also_asked.items():
            lines.append(f"    asked as well by {name_of(seat, names)}: {said}")
        for seat, answered in question.peer_answers.items():
            who = name_of(seat, names)
            if seat in question.askers:
                lines.append(f"    {who} answered it and asks it anyway, which is why it still comes to you: {answered}")
            else:
                lines.append(f"    a seat answered it instead of asking you — {who}: {answered}")
        if question.insisted:
            lines.append("    the asker says that answer was not enough, which is why it comes to you")
        if holders := stop_holders(docket, question):
            backers = ", ".join(name_of(one, names) for one in holders)
            lines.append(f"    asked that the run wait for this: {backers} ({len(holders)}/{docket.bar()} at the bar)")
        for number, option in enumerate(question.options, start=1):
            shown = _direction_line(option, docket, names) if question.direction_ask else option
            lines.append(f"      {number}) {shown}")
    if waiting:
        lines += [
            "",
            "answer with `H1: <text>` · a number and nothing else (`H1: 2`) takes that option verbatim, in the asker's words",
            "to take one with a qualifier, answer in your own words · ask the asker `explain H1: <question>`",
            "`skip` every open question, `skip H1` just one · a blank line ends this checkpoint",
        ]
    return "\n".join(lines)


def _folded(notes: Sequence[tuple[Note, str]]) -> list[tuple[str, list[Note]]]:
    """Seats that noted one sentence say it to the human once, under both names.

    The ledger keeps each seat its own entry: fold only here, where a note is read aloud.
    """
    out: dict[tuple[str, str], tuple[str, list[Note]]] = {}
    for note, kind in notes:
        out.setdefault((kind, note.key if kind == "note" else note.id), (kind, []))[1].append(note)
    return list(out.values())


def render_notes(notes: Sequence[tuple[Note, str]], *, names: dict[str, str]) -> str:
    """What the human is told about a note, once; the report holds the whole ledger."""
    from .render.text import name_of

    lines = ["notes from the council (nothing to answer):"]
    for kind, group in _folded(notes):
        first = group[0]
        ids = ", ".join(note.id for note in group)
        who = ", ".join(name_of(note.by, names) for note in group)
        if kind == "retraction":
            reason = f": {first.retract_reason}" if first.retract_reason else ""
            lines.append(f"  {ids} was retracted by {who} (round {first.retracted_round}){reason}")
            lines.append(f"    it had said: {first.text}")
            continue
        items = {note.item_id for note in group}
        where = f" on {first.item_id}" if len(items) == 1 and first.item_id else ""
        lines.append(f"  {ids}{where} {who} (round {min(note.round for note in group)}): {first.text}")
    return "\n".join(lines)


def take_selection(text: str, question: Question) -> tuple[str, int]:
    match = _SELECTION.match(text or "")
    if not match or not question.options:
        return text, 0
    number = int(match.group(1))
    if not 1 <= number <= len(question.options):
        return text, 0
    return question.options[number - 1], number


class Checkpoint:
    def __init__(self, docket: Docket, reader, *, console, names: dict[str, str], ask_seat=None) -> None:
        self.docket = docket
        self.reader = reader
        self.console = console
        self.names = names
        self.ask_seat = ask_seat

    async def run(
        self,
        listing: list[Question],
        *,
        ask_set: list[Question],
        round_: int,
        terminal: bool,
        waiting: bool,
        notes: Sequence[tuple[Note, str]] = (),
    ) -> CheckpointOutcome:
        result = CheckpointOutcome(waited=False)
        if notes:
            # above the questions: the human reads this before deciding what to type
            self.console.say(render_notes(notes, names=self.names))
        self.console.say(render_ask_set(self.docket, listing, names=self.names, waiting=waiting))
        if not listing and not ask_set:
            return result
        if not waiting:
            # print and keep going: nothing was put to the human here, so nothing is declined
            return result
        if not self.reader.interactive:
            self.console.say("stdin is not interactive: every question is declined for this run, and the report lists them")
            result.skipped = sorted({q.id for q in ask_set})
            return result
        result.waited = True
        pending = {q.id for q in ask_set}
        skipped: set[str] = set()
        while pending:
            line = await self.reader.next_line()
            from .humanio import EOF

            if line is EOF:
                self.reader.eof = True
                skipped |= pending
                break
            parsed = parse_line(line if isinstance(line, str) else "")
            if parsed.kind == "end":
                break
            if parsed.kind == "error":
                self.console.say(parsed.text)
                continue
            if parsed.kind == "skip":
                if not parsed.ids:
                    skipped |= pending
                    break
                for one in parsed.ids:
                    if one not in pending and one not in result.answers:
                        self.console.say(f"no open question {one}; the open ones are {', '.join(sorted(pending)) or '(none)'}")
                        continue
                    took_back = result.answers.pop(one, None) is not None
                    result.answer_options.pop(one, None)
                    skipped.add(one)
                    pending.discard(one)
                    said = f"{one}: declined — the report lists it as asked and not answered"
                    self.console.say(said + (", and the answer you gave is withdrawn" if took_back else ""))
                continue
            if parsed.kind == "answer":
                qid = parsed.id or self._first_pending(pending)
                if qid is None:
                    self.console.say("no question is open; an empty line ends this checkpoint")
                    continue
                open_ids = ", ".join(sorted(pending)) or "(none)"
                if qid not in self.docket.questions:
                    self.console.say(f"no question {qid}; the open ones are {open_ids}")
                    continue
                if qid not in pending and qid not in skipped and qid not in result.answers:
                    self.console.say(f"no open question {qid}; the open ones are {open_ids}")
                    continue
                took_back = qid in skipped
                skipped.discard(qid)
                target = self.docket.questions[qid]
                answer, number = take_selection(parsed.text, target)
                result.answers[qid] = answer
                if number:
                    result.answer_options[qid] = number
                else:
                    result.answer_options.pop(qid, None)
                pending.discard(qid)
                echoed = _direction_line(answer, self.docket, self.names) if number and target.direction_ask else answer
                chose = f' "{echoed}" (option {number})' if number else ""
                back = ", and the decline you gave is withdrawn" if took_back else ""
                self.console.say(f"{qid}: answered{chose} — the answer is authoritative and buys a full round{back}")
                continue
            if parsed.kind == "explain":
                reply = await self._explain(parsed.id, parsed.text)
                if reply is not None:
                    result.recommendations[parsed.id] = reply
        result.skipped = sorted(skipped)
        return result

    def _first_pending(self, pending: set[str]) -> str | None:
        for qid in self.docket.question_order:
            if qid in pending:
                return qid
        return None

    async def _explain(self, qid: str, question: str) -> str | None:
        target = self.docket.questions.get(qid)
        if target is None:
            self.console.say(f"no question {qid}")
            return None
        if target.direction_ask:
            self.console.say(f"{qid} is the council's split, worded by the chair, which holds no roster ID and never deliberates")
            return None
        asker = next((a for a in target.askers if a in self.docket.roster), None)
        if asker is None or self.ask_seat is None:
            self.console.say(f"no seat is available to explain {qid}")
            return None
        from .prompts import common
        from .render.text import name_of

        who = name_of(asker, self.names)
        self.console.say(f"asking {who} to explain {qid}; the run stays parked and this is the only ask in flight")
        outcome = await self.ask_seat(asker, common.explain_ask(target.text, question), round_=self.docket.round, phase="explain")
        text = (getattr(outcome, "text", "") or "").strip()
        if text:
            self.console.say(f"{qid}, {who}'s recommendation: {_clip(text)}")
        return text


def _clip(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()][:6]
    joined = " ".join(lines)
    return joined if len(joined) <= 600 else joined[:600] + " …"
