from __future__ import annotations

from dataclasses import dataclass, field

from ..extraction import parse_phase0, parse_verdict
from ..modes.base import adapter_for
from . import gaps as gap_rules
from .dispositions import closure_pass, derive
from .escalations import (
    HELD_BACK,
    MOOT,
    SETTLED,
    UNBACKED,
    answer_by_seat,
    ask_to_stop,
    challenges,
    demote,
    drop_ask,
    effective_class,
    held_back,
    insist,
    merge_asks,
    moot_item_asks,
    raise_direction_ask,
    raise_question,
    record_stance,
    refresh_question,
    release_stops,
    retire,
    retract,
    second,
    settlements,
    showable,
    stop_for,
)
from .notes import mark_shown, raise_note, retract_note, unshown
from .stalemate import signature
from .state import (
    ABSTAIN,
    ANSWERED,
    ASKED,
    CHAIR_ID,
    DECLINED,
    DISAGREE,
    GO,
    MERGED,
    NEEDS_HUMAN,
    QA,
    ROUND_LIMIT,
    SKIPPED,
    STALEMATE,
    STANDING,
    WAITING,
    Approach,
    Docket,
    Item,
    RoundRecord,
    Stance,
)
from .termination import evaluate


@dataclass
class RawAsk:
    text: str | None = None
    failure: str | None = None  # the ask failed terminally; the loop turns it into an `Abort`
    rejected: bool = False  # it arrived and lenient extraction refused it


@dataclass
class CheckpointOutcome:
    answers: dict[str, str] = field(default_factory=dict)
    # the option number the human typed, when the answer was one of the offered lines
    answer_options: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    waited: bool = False
    recommendations: dict[str, str] = field(default_factory=dict)

    @property
    def any_answer(self) -> bool:
        return bool(self.answers)


class Abort(Exception):
    def __init__(self, reason: str, *, member: str = "", provider: str | None = None, model: str | None = None, round_: int = 0) -> None:
        super().__init__(reason)
        self.reason, self.member = reason, member
        self.provider, self.model, self.round = provider, model, round_


