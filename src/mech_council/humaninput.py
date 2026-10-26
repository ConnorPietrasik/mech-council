from __future__ import annotations

import re

from .deliberation.engine import CheckpointOutcome
from .deliberation.escalations import effective_class
from .deliberation.state import Docket, Question

#: The id and the separator are both tolerant.
_ANSWER = re.compile(r"^\s*(h\d+)\s*[:=]\s*(.*)$", re.DOTALL | re.IGNORECASE)
_EXPLAIN = re.compile(r"^\s*(?:explain|ask)\s+(h\d+)\s*(?:[:=]\s*(.*))?$", re.DOTALL | re.IGNORECASE)
_SKIP_ALL = re.compile(r"^\s*(skip(?:\s+all)?|none)\s*$", re.IGNORECASE)
#: `skip H1`, `skip h1 h2` — the whole line has to be ids, so `skip the cache one` cannot pass for a command.
_SKIP_IDS = re.compile(r"^\s*(?:skip|none)\s+((?:h\d+[\s,]+)*h\d+)[\s,]*$", re.IGNORECASE)
#: A line that opens like a command is a command that failed to parse, and gets the grammar said back to it.
_COMMAND_HEAD = re.compile(r"^\s*(?:(?:skip|none)\b|(?:explain|ask)\s+h\d+)", re.IGNORECASE)
#: `2` takes option 2, and nothing else does: a number with any word after it is the human's own answer.  A number is a selection only
#: when the question offered that many — otherwise `H1: 30` is an answer of "30".
_SELECTION = re.compile(r"^\s*(\d{1,2})\s*$")

#: What a line that opened like a command and did not parse is told instead of being taken for an answer.
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

    A bare line answers the first still-unanswered question: a human mid-typing should not have to remember IDs, and a guessed
    ID beats a dropped answer.  A line that opens like a command and does not parse gets the grammar said back instead, because
    an answer the human never typed settles a question they were never asked.
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
    return Line("answer", id="", text=text.strip())


def render_ask_set(docket: Docket, listing: list[Question], *, names: dict[str, str], waiting: bool) -> str:
    from .render.text import caption

    if not listing:
        return "the council has no question for you right now"
    lines = ["the council needs you" if waiting else "open questions (the run keeps working):"]
    for question in listing:
        where = f" on {question.item_id}" if question.item_id else ""
        askers = ", ".join(caption(a, names) for a in question.askers) or "the chair"
        lines.append(f"  {question.id}{where} [{effective_class(docket, question)}] asked by {askers} (round {question.raised_round})")
        lines.append(f"    {question.text}")
        if question.context:
            lines.append(f"    context: {question.context}")
        # Numbered because the cheap reply is typing a number; `1)` is the whole of it.
        for number, option in enumerate(question.options, start=1):
            lines.append(f"      {number}) {option}")
    if waiting:
        lines += [
            "",
            "answer with `H1: <text>` · a number and nothing else (`H1: 2`) takes that option verbatim, in the asker's words",
            "to take one with a qualifier, answer in your own words · ask the asker `explain H1: <question>`",
            "`skip` every open question, `skip H1` just one · a blank line ends this checkpoint",
        ]
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
        self, listing: list[Question], *, ask_set: list[Question], round_: int, terminal: bool, waiting: bool
    ) -> CheckpointOutcome:
        result = CheckpointOutcome(waited=False)
        self.console.say(render_ask_set(self.docket, listing, names=self.names, waiting=waiting))
        if not listing and not ask_set:
            return result
        if not waiting:
            # Print and keep going.  A checkpoint that does not wait has put nothing to the human, so it has
            # nothing to decline: marking these skipped would turn the human checkpoint's `unasked` — the
            # one way a question ends a run unanswered and unasked — into an answer the human never gave.
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
                    # Last line wins, the rule the `answer` branch states with its consequence: a decline takes back an
                    # answer given earlier at this checkpoint.
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
                # The last line the human types about a question is their word on it: an answer takes back an earlier decline, exactly as a
                # decline takes back an answer.
                if qid not in pending and qid not in skipped and qid not in result.answers:
                    self.console.say(f"no open question {qid}; the open ones are {open_ids}")
                    continue
                took_back = qid in skipped
                skipped.discard(qid)
                answer, number = take_selection(parsed.text, self.docket.questions[qid])
                result.answers[qid] = answer
                if number:
                    result.answer_options[qid] = number
                else:
                    # A corrected answer drops the selection the first one took: `answer_option` names the line this
                    # answer came from, and an answer in the human's own words came from none of them.
                    result.answer_options.pop(qid, None)
                pending.discard(qid)
                chose = f' "{answer}" (option {number})' if number else ""
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
        if target.chair_raised:
            self.console.say(f"{qid} was raised by the chair, which holds no roster ID and never deliberates: there is nothing to explain")
            return None
        asker = next((a for a in target.askers if a in self.docket.roster), None)
        if asker is None or self.ask_seat is None:
            self.console.say(f"no seat is available to explain {qid}")
            return None
        from .prompts import common

        self.console.say(f"asking {asker} to explain {qid}; the run stays parked and this is the only ask in flight")
        outcome = await self.ask_seat(asker, common.explain_ask(target.text, question), round_=self.docket.round, phase="explain")
        text = (getattr(outcome, "text", "") or "").strip()
        if text:
            self.console.say(f"{qid}, {asker}'s recommendation: {_clip(text)}")
        return text


def _clip(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()][:6]
    joined = " ".join(lines)
    return joined if len(joined) <= 600 else joined[:600] + " …"
