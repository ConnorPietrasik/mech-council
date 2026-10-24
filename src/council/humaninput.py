"""The human checkpoint: the ask set, the wait rule, the answer lines, `explain`.

The parsing is a pure function (the deliberation units tests it directly); the flow
below is the only place the run stops for a person.  **Waiting has no timeout and no
auto-skip**: a parked checkpoint is not in-flight work, so no watchdog tick arises
from it, no seat is judged stuck, and no deadline applies however long the human takes.

Answers are **not** recorded at arrival: an answer received before a checkpoint
is applied before the checks; one arriving after it lands on the next one.
"""

from __future__ import annotations

import re

from .deliberation.engine import CheckpointOutcome
from .deliberation.escalations import effective_class
from .deliberation.state import Docket, Question

#: The human checkpoint's own examples include `h2 = later`, so the id and the
#: separator are both tolerant.
_ANSWER = re.compile(r"^\s*(h\d+)\s*[:=]\s*(.*)$", re.DOTALL | re.IGNORECASE)
_EXPLAIN = re.compile(r"^\s*(?:explain|ask)\s+(h\d+)\s*(?:[:=]\s*(.*))?$", re.DOTALL | re.IGNORECASE)
_SKIP_ALL = re.compile(r"^\s*(skip(?:\s+all)?|none)\s*$", re.IGNORECASE)


class Line:
    """One checkpoint line: `answer`, `explain`, `skip` or `end`."""

    __slots__ = ("id", "kind", "text")

    def __init__(self, kind: str, id: str = "", text: str = "") -> None:
        self.kind, self.id, self.text = kind, id, text


def parse_line(text: str) -> Line:
    """`H1: text` answers · `explain H1`/`ask H1: …` asks the asker · `skip` declines all · an empty line ends the checkpoint.

    Anything else is an answer to the first still-unanswered question: a human
    mid-typing should not have to remember IDs, and a guessed ID beats a dropped answer.
    """
    if not (text or "").strip():
        return Line("end")
    if _SKIP_ALL.match(text):
        return Line("skip", text=text.strip())
    if match := _ANSWER.match(text):
        return Line("answer", id=match.group(1).upper(), text=match.group(2).strip())
    if match := _EXPLAIN.match(text):
        return Line("explain", id=match.group(1).upper(), text=(match.group(2) or "").strip() or "Why are you asking this?")
    return Line("answer", id="", text=text.strip())


def render_ask_set(docket: Docket, listing: list[Question], *, names: dict[str, str], waiting: bool) -> str:
    """The console form: id, item, effective class, asker, question, context, options."""
    from .render.text import caption

    if not listing:
        return "[council] the council has no question for you right now"
    lines = ["[council] the council needs you" if waiting else "[council] open questions (the run keeps working):"]
    for question in listing:
        where = f" on {question.item_id}" if question.item_id else ""
        askers = ", ".join(caption(a, names) for a in question.askers) or "the chair"
        lines.append(
            f"[council]   {question.id}{where} [{effective_class(docket, question)}] asked by {askers} (round {question.raised_round})"
        )
        lines.append(f"[council]     {question.text}")
        if question.context:
            lines.append(f"[council]     context: {question.context}")
        for option in question.options:
            lines.append(f"[council]       - {option}")
    if waiting:
        lines.append(
            "[council] answer with `H1: <text>` · ask the asker `explain H1: "
            "<question>` · `skip` declines every question · an empty line ends "
            "this checkpoint"
        )
    return "\n".join(lines)


class Checkpoint:
    """The wait itself.  `explain` is the only thing that puts an ask back in flight."""

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
            # the non-interactive stdin choice: EOF or not-a-tty declines everything, logged, and the run proceeds.
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
            if parsed.kind == "skip":
                skipped |= pending
                break
            if parsed.kind == "answer":
                qid = parsed.id or self._first_pending(pending)
                if qid is None:
                    self.console.say("no question is open; an empty line ends this checkpoint")
                    continue
                if qid not in self.docket.questions:
                    self.console.say(f"no question {qid}; the open ones are {', '.join(sorted(pending)) or '(none)'}")
                    continue
                result.answers[qid] = parsed.text
                pending.discard(qid)
                self.console.say(f"{qid}: answered — the answer is authoritative and buys a full round")
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
        """The asker's reply is a **recommendation** — no stance changes, no escalation
        settles — and it is the only ask in flight while the run is parked."""
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
