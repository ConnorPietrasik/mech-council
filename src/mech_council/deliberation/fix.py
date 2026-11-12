"""The fix spine: the findings in, the fixes out — one finding at a time, each fix planned,
verified and committed, and the tree rolled back when a fix fails.

The run convenes at most three crews: the review crew (or a `--review-file`) that finds the
defects, the builder crew that plans and lands each fix, and the reviewer crew that verifies each
fix — the review crew doubles as the verifier when no reviewer is named. The chair is the run's;
every seat is a crew seat. A passed finding commits as its own commit; a blocked finding rolls
back to its pre-tree, so one bad fix cannot poison the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os

from ..console import Console
from ..crews import Crew, Crews
from ..extraction import Extraction, map_severity, parse_replan, parse_structure, parse_work
from ..humaninput import Checkpoint
from ..port import Port, RawAsk
from ..prompts import fix as fix_text
from ..render.text import severity_key
from .engine import Abort, CheckpointOutcome, Engine
from .escalations import HELD_BACK, NO_HUMAN, NOT_WAITED, effective_class, held_back, picked_option, raise_question, showable
from .notes import NOTE_MAX_CHARS, folded_away, item_notes, mark_shown, raise_note, resolve_note_item, unshown
from .state import (
    AGREED,
    ANSWERED,
    CHAIR_ID,
    FIXED,
    NOTHING_TO_FIX,
    OPEN,
    PARTIAL,
    SKIPPED,
    STANDING,
    Docket,
    Execution,
    FindingRecord,
    Item,
    Note,
    Question,
    normalize_text,
    one_line,
)
from .termination import unsettled_questions
from .tree import DIFF_CAP, Tree

_FILE_CAP = 4000
_GATE_EVIDENCE_CAP = 2000
#: a trivial finding stops holding a fix open after the builder's second pass, so the ladder cannot churn on nits
_NIT_PASSES = 2
_NO_WORK: dict = {"summary": "", "gate_output": "", "changed": [], "user_text": [], "open_issues": [], "addressed": []}


def _fold(text: object, cap: int) -> str:
    return one_line(str(text))[:cap]


def _without_review_id(text: str, item_id: str) -> str:
    """A carried note's review-run id prefix: the note is filed on the finding, which has its own id."""
    words = text.split(None, 1)
    if len(words) == 2 and item_id and words[0].strip(".:;,!?()[]\"'`-") == item_id:
        return words[1]
    return text