class Engine:
    def __init__(
        self,
        docket: Docket,
        port,
        *,
        goal: str | None = None,
        project: str = "",
        emit=None,
        brief=None,
        max_rounds: int = 10,
        grace_rounds: int = 1,
        names: dict[str, str] | None = None,
    ) -> None:
        self.docket = docket
        self.port = port
        self.adapter = adapter_for(docket.mode)
        self.goal = goal
        self.project = project
        self.emit = emit or (lambda name, **fields: None)
        self.brief = brief
        self.names = names or {}
        self.previous_signature: tuple | None = None
        self.checkpoint_moved = False
        self.shown: dict[str, list[str]] = {}
        self.asks_used: dict[str, int] = {}
        self.answers: dict[str, str] = {}
        self.deferred_questions: list[str] = []
        self.merge_refusals = 0
        self._next_item = 1
        self.phase0_counts: dict[str, int] = {}
        self._phase0_summary: dict[str, str] = {}
        self._class_of: dict[str, str] = {}
        self._camps_seen: tuple[tuple[str, ...], ...] | None = None
        self._camps_held = 0
        self._ceiling = max_rounds
        self._grace_left = max(0, grace_rounds)  # how many rounds past the ceiling an answer may buy in total
        self._peer_pass_done = False
        self._folded_asks: tuple | None = None  # the open asks the chair last read, so an unchanged set is not folded twice
        self._asked_of_human: set[str] = set()  # asks a waiting checkpoint actually put in front of the human
        self._insists: list[tuple[str, str, int]] = []  # (seat, ask id, round): read once every answer of the pass landed
        self._demoted_this_round: set[tuple[str, str]] = set()
        self._stale = False

    def new_id(self) -> str:
        while f"{self.adapter.id_prefix}{self._next_item}" in self.docket.items:
            self._next_item += 1
        item_id = f"{self.adapter.id_prefix}{self._next_item}"
        self._next_item += 1
        return item_id

    def phase(self, where: str) -> None:
        """Where the run had reached, written as it happens."""
        self.docket.phase = where

    def add_item(self, entry: dict, raiser: str, round_: int) -> Item:
        item = Item(
            id=self.new_id(),
            claim=entry.get("claim", "").strip()[:600],
            detail=entry.get("detail", "")[:2000],
            evidence=entry.get("evidence", "")[:2000],
            severity=entry.get("severity", "minor"),
            raised_by=[raiser],
            opened_round=round_,
        )
        return self.docket.add_item(item)

    async def phase0(self) -> list[str]:
        assert self.brief is not None
        self.phase("phase 0")
        message = self.brief.phase0(mode=self.docket.mode, goal=self.goal, project=self.project)
        batch = [(agent, message) for agent in self.docket.roster]
        self.emit("phase0_start", seats=len(batch))
        results = await self.port.ask_seats(batch, round_=0, phase="phase0", classify_unusable=True)
        valid: list[str] = []
        for agent in self.docket.roster:
            result = results.get(agent) or RawAsk(failure="no answer")
            if result.failure:
                raise Abort(result.failure, member=agent, round_=0)
            parsed = parse_phase0(result.text or "", mode=self.docket.mode)
            if parsed.invalid or parsed.errors:
                self.emit("phase0_rejected", member=agent, reason="; ".join(parsed.errors) or "no JSON object")
                continue
            valid.append(agent)
            if self.docket.mode == "plan":
                plan = parsed.data.get("plan") or {}
                self.phase0_counts[agent] = 1
                self.docket.phase0_counts[agent] = 1
                self.docket.approaches[agent] = Approach(
                    agent=agent,
                    original=plan.get("text", ""),
                    stages=plan.get("stages") or [],
                    rejected_alternatives=plan.get("rejected_alternatives") or [],
                    risks=plan.get("risks") or [],
                    constraints=plan.get("constraints") or [],
                )
            else:
                findings = parsed.data.get("findings") or []
                self.phase0_counts[agent] = len(findings)
                self.docket.phase0_counts[agent] = len(findings)
                for entry in findings:
                    self.add_item(entry, agent, 0)
            for question in parsed.data.get("open_questions") or []:
                raise_question(self.docket, text=question, asker=agent, item_id=None, round_=0)
            for entry in parsed.data.get("human_input") or []:
                if entry.get("question"):
                    question, _ = raise_question(
                        self.docket,
                        text=entry["question"],
                        asker=agent,
                        item_id=None,
                        round_=0,
                        context=entry.get("context", ""),
                        options=entry.get("options"),
                    )
                    if entry.get("blocking") and ask_to_stop(self.docket, question, agent):
                        self.emit("question_stop_requested", id=question.id, seat=agent, holders=list(question.blocked_by), round=0)
            self._notes(parsed.data.get("notes") or [], agent, 0)
            self._phase0_summary[agent] = parsed.data.get("summary", "")
        if len(valid) < 2:
            raise Abort(
                f"phase 0 returned {len(valid)} valid {self.adapter.phase0_schema} message(s); fewer than 2 is not a council", round_=0
            )
        self.emit("phase0_done", valid=len(valid), items=len(self.docket.order))
        return valid

    async def publish_review(self) -> None:
        self.phase("the chair's merge")
        if not self.docket.order:
            self.emit("chair_merge_skipped", reason="nothing to merge")
            return
        await self._merge_pass(round_=0, ids=list(self.docket.order))

    async def publish_plan(self) -> None:
        self.phase("the chair's comparison")
        listing = self._approach_listing()
        comparison = await self.port.chair_compare(listing, round_=0)
        self.docket.agreed_points = comparison.get("agreed") or []
        for conflict in comparison.get("conflicts") or []:
            item = Item(
                id=self.new_id(),
                claim=conflict.get("claim", "")[:600],
                detail="; ".join(f"{p.get('agent')}: {p.get('claim')}" for p in conflict.get("positions") or []),
                severity="major",
                raised_by=[CHAIR_ID],
                opened_round=0,
            )
            self.docket.add_item(item)
        for gap_text in comparison.get("uncovered") or []:
            self.docket.add_item(Item(id=self.new_id(), claim=gap_text[:600], raised_by=[CHAIR_ID], opened_round=0))
        self.emit(
            "chair_compare",
            agreed=len(self.docket.agreed_points),
            conflicts=len(comparison.get("conflicts") or []),
            uncovered=len(comparison.get("uncovered") or []),
        )

    def _approach_listing(self) -> str:
        blocks = []
        for agent in self.docket.roster:
            approach = self.docket.approaches.get(agent)
            if approach is None:
                continue
            stages = " → ".join(_stage_name(stage) for stage in approach.stages)
            blocks.append(f"### {agent}\n\n{approach.original}" + (f"\n\nStages: {stages}" if stages else ""))
        return "\n\n".join(blocks)

    async def _direction_ask(self, round_: int) -> None:
        from ..render.text import direction_label

        if self.docket.mode != "plan" or self.docket.direction_ask() is not None:
            return
        camps = self.docket.camps()
        if len(camps) < 2 or camps != self._camps_seen:
            self._camps_seen, self._camps_held = camps, 1 if camps else 0
            return
        self._camps_held += 1
        if not self._stale and self._camps_held < _SPLIT_ROUNDS and round_ < self._ceiling:
            return
        split = self.docket.directions()
        held = ", ".join(direction_label(token, self.names) for token in split)
        self.phase("the chair wording the council's split")
        wording = await self._direction_wording(split, round_)
        question = raise_direction_ask(
            self.docket,
            text=wording.get("question") or "The seats will not converge on one direction: which do you want?",
            context=wording.get("context") or f"{self._camps_held} rounds: {held} still standing, no camp changed.",
            options=[self._direction_option(token) for token in split],
            round_=round_,
        )
        self._recheck_class(question, round_)
        self.emit(
            "direction_ask",
            id=question.id,
            directions=list(split),
            round=round_,
            camps_held=self._camps_held,
            phrased_by="chair" if wording.get("question") else "docket",
        )

    async def _direction_wording(self, split: tuple[str, ...], round_: int) -> dict:
        try:
            wording = await self.port.chair_direction(self._split_listing(split), round_=round_)
        except Abort as failure:
            self.emit("chair_direction_unreadable", round=round_, reason=str(failure))
            return {}
        return {key: str(wording.get(key) or "")[:600] for key in ("question", "context")}

    def _split_listing(self, split: tuple[str, ...]) -> str:
        blocks = []
        for token in split:
            lines = [f"### {token}"]
            for agent in self.docket.roster:
                if self.docket.backing.get(agent) != token:
                    continue
                approach = self.docket.approaches.get(agent)
                artifact = approach.artifact() if approach else {}
                text = _plan_text(artifact) or "(no plan text)"
                lines.append(f"- {agent}, backing `{artifact.get('name') or 'the original plan'}`:\n{text}")
            blocks.append("\n\n".join(lines))
        return "\n\n".join(blocks)

    def _direction_plan(self, token: str) -> dict:
        if not token.startswith(MERGED):
            approach = self.docket.approaches.get(token)
            return approach.artifact() if approach else {}
        label = token.partition(MERGED)[2].strip()
        fallback: dict = {}
        for agent in self.docket.roster:
            approach = self.docket.approaches.get(agent)
            if approach is None or self.docket.backing.get(agent) != token:
                continue
            for revision in approach.revisions:
                if revision.get("name") == label:
                    return revision
            fallback = fallback or approach.artifact()
        return fallback

    def _direction_option(self, token: str) -> str:
        plan = self._direction_plan(token)
        states = [_one_line(state) for state in _stage_states(plan)]
        tail = " → ".join(states[:_MENU_STAGES]) + (" → …" if len(states) > _MENU_STAGES else "")
        claim = _one_line(plan.get("text") or "")
        if claim:
            return f"{token}: {claim}" + (f" → {tail}" if tail else "")
        return f"{token}: {tail}" if tail else token

    async def run_round(self, round_: int) -> None:
        self.docket.round = round_
        await self._listen(round_)
        self.asks_used = {}
        self.phase("verdicts")
        self.shown = {agent: [item.id for item in self.docket.open_items()] for agent in self.docket.roster}
        batch = []
        for agent in self.docket.roster:
            docket_text = self.brief.docket(self.docket, agent)
            batch.append(
                (
                    agent,
                    self.brief.round(
                        mode=self.docket.mode,
                        goal=self.goal,
                        project=self.project,
                        me=agent,
                        docket=docket_text,
                        phase0_summary=self._phase0_summary.get(agent, ""),
                    ),
                )
            )
        results = await self.port.ask_seats(batch, round_=round_)
        rejected: list[str] = []
        for agent in self.docket.roster:
            result = results.get(agent) or RawAsk(failure="no answer")
            if result.failure:
                raise Abort(result.failure, member=agent, round_=round_)
            if result.rejected:
                rejected.append(agent)
                continue
            self._apply_verdict(agent, result.text or "", round_)
        await self._merge_new_items(round_)
        self.phase("the gap ladder")
        await self._gap_ladder(round_, rejected)
        self.phase("the challenge pass")
        await self._challenge_pass(round_)
        self._read_insists(seen=round_ - 1)
        await self._merge_asks(round_)
        self._closure(round_)
        self._summarize(round_)
        current = signature(self.docket)
        self._stale = current == self.previous_signature and round_ > 1
        self.previous_signature = current

    def _apply_verdict(self, agent: str, text: str, round_: int) -> None:
        known = set(self.docket.items)
        closed = {i for i in known if self.docket.items[i].closed}
        parsed = parse_verdict(text, mode=self.docket.mode, me=agent, known_items=known, closed_items=closed)
        data = parsed.data
        for raw_id in data.get("unknown_items") or []:
            self.emit("verdict_unknown_findings", member=agent, id=raw_id, round=round_)
        for position in data.get("positions") or []:
            item = self.docket.items.get(position["item"])
            if item is None:
                continue
            self._stance(
                item_id=item.id,
                agent=agent,
                stance=position["stance"],
                note=position.get("note", ""),
                evidence=position.get("evidence", ""),
                round_=round_,
            )
        for entry in data.get("new_items") or []:
            self.add_item(entry, agent, round_)
        self._answers(data.get("answers") or [], agent, round_)
        self._questions(data.get("questions") or [], agent, round_)
        self._human_input(data.get("human_input") or [], agent, round_)
        # before the raises: a retraction cannot name an id this same reply is minting
        self._retract_notes(data.get("retract_note") or [], agent, round_)
        self._notes(data.get("notes") or [], agent, round_)
        self._apply_asks(agent, data, round_)
        for question in data.get("open_questions") or []:
            raise_question(self.docket, text=question, asker=agent, item_id=None, round_=round_)
        if self.docket.mode == "plan":
            self._apply_plan_fields(agent, data, round_)
        if data.get("signoff"):
            self.docket.signoffs[agent] = round_

    def _apply_asks(self, agent: str, data: dict, round_: int) -> None:
        """What a verdict says about the council's asks for the human: back one, answer one, drop one."""
        for qid in data.get("second") or []:
            question, reason = second(self.docket, qid=qid, agent=agent, round_=round_)
            if question is None or reason:
                self.emit("verdict_ask_refused", member=agent, id=qid, field="second", round=round_, reason=reason or "no such question")
                continue
            if showable(self.docket, question):
                self.emit("question_seconded", id=question.id, seat=agent, askers=list(question.askers), round=round_)
                self._recheck_class(question, round_)
                continue
            self.emit(
                "question_seconded_but_held_back",
                id=question.id,
                seat=agent,
                askers=list(question.askers),
                held=held_back(self.docket, question),
                answered_by=sorted(settlements(question)),
                round=round_,
            )
        for qid in data.get("blocking_for") or []:
            question, reason = stop_for(self.docket, qid=qid, agent=agent, round_=round_)
            if question is None or reason:
                self.emit(
                    "verdict_ask_refused", member=agent, id=qid, field="blocking_for", round=round_, reason=reason or "no such question"
                )
                continue
            self.emit("question_stop_requested", id=question.id, seat=agent, holders=list(question.blocked_by), round=round_)
            self._recheck_class(question, round_)
        for entry in data.get("answer_question") or []:
            question, reason = answer_by_seat(self.docket, qid=entry["id"], agent=agent, text=entry["text"], round_=round_)
            if question is None or reason:
                self.emit("verdict_ask_refused", member=agent, id=entry["id"], field="answer_question", round=round_, reason=reason)
                continue
            self.emit("question_answered_by_seat", id=question.id, seat=agent, round=round_)
            self._recheck_class(question, round_)
        for qid in data.get("retract") or []:
            question, reason = retract(self.docket, qid=qid, agent=agent, round_=round_)
            if question is None or reason:
                self.emit("verdict_ask_refused", member=agent, id=qid, field="retract", round=round_, reason=reason)
                continue
            self.emit(
                "question_retracted",
                id=question.id,
                seat=agent,
                round=round_,
                state=question.state,
                still_asking=list(question.askers),
            )
            if question.item_id:
                # only a stance lets go of an item, and retract is not one
                for one in self.docket.questions_for_item(question.item_id):
                    self._recheck_class(one, round_)
            else:
                self._recheck_class(question, round_)
        for qid in data.get("insist") or []:
            # queued: an insistence reads the docket once every answer this pass carries has landed
            self._insists.append((agent, qid, round_))

    def _read_insists(self, *, seen: int) -> None:
        """Read the insists a pass queued, once every answer it carries is on the docket.

        Draining here rather than where the verdict was applied keeps the order the engine applied the seats from
        deciding what an insistence was aimed at. `seen` is the latest round of peer answers those seats were shown:
        the last pass shows them the answers it carries, a round does not show them its own.
        """
        pending, self._insists = self._insists, []
        for agent, qid, round_ in pending:
            question, reason = insist(self.docket, qid=qid, agent=agent, round_=round_, seen=seen)
            if question is None or reason:
                self.emit("verdict_ask_refused", member=agent, id=qid, field="insist", round=round_, reason=reason or "no such question")
                continue
            self.emit("question_insisted", id=question.id, seat=agent, round=round_)
            self._recheck_class(question, round_)

    def _apply_plan_fields(self, agent: str, data: dict, round_: int) -> None:
        revised = data.get("revised_plan")
        if revised:
            approach = self.docket.approaches.get(agent) or Approach(agent=agent)
            self.docket.approaches[agent] = approach
            approach.revisions.append(
                {
                    "name": revised.get("name") or f"{agent}-{round_}",
                    "round": round_,
                    "text": revised.get("text", ""),
                    "stages": revised.get("stages") or [],
                }
            )
        token = data.get("backing")
        if token:
            self.docket.backing[agent] = token
        elif "backing" in data and data["backing"] is None:
            self.emit(
                "verdict_unknown_findings",
                member=agent,
                id="backing",
                round=round_,
                reason="the backing token did not map to one of the three",
            )

    def _answers(self, entries: list[dict], agent: str, round_: int) -> None:
        for entry in entries:
            target = entry.get("to") or ""
            qid = (entry.get("question") or "").strip()
            qa = self.docket.qa.get(qid)
            if qa is None and target:
                qa = self._latest_unanswered(answered_by=agent, addressed_to=target)
            if qa is None:
                self.emit("qa_answer_unmatched", member=agent, question=qid, round=round_)
                continue
            qa.answer = entry.get("text") or entry.get("answer") or ""
            qa.answered_round = round_

    def _latest_unanswered(self, *, answered_by: str, addressed_to: str):
        matches = [qa for qa in self.docket.qa.values() if qa.asker == addressed_to and qa.to == answered_by and not qa.answer]
        return max(matches, key=lambda qa: qa.round, default=None)

    def _questions(self, entries: list[dict], agent: str, round_: int) -> None:
        for entry in entries:
            target = entry.get("to") or ""
            if target not in self.docket.roster:
                self.emit("qa_question_unmatched", member=agent, to=target, round=round_)
                continue
            qa = QA(
                id=f"Q{self.docket.next_qa}", asker=agent, to=target, text=entry.get("text") or entry.get("question") or "", round=round_
            )
            self.docket.next_qa += 1
            self.docket.qa[qa.id] = qa
            self.docket.qa_order.append(qa.id)

    def _notes(self, entries: list[dict], asker: str, round_: int) -> None:
        """An unknown item costs the link, never the note."""
        for entry in entries:
            item_id = entry.get("item")
            if item_id and item_id not in self.docket.items:
                self.emit("verdict_unknown_findings", member=asker, id=item_id, round=round_, reason="a note named an unknown item")
                item_id = None
            note, updated = raise_note(self.docket, by=asker, text=entry["text"], item_id=item_id, round_=round_)
            self.emit(
                "note_updated" if updated else "note_raised",
                id=note.id,
                seat=asker,
                item=note.item_id,
                round=round_,
                chars=len(note.text),
            )

    def _retract_notes(self, entries: list[dict], asker: str, round_: int) -> None:
        for entry in entries:
            nid, why = entry["id"], entry["reason"]
            note, refusal = retract_note(self.docket, nid=nid, agent=asker, round_=round_, reason=why)
            if note is None or refusal:
                self.emit("note_retract_refused", member=asker, id=nid, round=round_, reason=refusal or "no standing note with that id")
                continue
            self.emit("note_retracted", id=nid, seat=asker, round=round_, shown=note.shown_round is not None, reason=why)

    def _human_input(self, entries: list[dict], asker: str, round_: int) -> None:
        for entry in entries:
            item_id = entry.get("item")
            if item_id and item_id not in self.docket.items:
                self.emit(
                    "verdict_unknown_findings", member=asker, id=item_id, round=round_, reason="a human_input entry named an unknown item"
                )
                item_id = None
            if item_id and self.docket.items[item_id].closed:
                # the closure is the council's answer about that item; a question left over is a question of its own
                self.emit(
                    "verdict_ask_refused",
                    member=asker,
                    id=item_id,
                    field="human_input",
                    round=round_,
                    reason=f"{item_id} is settled as {self.docket.items[item_id].disposition}: ask over no item what the closure left "
                    "open, or stance the item again to reopen it",
                )
                item_id = None
            question, updated = self._re_or_raise(asker, item_id=item_id, entry=entry, round_=round_)
            if updated:
                self.emit("question_updated", id=question.id, asker=asker, round=round_)
            if item_id:
                # only a needs_human stance holds the item
                escalation = self.docket.escalations.get((item_id, asker))
                if escalation is not None and escalation.state == STANDING:
                    escalation.question_id = question.id
            if entry.get("blocking") and ask_to_stop(self.docket, question, asker):
                self.emit("question_stop_requested", id=question.id, seat=asker, holders=list(question.blocked_by), round=round_)
            self._recheck_class(question, round_)

    def _re_or_raise(self, asker: str, *, item_id: str | None, entry: dict, round_: int):
        fields = {
            "text": entry["question"],
            "context": entry.get("context", ""),
            "options": entry.get("options"),
        }
        owned = self._owned_open_question(item_id, asker)
        if owned is not None:
            return owned, refresh_question(owned, asker=asker, item_id=item_id, round_=round_, **fields)
        return raise_question(self.docket, asker=asker, item_id=item_id, round_=round_, **fields)

    def _owned_open_question(self, item_id: str | None, asker: str):
        if not item_id:
            return None
        escalation = self.docket.escalations.get((item_id, asker))
        if escalation is None or escalation.state != STANDING or not escalation.question_id:
            return None
        question = self.docket.questions.get(escalation.question_id)
        if question is None or question.state != ASKED or asker not in question.askers:
            return None
        return question

    def _stance(self, *, item_id: str, agent: str, stance: str, note: str = "", evidence: str = "", round_: int) -> None:
        for event, fields in record_stance(
            self.docket, item_id=item_id, agent=agent, stance=stance, note=note, evidence=evidence, round_=round_
        ):
            self.emit(event, **fields)
        for question in self.docket.questions_for_item(item_id):
            self._recheck_class(question, round_)

    def _recheck_class(self, question, round_: int) -> None:
        now = effective_class(self.docket, question)
        before = self._class_of.get(question.id)
        self._class_of[question.id] = now
        if before and before != now:
            self.emit("question_effective_class", id=question.id, prior=before, effective=now, round=round_)

    async def _gap_ladder(self, round_: int, rejected: list[str]) -> None:
        """Ask each seat for what it owes until its cause's budget runs out, then record the rest as declined."""
        pending: dict[str, tuple[list[str], str]] = {}
        for agent in self.docket.roster:
            missing = self._still_missing(agent)
            if missing:
                cause = gap_rules.MECHANICAL if agent in rejected or _partial(self.docket, agent, round_) else gap_rules.DELIBERATE
                pending[agent] = (missing, cause)
        for _ in range(max(gap_rules.CEILING.values())):
            if not pending:
                return
            batch, plan = [], {}
            for agent, (missing, cause) in pending.items():
                groups = gap_rules.ladder_plan(missing=missing, cause=cause, asks_used=self.asks_used.get(agent, 0))
                if not groups:
                    continue  # out of budget: declined with the rest below
                chunk = groups[0]
                plan[agent] = chunk
                batch.append(
                    (
                        agent,
                        self.brief.gap(
                            chunk, deliberate=cause == gap_rules.DELIBERATE, reason="the round just closed and these are still unanswered"
                        ),
                    )
                )
            if not batch:
                break
            results = await self.port.ask_seats(batch, round_=round_, phase="gap")
            for agent in plan:
                cause = pending[agent][1]
                result = results.get(agent) or RawAsk(failure="no answer")
                if result.failure:
                    raise Abort(result.failure, member=agent, round_=round_)
                self.asks_used[agent] = self.asks_used.get(agent, 0) + 1
                landed = self._apply_gap(agent, plan[agent], result, round_)
                still = self._still_missing(agent)
                if not still:
                    del pending[agent]
                else:
                    # an answer that landed, or one that never parsed, is a seat still trying: its cause holds
                    pending[agent] = (still, cause if landed or result.rejected else gap_rules.DELIBERATE)
        for agent, (missing, _cause) in pending.items():
            self._decline(agent, missing, round_)

    def _still_missing(self, agent: str) -> list[str]:
        """What this seat owes: no stance on an item it was shown, and in plan mode no backing."""
        missing = gap_rules.missing_items(self.docket, agent, self.shown.get(agent, []))
        if gap_rules.missing_backing(self.docket, agent):
            missing.append("backing")
        return missing

    def _apply_gap(self, agent: str, chunk: list[str], result: RawAsk, round_: int) -> int:
        """Record what the gap answer said, and return how much of it landed."""
        if result.rejected:
            return 0
        data = parse_verdict(
            result.text or "",
            mode=self.docket.mode,
            me=agent,
            known_items=set(self.docket.items),
            closed_items={i for i in self.docket.items if self.docket.items[i].closed},
        ).data
        asked_items = [c for c in chunk if c != "backing"]
        landed = 0
        for position in data.get("positions") or []:
            if position["item"] not in asked_items:
                continue
            self._stance(
                item_id=position["item"],
                agent=agent,
                stance=position["stance"],
                note=position.get("note", ""),
                evidence=position.get("evidence", ""),
                round_=round_,
            )
            landed += 1
            # A gap answer may withdraw a sign-off, never re-grant it.
            if self.docket.signoffs.get(agent) == round_ and position["item"] in self.shown.get(agent, []):
                del self.docket.signoffs[agent]
                self.emit("signoff_revoked", member=agent, round=round_, item=position["item"])
        if "backing" in chunk:
            token = data.get("backing")
            if token:
                self.docket.backing[agent] = token
                landed += 1
        self._apply_asks(agent, data, round_)
        return landed

    def _decline(self, agent: str, missing: list[str], round_: int) -> None:
        items = [entry for entry in missing if entry != "backing"]
        for item_id in items:
            self.docket.stances.setdefault(item_id, {})[agent] = Stance(stance=ABSTAIN, round=round_, declined=True)
        if "backing" in missing:
            self.docket.backing[agent] = DECLINED
        self.emit("gap_declined", member=agent, round=round_, items=missing)

    async def _challenge_pass(self, round_: int) -> None:
        for (item_id, asker), escalation in list(self.docket.escalations.items()):
            if escalation.state != STANDING:
                continue
            fresh = [c for c in challenges(self.docket, item_id, round_, asker) if c["round"] == round_]
            if not fresh:
                continue
            escalation.challenges.extend(fresh)
            self.emit("escalation_challenged", item=item_id, asker=asker, by=[c["by"] for c in fresh], round=round_)
            question = self.docket.questions.get(escalation.question_id)
            text = question.text if question else ""
            result = await self.port.ask_seat(asker, self.brief.defense(item_id, fresh[0], text), round_=round_, phase="defense")
            if result.failure:
                raise Abort(result.failure, member=asker, round_=round_)
            defended = self._apply_defense(item_id, asker, result, round_)
            if defended is None:
                continue
            if escalation.state != STANDING:
                # the defense restanced the item: the escalation is withdrawn, not demoted
                continue
            escalation.defense, escalation.defended_round = defended, round_
            question = self.docket.questions.get(escalation.question_id)
            if _answers_challenge(defended, question.text if question else ""):
                self.emit("escalation_defended", item=item_id, asker=asker, round=round_)
                continue
            demote(escalation, round_)
            escalation.undefended = True
            release_stops(self.docket, item_id=item_id, agent=asker)
            self._demoted_this_round.add((item_id, asker))
            self.emit("escalation_demoted", item=item_id, asker=asker, round=round_)
            for event, fields in drop_ask(self.docket, escalation, asker, round_):
                self.emit(event, **fields)

    def _apply_defense(self, item_id: str, asker: str, result: RawAsk, round_: int) -> str | None:
        if result.rejected:
            return ""
        data = parse_verdict(
            result.text or "",
            mode=self.docket.mode,
            me=asker,
            known_items=set(self.docket.items),
            closed_items={i for i in self.docket.items if self.docket.items[i].closed},
        ).data
        note, evidence, restated = "", "", False
        for position in data.get("positions") or []:
            if position["item"] != item_id:
                continue
            restated = position["stance"] == NEEDS_HUMAN
            note, evidence = position.get("note", ""), position.get("evidence", "")
            self._stance(
                item_id=item_id,
                agent=asker,
                stance=position["stance"],
                note=note,
                evidence=evidence,
                round_=round_,
            )
        for entry in data.get("human_input") or []:
            if entry.get("item") == item_id and entry.get("question"):
                question, updated = self._re_or_raise(asker, item_id=item_id, entry=entry, round_=round_)
                escalation = self.docket.escalations.get((item_id, asker))
                # only a standing hold owns an ask: a defense that released the item must not adopt the ask it just raised
                if escalation is not None and escalation.state == STANDING:
                    escalation.question_id = question.id
                if updated:
                    self.emit("question_updated", id=question.id, asker=asker, round=round_)
        self._apply_asks(asker, data, round_)
        if not restated:
            return ""
        if (item_id, asker) in self._demoted_this_round:
            return ""
        return (note + (" | " + evidence if evidence else "")).strip()

    async def _merge_new_items(self, round_: int) -> None:
        self.phase("the chair's merge of what the round raised")
        fresh = [i for i in self.docket.order if not self.docket.items[i].closed and self.docket.items[i].opened_round == round_]
        if not fresh:
            return
        await self._merge_pass(round_=round_, ids=fresh)

    async def _merge_pass(self, round_: int, ids: list[str]) -> None:
        listing = "\n".join(_merge_entry(self.docket.items[i], self.adapter.detail_label) for i in ids)
        merged = await self.port.chair_merge(listing, round_=round_)
        applied, reason = self._apply_merge(merged.get("groups") or [])
        if applied is None:
            self.merge_refusals += 1
            self.emit("merge_refused", round=round_, reason=reason, items=len(ids))
            self.emit_note(
                f"merge plan refused ({reason}): the docket stays unmerged ({len(ids)} {self.adapter.item_noun_plural} printed as raised)"
            )
            return
        self.emit("chair_merge", round=round_, groups=applied)

    def _apply_merge(self, groups: list[dict]) -> tuple[int | None, str]:
        seen: set[str] = set()
        usable = []
        for group in groups:
            canonical, duplicates = group["canonical"], group["duplicates"]
            if canonical in seen or any(d in seen for d in duplicates):
                return None, f"{canonical} is reused across groups"
            seen.add(canonical)
            seen.update(d for d in duplicates if d != canonical)
            if canonical not in self.docket.items:
                continue  # unknown IDs are skipped
            if self.docket.items[canonical].closed:
                continue  # a merge may not widen a claim the council settled
            keep = []
            for duplicate in duplicates:
                item = self.docket.items.get(duplicate)
                if item is None or item.closed or duplicate == canonical:
                    continue
                if self.docket.stances.get(duplicate):
                    continue  # merging would discard verdicts
                keep.append(duplicate)
            if keep:
                usable.append((canonical, keep))
        for canonical, duplicates in usable:
            target = self.docket.items[canonical]
            for duplicate in duplicates:
                source = self.docket.items.pop(duplicate)
                self.docket.order.remove(duplicate)
                self.docket.stances.pop(duplicate, None)
                for agent in source.raised_by:
                    if agent not in target.raised_by:
                        target.raised_by.append(agent)
                    if agent not in target.also_reported_by:
                        target.also_reported_by.append(agent)
                for escalation_key in [k for k in self.docket.escalations if k[0] == duplicate]:
                    del self.docket.escalations[escalation_key]
        return len(usable), ""

    def _summarize(self, round_: int) -> None:
        states = {i: derive(self.docket, i) for i in self.docket.order if not self.docket.items[i].closed}
        counts: dict[str, int] = {}
        for state in states.values():
            counts[state] = counts.get(state, 0) + 1
        signed = len(self.docket.signoffs)
        summary = (
            f"round {round_}: {signed}/{self.docket.n} signed off · "
            + ", ".join(f"{count} {state}" for state, count in sorted(counts.items()))
            or f"round {round_}: {signed}/{self.docket.n} signed off"
        )
        record = RoundRecord(round=round_, summary=summary)
        for agent in self.docket.roster:
            backing = self.docket.backing.get(agent, "—") if self.docket.mode == "plan" else "n/a"
            signoff = "yes" if self.docket.signoffs.get(agent) == round_ else "no"
            shown = len(self.shown.get(agent, []))
            record.lines.append((agent, f"stance {shown} · backing {backing} · signoff {signoff}"))
        self.docket.rounds.append(record)
        self.emit("round_summary", round=round_, signed_off=signed, states=counts)

    def emit_note(self, text: str) -> None:
        self.emit("console_note", text=text)

    async def run(self) -> str:
        outcome = await self._deliberate()
        # the report's pass: close what the last passes settled, reopen nothing
        self._closure(self.docket.round, reopen=False)
        return outcome

    def _closure(self, round_: int, *, reopen: bool = True) -> None:
        """Close what the seats settled, and moot the deferred asks that asked about an item they settled."""
        for event, fields in closure_pass(self.docket, round_, reopen=reopen):
            self.emit(event, **fields)
            if event == "item_closed":
                for moot, detail in moot_item_asks(self.docket, item_id=fields["id"], disposition=fields["disposition"], round_=round_):
                    self.emit(moot, **detail)

    async def _deliberate(self) -> str:
        await self.phase0()
        if self.docket.mode == "plan":
            await self.publish_plan()
        else:
            await self.publish_review()
        await self._merge_asks(round_=0)
        self.docket.round = 1
        await self._checkpoint(round_=0, terminal=False)
        round_ = 1
        while round_ <= self._ceiling:
            await self.run_round(round_)
            outcome = await self._after_round(round_)
            if outcome != GO:
                return outcome
            round_ += 1
        return ROUND_LIMIT

    async def _after_round(self, round_: int) -> str:
        await self._listen(round_)
        await self._direction_ask(round_)
        provisional, _ = evaluate(self.docket)
        stale = getattr(self, "_stale", False) and not self.checkpoint_moved
        terminal = provisional != GO or stale or round_ >= self._ceiling
        if terminal:
            settled = signature(self.docket)
            await self._peer_pass(round_)
            # what that pass lets go of is the last movement the run may see, so read the end again
            if (moved := signature(self.docket)) != settled:
                self.previous_signature = moved
                stale = False
            provisional, _ = evaluate(self.docket)
        outcome = await self._checkpoint(round_=round_, terminal=terminal, provisional=provisional, stale=stale)
        if outcome is not None:
            return outcome
        if self.checkpoint_moved:
            self._ceiling = max(self._ceiling, round_ + 1)
            self.previous_signature = signature(self.docket)
            self.checkpoint_moved = False
            self.emit("round_continue", round=round_, reason="an answer buys a full round")
            return GO
        action, reason = evaluate(self.docket)
        if action != GO:
            self.emit("termination", outcome=action, reason=reason, round=round_)
            return action
        if stale:
            self.emit("termination", outcome=STALEMATE, reason="two consecutive rounds with no movement", round=round_)
            return STALEMATE
        if round_ >= self._ceiling:
            unasked = self._mark_unasked()
            self.emit("termination", outcome=ROUND_LIMIT, reason=f"round limit ({self._ceiling}) reached", round=round_, unasked=unasked)
            return ROUND_LIMIT
        self.emit("round_continue", round=round_, reason=reason)
        return GO

    def _open_asks(self) -> list:
        """The asks a seat raised and that are still open — everything the chair can fold."""
        return [q for q in self.docket.questions.values() if q.state == ASKED and not q.direction_ask and q.askers]

    async def _merge_asks(self, round_: int) -> None:
        """The chair folds the asks that want one decision; a chair that cannot read this pass costs the run nothing.

        An ask set the chair has already read is not read again, so a fold costs nothing while nothing moves. A
        refusal or an unreadable chair leaves the next round armed.
        """
        asks = self._open_asks()
        if len(asks) < 2 or len({agent for q in asks for agent in q.askers}) < 2:
            return
        if _ask_signature(asks) == self._folded_asks:
            return
        self.phase("the chair folding the asks")
        try:
            merged = await self.port.chair_merge_questions(self._ask_listing(asks), round_=round_)
        except Abort as failure:
            self.emit("chair_ask_merge_unreadable", round=round_, reason=str(failure), asks=len(asks))
            return
        events, reason = merge_asks(self.docket, groups=merged.get("groups") or [], round_=round_)
        if reason:
            self.emit("chair_ask_merge_refused", round=round_, reason=reason, asks=len(asks))
            return
        for event, fields in events:
            self.emit(event, **fields)
        for question in asks:
            self._recheck_class(question, round_)
        self._folded_asks = _ask_signature(self._open_asks())

    async def _peer_pass(self, round_: int) -> None:
        """Once, before the run ends: every seat gets one last look at the asks the human will not be shown.

        It lets go — answer one, back one, drop one's own, or stance away from an item a peer settled — and it lets
        the seat that raised an ask say a peer's answer was not enough. Notes and their retractions are read here
        too. Nothing else in that reply is read.
        """
        from .termination import held_back_questions

        if self._peer_pass_done:
            return
        weighing = held_back_questions(self.docket)
        if not weighing:
            return
        self._peer_pass_done = True
        self.phase("the last look at the asks before you are asked")
        self.emit("peer_pass_start", round=round_, asks=[q.id for q in weighing])
        message = self.brief.peer_pass(self._ask_listing(weighing))
        results = await self.port.ask_seats([(agent, message) for agent in self.docket.roster], round_=round_, phase="seconding")
        known = set(self.docket.items)
        closed = {i for i in known if self.docket.items[i].closed}
        replies: dict[str, dict] = {}
        for agent in self.docket.roster:
            result = results.get(agent) or RawAsk()
            # a seat that fails this pass simply says nothing
            if result.failure or result.rejected:
                continue
            replies[agent] = parse_verdict(result.text or "", mode=self.docket.mode, me=agent, known_items=known, closed_items=closed).data
        # every seat's answers and seconds land before any insistence is read, and releases go last: a releasing
        # stance takes its own stop and ask with it. One loop over the roster would let that order decide what a
        # peer's reply means.
        for agent, data in replies.items():
            self._retract_notes(data.get("retract_note") or [], agent, round_)
            self._notes(data.get("notes") or [], agent, round_)
            self._apply_asks(agent, data, round_)
        self._read_insists(seen=round_)
        for agent, data in replies.items():
            self._release_holds(agent, data, round_)
        self._closure(round_, reopen=False)

    def _release_holds(self, agent: str, data: dict, round_: int) -> None:
        """One kind of stance is read on that pass, and one only: a seat letting go of an item it holds."""
        for position in data.get("positions") or []:
            item_id, stance = position["item"], position["stance"]
            if stance == NEEDS_HUMAN:
                reason = "opening or keeping a hold is a round's work: leave the item out of this reply"
            elif self.docket.is_standing(item_id, agent):
                self._stance(
                    item_id=item_id,
                    agent=agent,
                    stance=stance,
                    note=position.get("note", ""),
                    evidence=position.get("evidence", ""),
                    round_=round_,
                )
                self.emit("hold_released", item=item_id, seat=agent, stance=stance, round=round_)
                continue
            elif stance == DISAGREE:
                reason = "that pass releases holds and opens none: an objection belongs in a round"
            else:
                continue  # a seat that holds nothing changes nothing
            self.emit("verdict_ask_refused", member=agent, id=item_id, field="positions", round=round_, reason=reason)

    def _ask_listing(self, questions: list) -> str:
        blocks = []
        for question in questions:
            lines = [f"### {question.id}" + (f" (on {question.item_id})" if question.item_id else ""), question.text]
            if question.context:
                lines.append(f"context: {question.context}")
            lines.append(f"asked by: {', '.join(question.askers) or 'the chair'}")
            if question.raised_by and question.raised_by != question.askers:
                lines.append(f"raised by: {', '.join(question.raised_by)}")
            for seat, said in question.also_asked.items():
                lines.append(f"asked as well by {seat}: {said}")
            for seat, answer in question.peer_answers.items():
                lines.append(f"answered instead of asking: {seat}: {answer}")
            for number, option in enumerate(question.options, start=1):
                lines.append(f"{number}) {option}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _mark_unasked(self) -> list[str]:
        """Label the asks the run ends on, and return the ones the human was never shown."""
        from .termination import unsettled_questions

        never_asked = []
        for question in unsettled_questions(self.docket):
            # a held-back marker already says why the human never saw it
            if question.state != ASKED or any(marker in question.markers for marker in HELD_BACK):
                continue
            # a waiting checkpoint showed it: not the same silence as never asking
            marker = "unanswered" if question.id in self._asked_of_human else "unasked"
            if marker not in question.markers:
                question.markers.append(marker)
            if marker == "unasked":
                never_asked.append(question.id)
        return never_asked

    async def _checkpoint(self, round_: int, *, terminal: bool, provisional: str = "", stale: bool = False):
        from .escalations import effective_class
        from .termination import unsettled_questions

        open_asks = unsettled_questions(self.docket)
        ask = [q for q in open_asks if showable(self.docket, q)]
        if terminal:
            self._mark_held_back([q for q in open_asks if q not in ask], round_)
        listing = ask
        pending_notes = unshown(self.docket)
        if not listing:
            if terminal and len(open_asks) > len(ask):
                self.emit_note(_held_back_note(self.docket, open_asks))
            if pending_notes:
                # nothing to ask, and still the notes' only showing
                self.phase("the notes for you")
                await self.port.checkpoint([], round_=round_, terminal=terminal, listing=[], waiting=False, notes=pending_notes)
                mark_shown(pending_notes, round_)
            self.emit(
                "human_checkpoint",
                round=round_,
                ask_set=[],
                waited=False,
                answered=0,
                skipped=0,
                terminal=terminal,
                notes=[n.id for n, _kind in pending_notes],
            )
            return None
        at_ceiling = round_ >= self._ceiling
        blocking = any(effective_class(self.docket, q) == "blocking" for q in ask)
        # the ceiling asks too, on the grace budget — and an answer already given off-cycle has paid it for this round,
        # which is no reason to stop asking the human about the rest
        waiting = (terminal or blocking) and (not at_ceiling or self._grace_left > 0 or self.checkpoint_moved)
        if terminal and not waiting:
            self._mark_unasked()  # the ledger has to say the run ended without asking
        self.phase(WAITING if waiting else "showing you the asks")
        self.docket.parked_on = [q.id for q in ask] if waiting else []
        try:
            outcome = await self.port.checkpoint(
                ask, round_=round_, terminal=terminal, listing=listing, waiting=waiting, notes=pending_notes
            )
        finally:
            # the park is over the moment the checkpoint is done: a run cut short later must not go on
            # reporting that it waits on an answer, or naming asks it never put to the human
            self.docket.parked_on = []
            self.phase("applying what you said")
        # the block reached the console whether or not the run waited
        mark_shown(pending_notes, round_)
        if outcome.waited:
            self._asked_of_human.update(q.id for q in ask)
        self.emit(
            "human_checkpoint",
            round=round_,
            ask_set=[q.id for q in listing],
            waited=outcome.waited,
            answered=len(outcome.answers),
            skipped=len(outcome.skipped),
            terminal=terminal,
            notes=[n.id for n, _kind in pending_notes],
        )
        self._apply_human(outcome, round_, at_ceiling=at_ceiling)
        # an answer is the one thing that outranks a terminal reading of the docket
        if terminal and provisional != GO and not outcome.any_answer:
            self.emit("termination", outcome=provisional, reason="the checkpoint closed the run", round=round_)
            return provisional
        return None

    async def _listen(self, round_: int) -> None:
        """Answers the human gave while the run was working, applied at a boundary with no ask in flight."""
        self.phase("reading what you typed")
        outcome = await self.port.drain_human()
        if not (outcome.answers or outcome.skipped or outcome.recommendations):
            return
        self.emit(
            "human_answer_off_cycle",
            round=round_,
            answered=sorted(outcome.answers),
            declined=list(outcome.skipped),
            explained=sorted(outcome.recommendations),
        )
        self._apply_human(outcome, round_, at_ceiling=round_ >= self._ceiling)

    def _apply_human(self, outcome: CheckpointOutcome, round_: int, *, at_ceiling: bool) -> None:
        """What the human's dispositions do to the docket: the checkpoint and the off-cycle drain apply them alike."""
        selections: dict[str, int] = {}
        for qid, number in outcome.answer_options.items():
            question = self.docket.questions.get(qid)
            if question is None:
                self.emit(
                    "answer_option_rejected",
                    id=qid,
                    option=number,
                    menu=0,
                    reason="no such question is on the docket, so its option and its answer are both dropped",
                )
                continue
            if not 1 <= number <= len(question.options):
                self.emit(
                    "answer_option_rejected",
                    id=qid,
                    option=number,
                    menu=len(question.options),
                    reason=f"its menu holds {len(question.options)} option(s), so that number was never shown — the answer stands in "
                    "the human's own words and records no menu line",
                )
                continue
            if outcome.answers.get(qid) != question.options[number - 1]:
                self.emit(
                    "answer_option_rejected",
                    id=qid,
                    option=number,
                    menu=len(question.options),
                    reason=f"option {number} is {question.options[number - 1]!r} and the answer is not those words — the answer "
                    "stands in the human's own words and records no menu line",
                )
                continue
            selections[qid] = number
        for qid, answer in outcome.answers.items():
            question = self.docket.questions.get(qid)
            if question is None:
                continue
            question.state = ANSWERED
            question.answer, question.answer_round = answer, round_
            number = selections.get(qid) or 0
            if number:
                question.answer_option = number
                question.answer_option_text = question.options[number - 1]
            self._retire_covered(question, round_, answered=True)
        for qid in outcome.skipped:
            question = self.docket.questions.get(qid)
            if question is None:
                continue
            question.state = SKIPPED
            self._retire_covered(question, round_, answered=False)
        for qid, text in outcome.recommendations.items():
            question = self.docket.questions.get(qid)
            if question is not None:
                question.recommendation, question.recommendation_round = text, round_
        if not outcome.any_answer:
            return
        if at_ceiling and not self.checkpoint_moved:
            if self._grace_left <= 0:
                # the answer is on the docket and in the report; a round past the ceiling is the one thing it cannot buy
                self.emit("answer_buys_no_round", round=round_, answered=len(outcome.answers))
                return
            self._grace_left -= 1
            self.emit(
                "grace_round_bought",
                round=round_ + 1,
                answered=len(outcome.answers),
                grace_left=self._grace_left,
                round_limit=self._ceiling,
            )
        self.checkpoint_moved = True
        self.emit("answer_buys_a_round", round=round_ + 1, answered=len(outcome.answers))

    def _mark_held_back(self, held_asks: list, round_: int) -> None:
        """Mark the asks the human will not be shown; one a seat backs later loses the mark."""
        for question in held_asks:
            marker = held_back(self.docket, question)
            if not marker or marker in question.markers:
                continue
            question.markers.append(marker)
            if marker == UNBACKED:
                self.emit("question_unseconded", id=question.id, askers=list(question.askers), round=round_)
        for question in self.docket.questions.values():
            if showable(self.docket, question):
                question.markers = [m for m in question.markers if m not in HELD_BACK]

    def _retire_covered(self, question, round_: int, *, answered: bool) -> None:
        for (item_id, asker), escalation in list(self.docket.escalations.items()):
            if escalation.question_id != question.id and (item_id != question.item_id or asker not in question.askers):
                continue
            if escalation.state != STANDING:
                continue
            retire(escalation, round_)
            self.emit("escalation_retired", item=item_id, asker=asker, question=question.id, round=round_, answered=answered)


#: rounds the same camps may stand before the split goes to the human
_SPLIT_ROUNDS = 3

#: of a plan's stages, how many reach the human's menu
_MENU_STAGES = 3


def _one_line(text: str, width: int = 90) -> str:
    line = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    return line if len(line) <= width else line[: width - 1] + "…"


def _ask_signature(questions: list) -> tuple:
    """What the chair reads about the asks: a signature it has seen holds nothing new to fold."""
    return tuple((q.id, q.text, q.item_id, tuple(q.askers), tuple(sorted(q.peer_answers))) for q in questions)


def _held_back_note(docket: Docket, held_asks: list) -> str:
    """Why the run ends without asking you anything, said per ask: a seat answered it, no seat backed it, its item settled."""
    answered = [q.id for q in held_asks if held_back(docket, q) == SETTLED]
    unbacked = [q.id for q in held_asks if held_back(docket, q) == UNBACKED]
    moot = [q.id for q in held_asks if held_back(docket, q) == MOOT]
    said = []
    if answered:
        said.append(
            f"{', '.join(answered)}: a seat answered {'these' if len(answered) > 1 else 'this'} from the workspace instead of asking you"
        )
    if unbacked:
        said.append(f"{', '.join(unbacked)}: no second seat backed {'them' if len(unbacked) > 1 else 'it'}, so the council never asked you")
    if moot:
        said.append(f"{', '.join(moot)}: {'those items were' if len(moot) > 1 else 'that item was'} settled among the seats")
    return " · ".join(said) + ". The report lists them."


def _plan_text(artifact: dict) -> str:
    text = (artifact.get("text") or "").strip()
    stages = [f"- stage: {state}" for state in _stage_states(artifact)]
    return "\n".join(([text] if text else []) + stages)


def _stage_states(artifact: dict) -> list[str]:
    return [str(stage.get("state") or "").strip() for stage in artifact.get("stages") or [] if str(stage.get("state") or "").strip()]


def _partial(docket: Docket, agent: str, round_: int) -> bool:
    answered = sum(1 for item_id in [i for i in docket.order] if (r := docket.stance(item_id, agent)) and r.round == round_)
    return 0 < answered < len(docket.open_items())


def _answers_challenge(defense: str, question: str) -> bool:
    said = _tokens(defense)
    return bool(said - _tokens(question))


def _tokens(text: str) -> set[str]:
    return {word for word in "".join(c if c.isalnum() else " " for c in (text or "").casefold()).split() if len(word) > 3}


def _merge_entry(item, detail_label: str) -> str:
    lines = [f"- {item.id}: {item.claim}"]
    if item.detail:
        lines.append(f"    {detail_label}: {item.detail}")
    if item.evidence:
        lines.append(f"    evidence: {item.evidence}")
    return "\n".join(lines)


def _stage_name(stage) -> str:
    if isinstance(stage, dict):
        return str(stage.get("name") or stage.get("title") or "")
    return str(stage)
