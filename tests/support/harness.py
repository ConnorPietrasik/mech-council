"""The engine suite's harness: the engine over a scripted port, with no pi and no sandbox."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from mech_council.deliberation.engine import CheckpointOutcome, RawAsk
from mech_council.deliberation.state import Docket


def payload(**kw) -> str:
    return json.dumps(kw)


@dataclass
class Ask:
    who: str  # an agent id, or "chair"
    phase: str
    round: int
    kind: str = "verdict"
    text: str = ""


@dataclass
class ScriptPort:
    """A port whose replies are chosen by a function of the recorded ask."""

    docket: Docket
    reply: object = None
    answers: dict = field(default_factory=dict)  # H-id → the human's answer
    skip: list[str] = field(default_factory=list)
    recommendations: dict = field(default_factory=dict)
    selections: dict = field(default_factory=dict)  # H-id → the option number the answer took (what the Checkpoint records)
    off_cycle: dict = field(default_factory=dict)  # H-id → an answer the human typed while the run was working

    asks: list[Ask] = field(default_factory=list)
    checkpoints: list[dict] = field(default_factory=list)

    # -- the seat lanes ---------------------------------------------------------

    async def ask_seats(self, batch, *, round_: int, phase: str = "round", classify_unusable: bool = False) -> dict[str, RawAsk]:
        out: dict[str, RawAsk] = {}
        for agent, message in batch:
            self.asks.append(Ask(agent, phase, round_, "verdict", message))
            out[agent] = self._answer(agent, phase, round_, message)
        return out

    async def ask_seat(self, agent: str, message: str, *, round_: int, phase: str = "round") -> RawAsk:
        self.asks.append(Ask(agent, phase, round_, "verdict", message))
        return self._answer(agent, phase, round_, message)

    def _answer(self, who: str, phase: str, round_: int, message: str) -> RawAsk:
        answer = self.reply(Ask(who, phase, round_, "verdict", message)) if callable(self.reply) else self.reply
        if isinstance(answer, RawAsk):
            return answer
        if isinstance(answer, dict):
            return RawAsk(text=json.dumps(answer))
        return RawAsk(text=str(answer or ""))

    # -- the chair lane ---------------------------------------------------------

    async def chair_merge(self, listing: str, *, round_: int) -> dict:
        # The real Port runs this same extractor, so the harness must too: a scripted plan is JSON text first.
        self.asks.append(Ask("chair", "merge", round_, "merge", listing))
        from mech_council.extraction import parse_merge_plan

        text = self._chair_answer(Ask("chair", "merge", round_, "merge", listing))
        data, _errors = parse_merge_plan(text or "")
        return {"groups": (data or {}).get("groups") or []}

    async def chair_merge_questions(self, listing: str, *, round_: int) -> dict:
        # `Port` runs the extractor, so the harness does too: the chair's fold is JSON text first.
        self.asks.append(Ask("chair", "asks", round_, "asks", listing))
        from mech_council.extraction import parse_ask_merge

        text = self._chair_answer(Ask("chair", "asks", round_, "asks", listing))
        data, _errors = parse_ask_merge(text or "")
        return {"groups": (data or {}).get("groups") or []}

    async def chair_direction(self, listing: str, *, round_: int) -> dict:
        self.asks.append(Ask("chair", "direction", round_, "direction", listing))
        from mech_council.extraction import parse_direction

        text = self._chair_answer(Ask("chair", "direction", round_, "direction", listing))
        data, _errors = parse_direction(text or "")
        return data or {}

    async def chair_compare(self, listing: str, *, round_: int) -> dict:
        self.asks.append(Ask("chair", "compare", round_, "compare", listing))
        from mech_council.extraction import parse_comparison

        text = self._chair_answer(Ask("chair", "compare", round_, "compare", listing))
        data, _errors = parse_comparison(text or "")
        return data or {}

    def _chair_answer(self, ask: Ask) -> str | None:
        """A scripted `RawAsk(failure=…)` on a chair ask becomes the engine's `Abort`, as `Port` does."""
        answer = self.reply(ask) if callable(self.reply) else {}
        if isinstance(answer, RawAsk) and answer.failure:
            from mech_council.deliberation.engine import Abort

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

    async def checkpoint(self, ask_set, *, round_: int, terminal: bool, listing: list, waiting: bool, notes=()):
        self.checkpoints.append(
            {
                "round": round_,
                "terminal": terminal,
                "waiting": waiting,
                "ask_set": [q.id for q in ask_set],
                "listing": [q.id for q in listing],
                "notes": [f"{note.id}:{kind}" for note, kind in notes],
            }
        )
        ids = [q.id for q in ask_set]
        if not waiting:
            # the real Checkpoint prints and keeps going: nothing was put to the human, so nothing is answered
            return CheckpointOutcome(waited=False)
        answered = {qid: text for qid, text in self.answers.items() if qid in ids}
        skipped = [qid for qid in ids if qid in self.skip]
        recommended = {qid: text for qid, text in self.recommendations.items() if qid in ids}
        options = {qid: number for qid, number in self.selections.items() if qid in answered}
        return CheckpointOutcome(answers=answered, answer_options=options, skipped=skipped, waited=waiting, recommendations=recommended)

    async def drain_human(self) -> CheckpointOutcome:
        """The off-cycle answers: the first round boundary where the ask is still open takes each one."""
        live = [q.id for q in self.docket.questions.values() if q.state == "asked"]
        return CheckpointOutcome(answers={qid: self.off_cycle.pop(qid) for qid in live if qid in self.off_cycle}, waited=False)

    # -- convenience for assertions --------------------------------------------

    def count(self, *, who: str | None = None, phase: str | None = None, round_: int | None = None) -> int:
        return sum(
            1
            for ask in self.asks
            if (who is None or ask.who == who) and (phase is None or ask.phase == phase) and (round_ is None or ask.round == round_)
        )

    def asked(self, who: str, phase: str | None = None, round_: int | None = None) -> bool:
        return self.count(who=who, phase=phase, round_=round_) > 0


def events_collector() -> tuple[list[dict], object]:
    """`(records, emit)` — the engine's event feed as data."""
    records: list[dict] = []

    def emit(name: str, **fields) -> None:
        records.append({"event": name, **fields})

    return records, emit