class Fix:
    """One fix run, spine and all."""

    def __init__(
        self,
        *,
        crews: Crews,
        docket: Docket,
        checkpoint: Checkpoint,
        console: Console,
        review_file: str = "",
        emit=None,
    ) -> None:
        self.crews = crews
        self.docket = docket  # the run's docket: the finding items, the gate, the questions
        self.checkpoint = checkpoint
        self.console = console
        self.review_file = review_file
        self.emit = emit or (lambda name, **fields: None)
        council = crews["builder"].council
        self.project = council.project
        self.human = council.human
        self.fix_budget = council.fix_budget
        self.replans = council.replans
        self.goal = council.goal or ""
        self.commit = council.commit
        self.names = crews.names()
        self.run_dir = crews["builder"].run_dir
        self.tree = Tree(self.project, self.run_dir)
        execution = docket.execution
        if execution is None:
            execution = Execution()
            docket.execution = execution
        self.execution = execution
        self.review_docket: Docket | None = None  # the review phase's docket, when the review crew ran
        self.baseline: str | None = None
        self.last_tree: str | None = None
        self.selected: list[FindingRecord] = []
        self._gate_entries: dict[str, tuple[Item, Question | None, str]] = {}  # review item id → (item, gate question, default)
        self._next_f = 1
        self._next_c = 1
        self._asked_of_human: set[str] = set()

    # ------------------------------------------------------------- the run

    async def run(self) -> str:
        try:
            return await self._run()
        finally:
            self._unsent_messages()

    def _unsent_messages(self) -> None:
        """A say_to line whose target never got another prompt: said out loud, kept in the report."""
        messages = self.docket.peer_messages
        if not messages:
            return
        self.emit("fix_say_to_unsent", count=len(messages))
        for message in messages:
            who = self.names.get(message.from_, message.from_)
            to = self.names.get(message.to, message.to)
            self.console.say(f"say_to never landed: {who} to {to}: {message.text}")

    async def _run(self) -> str:
        if self.review_file:
            self._source_from_file()
        else:
            await self._review_phase()
            self._pick_findings()
        await self._gate()
        self.baseline = await asyncio.to_thread(self.tree.snapshot)
        if not self.baseline:
            raise Abort(
                "the project's tree could not be snapshotted: a fix run needs a working git",
                member=CHAIR_ID,
                round_=self.docket.round,
            )
        self.last_tree = self.baseline
        self.execution.baseline = self.baseline
        self.execution.final_tree = self.baseline
        if not self.selected:
            self._mark_unasked()
            self.emit("termination", outcome=NOTHING_TO_FIX, reason="the review found nothing to fix")
            return NOTHING_TO_FIX
        await self._spawn_execution_crews()
        held = False
        for rec in self.selected:
            if held:
                rec.status = "blocked"
                rec.blocked_reasons = ["the human held the run"]
                continue
            held = await self._fix_finding(rec)
        self.execution.final_tree = await asyncio.to_thread(lambda: self.tree.snapshot() or "") or self.execution.final_tree
        await self._final_check()
        all_fixed = bool(self.selected) and all(rec.status == "fixed" for rec in self.selected)
        outcome = FIXED if all_fixed and self.execution.final_check.get("ok", True) else PARTIAL
        self._mark_unasked()
        fixed = sum(1 for rec in self.selected if rec.status == "fixed")
        self.emit("termination", outcome=outcome, reason=f"{fixed} of {len(self.selected)} finding(s) fixed")
        return outcome

    def _port(self, crew: Crew, *, docket: Docket | None = None, checkpoint: Checkpoint | None = None) -> Port:
        lanes, chair, brief = crew.lanes, crew.chair, crew.brief
        if lanes is None or chair is None or brief is None:
            raise Abort(f"the {crew.label} crew is not up yet", member=CHAIR_ID, round_=self.docket.round)
        return Port(
            council=crew.council,
            lanes=lanes,
            chair=chair,
            brief=brief,
            console=self.console,
            checkpoint_run=checkpoint or self.checkpoint,
            docket=docket or self.docket,
            names=self.names,
        )

    # ------------------------------------------------------------- the review phase

    async def _review_phase(self) -> None:
        """The findings: the review crew runs a plain review over the project's current state."""
        crew = self.crews["review"]
        assert crew is not None  # a run without --review-file always convenes the review crew
        review_docket = Docket(
            mode="review",
            goal=crew.council.goal or self.goal,
            roster=list(crew.council.seat_ids),
            chair_name=crew.council.chair.name,
            seconding=crew.council.seconding,
            human=self.human,
        )
        # the review docket has its own checkpoint: the shared one is bound to the run's docket
        review_checkpoint = Checkpoint(
            review_docket, self.checkpoint.reader, console=self.console, names=self.names, ask_seat=self.crews.send_message
        )
        port = self._port(crew, docket=review_docket, checkpoint=review_checkpoint)
        engine = Engine(
            review_docket,
            port,
            goal=review_docket.goal,
            project=self.project,
            brief=crew.brief,
            max_rounds=crew.council.max_rounds,
            grace_rounds=crew.council.grace_rounds,
            names=self.names,
            emit=self.emit,
        )
        self.emit("fix_review_start", crew=crew.label, seats=len(review_docket.roster), rounds=crew.council.max_rounds)
        self.docket.phase = "review"
        await engine.run()
        self.review_docket = review_docket
        self.execution.source = {"how": "council", "council": crew.council.name, "rounds": review_docket.round}
        if self.crews.get("reviewer") is not None:
            # a separate verifier stands up later: the review crew's work is done
            await crew.dispose()
            self._review_seats_leave()

    def _review_seats_leave(self) -> None:
        """The review crew is disposed: its seats leave the shared docket, so the bar the run's stops
        are measured against counts the live crew, not the review crew that is gone."""
        crew = self.crews.get("review")
        if crew is None:
            return
        gone = {spec.id for spec in crew.council.agents if spec.id}
        self.docket.roster = [seat for seat in self.docket.roster if seat not in gone]

    def _pick_findings(self) -> None:
        """The run's finding list, before the gate: the review's agreed findings, by severity then id.
        Everything else the review left on the record — disputed, open, rejected — is addable at the gate only."""
        assert self.review_docket is not None
        agreed: list[Item] = []
        other: list[Item] = []
        for item_id in self.review_docket.order:
            item = self.review_docket.items[item_id]
            (agreed if item.disposition == AGREED else other).append(item)

        def key(item):
            return (severity_key(item.severity), item.id)

        agreed.sort(key=key)
        other.sort(key=key)
        for item in agreed + other:
            self._gate_entries[item.id] = (item, None, "selected" if item in agreed else "skipped")

    def _new_f(self) -> str:
        item_id = f"F{self._next_f}"
        while item_id in self.docket.items:
            self._next_f += 1
            item_id = f"F{self._next_f}"
        self._next_f += 1
        return item_id

    def _transfer_item(self, item: Item) -> str:
        """A review finding onto the run's docket, where the fix work happens: its own F id, with the
        review's standing notes on it, so the builder reads the corrected mechanism and its context too."""
        new = Item(
            id=self._new_f(),
            claim=item.claim,
            detail=item.detail,
            evidence=item.evidence,
            severity=item.severity,
            disposition=OPEN,
            raised_by=list(item.raised_by),
            also_reported_by=list(item.also_reported_by),
            opened_round=0,
        )
        self.docket.add_item(new)
        for note in self._riding_notes(item.id):  # the review's record of the finding rides with it
            note_text = _without_review_id(note.text, item.id)
            carried, _ = raise_note(self.docket, by=note.by, text=note_text, item_id=new.id, round_=0, kind=note.kind)
            if note.shown_round is not None:
                carried.shown_round = 0  # shown to the human in the review: the fix run does not show it again
        return new.id

    def _riding_notes(self, review_id: str) -> list[Note]:
        """The review's standing notes filed on one of its findings: what rides with the finding.
        A note folded into one that still stands is read through the survivor, as the review's report reads it."""
        review = self.review_docket
        assert review is not None
        out: list[Note] = []
        for nid in review.note_order:
            note = review.human_notes[nid]
            if note.item_id != review_id or note.state != STANDING or folded_away(review, note):
                continue
            if note.text.strip():
                out.append(note)
        return out

    # ------------------------------------------------------------- the gate

    async def _gate(self) -> None:
        """The human's gate over the findings, one question per finding: fix or skip. The agreed ones
        default to fixing, the rest to skipping; a question left unanswered keeps its default. In a
        blocking run the gate waits and a closed stdin declines the rest — the defaults take them;
        that is the overnight run's meaning of 'no human'. A run with no human at all takes the
        defaults without asking; a non-blocking run shows the gate but does not wait on it."""
        if self.review_file:
            return  # a curated file is the curation: its agreed items are the list, ungated
        if self.human == "none":
            # no human at all: the defaults take the list without a question, and `_select_gated` records them
            self._select_gated()
            return
        # the gate takes the run's first round, which finding 1's passes share
        self.docket.round = 1
        for review_id, (item, _question, _default) in self._gate_entries.items():
            context = f"{review_id} · {item.severity} · {item.disposition}" + (f" · {item.evidence[:300]}" if item.evidence else "")
            if corrections := self._gate_corrections(review_id):
                context += f" · correction: {'; '.join(corrections)[:300]}"
            question, _ = raise_question(
                self.docket,
                text=f"fix {item.claim[:200]}",
                asker=CHAIR_ID,
                item_id=None,
                round_=self.docket.round,
                options=["fix", "skip"],
                context=context,
            )
            self._gate_entries[review_id] = (item, question, _default)
        questions = [entry[1] for entry in self._gate_entries.values() if entry[1] is not None]
        if self.human != "blocking":
            # the gate shows but does not wait: the defaults take the list now, an answer may still land in the record
            for question in questions:
                if NOT_WAITED not in question.markers:
                    question.markers.append(NOT_WAITED)
            await self._checkpoint(terminal=False, waiting=False, forced=questions)
        else:
            await self._checkpoint(terminal=False, waiting=True, forced=questions)
        self._select_gated()

    def _gate_corrections(self, review_id: str) -> list[str]:
        """The review's standing corrections on a gate finding: the fix-or-skip call should weigh the
        corrected mechanism, not only the original claim."""
        review = self.review_docket
        if review is None:
            return []
        out: list[str] = []
        for nid in review.note_order:
            note = review.human_notes[nid]
            if note.item_id == review_id and note.kind == "correction" and note.state == STANDING and not folded_away(review, note):
                out.append(note.text[:120])
        return out

    def _select_gated(self) -> None:
        """The gate's decisions into the run's list: the selected findings, in the gate's order.
        The pick is the answer's option: a bare number, or an answer in the option's own words."""
        for review_id, (item, question, default) in self._gate_entries.items():
            choice = default
            if question is not None and question.state == ANSWERED:
                number = question.answer_option or self._option_number(question)
                if number:
                    choice = "selected" if number == 1 else "skipped"
            self.execution.gate[question.id if question is not None else review_id] = (
                "added" if choice == "selected" and default == "skipped" else choice
            )
            if choice == "selected":
                finding_id = self._transfer_item(item)
                rec = FindingRecord(
                    index=len(self.selected) + 1,
                    finding_id=finding_id,
                    severity=item.severity,
                    claim=item.claim,
                )
                self.selected.append(rec)
                self.execution.findings.append(rec)
        self.emit("fix_gate", selected=[rec.finding_id for rec in self.selected], skipped=len(self._gate_entries) - len(self.selected))
        if self.selected:
            self.console.say(f"the gate: {len(self.selected)} finding(s) to fix, in the gate's order")
        else:
            self.console.say("the gate: nothing to fix")

    @staticmethod
    def _option_number(question: Question) -> int:
        """The option the answer's own words take, when the answer is not a bare number."""
        answer = (question.answer or "").strip().casefold()
        for number, option in enumerate(question.options, start=1):
            if answer == option.strip().casefold():
                return number
        return 0

    # ------------------------------------------------------------- --review-file

    def _source_from_file(self) -> None:
        """The findings of a previous review's report.json: its agreed items, by severity, with the review's standing
        notes filed under them. No gate — a curated file is the curation."""
        try:
            with open(self.review_file, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as failure:
            raise Abort(f"--review-file is not a readable JSON document: {failure}", member=CHAIR_ID) from failure
        meta = document.get("run") or {}
        if document.get("format") != "council-report" or meta.get("mode") != "review":
            raise Abort("--review-file is not a review report.json: run the review with --format json", member=CHAIR_ID)
        items: list[tuple[str, Item]] = []
        for entry in document.get("items") or []:
            if not isinstance(entry, dict):
                continue
            claim = one_line(str(entry.get("claim") or ""))
            if not claim:
                continue
            items.append(
                (
                    str(entry.get("id") or ""),
                    Item(
                        id="",
                        claim=claim[:600],
                        detail=one_line(str(entry.get("detail") or ""))[:2000],
                        evidence=one_line(str(entry.get("evidence") or ""))[:2000],
                        severity=map_severity(entry.get("severity")),
                        disposition=str(entry.get("disposition") or OPEN),
                    ),
                )
            )
        entries = [entry for entry in document.get("notes") or [] if isinstance(entry, dict)]
        standing_ids = {str(entry.get("id") or "") for entry in entries if entry.get("state") == STANDING}
        notes: dict[str, list[tuple[str, str, str, int | None]]] = {}
        for entry in entries:
            if entry.get("state") != STANDING:
                continue
            folded_into = str(entry.get("folded_into") or "")
            if folded_into and folded_into in standing_ids:
                continue
            text = str(entry.get("note") or "").strip()
            if not text:
                continue
            kind = "correction" if entry.get("kind") == "correction" else "info"
            notes.setdefault(str(entry.get("item") or ""), []).append((str(entry.get("seat") or ""), kind, text, entry.get("shown_round")))
        self.execution.source = {
            "how": "file",
            "path": self.review_file,
            "council": str(meta.get("council") or ""),
            "date": str(meta.get("date") or ""),
            "outcome": str(meta.get("outcome") or ""),
        }
        agreed = sorted(
            (pair for pair in items if pair[1].disposition == AGREED),
            key=lambda pair: (severity_key(pair[1].severity), pair[1].claim),
        )
        for old_id, item in agreed:
            item.id = self._new_f()
            self.docket.add_item(item)
            rec = FindingRecord(index=len(self.selected) + 1, finding_id=item.id, severity=item.severity, claim=item.claim)
            self.selected.append(rec)
            self.execution.findings.append(rec)
            for seat, kind, text, shown in notes.get(old_id, ()):  # the review's record of the finding rides with it
                note_text = one_line(_without_review_id(text, old_id))[:NOTE_MAX_CHARS]
                carried, _ = raise_note(self.docket, by=seat, text=note_text, item_id=item.id, round_=0, kind=kind)
                if shown is not None:
                    carried.shown_round = 0  # shown to the human in the review: the fix run does not show it again
        self.emit("fix_review_file", path=self.review_file, agreed=len(self.selected), items=len(items))
        self.console.say(f"the review file: {len(self.selected)} agreed finding(s) to fix, in severity order")

    # ------------------------------------------------------------- the crews

    async def _spawn_execution_crews(self) -> None:
        """What the fix loop stands up: the builder's seats when a review crew sat them out — a
        --review-file run starts them at the convene — and the named reviewer crew."""
        if self.crews.get("review") is not None:
            await self.crews["builder"].start_seats()
        reviewer = self.crews.get("reviewer")
        if reviewer is not None:
            await reviewer.start(seats=True)
        self.docket.phase = "fixing"

    def _verifier_crew(self) -> Crew | None:
        """The crew that verifies each fix: the named reviewer, else the review crew that stayed up."""
        reviewer = self.crews.get("reviewer")
        if reviewer is not None and reviewer.live:
            return reviewer
        crew = self.crews.get("review")
        if crew is not None and crew.live:
            return crew
        return None

    def _seats(self, me: str | None) -> list[tuple[str, str]]:
        """The run's live seats as `(id, note)`: a prompt's roster, how a seat addresses a peer in `say_to`."""
        out: list[tuple[str, str]] = []
        for label in self.crews.order:
            crew = self.crews[label]
            if not crew.live:
                continue
            for spec in crew.council.agents:
                if spec.id is None:
                    continue
                if label == "builder":
                    note = "you" if spec.id == me else ""
                else:
                    note = label
                out.append((spec.id, note))
        return out

    # ------------------------------------------------------------- the checkpoint

    async def _checkpoint(
        self, *, terminal: bool = False, waiting: bool | None = None, forced: Question | list[Question] | None = None
    ) -> CheckpointOutcome | None:
        """The run's side of the human: the shared docket's open asks, at a finding boundary.

        `forced` is the run's own gate(s) — the gate's questions, or the hold-or-carry-on call: shown
        whether or not the seconding bar holds them back. A finding boundary is the run's round boundary:
        what the human typed while the fixes worked is drained here.
        """
        drained = await self.checkpoint.drain()
        if drained.answers or drained.skipped or drained.recommendations:
            self.emit(
                "human_answer_off_cycle",
                round=self.docket.round,
                answered=sorted(drained.answers),
                declined=list(drained.skipped),
                explained=sorted(drained.recommendations),
            )
            self._apply_human(drained)
        open_asks = unsettled_questions(self.docket)
        asked = [q for q in open_asks if showable(self.docket, q)]
        forced_list = [forced] if isinstance(forced, Question) else list(forced or [])
        for question in forced_list:
            if question not in asked:
                asked.append(question)
        if self.docket.human == "none":
            for question in asked:
                if NO_HUMAN not in question.markers:
                    question.markers.append(NO_HUMAN)
            asked = []
        pending_notes = unshown(self.docket)
        if not asked and not pending_notes:
            return None
        if waiting is None:
            waiting = self.docket.human == "blocking" and (terminal or any(effective_class(self.docket, q) == "blocking" for q in asked))
        self.docket.parked_on = [q.id for q in asked] if waiting else []
        try:
            outcome = await self.checkpoint.run(
                asked,
                ask_set=asked,
                round_=self.docket.round,
                terminal=terminal,
                waiting=waiting,
                notes=pending_notes,
            )
        finally:
            self.docket.parked_on = []
        mark_shown(pending_notes, self.docket.round)
        for question in asked:
            self._asked_of_human.add(question.id)
        self._apply_human(outcome)
        self.emit(
            "human_checkpoint",
            round=self.docket.round,
            ask_set=[q.id for q in asked],
            waited=outcome.waited,
            answered=len(outcome.answers),
            skipped=len(outcome.skipped),
            terminal=False,
            notes=[note.id for note, _kind in pending_notes],
        )
        return outcome

    def _mark_unasked(self) -> None:
        """Label the asks the run ends on: a shown ask goes unanswered, a held-back one keeps its
        reason, the rest was never asked — with no human at all, none was ever asked."""
        for question in unsettled_questions(self.docket):
            if NO_HUMAN in question.markers or any(marker in question.markers for marker in HELD_BACK):
                continue
            if self.human == "none":
                marker = NO_HUMAN
            elif question.id in self._asked_of_human:
                marker = "unanswered"
            else:
                marker = held_back(self.docket, question) or "unasked"
            if marker not in question.markers:
                question.markers.append(marker)

    def _apply_human(self, outcome: CheckpointOutcome) -> None:
        selections: dict[str, int] = {}
        for qid, number in outcome.answer_options.items():
            question = self.docket.questions.get(qid)
            if question is None or not question.options or not 1 <= number <= len(question.options):
                continue
            if outcome.answers.get(qid) == question.options[number - 1]:
                selections[qid] = number
        for qid, answer in outcome.answers.items():
            question = self.docket.questions.get(qid)
            if question is None:
                continue
            question.state = ANSWERED
            question.answer, question.answer_round = answer, self.docket.round
            number = selections.get(qid) or 0
            if number:
                question.answer_option = number
                question.answer_option_text = question.options[number - 1]
        for qid in outcome.skipped:
            question = self.docket.questions.get(qid)
            if question is not None:
                question.state = SKIPPED
        for qid, text in outcome.recommendations.items():
            question = self.docket.questions.get(qid)
            if question is not None:
                question.recommendation, question.recommendation_round = one_line(text), self.docket.round

    # ------------------------------------------------------------- a finding

    def _finding(self, rec: FindingRecord) -> dict:
        item = self.docket.items[rec.finding_id]
        # standing only: the report's ledger may still print a retracted note the human saw, marked
        standing = [note for note in item_notes(self.docket, rec.finding_id) if note.state == STANDING]
        return {
            "id": rec.finding_id,
            "severity": rec.severity,
            "claim": item.claim,
            "detail": item.detail,
            "evidence": item.evidence,
            "corrections": [note.text for note in standing if note.kind == "correction"],
            "notes": [note.text for note in standing if note.kind != "correction"],
        }

    def _fixed_lines(self) -> list[str]:
        lines = []
        for rec in self.execution.findings:
            if rec.status != "fixed":
                continue
            tail = f" — committed {rec.commit_sha[:10]}" if rec.commit_sha else " — uncommitted"
            lines.append(f"{rec.finding_id} ({rec.severity}): {rec.claim}{tail}")
        return lines

    async def _fix_finding(self, rec: FindingRecord) -> bool:
        """Plan, implement, verify one finding — and commit it, or roll it back.
        Returns True when the human held the run here."""
        rec.pre_tree = await asyncio.to_thread(lambda: self.tree.snapshot() or "") or (self.last_tree or "")
        self.docket.round = rec.index
        rec.builder = self._builder()
        self.emit(
            "fix_finding_start",
            finding=rec.index,
            total=len(self.selected),
            finding_id=rec.finding_id,
            builder=rec.builder,
            severity=rec.severity,
        )
        who = self.names.get(rec.builder, rec.builder)
        self.console.say(f"finding {rec.index}/{len(self.selected)}: {rec.finding_id} ({rec.severity}) — the baton goes to {who}")
        plan = await self._plan_pass(rec)
        rec.plan = plan
        data = await self._work_pass(rec, plan)
        findings = await self._verify_pass(rec, plan, changed=[*data["changed"], *data["user_text"]], pass_number=1)
        fix_pass = 0
        while (findings or not rec.gate_ok) and fix_pass < self.fix_budget:
            fix_pass += 1
            data = await self._fix_pass(rec, plan, self._finding_items(rec), fix_pass)
            findings = await self._verify_pass(rec, plan, changed=data["changed"], pass_number=1 + fix_pass)
        replan = 0
        while (findings or not rec.gate_ok) and replan < self.replans:
            replan += 1
            revised, note = await self._replan_pass(rec, plan, self._finding_items(rec), replan)
            rec.replans.append({"pass": replan, "note": note, "stages": len(revised)})
            if not revised:
                # the replan was unreadable: no work was attempted this rung, so it ends without a re-check
                continue
            # the finding's own plan stays the contract: the breakdown is the way the fix lands, and the
            # verify re-runs the finding's gate, not a step's
            data = await self._fix_pass(rec, plan, self._finding_items(rec), fix_pass + replan, revised=revised)
            findings = await self._verify_pass(rec, plan, changed=data["changed"], pass_number=1 + fix_pass + replan)
        if findings or not rec.gate_ok:
            await self._block_finding(rec)
            return await self._hold_or_carry_on(rec)
        await self._commit_finding(rec)
        self.emit("fix_finding_fixed", finding=rec.index, finding_id=rec.finding_id, commit=rec.commit_sha[:10] or "uncommitted")
        tail = f" — committed {rec.commit_sha[:10]}" if rec.commit_sha else ""
        self.console.say(f"finding {rec.index}/{len(self.selected)} fixed{tail}")
        return False

    def _builder(self) -> str:
        """The baton for every finding: the first builder that is not the dedicated prose seat."""
        builders = [spec for spec in self.crews["builder"].council.agents if spec.role != "prose"]
        if not builders:
            builders = list(self.crews["builder"].council.agents)
        return builders[0].id or ""

    async def _plan_pass(self, rec: FindingRecord) -> dict:
        """The builder plans the one-stage fix: a state, a gate the verifier can re-run, a scope.
        An unreadable plan does not kill the run: the pass goes on planless — the verifier judges
        the diff against the finding, with no gate and no scope to hold it to."""
        task = fix_text.plan_task(
            finding=self._finding(rec),
            index=rec.index,
            total=len(self.selected),
            fixed_so_far=self._fixed_lines() or None,
            seats=self._seats(rec.builder),
            messages=self._drain(rec.builder) or None,
            human=self.human,
        )
        result = await self._port(self.crews["builder"]).builder_plan(rec.builder, task, round_=rec.index)
        if result.failure:
            raise Abort(result.failure, member=rec.builder, round_=rec.index)
        plan, errors = parse_structure(result.text or "")
        stages = [clean for clean in (self._clean_stage(s) for s in (plan or {}).get("stages") or []) if clean]
        if not stages:
            self.console.say(f"finding {rec.index}: the plan reply was unreadable after its repair re-asks — the pass goes on planless")
            self.emit("fix_plan_unreadable", finding=rec.index, seat=rec.builder, reason="; ".join(errors) or "it named no stages")
            return {"state": rec.claim, "gate": "", "scope": "", "exceptions": []}
        self.emit("fix_plan", finding=rec.index, state=_fold(stages[0].get("state"), 160))
        return stages[0]

    @staticmethod
    def _clean_stage(stage: object) -> dict:
        if not isinstance(stage, dict) or not str(stage.get("state") or "").strip():
            return {}
        return {
            "state": str(stage.get("state") or "").strip(),
            "gate": str(stage.get("gate") or "").strip(),
            "scope": str(stage.get("scope") or "").strip(),
            "exceptions": [str(e) for e in stage.get("exceptions") or [] if str(e).strip()],
        }

    async def _work_pass(self, rec: FindingRecord, plan: dict) -> dict:
        task = fix_text.fix_work_task(
            finding=self._finding(rec),
            plan=plan,
            index=rec.index,
            total=len(self.selected),
            messages=self._drain(rec.builder) or None,
            seats=self._seats(rec.builder),
            human=self.human,
        )
        result = await self._port(self.crews["builder"]).builder_work(rec.builder, task, round_=rec.index)
        if result.failure:
            raise Abort(result.failure, member=rec.builder, round_=rec.index)
        parsed = parse_work(result.text or "")
        data = self._apply_work(rec, parsed)
        if parsed.invalid or parsed.errors:
            # an unreadable reply does not kill the run: the gate and the verify judge what actually landed
            self.console.say(
                f"finding {rec.index}: the work reply was unreadable after its repair re-asks — "
                "the gate and the verify will judge what landed"
            )
            self.emit("fix_work_unreadable", finding=rec.index, seat=rec.builder, reason="; ".join(parsed.errors))
        return {**_NO_WORK, **data}

    async def _fix_pass(
        self, rec: FindingRecord, plan: dict, items: list[dict], pass_number: int, *, revised: list[dict] | None = None
    ) -> dict:
        task = fix_text.fix_fix_task(
            finding=self._finding(rec),
            plan=plan,
            index=rec.index,
            total=len(self.selected),
            pass_number=pass_number,
            items=items,
            gate_output=rec.gate_rerun,
            revised=revised,
            messages=self._drain(rec.builder) or None,
            seats=self._seats(rec.builder),
            human=self.human,
        )
        result = await self._port(self.crews["builder"]).builder_work(rec.builder, task, round_=rec.index, phase="fix")
        if result.failure:
            raise Abort(result.failure, member=rec.builder, round_=rec.index)
        parsed = parse_work(result.text or "")
        data = self._apply_work(rec, parsed, fix_pass=pass_number)
        if parsed.invalid or parsed.errors:
            # the pass is spent either way: the ladder counts it, the gate and the verify judge the result
            self.console.say(
                f"finding {rec.index}: fix pass {pass_number} was unreadable after its repair re-asks — it counts against the fix budget"
            )
            self.emit("fix_fix_unreadable", finding=rec.index, seat=rec.builder, pass_number=pass_number, reason="; ".join(parsed.errors))
        return {**_NO_WORK, **data}

    async def _replan_pass(self, rec: FindingRecord, plan: dict, items: list[dict], pass_number: int) -> tuple[list[dict], str]:
        task = fix_text.fix_replan_task(
            finding=self._finding(rec),
            plan=plan,
            index=rec.index,
            total=len(self.selected),
            pass_number=pass_number,
            items=items,
            gate_output=rec.gate_rerun,
            messages=self._drain(rec.builder) or None,
            seats=self._seats(rec.builder),
        )
        result = await self._port(self.crews["builder"]).builder_replan(rec.builder, task, round_=rec.index)
        if result.failure:
            raise Abort(result.failure, member=rec.builder, round_=rec.index)
        parsed = parse_replan(result.text or "")
        revised = [clean for clean in (self._clean_stage(s) for s in parsed.data.get("stages") or []) if clean]
        if parsed.invalid or not revised:
            self.console.say(
                f"finding {rec.index}: replan {pass_number} was unreadable after its repair re-asks — it counts against the replan budget"
            )
            self.emit("fix_replan_unreadable", finding=rec.index, seat=rec.builder, reason="; ".join(parsed.errors) or "it named no stages")
            return [], "the replan was unreadable"
        return revised, str(parsed.data.get("note") or "")

    def _apply_work(self, rec: FindingRecord, parsed, *, fix_pass: int | None = None) -> dict:
        """What a work reply puts on the record: the docket side effects, whatever the shape check said."""
        data = parsed.data
        rec.gate_output = str(data.get("gate_output") or "")
        for ask in data.get("human_input") or []:
            self._raise_ask(rec.builder, ask, round_=rec.index)
        for note in data.get("notes") or []:
            raise_note(
                self.docket,
                by=rec.builder,
                text=note["text"],
                item_id=resolve_note_item(self.docket, note),
                round_=rec.index,
                kind=note.get("kind", "info"),
            )
        for line in data.get("say_to") or []:
            self.docket.queue_message(to=line["to"], from_=rec.builder, text=line["text"], stage=rec.index)
        rec.changed.extend(dict.fromkeys(str(p) for p in data.get("changed") or [] if str(p).strip()))
        if fix_pass is not None:
            rec.fix_passes.append({"pass": fix_pass, "addressed": data.get("addressed") or []})
        return data

    # ------------------------------------------------------------- the verify

    @staticmethod
    def _holds_finding(item: Item, pass_number: int) -> bool:
        """Whether a verifier finding holds the fix open at this rung: anything through the first two
        builder passes; from the third on, `minor` or higher only, so the ladder cannot churn on nits."""
        return pass_number <= _NIT_PASSES or item.severity != "trivial"

    async def _verify_pass(self, rec: FindingRecord, plan: dict, *, changed: list[str], pass_number: int) -> list[str]:
        """The verifier's scoped pass: the finding's gate re-run by the crew, and the diff reviewed.
        Findings land on the run docket; the gate verdict on the finding record. Returns the items
        that hold the finding open."""
        crew = self._verifier_crew()
        if crew is None or crew.brief is None:
            if plan.get("gate"):
                ok, ran, evidence = await self._chair_gate_check(plan)
                rec.gate_ok, rec.gate_rerun = ok, _fold(" ".join(part for part in (ran, evidence) if part), _GATE_EVIDENCE_CAP)
                rec.verify_note = "no verifier crew: the chair ran the gate"
            else:
                rec.gate_ok, rec.gate_rerun = True, ""
                rec.verify_note = "no verifier crew and no gate: the pass stands on the builder's word"
            self._settle_gate_item(rec)
            await self._checkpoint(terminal=False)
            return []
        brief = crew.brief
        diff = await self._stage_diff(changed)
        sections = [
            "",
            "## The finding",
            "",
            f"{rec.finding_id} ({rec.severity}) — fix the defect so this becomes true: {rec.plan.get('state') or rec.claim}",
            "",
            "## The planned fix",
            "",
            fix_text.stage_block(rec.index, len(self.selected), plan),
            self._review_gate_section(rec, plan),
        ]
        open_findings = self._standing_items(rec)
        if open_findings:
            sections += ["", "## The findings still standing"]
            sections += [
                f"- {item['id']}: {item['claim']}" + (f" — {item['evidence']}" if item["evidence"] else "") for item in open_findings
            ]
            sections.append("Re-report any that are still wrong, with the same claim: the re-report joins the standing item.")
        sections += [
            "",
            "## What the builder did since the last verify",
            "",
            f"finding {rec.index}/{len(self.selected)} · changed: {', '.join(changed) if changed else 'nothing'}",
            "",
            "## The diff since the last verify",
            "",
            "```",
            diff,
            "```",
        ]
        goal = f"Verify the fix for finding {rec.index} of {len(self.selected)}: {plan.get('state', '')}"
        base = brief.phase0(mode="review", goal=goal, project=self.project) + "\n".join(sections) + "\n" + fix_text.SAY_TO
        port = self._port(crew)
        self.emit("fix_verify", finding=rec.index, crew=crew.label, seats=len(crew.council.seat_ids))
        batch = []
        for agent in crew.council.seat_ids:
            extra = "\n" + fix_text.seat_roster(self._seats(agent))
            if messages := self._drain(agent):
                extra += "\n" + fix_text.messages_section(messages)
            batch.append((agent, base + extra))
        results = await port.ask_seats(batch, round_=rec.index, phase="phase0", classify_unusable=True)
        new_ids: list[str] = []
        re_reported: list[str] = []
        for agent in crew.council.seat_ids:
            result = results.get(agent)
            if result is None or result.failure:
                raise Abort(str((result and result.failure) or "no answer"), member=agent, round_=rec.index)
            if result.rejected:
                self.emit("fix_verify_rejected", member=agent, finding=rec.index)
                continue
            parsed = result.data
            assert isinstance(parsed, Extraction), "an answered, kept verdict always carries its parse"
            for entry in parsed.data.get("new_items") or []:
                claim = one_line(str(entry.get("claim") or ""))[:600]
                standing = self._open_item_for(claim)
                if standing is not None:
                    if agent not in standing.also_reported_by:
                        standing.also_reported_by.append(agent)
                    if standing.id in rec.review_findings and standing.id not in re_reported:
                        re_reported.append(standing.id)
                    continue
                item = Item(
                    id=self._new_c(),
                    claim=claim,
                    detail=one_line(str(entry.get("detail") or ""))[:2000],
                    evidence=one_line(str(entry.get("evidence") or ""))[:2000],
                    severity=str(entry.get("severity") or "minor"),
                    raised_by=[agent],
                    opened_round=rec.index,
                )
                self.docket.add_item(item)
                new_ids.append(item.id)
            for ask in parsed.data.get("human_input") or []:
                self._raise_ask(agent, ask, round_=rec.index)
            for note in parsed.data.get("notes") or []:
                raise_note(
                    self.docket,
                    by=agent,
                    text=note["text"],
                    item_id=resolve_note_item(self.docket, note),
                    round_=rec.index,
                    kind=note.get("kind", "info"),
                )
            for line in parsed.data.get("say_to") or []:
                self.docket.queue_message(to=line["to"], from_=agent, text=line["text"], stage=rec.index)
        rec.review_blind = all(result.rejected for result in results.values())
        if rec.review_blind:
            self.emit("fix_verify_unreadable", finding=rec.index)
        await self._settle_gate(rec, plan, results)
        held_new = [i for i in new_ids if self._holds_finding(self.docket.items[i], pass_number)]
        held_standing = [i for i in re_reported if self._holds_finding(self.docket.items[i], pass_number)]
        if new_ids:
            self.emit("fix_verify_findings", finding=rec.index, findings=new_ids)
            self.console.say(f"finding {rec.index}: the verify crew found {', '.join(new_ids)}")
        if held_new:
            rec.review_findings.extend(held_new)
        for item_id in [i for i in new_ids + re_reported if i not in held_new + held_standing]:
            if item_id in rec.review_findings:
                rec.review_findings.remove(item_id)
            item = self.docket.items[item_id]
            self.emit("fix_finding_not_holding", finding=rec.index, item=item_id, severity=item.severity)
        if held_standing:
            self.console.say(f"finding {rec.index}: the verify crew still finds {', '.join(held_standing)}")
        await self._checkpoint(terminal=False)
        return held_new + held_standing

    def _open_item_for(self, claim: str) -> Item | None:
        """The open item this claim already stands on, if any: a re-report joins it, never a duplicate."""
        key = normalize_text(claim)
        if not key:
            return None
        for item_id in self.docket.order:
            item = self.docket.items[item_id]
            if not item.closed and normalize_text(item.claim) == key:
                return item
        return None

    @staticmethod
    def _review_gate_section(rec: FindingRecord, plan: dict) -> str:
        """The verify's gate ask: run the finding's gate yourself, and report the verdict in the reply."""
        gate = plan.get("gate", "")
        if not gate:
            return "Gate: none named — the review is the check"
        parts = [
            f"Gate: {gate}",
            "",
            "Run the gate yourself — or verify its condition by other means — and report the verdict in your reply:",
            '```json\n"gate": {"ok": true, "ran": "what you ran or checked", "evidence": "its last lines"}\n```',
        ]
        if rec.gate_output:
            parts += ["", f"The builder says it saw: {_fold(rec.gate_output, 1000)}"]
        if rec.gate_rerun:
            parts += ["", "The last verdict:", "```", rec.gate_rerun, "```"]
        return "\n".join(parts)

    async def _settle_gate(self, rec: FindingRecord, plan: dict, results: dict[str, RawAsk]) -> None:
        """The finding's gate, as its verifier read it: the crew's verdicts, or the chair's when none read."""
        gate = plan.get("gate", "")
        if not gate:
            rec.gate_ok, rec.gate_rerun = True, ""
            self._close_gate_item(rec)
            return
        verdicts: list[tuple[str, bool, str, str]] = []
        for agent, result in results.items():
            if result.rejected or result.data is None:
                continue
            parsed = result.data.data.get("gate") if isinstance(result.data, Extraction) else None
            if not isinstance(parsed, dict):
                continue
            verdicts.append(
                (
                    self.names.get(agent, agent),
                    bool(parsed["ok"]),
                    str(parsed.get("ran") or ""),
                    str(parsed.get("evidence") or ""),
                )
            )
        if verdicts:
            rec.gate_ok = all(passed for _, passed, _, _ in verdicts)
            lines = [f"{who}: {ran or 'no run reported'}" + (f" — {evidence}" if evidence else "") for who, _, ran, evidence in verdicts]
            rec.gate_rerun = _fold("\n".join(lines), _GATE_EVIDENCE_CAP)
        else:
            ok, ran, evidence = await self._chair_gate_check(plan)
            rec.gate_ok, rec.gate_rerun = ok, _fold(" ".join(part for part in (ran, evidence) if part), _GATE_EVIDENCE_CAP)
        self._settle_gate_item(rec)

    async def _chair_gate_check(self, plan: dict) -> tuple[bool, str, str]:
        """The gate's fallback verifier: the run's chair, when the crew's verdicts did not read."""
        port = self._port(self.crews["builder"])
        ok, ran, evidence = await port.chair_gate_check(
            fix_text.chair_gate_task(gate=plan.get("gate", ""), state=plan.get("state", ""), project=self.project),
            round_=self.docket.round,
        )
        return ok, ran, evidence

    def _settle_gate_item(self, rec: FindingRecord) -> None:
        """The gate item follows the verdict: closed when it passes, standing while it fails."""
        if rec.gate_ok or not rec.plan.get("gate"):
            self._close_gate_item(rec)
        else:
            self._ensure_gate_item(rec)

    def _ensure_gate_item(self, rec: FindingRecord) -> None:
        if rec.gate_item:
            item = self.docket.items.get(rec.gate_item)
            if item is not None and not item.closed:
                return
            rec.gate_item = ""  # it closed when the gate last passed, and it has failed again: raise a fresh one
        item = Item(
            id=self._new_c(),
            claim=f"the fix's gate fails: {rec.plan.get('gate') or '(none named)'}",
            detail="the finding's gate was verified and it did not pass",
            evidence=rec.gate_rerun,
            severity="major",
            raised_by=[CHAIR_ID],
            opened_round=rec.index,
        )
        self.docket.add_item(item)
        rec.review_findings.append(item.id)
        rec.gate_item = item.id

    def _close_gate_item(self, rec: FindingRecord) -> None:
        if not rec.gate_item:
            return
        item = self.docket.items.get(rec.gate_item)
        if item is not None and not item.closed:
            item.disposition = AGREED
            item.closed_round = rec.index
        rec.gate_item = ""

    # ------------------------------------------------------------- block and commit

    async def _block_finding(self, rec: FindingRecord) -> None:
        """The finding is done for this run: the reasons are on the docket, and the tree goes back
        to where the finding found it — the failed fix's diff saved, the next finding starts clean."""
        rec.status = "blocked"
        reasons = [f"{item['id']}: {item['claim']}" for item in self._standing_items(rec) if item["id"] != rec.gate_item]
        if not rec.gate_ok:
            reasons.append(f"the gate still fails: {rec.plan.get('gate') or '(none named)'}")
        rec.blocked_reasons = reasons
        post = await asyncio.to_thread(self.tree.snapshot)
        if rec.pre_tree and post:
            diff = await asyncio.to_thread(self.tree.diff, rec.pre_tree, post)
            path = os.path.join(self.run_dir, f"finding-{rec.index:02d}.diff")
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(diff or "(no changes to show)")
                rec.diff_path = path
            except OSError:
                pass
        if rec.pre_tree:
            ok, why = await asyncio.to_thread(self.tree.restore, rec.pre_tree)
            if not ok:
                raise Abort(
                    f"finding {rec.index}: the rollback failed — {why}; the tree may not match the pre-tree, stop and look",
                    member=CHAIR_ID,
                    round_=rec.index,
                )
        self.last_tree = rec.pre_tree or self.last_tree
        self.emit("fix_finding_blocked", finding=rec.index, finding_id=rec.finding_id, reasons=reasons)
        self.console.say(
            f"finding {rec.index}/{len(self.selected)} is blocked: {len(reasons)} reason(s) — the tree is back to the pre-finding state"
        )

    async def _hold_or_carry_on(self, rec: FindingRecord) -> bool:
        """A blocked finding in a blocking run: the human's call — hold the run, or carry on."""
        if self.human != "blocking":
            return False
        question, _ = raise_question(
            self.docket,
            text=f"finding {rec.index} is blocked: hold the run here, or carry on with the next finding?",
            asker=CHAIR_ID,
            item_id=None,
            round_=rec.index,
            options=["carry on", "hold"],
            context="the blocked finding is rolled back; the later findings still fix on top of what has committed",
        )
        # the run's own gate: shown whether or not the seconding bar would clear it
        await self._checkpoint(terminal=False, waiting=True, forced=question)
        held = question.state == ANSWERED and picked_option(question, 2)
        if held:
            self.console.say("the human held the run: the remaining findings stay unfixed")
        return held

    async def _commit_finding(self, rec: FindingRecord) -> None:
        """The run's commit of a passed finding: the orchestrator's own commit, one per finding.
        The finding and its verifier items close on the docket as it lands."""
        rec.status = "fixed"
        item = self.docket.items[rec.finding_id]
        item.disposition = AGREED
        item.closed_round = rec.index
        for item_id in rec.review_findings:
            verifier = self.docket.items.get(item_id)
            if verifier is not None and not verifier.closed:
                verifier.disposition = AGREED
                verifier.closed_round = rec.index
        self._close_gate_item(rec)
        if not self.commit:
            self.console.say(f"finding {rec.index}: fixed — commit = false, the change stays uncommitted")
            return
        message = f"fix {rec.finding_id}: {rec.claim[:200]}"
        sha, why = await asyncio.to_thread(self.tree.commit, message)
        if not sha:
            if why == "nothing to commit":
                # the pass proved the gate with no tree change: the finding was not what the review said, and nothing to land
                rec.verify_note = "the gate passed with no tree change: the fix landed as no commit"
                self.console.say(f"finding {rec.index}: fixed with no tree change — the gate passed as the tree stood; nothing committed")
                return
            raise Abort(f"finding {rec.index}: the commit failed — {why}", member=CHAIR_ID, round_=rec.index)
        rec.commit_sha = sha
        self.execution.commits.append({"sha": sha, "finding": rec.finding_id, "message": message})
        self.last_tree = await asyncio.to_thread(self.tree.snapshot) or self.last_tree
        self.emit("fix_commit", finding=rec.index, finding_id=rec.finding_id, sha=sha, message=message[:120])

    # ------------------------------------------------------------- the final check

    async def _final_check(self) -> None:
        """The whole-project check over the finished tree: the fix mode's final-stage rule, run by
        the verifier when one stands, skipped when nothing was fixed or no verifier does."""
        if not any(rec.status == "fixed" for rec in self.selected):
            self.execution.final_check = {"ok": True, "ran": "", "evidence": "nothing was fixed: no final check to run"}
            return
        crew = self._verifier_crew()
        if crew is None or crew.brief is None:
            self.execution.final_check = {"ok": True, "ran": "", "evidence": "no verifier crew: the per-finding gates are the check"}
            return
        port = self._port(crew)
        self.docket.phase = "final check"
        ok, ran, evidence = await port.chair_gate_check(fix_text.final_check_task(self.project), round_=self.docket.round)
        self.execution.final_check = {"ok": ok, "ran": ran, "evidence": _fold(evidence, _GATE_EVIDENCE_CAP)}
        self.docket.phase = "fixing"
        self.emit("fix_final_check", ok=ok, ran=_fold(ran, 160))
        self.console.say("the final check " + ("passed" if ok else f"failed: {_fold(evidence, 200)}"))

    # ------------------------------------------------------------- the diff

    async def _stage_diff(self, changed: list[str]) -> str:
        """What the tree changed since the last verify, or the changed files where the snapshot failed."""
        if self.last_tree is not None:
            tree = await asyncio.to_thread(self.tree.snapshot)
            if tree is not None:
                diff = await asyncio.to_thread(self.tree.diff, self.last_tree, tree)
                if diff:
                    # only a diff the verify actually saw becomes the next verify's baseline
                    self.last_tree = tree
                    return diff
        parts: list[str] = []
        for path in dict.fromkeys(p for p in changed if p):
            full = os.path.join(self.project, path)
            with contextlib.suppress(OSError):
                with open(full, encoding="utf-8", errors="replace") as handle:
                    parts.append(f"### {path}\n```\n{handle.read()[:_FILE_CAP]}\n```")
        return "\n".join(parts)[:DIFF_CAP] or "(no changes to show)"

    # ------------------------------------------------------------- shared small bits

    def _drain(self, agent_id: str) -> list[dict]:
        return [{"to": m.to, "from_": m.from_, "text": m.text} for m in self.docket.drain_messages(agent_id)]

    def _new_c(self) -> str:
        item_id = f"C{self._next_c}"
        while item_id in self.docket.items:
            self._next_c += 1
            item_id = f"C{self._next_c}"
        self._next_c += 1
        return item_id

    def _raise_ask(self, agent: str, ask: dict, *, round_: int) -> None:
        text = str(ask.get("question") or "").strip()
        if not text:
            return
        raise_question(
            self.docket,
            text=text,
            asker=agent,
            item_id=None,
            round_=round_,
            context=str(ask.get("context") or ""),
            options=[str(o) for o in ask.get("options") or []] or None,
            blocking=bool(ask.get("blocking")),
        )

    def _standing_items(self, rec: FindingRecord) -> list[dict]:
        """The finding's review items still open on the docket: what a re-verify is judged against."""
        return [
            {"id": i.id, "claim": i.claim, "evidence": i.evidence}
            for i in (self.docket.items[i] for i in self.docket.order)
            if not i.closed and i.id in rec.review_findings
        ]

    def _finding_items(self, rec: FindingRecord) -> list[dict]:
        """The finding's standing items, for the builder's fix prompt."""
        out = self._standing_items(rec)
        if not rec.gate_ok and not any(i["id"] == rec.gate_item for i in out):
            out.append({"id": "—", "claim": f"the fix's gate fails: {rec.plan.get('gate')}", "evidence": rec.gate_rerun})
        return out
