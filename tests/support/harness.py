"""§13.2's harness: the engine over a scripted port, with no pi and no sandbox.

The port speaks the same shapes `port.Port` speaks — `RawAsk` in, structured data out of
the real extractor — so these cases test the protocol, not a mock's invention.  Every ask
is recorded, which is how "zero asks" and "exactly one defense ask" are assertable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from council.deliberation.engine import CheckpointOutcome, RawAsk
from council.deliberation.state import Docket


def payload(**kw) -> str:
    return json.dumps(kw)


@dataclass
class Ask:
    """One recorded ask."""
    who: str          # an agent id, or "chair"
    phase: str
    round: int
    kind: str = "verdict"
    text: str = ""


@dataclass
class ScriptPort:
    """A port whose replies are chosen by a function of the recorded ask.

    `reply(ask)` returns either a JSON string, a dict, or a `RawAsk`.  A dict is
    serialized; a `RawAsk` passes through, which is how a failure or a rejection is
    scripted.
    """

    docket: Docket
    reply: object = None
    answers: dict = field(default_factory=dict)        # H-id → the human's answer
    skip: list[str] = field(default_factory=list)
    recommendations: dict = field(default_factory=dict)

    asks: list[Ask] = field(default_factory=list)
    checkpoints: list[dict] = field(default_factory=list)

    # -- the seat lanes ---------------------------------------------------------

    async def ask_seats(self, batch, *, round_: int, phase: str = "round",
                        classify_unusable: bool = False) -> dict[str, RawAsk]:
        out: dict[str, RawAsk] = {}
        for agent, message in batch:
            self.asks.append(Ask(agent, phase, round_, "verdict", message))
            out[agent] = self._answer(agent, phase, round_, message)
        return out

    async def ask_seat(self, agent: str, message: str, *, round_: int,
                       phase: str = "round") -> RawAsk:
        self.asks.append(Ask(agent, phase, round_, "verdict", message))
        return self._answer(agent, phase, round_, message)

    def _answer(self, who: str, phase: str, round_: int, message: str) -> RawAsk:
        answer = self.reply(Ask(who, phase, round_, "verdict", message)) if \
            callable(self.reply) else self.reply
        if isinstance(answer, RawAsk):
            return answer
        if isinstance(answer, dict):
            return RawAsk(text=json.dumps(answer))
        return RawAsk(text=str(answer or ""))

    # -- the chair lane ---------------------------------------------------------

    async def chair_merge(self, listing: str, *, round_: int) -> dict:
        # The real Port runs the same extractor the chair's reply goes through, so the
        # harness must too: a scripted plan is JSON text first, parsed data second.
        self.asks.append(Ask("chair", "merge", round_, "merge", listing))
        from council.extraction import parse_merge_plan
        text = self._chair_answer(Ask("chair", "merge", round_, "merge", listing))
        data, questions, _errors = parse_merge_plan(text or "")
        return {"groups": (data or {}).get("groups") or [], "questions": questions}

    async def chair_compare(self, listing: str, *, round_: int) -> dict:
        self.asks.append(Ask("chair", "compare", round_, "compare", listing))
        from council.extraction import parse_comparison
        text = self._chair_answer(Ask("chair", "compare", round_, "compare", listing))
        data, _errors = parse_comparison(text or "")
        return data or {}

    def _chair_answer(self, ask: "Ask") -> str | None:
        """A scripted `RawAsk(failure=…)` on a chair ask becomes §6.1's abort, as `Port` does."""
        answer = self.reply(ask) if callable(self.reply) else {}
        if isinstance(answer, RawAsk) and answer.failure:
            from council.deliberation.engine import Abort
            raise Abort(answer.failure, member="chair", round_=ask.round)
        return self._text(answer)

    @staticmethod
    def _text(answer) -> str:
        if isinstance(answer, RawAsk):
            return answer.text or ""
        if isinstance(answer, dict):
            return json.dumps(answer)
        return str(answer or "")

    # -- the human --------------------------------------------------------------

    async def checkpoint(self, ask_set, *, round_: int, terminal: bool, listing: list,
                         waiting: bool):
        self.checkpoints.append({"round": round_, "terminal": terminal, "waiting": waiting,
                                 "ask_set": [q.id for q in ask_set],
                                 "listing": [q.id for q in listing]})
        ids = [q.id for q in ask_set]
        answered = {qid: text for qid, text in self.answers.items() if qid in ids}
        skipped = [qid for qid in ids if qid in self.skip]
        recommended = {qid: text for qid, text in self.recommendations.items() if qid in ids}
        return CheckpointOutcome(answers=answered, skipped=skipped, waited=waiting,
                                 recommendations=recommended)

    # -- convenience for assertions --------------------------------------------

    def count(self, *, who: str | None = None, phase: str | None = None,
              round_: int | None = None) -> int:
        return sum(1 for ask in self.asks
                   if (who is None or ask.who == who)
                   and (phase is None or ask.phase == phase)
                   and (round_ is None or ask.round == round_))

    def asked(self, who: str, phase: str | None = None, round_: int | None = None) -> bool:
        return self.count(who=who, phase=phase, round_=round_) > 0


def events_collector() -> tuple[list[dict], object]:
    """`(records, emit)` — the engine's event feed as data."""
    records: list[dict] = []

    def emit(name: str, **fields) -> None:
        records.append({"event": name, **fields})

    return records, emit
