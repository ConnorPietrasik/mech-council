from __future__ import annotations

from dataclasses import dataclass, field

from ..extraction import parse_phase0, parse_verdict
from ..modes.base import adapter_for
from . import gaps as gap_rules
from .dispositions import closure_pass, derive
from .escalations import challenges, demote, effective_class, raise_question, record_stance, refresh_question, retire
from .stalemate import signature
from .state import (
    ABSTAIN,
    ANSWERED,
    ASKED,
    CHAIR_ID,
    DECLINED,
    GO,
    NEEDS_HUMAN,
    QA,
    ROUND_LIMIT,
    SKIPPED,
    STALEMATE,
    STANDING,
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
    # `H1: 2` selects an offered option: the answer is that option's own words, and the number records the selection.
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
    ) -> None:
        self.docket = docket
        self.port = port
        self.adapter = adapter_for(docket.mode)
        self.goal = goal
        self.project = project
        self.max_rounds = max_rounds
        self.emit = emit or (lambda name, **fields: None)
        self.brief = brief
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
        self._demoted_this_round: set[tuple[str, str]] = set()
        self._stale = False

    # -- ids and items --------------------------------------------------------

    def new_id(self) -> str:
        while f"{self.adapter.id_prefix}{self._next_item}" in self.docket.items:
            self._next_item += 1
        item_id = f"{self.adapter.id_prefix}{self._next_item}"
        self._next_item += 1
        return item_id

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

    # -- phase 0 --------------------------------------------------------------

    async def phase0(self) -> list[str]:
        assert self.brief is not None
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
                # An `open_questions` entry is an item-less request raised at round 0.
                raise_question(self.docket, text=question, asker=agent, item_id=None, blocking=False, round_=0)
            for entry in parsed.data.get("human_input") or []:
                # Phase 0 raises requests, never escalations: an entry naming an item names one whose finding may not
                # exist yet, so the item is dropped and the request is raised item-less — with the context and the options
                # the brief asked for.  It is item-less only until the same words come back naming an item, which is when
                # it becomes the escalation.
                if entry.get("question"):
                    raise_question(
                        self.docket,
                        text=entry["question"],
                        asker=agent,
                        item_id=None,
                        blocking=False,
                        round_=0,
                        context=entry.get("context", ""),
                        options=entry.get("options"),
                    )
            self._phase0_summary[agent] = parsed.data.get("summary", "")
        if len(valid) < 2:
            raise Abort(
                f"phase 0 returned {len(valid)} valid {self.adapter.phase0_schema} message(s); fewer than 2 is not a council", round_=0
            )
        self.emit("phase0_done", valid=len(valid), items=len(self.docket.order))
        return valid

    # -- publication ----------------------------------------------------------

    async def publish_review(self) -> None:
        if not self.docket.order:
            self.emit("chair_merge_skipped", reason="nothing to merge")
            return
        await self._merge_pass(round_=0, ids=list(self.docket.order))

    async def publish_plan(self) -> None:
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
        self._chair_questions(comparison.get("human_input") or [], CHAIR_ID, 0)
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

    # -- rounds ---------------------------------------------------------------

    async def run_round(self, round_: int) -> None:
        self.docket.round = round_
        self.asks_used = {}
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
        await self._gap_ladder(round_, rejected, mechanical=all(rejected))
        await self._challenge_pass(round_)
        for event, fields in closure_pass(self.docket, round_):
            self.emit(event, **fields)
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
            # A stance that cannot be placed must not vanish.
            self.emit("verdict_unknown_findings", member=agent, id=raw_id, round=round_)
        for position in data.get("positions") or []:
            item = self.docket.items.get(position["item"])
            if item is None:
                continue
            for event, fields in record_stance(
                self.docket,
                item_id=item.id,
                agent=agent,
                stance=position["stance"],
                note=position.get("note", ""),
                evidence=position.get("evidence", ""),
                round_=round_,
            ):
                self.emit(event, **fields)
        for entry in data.get("new_items") or []:
            self.add_item(entry, agent, round_)
        self._answers(data.get("answers") or [], agent, round_)
        self._questions(data.get("questions") or [], agent, round_)
        self._human_input(data.get("human_input") or [], agent, round_)
        for question in data.get("open_questions") or []:
            raise_question(self.docket, text=question, asker=agent, item_id=None, blocking=False, round_=round_)
        if self.docket.mode == "plan":
            self._apply_plan_fields(agent, data, round_)
        if data.get("signoff"):
            self.docket.signoffs[agent] = round_

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

    def _human_input(self, entries: list[dict], asker: str, round_: int) -> None:
        for entry in entries:
            item_id = entry.get("item")
            if item_id and item_id not in self.docket.items:
                self.emit(
                    "verdict_unknown_findings", member=asker, id=item_id, round=round_, reason="a human_input entry named an unknown item"
                )
                item_id = None
            question, updated = self._re_or_raise(asker, item_id=item_id, entry=entry, round_=round_)
            if updated:
                self.emit("question_updated", id=question.id, asker=asker, round=round_)
            if item_id:
                # The escalation and its question are one act: stance the item `needs_human`.
                if self.docket.stated_stance(item_id, asker) != NEEDS_HUMAN:
                    for event, fields in record_stance(
                        self.docket, item_id=item_id, agent=asker, stance=NEEDS_HUMAN, note=entry["question"], round_=round_
                    ):
                        self.emit(event, **fields)
                escalation = self.docket.escalations.get((item_id, asker))
                if escalation is not None:
                    escalation.question_id = question.id
            self._recheck_class(question, round_)

    def _chair_questions(self, entries: list[dict], asker: str, round_: int) -> None:
        for entry in entries:
            question, _ = raise_question(
                self.docket,
                text=entry["question"],
                asker=asker,
                item_id=None,
                blocking=True,
                round_=round_,
                context=entry.get("context", ""),
                options=entry.get("options"),
            )
            self.emit("chair_question", id=question.id, effective="blocking", round=round_)

    def _re_or_raise(self, asker: str, *, item_id: str | None, entry: dict, round_: int):
        fields = {
            "text": entry["question"],
            "context": entry.get("context", ""),
            "options": entry.get("options"),
            "blocking": entry.get("blocking", False),
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

    def _recheck_class(self, question, round_: int) -> None:
        now = effective_class(self.docket, question)
        before = self._class_of.get(question.id)
        self._class_of[question.id] = now
        if before and before != now:
            self.emit("question_effective_class", id=question.id, prior=before, effective=now, round=round_)

    # -- gap ladder -----------------------------------------------------

    async def _gap_ladder(self, round_: int, rejected: list[str], *, mechanical: bool) -> None:
        pending = {}
        for agent in self.docket.roster:
            missing = gap_rules.missing_items(self.docket, agent, self.shown.get(agent, []))
            if gap_rules.missing_backing(self.docket, agent):
                missing = missing + ["backing"]
            if missing:
                cause = (
                    gap_rules.MECHANICAL
                    if (agent in rejected or mechanical or _partial(self.docket, agent, round_))
                    else gap_rules.DELIBERATE
                )
                pending[agent] = (missing, cause)
        for _ in range(gap_rules.LADDER_CEILING):
            if not pending:
                return
            batch, plan = [], {}
            for agent, (missing, cause) in list(pending.items()):
                groups = gap_rules.ladder_plan(
                    missing=missing, missing_backing="backing" in missing, cause=cause, asks_used=self.asks_used.get(agent, 0)
                )
                if not groups:
                    self._decline(agent, missing, round_)
                    del pending[agent]
                    continue
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
                return
            results = await self.port.ask_seats(batch, round_=round_, phase="gap")
            for agent, (missing, cause) in list(pending.items()):
                chunk = plan.get(agent) or []
                result = results.get(agent) or RawAsk(failure="no answer")
                if result.failure:
                    raise Abort(result.failure, member=agent, round_=round_)
                self.asks_used[agent] = self.asks_used.get(agent, 0) + 1
                still = self._apply_gap(agent, chunk, result, round_)
                if still:
                    # A deliberate refusal is answered with one ask, not four.
                    pending[agent] = (still, gap_rules.DELIBERATE)
                else:
                    del pending[agent]

    def _apply_gap(self, agent: str, chunk: list[str], result: RawAsk, round_: int) -> list[str]:
        if result.rejected:
            return list(chunk)
        data = parse_verdict(
            result.text or "",
            mode=self.docket.mode,
            me=agent,
            known_items=set(self.docket.items),
            closed_items={i for i in self.docket.items if self.docket.items[i].closed},
        ).data
        asked_items = [c for c in chunk if c != "backing"]
        for position in data.get("positions") or []:
            if position["item"] not in asked_items:
                continue
            for event, fields in record_stance(
                self.docket,
                item_id=position["item"],
                agent=agent,
                stance=position["stance"],
                note=position.get("note", ""),
                evidence=position.get("evidence", ""),
                round_=round_,
            ):
                self.emit(event, **fields)
            # A gap answer may withdraw a sign-off, never re-grant it.
            if self.docket.signoffs.get(agent) == round_ and position["item"] in self.shown.get(agent, []):
                del self.docket.signoffs[agent]
                self.emit("signoff_revoked", member=agent, round=round_, item=position["item"])
        if "backing" in chunk:
            token = data.get("backing")
            if token:
                self.docket.backing[agent] = token
        still = [
            entry
            for entry in chunk
            if entry == "backing" and not self.docket.backing.get(agent) or entry != "backing" and self.docket.stance(entry, agent) is None
        ]
        return still

    def _decline(self, agent: str, missing: list[str], round_: int) -> None:
        items = [entry for entry in missing if entry != "backing"]
        for item_id in items:
            self.docket.stances.setdefault(item_id, {})[agent] = Stance(stance=ABSTAIN, round=round_, declined=True)
        if "backing" in missing:
            self.docket.backing[agent] = DECLINED
        self.emit("gap_declined", member=agent, round=round_, items=missing)

    # -- challenge / defense ------------------------------------------

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
                # The defense *restanced* the item, so the escalation was already withdrawn.  Nothing is
                # left to defend or demote: overwriting `withdrawn` with `demoted` would report a seat
                # that changed its mind as one that failed to answer, and would bar it from re-escalating.
                continue
            escalation.defense, escalation.defended_round = defended, round_
            question = self.docket.questions.get(escalation.question_id)
            if _answers_challenge(defended, question.text if question else ""):
                self.emit("escalation_defended", item=item_id, asker=asker, round=round_)
                continue
            demote(escalation, round_)
            escalation.undefended = True
            self._demoted_this_round.add((item_id, asker))
            self.emit("escalation_demoted", item=item_id, asker=asker, round=round_)

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
            for event, fields in record_stance(
                self.docket, item_id=item_id, agent=asker, stance=position["stance"], note=note, evidence=evidence, round_=round_
            ):
                self.emit(event, **fields)
        for entry in data.get("human_input") or []:
            if entry.get("item") == item_id and entry.get("question"):
                # A defense is a verdict like any other, so the menu the seat offered rides with it — the schema asks for one, and this is
                # the ask where a seat most wants to offer it.  It is also the ask that *invites* refreshed question text, so it is where
                # forking bites: the refreshed wording updates the escalation's own question instead of opening a second, bare one.
                question, updated = self._re_or_raise(asker, item_id=item_id, entry=entry, round_=round_)
                escalation = self.docket.escalations.get((item_id, asker))
                if escalation is not None:
                    escalation.question_id = question.id
                if updated:
                    self.emit("question_updated", id=question.id, asker=asker, round=round_)
        if not restated:
            return ""
        if (item_id, asker) in self._demoted_this_round:
            return ""
        return (note + (" | " + evidence if evidence else "")).strip()

    # -- merge passes ---------------------------------------------------------

    async def _merge_new_items(self, round_: int) -> None:
        fresh = [i for i in self.docket.order if not self.docket.items[i].closed and self.docket.items[i].opened_round == round_]
        if not fresh:
            return  # the pass runs only in rounds where new items arrived
        await self._merge_pass(round_=round_, ids=fresh)

    async def _merge_pass(self, round_: int, ids: list[str]) -> None:
        listing = "\n".join(_merge_entry(self.docket.items[i], self.adapter.detail_label) for i in ids)
        merged = await self.port.chair_merge(listing, round_=round_)
        self._chair_questions(merged.get("questions") or [], CHAIR_ID, round_)
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

    # -- summary and termination ----------------------------------------------

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

    # -- the whole run ---------------------------------------------------------

    async def run(self) -> str:
        await self.phase0()
        if self.docket.mode == "plan":
            await self.publish_plan()
        else:
            await self.publish_review()
        self.docket.round = 1
        await self._checkpoint(round_=0, terminal=False)
        for round_ in range(1, self.max_rounds + 1):
            await self.run_round(round_)
            outcome = await self._after_round(round_)
            if outcome != GO:
                return outcome
        return ROUND_LIMIT

    async def _after_round(self, round_: int) -> str:
        provisional, _ = evaluate(self.docket)
        stale = getattr(self, "_stale", False) and not self.checkpoint_moved
        terminal = provisional != GO or stale or round_ >= self.max_rounds
        outcome = await self._checkpoint(round_=round_, terminal=terminal, provisional=provisional, stale=stale)
        if outcome is not None:
            return outcome
        if self.checkpoint_moved:
            # an answer buys a round — unconditionally, ahead of the round limit, and as a full round.  Re-evaluating
            # here would let the answer itself settle the run and throw away the round it paid for.
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
        if self.checkpoint_moved:
            self.previous_signature = signature(self.docket)
            self.checkpoint_moved = False
        if round_ >= self.max_rounds:
            # This is the only exit that ends a run with a question unasked, and the report has to say so rather than
            # print a question that reads as merely deferred.  Marked here, at the exit, and nowhere earlier: a
            # mid-run checkpoint that does not wait for a deferred question may still be followed by one that does.
            unasked = self._mark_unasked()
            self.emit("termination", outcome=ROUND_LIMIT, reason=f"round limit ({self.max_rounds}) reached", round=round_, unasked=unasked)
            return ROUND_LIMIT
        self.emit("round_continue", round=round_, reason=reason)
        return GO

    def _mark_unasked(self) -> list[str]:
        from .termination import unsettled_questions

        marked = []
        for question in unsettled_questions(self.docket):
            if question.state == "asked" and "unasked" not in question.markers:
                question.markers.append("unasked")
                marked.append(question.id)
        return marked

    async def _checkpoint(self, round_: int, *, terminal: bool, provisional: str = "", stale: bool = False):
        from .escalations import effective_class
        from .termination import unsettled_questions

        # The human checkpoint's ask set is the whole unsettled set — a deferred question is still *in* the ask set and is
        # waited for at the terminal checkpoint.  What decides a wait is each question's effective class, not membership.
        ask = unsettled_questions(self.docket)
        listing = ask
        if not listing and not ask:
            self.emit("human_checkpoint", round=round_, ask_set=[], waited=False, answered=0, skipped=0, terminal=terminal)
            return None
        # Only an outcome the human can still change makes the run wait.  The
        # round-limit exit is the one exempt — there the questions print `unasked`.
        at_round_limit = round_ >= self.max_rounds and provisional == GO and not stale
        blocking = any(effective_class(self.docket, q) == "blocking" for q in ask)
        waiting = bool(ask) and (terminal or blocking) and not at_round_limit
        outcome = await self.port.checkpoint(ask, round_=round_, terminal=terminal, listing=listing, waiting=waiting)
        self.emit(
            "human_checkpoint",
            round=round_,
            ask_set=[q.id for q in listing],
            waited=outcome.waited,
            answered=len(outcome.answers),
            skipped=len(outcome.skipped),
            terminal=terminal,
        )
        # Every selection is resolved against the menu it was chosen from, before any answer lands on the docket.  A number the menu never
        # held was never shown, so it names nothing: that is a bug in the port and the run says which, but it does not end the run — the
        # answer arrived as the human's own line and keeps its authority, and only the provenance is dropped (`answer_option` 0 says so).
        # The last check is what makes the record more than plausible: the numbered line has to *be* the answer, so a menu that moved
        # between the console line and here cannot silently credit the human with choosing a line they never read.
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
                # The Checkpoint resolved this index against this menu, the run is parked while the human answers so
                # nothing files a question in between, and the check above proved line `number` is this answer — so this
                # names the very line the human was shown.
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
        if outcome.any_answer:
            self.checkpoint_moved = True
            self.emit("answer_buys_a_round", round=round_ + 1, answered=len(outcome.answers))
            return None
        if terminal and provisional != GO:
            # The outcome is an event wherever it is decided.  A checkpoint that ends the run (every
            # question declined, an outcome already waiting) is the common way `needs_human` happens,
            # and a run whose events stop at `human_checkpoint` reads as though it never terminated.
            self.emit("termination", outcome=provisional, reason="the checkpoint closed the run", round=round_)
            return provisional
        return None

    def _retire_covered(self, question, round_: int, *, answered: bool) -> None:
        for (item_id, asker), escalation in list(self.docket.escalations.items()):
            if escalation.question_id != question.id and (item_id != question.item_id or asker not in question.askers):
                continue
            if escalation.state != STANDING:
                continue
            retire(escalation, round_)
            self.emit("escalation_retired", item=item_id, asker=asker, question=question.id, round=round_, answered=answered)


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
