"""The implement run's spine: plan, approval, staged build with the baton, and the outcome.

The crews stay up and down at phase boundaries; this class decides when. The shared docket
holds the run's items (the stage reviews' findings), its questions and its `say_to` lines,
so the report reads one docket for the whole run.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess

from ..console import Console
from ..crews import Crew, Crews
from ..extraction import Extraction, parse_replan, parse_work
from ..humaninput import Checkpoint
from ..port import Port
from ..prompts import implement as impl_text
from .engine import Abort, CheckpointOutcome, Engine, RawAsk
from .escalations import HELD_BACK, NO_HUMAN, NOT_WAITED, effective_class, held_back, raise_question, showable
from .notes import mark_shown, raise_note, unshown
from .state import (
    AGREED,
    ANSWERED,
    ASKED,
    CHAIR_ID,
    DECLINED,
    IMPLEMENTED,
    NEEDS_OUTCOME,
    NO_PREFERENCE,
    PARTIAL,
    SKIPPED,
    Docket,
    Execution,
    Item,
    Question,
    StageRecord,
    normalize_text,
)
from .termination import direction_rule, unsettled_questions

_DIFF_CAP = 12000
_FILE_CAP = 4000
#: the snapshot's git calls walk the worktree, so they get a long leash
_SNAPSHOT_TIMEOUT_S = 600
#: how much of the verifier's account of the gate a stage carries: the prompts and the report
_GATE_EVIDENCE_CAP = 2000
#: the builder passes on which a `trivial` finding still holds the stage open: the work pass and the first fix pass
#: from the third on, `minor` or higher is what keeps the stage open, so the ladder cannot churn on nits
_NIT_PASSES = 2

#: a work reply that carried nothing: what the run reads of a pass that was unreadable after its repairs
_NO_WORK: dict = {
    "summary": "",
    "gate_output": "",
    "changed": [],
    "user_text": [],
    "open_issues": [],
    "human_input": [],
    "notes": [],
    "say_to": [],
    "addressed": [],
}


def blind_note(rec: StageRecord) -> str:
    """The line the console and the report share for a stage that passed while the review was unreadable."""
    tail = "the chair verified the gate, and it passed" if rec.gate else "it named no gate, so nothing else checked the tree"
    return f"stage {rec.index}: the final review was unreadable — {tail}"


def _user_text_surfaces(project: str, entries: list[str]) -> tuple[list[str], list[str]]:
    """Which entries name files in the tree, and which do not."""
    kept: list[str] = []
    rejected: list[str] = []
    for entry in dict.fromkeys(entries):
        if os.path.isfile(os.path.join(project, entry)):
            kept.append(entry)
        else:
            rejected.append(entry)
    return kept, rejected


#: how many stages the approval lists before collapsing the rest
_APPROVAL_STAGES = 12


def _fold(text: object, cap: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:cap] + ("…" if len(flat) > cap else "")


def approval_context(stages: list[dict]) -> str:
    """The approval's context: the no-commit line and the adopted plan, stage by stage, so the
    human agrees to a plan — what lands and which gates will be checked — not a bare yes/no."""
    lines = [
        "the run will edit the project in place, stage by stage, and never commits; you review the diff when it is done",
        " " * 13 + f"the adopted plan, in build order — {len(stages)} stage(s):",
    ]
    for index, stage in enumerate(stages[:_APPROVAL_STAGES], 1):
        line = f"{index}. {_fold(stage.get('state'), 120) or '(unnamed)'}"
        gate = _fold(stage.get("gate"), 160)
        line += f" — gate: {gate}" if gate else " — no gate named"
        scope = _fold(stage.get("scope"), 80)
        if scope:
            line += f" — touches: {scope}"
        lines.append(" " * 13 + line)
    if len(stages) > _APPROVAL_STAGES:
        lines.append(" " * 13 + f"… and {len(stages) - _APPROVAL_STAGES} more stage(s)")
    return "\n".join(lines)


def snapshot_env(run_dir: str) -> dict[str, str]:
    """The git env the run's tree snapshots run under: a private index and object directory in the
    run directory, so the run's trees never enter the repo's object store."""
    return {
        "GIT_INDEX_FILE": os.path.join(run_dir, "review_index"),
        "GIT_OBJECT_DIRECTORY": os.path.join(run_dir, "review_objects"),
    }


class Implement:
    def __init__(
        self,
        *,
        crews: Crews,
        docket: Docket,
        checkpoint: Checkpoint,
        console: Console,
        plan_file: str = "",
        emit=None,
    ) -> None:
        self.crews = crews
        self.docket = docket
        self.checkpoint = checkpoint
        self.console = console
        self.plan_file = plan_file
        self.emit = emit or (lambda name, **fields: None)
        council = crews["builder"].council
        self.project = council.project
        self.human = council.human
        self.seconding = council.seconding
        self.planning_rounds = council.planning_rounds
        self.fix_budget = council.fix_budget
        self.replans = council.replans
        self.goal = council.goal or ""
        self.names = crews.names()
        execution = docket.execution
        if execution is None:
            execution = Execution()
            docket.execution = execution
        self.execution = execution
        self.stages: list[dict] = []
        self._asked_of_human: set[str] = set()  # asks a checkpoint put in front of the human
        self.run_dir = crews["builder"].run_dir
        self.baseline: str | None = None  # the tree the run starts from, or None without git
        self.last_tree: str | None = None  # the previous review's snapshot
        self._next_c = 1

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
        self.emit("implement_say_to_unsent", count=len(messages))
        for message in messages:
            who = self.names.get(message.from_, message.from_)
            to = self.names.get(message.to, message.to)
            self.console.say(f"say_to never landed: {who} to {to}: {message.text}")

    async def _run(self) -> str:
        if self.plan_file:
            try:
                document = self._read_plan_file()
            except OSError as failure:
                raise Abort(f"the plan document could not be read: {failure}", member=CHAIR_ID) from failure
            self.emit("implement_plan_structured", plan_file=self.plan_file)
            self.stages = await self._structure(document)
            self.execution.adopted, self.execution.adopted_how = "the plan document", "structured"
            self.execution.plan_text = document
        else:
            self.stages, self.execution.plan_text = await self._plan_phase()
            await self._classify()
        for index, stage in enumerate(self.stages, 1):
            self.execution.stages.append(
                StageRecord(
                    index=index,
                    state=stage.get("state", ""),
                    gate=stage.get("gate", ""),
                    scope=stage.get("scope", ""),
                    kind=stage.get("kind") or "code",
                )
            )
        if not await self._approval():
            self._mark_unasked()
            self.emit("termination", outcome=NEEDS_OUTCOME, reason="the human declined to proceed to implement")
            return NEEDS_OUTCOME
        await self._spawn_execution_crews()
        self.baseline = await asyncio.to_thread(self._snapshot_tree)
        self.last_tree = self.baseline
        self.execution.baseline = self.baseline or ""
        held = False
        for rec in self.execution.stages:
            if held:
                rec.status = "blocked"
                rec.blocked_reasons = ["the human held the run"]
                continue
            held = await self._build_stage(rec)
        final_tree = await asyncio.to_thread(self._snapshot_tree)
        self.execution.final_tree = final_tree or ""
        outcome = PARTIAL if self.execution.blocked else IMPLEMENTED
        self._mark_unasked()
        self.emit(
            "termination",
            outcome=outcome,
            reason=f"{len(self.execution.landed)} stage(s) landed, {len(self.execution.blocked)} blocked",
        )
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

    # ------------------------------------------------------------- planning

    async def _plan_phase(self) -> tuple[list[dict], str]:
        """The planning half: the planner crew converges on a direction."""
        crew = self.crews.planner
        assert crew is not None  # a run without --plan always convenes a planner crew
        plan_docket = Docket(
            mode="plan",
            goal=self.goal + "\n" + impl_text.PLAN_GOAL_TAIL,
            roster=list(crew.council.seat_ids),
            chair_name=crew.council.chair.name,
            seconding=self.seconding,
            human=self.human,
        )
        # the planning docket has its own checkpoint: the shared one is bound to the run's docket; a message
        # routes across the crews, so a seat not up yet is refused, not a KeyError
        plan_checkpoint = Checkpoint(
            plan_docket, self.checkpoint.reader, console=self.console, names=self.names, ask_seat=self.crews.send_message
        )
        port = self._port(crew, docket=plan_docket, checkpoint=plan_checkpoint)
        engine = Engine(
            plan_docket,
            port,
            goal=plan_docket.goal,
            project=self.project,
            brief=crew.brief,
            max_rounds=self.planning_rounds,
            grace_rounds=crew.council.grace_rounds,
            names=self.names,
            emit=self.emit,
        )
        self.emit("implement_planning_start", crew=crew.label, seats=len(plan_docket.roster), rounds=self.planning_rounds)
        self.docket.phase = "planning"
        await engine.run()
        token = await self._resolve_direction(plan_docket, engine)
        self._record_assumptions(plan_docket)
        artifact = engine.direction_plan(token)
        stages = [
            {
                "state": str(stage.get("state") or "").strip(),
                "gate": str(stage.get("gate") or "").strip(),
                "scope": str(stage.get("scope") or "").strip(),
                "exceptions": [str(e) for e in stage.get("exceptions") or [] if str(e).strip()],
                "kind": "code",
            }
            for stage in artifact.get("stages") or []
            if str(stage.get("state") or "").strip()
        ]
        if not stages:
            raise Abort(f"the adopted plan ({self.names.get(token, token)}) names no stages to build")
        # the planning half is over: the planner crew disposes at the boundary, the build runs on two crews
        await crew.dispose()
        self._planners_leave()
        return stages, engine.plan_text(token)

    async def _resolve_direction(self, plan_docket: Docket, engine: Engine) -> str:
        """The direction the run builds: the human's pick, then consensus, then a strict majority, then the run's chair."""
        if not plan_docket.directions():
            raise Abort("the planning phase ended without a direction: the run has nothing to build", member=CHAIR_ID, round_=0)
        question = plan_docket.direction_ask()
        if question is not None and question.state == ANSWERED and question.answer:
            for choice in plan_docket.directions():
                if question.answer == choice or question.answer.startswith(choice + ":"):
                    self._adopted(choice, "human", "the human chose")
                    return choice
            self.console.say(f"the human's pick ({question.answer}) matches no direction: the run adopts by the rules")
        satisfied, token, _reason = direction_rule(plan_docket)
        if satisfied and token:
            self._adopted(token, "consensus", "every planner backs it")
            return token
        counts: dict[str, int] = {}
        for backing in plan_docket.backing.values():
            if backing not in (None, NO_PREFERENCE, DECLINED):  # the same camps directions() counts: a decline is not one
                counts[backing] = counts.get(backing, 0) + 1
        if counts:
            best = max(sorted(counts), key=lambda t: counts[t])
            if counts[best] >= plan_docket.bar():  # bar() is the strict-majority threshold: n//2 + 1
                self._adopted(best, "majority", f"{counts[best]} of {plan_docket.n} planners back it")
                return best
        message = impl_text.pick_task(self.goal, [(t, engine.direction_option(t)[:300]) for t in plan_docket.directions()])
        # the D1 tie-break is the run's chair's pass (D2): the planner crew is done with its own
        picked = (await self._port(self.crews["builder"]).chair_pick(message)).get("backing") or ""
        if picked not in plan_docket.directions():
            raise Abort(f"the run's chair picked {picked!r}, which is not one of the plans' directions")
        self._adopted(picked, "chair", "the run's chair picked it")
        return picked

    def _adopted(self, token: str, kind: str, why: str = "") -> None:
        self.execution.adopted = token
        self.execution.adopted_how = kind
        self.console.say(f"direction adopted: {self.names.get(token, token)}" + (f" ({why})" if why else ""))

    def _record_assumptions(self, plan_docket: Docket) -> None:
        """The planning phase's decision points: every one into the report, the unsettled ones into the build.

        An answer is already baked into the plan the builders execute, so a settled decision is report
        material only; the open ones ride into the work prompt as "the plan's wording is the decision".
        """
        for qid in plan_docket.question_order:
            question = plan_docket.questions[qid]
            if question.direction_ask:
                continue
            if question.state == ANSWERED:
                settled = f"the human answered: {question.answer}"
            elif question.state == SKIPPED:
                settled = "the human declined: the run proceeds on the plan's wording"
            elif question.state == ASKED:
                settled = "the planning phase ended with it unanswered: the run proceeds on the plan's wording"
            else:
                continue
            line = f"{qid}: {question.text} — {settled}"
            self.execution.assumptions.append(line)
            if question.state != ANSWERED:
                self.execution.open_assumptions.append(line)

    # ------------------------------------------------------------- structure and classify

    async def _structure(self, document: str) -> list[dict]:
        """The run's chair turns a `--plan` document into the gated stages the run builds."""
        port = self._port(self.crews["builder"])
        self.docket.phase = "structuring the plan"
        data = await port.chair_structure(impl_text.structure_task(document, self.goal))
        stages = [clean for clean in (self._clean_stage(s) for s in data.get("stages") or []) if clean]
        if not stages:
            raise Abort("the chair structured the plan document into no stages: give it a document with something to build")
        return stages

    async def _classify(self) -> None:
        """The run's chair marks a stage `prose` when its entire artifact is user-visible text."""
        port = self._port(self.crews["builder"])
        self.docket.phase = "classifying the stages"
        data = await port.chair_structure(impl_text.classify_task(self.goal, self.stages))
        kinds = [str(stage.get("kind") or "code") for stage in data.get("stages") or []]
        for index, stage in enumerate(self.stages):
            kind = kinds[index] if index < len(kinds) else "code"
            stage["kind"] = kind if kind in ("code", "prose") else "code"

    @staticmethod
    def _clean_stage(stage: object) -> dict:
        if not isinstance(stage, dict) or not str(stage.get("state") or "").strip():
            return {}
        kind = str(stage.get("kind") or "").strip().lower()
        return {
            "state": str(stage.get("state") or "").strip(),
            "gate": str(stage.get("gate") or "").strip(),
            "scope": str(stage.get("scope") or "").strip(),
            "exceptions": [str(e) for e in stage.get("exceptions") or [] if str(e).strip()],
            "kind": kind if kind in ("code", "prose") else "code",
        }

    # ------------------------------------------------------------- approval

    async def _approval(self) -> bool:
        """The run's chair asks to proceed; a blocking run waits, the others record and keep going."""
        who = self.names.get(self.execution.adopted, self.execution.adopted)
        if self.execution.adopted in self.names:
            text = f"{who}'s plan is adopted and the run is ready: implement it in the real tree?"
        else:
            text = f"{who} is adopted and the run is ready: implement it in the real tree?"
        question, _ = raise_question(
            self.docket,
            text=text,
            asker=CHAIR_ID,
            item_id=None,
            round_=0,
            options=["proceed", "hold"],
            context=approval_context(self.stages),
        )
        if self.human == "none":
            if NO_HUMAN not in question.markers:
                question.markers.append(NO_HUMAN)
            self.execution.approval = "recorded"
            return True
        # the run's own gate, raised by the chair: the seconding bar is for the seats' asks, so the
        # question is shown whether or not it would clear it; a hold still lands in the report, not a refusal
        await self._checkpoint(terminal=False, waiting=self.human == "blocking", forced=question)
        if self.human != "blocking":
            if NOT_WAITED not in question.markers:
                question.markers.append(NOT_WAITED)
            self.execution.approval = "recorded"
            return True
        granted = question.state == ANSWERED and self._picked(question, 1)
        self.execution.approval = "granted" if granted else "declined"
        return granted

    @staticmethod
    def _picked(question: Question, option_number: int) -> bool:
        """The explicit pick of option N: its number, or its own words and nothing else.

        A number is stored as the option's words, so the words are all there is to compare. A
        free-text qualifier is not a decision: the run reads one bit from its own gates and
        applies nothing else, so anything that does not say the option is no pick of it.
        """
        return (question.answer or "").strip().casefold() == question.options[option_number - 1].strip().casefold()

    # ------------------------------------------------------------- the checkpoint

    async def _checkpoint(
        self, *, terminal: bool = False, waiting: bool | None = None, forced: Question | None = None
    ) -> CheckpointOutcome | None:
        """The run's side of the human: the shared docket's open asks, at a stage boundary.

        `forced` is the run's own gate (the approval): shown whether or not the seconding bar holds it back.
        A stage boundary is the run's round boundary: what the human typed while the build worked is
        drained here, the way the engine drains at its own.
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
        if forced is not None and forced not in asked:
            asked.append(forced)
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
                asked, ask_set=asked, round_=self.docket.round, terminal=terminal, waiting=waiting, notes=pending_notes
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
        reason, the rest was never asked."""
        for question in unsettled_questions(self.docket):
            if NO_HUMAN in question.markers or any(marker in question.markers for marker in HELD_BACK):
                continue
            if question.id in self._asked_of_human:
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
                question.recommendation, question.recommendation_round = text, self.docket.round

    # ------------------------------------------------------------- the crews

    async def _spawn_execution_crews(self) -> None:
        planner = self.crews.planner
        if planner is not None:
            # the builders sat the planning out as the run's chair: their seats join now
            await self.crews["builder"].start_seats()
        reviewer = self.crews.reviewer
        if reviewer is not None:
            await reviewer.start(seats=True)
        self.docket.phase = "building"

    def _builder_for(self, stage: dict) -> str:
        """The baton for this stage: a prose stage goes to the dedicated prose seat when there is
        one — a `both` seat is a fallback builder, not the prose writer — else the first builder
        whose role covers the stage, in roster order."""
        builders = list(self.crews["builder"].council.agents)
        role = stage.get("kind") or "code"
        if role == "prose":
            candidates = [spec for spec in builders if spec.role == "prose"] or [spec for spec in builders if spec.role == "both"]
        else:
            candidates = [spec for spec in builders if spec.role in (role, "both")]
        if not candidates:
            candidates = builders
            fallback = candidates[0]
            who = self.names.get(fallback.id or "", fallback.id or "")
            self.console.say(f"no builder seats take the {role} work: {who} builds it anyway")
        return candidates[0].id or ""

    def _prose_seat(self) -> str | None:
        # the dedicated prose seat: a `both` seat is a fallback builder, not the prose pass's writer
        for spec in self.crews["builder"].council.agents:
            if spec.role == "prose":
                return spec.id
        return None

    def _planners_leave(self) -> None:
        """The planner crew is disposed: its seats leave the shared docket, so the bar the build's
        stops are measured against counts the live crew, not the planners who are gone."""
        crew = self.crews.planner
        assert crew is not None
        gone = {spec.id for spec in crew.council.agents if spec.id}
        self.docket.roster = [seat for seat in self.docket.roster if seat not in gone]

    # ------------------------------------------------------------- a stage

    async def _build_stage(self, rec: StageRecord) -> bool:
        """Build one stage. Returns True when the human held the run here."""
        stage = self.stages[rec.index - 1]
        rec.builder = self._builder_for(stage)
        self.docket.round = rec.index
        self.emit(
            "implement_stage_start",
            stage=rec.index,
            total=len(self.stages),
            builder=rec.builder,
            state=rec.state,
            gate=rec.gate,
            kind=rec.kind,
        )
        self.console.say(
            f"stage {rec.index}/{len(self.stages)}: {rec.state} — the baton goes to {self.names.get(rec.builder, rec.builder)}"
        )
        data = await self._work_pass(rec, stage)
        await self._maybe_prose_pass(rec, stage, data)
        findings = await self._stage_review(rec, stage=stage, changed=[*data["changed"], *data["user_text"]], pass_number=1)
        fix_pass = 0
        while (findings or not rec.gate_ok) and fix_pass < self.fix_budget:
            fix_pass += 1
            data = await self._fix_pass(rec, stage, self._stage_items(rec), fix_pass)
            await self._maybe_prose_pass(rec, stage, data)
            findings = await self._stage_review(
                rec, stage=stage, changed=data["changed"], addressed=data.get("addressed"), pass_number=1 + fix_pass
            )
        replan = 0
        while (findings or not rec.gate_ok) and replan < self.replans:
            replan += 1
            revised, note = await self._replan_pass(rec, stage, self._stage_items(rec), replan)
            rec.replans.append({"pass": replan, "note": note, "stages": len(revised)})
            if not revised:
                # the replan was unreadable: no work was attempted this rung, so it ends without a re-check
                continue
            for sub in revised:
                sub["kind"] = stage.get("kind") or "code"
            data = await self._fix_pass(rec, stage, self._stage_items(rec), fix_pass + replan, revised=revised)
            await self._maybe_prose_pass(rec, stage, data)
            findings = await self._stage_review(
                rec, stage=stage, changed=data["changed"], addressed=data.get("addressed"), pass_number=1 + fix_pass + replan
            )
        if findings or not rec.gate_ok:
            return await self._block_stage(rec)
        rec.status = "passed"
        if rec.review_blind:
            self.console.say(blind_note(rec))
        self._close_stage_items(rec)
        self.emit("implement_stage_passed", stage=rec.index, gate=rec.gate_rerun[:200], fix_passes=len(rec.fix_passes))
        self.console.say(f"stage {rec.index}/{len(self.stages)} passed")
        return False

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
                    note = "you" if spec.id == me else ("prose" if spec.role == "prose" else "")
                else:
                    note = label
                out.append((spec.id, note))
        return out

    def _work_task(self, rec: StageRecord, stage: dict) -> str:
        messages = self._drain(rec.builder)
        return impl_text.work_task(
            goal=self.goal,
            stage=stage,
            index=rec.index,
            total=len(self.stages),
            plan_text=self.execution.plan_text,
            assumptions=self.execution.open_assumptions or None,
            messages=messages or None,
            seats=self._seats(rec.builder),
            human=self.human,
        )

    async def _work_pass(self, rec: StageRecord, stage: dict) -> dict:
        result = await self._port(self.crews["builder"]).builder_work(rec.builder, self._work_task(rec, stage), round_=rec.index)
        if result.failure:
            raise Abort(result.failure, member=rec.builder, round_=rec.index)
        parsed = parse_work(result.text or "")
        data = self._apply_work(rec, parsed)
        if parsed.invalid or parsed.errors:
            # an unreadable reply does not kill the run: the gate and the review judge what actually landed
            self.console.say(
                f"stage {rec.index}: the builder's work reply was unreadable after its repair re-asks — "
                "the gate and the review will judge what landed"
            )
            self.emit("implement_work_unreadable", stage=rec.index, seat=rec.builder, reason="; ".join(parsed.errors))
        return {**_NO_WORK, **data}

    def _fix_task(self, rec: StageRecord, stage: dict, items: list[dict], pass_number: int, revised: list[dict] | None = None) -> str:
        messages = self._drain(rec.builder)
        seats = self._seats(rec.builder)
        if revised:
            parts = [
                "# The revised stage — one fix pass of its own",
                "",
                f"Stage {rec.index}/{len(self.stages)} is stuck, so it was replanned into a smaller breakdown. Implement it, address the",
                "standing findings, and satisfy the stage's gate again.",
                "",
                f"Goal: {self.goal}",
                "",
                "## The revised breakdown",
                "",
            ]
            parts += [impl_text.stage_block(i, len(revised), s) for i, s in enumerate(revised, 1)]
            parts += ["", "## The findings still standing"]
            parts += [f"- {item['id']}: {item['claim']}" + (f" — {item['evidence']}" if item.get("evidence") else "") for item in items]
            if rec.gate_rerun:
                parts += ["", "The last gate verdict:", "```", rec.gate_rerun, "```"]
            parts += ["", impl_text.seat_roster(seats)]
            if messages:
                parts += ["", impl_text.messages_section(messages)]
            steps = [
                "1. Implement the revised breakdown until the stage's final state is reached.",
                "2. Address every standing finding — or show, with cited evidence, that it is wrong.",
            ]
            if stage.get("gate"):
                steps.append(f"{len(steps) + 1}. Satisfy the stage's gate again — re-run it, or re-verify its condition; it must pass.")
            steps.append(f"{len(steps) + 1}. Report with the work shape: `addressed` names each item and what you did about it.")
            parts += [
                "",
                "## Do this",
                "",
                *steps,
                "",
                impl_text.SAY_TO,
                impl_text.work_schema(),
                impl_text.BUILDER_WORKSPACE,
            ]
            return "\n".join(parts)
        return impl_text.fix_task(
            goal=self.goal,
            stage=stage,
            index=rec.index,
            total=len(self.stages),
            pass_number=pass_number,
            items=items,
            gate_output=rec.gate_rerun,
            messages=messages or None,
            seats=seats,
            human=self.human,
        )

    async def _fix_pass(
        self,
        rec: StageRecord,
        stage: dict,
        items: list[dict],
        pass_number: int,
        *,
        revised: list[dict] | None = None,
    ) -> dict:
        result = await self._port(self.crews["builder"]).builder_work(
            rec.builder,
            self._fix_task(rec, stage, items, pass_number, revised),
            round_=rec.index,
            phase="fix",
        )
        if result.failure:
            raise Abort(result.failure, member=rec.builder, round_=rec.index)
        parsed = parse_work(result.text or "")
        data = self._apply_work(rec, parsed, fix_pass=pass_number)
        if parsed.invalid or parsed.errors:
            # the pass is spent either way: the ladder counts it, the gate and the review judge the result
            self.console.say(
                f"stage {rec.index}: fix pass {pass_number} was unreadable after its repair re-asks — "
                "it counts against the stage's fix budget"
            )
            self.emit(
                "implement_fix_unreadable",
                stage=rec.index,
                seat=rec.builder,
                pass_number=pass_number,
                reason="; ".join(parsed.errors),
            )
        return {**_NO_WORK, **data}

    async def _replan_pass(self, rec: StageRecord, stage: dict, items: list[dict], pass_number: int) -> tuple[list[dict], str]:
        messages = self._drain(rec.builder)
        task = impl_text.replan_task(
            goal=self.goal,
            stage=stage,
            index=rec.index,
            total=len(self.stages),
            pass_number=pass_number,
            items=items,
            gate_output=rec.gate_rerun,
            messages=messages or None,
            seats=self._seats(rec.builder),
        )
        result = await self._port(self.crews["builder"]).builder_replan(rec.builder, task, round_=rec.index)
        if result.failure:
            raise Abort(result.failure, member=rec.builder, round_=rec.index)
        parsed = parse_replan(result.text or "")
        revised = [clean for clean in (self._clean_stage(s) for s in parsed.data.get("stages") or []) if clean]
        if parsed.invalid or not revised:
            # the rung is spent either way: the next rung (or the block) follows
            self.console.say(
                f"stage {rec.index}: replan {pass_number} was unreadable after its repair re-asks — "
                "it counts against the stage's replan budget"
            )
            self.emit(
                "implement_replan_unreadable",
                stage=rec.index,
                seat=rec.builder,
                pass_number=pass_number,
                reason="; ".join(parsed.errors) or "it named no stages",
            )
            return [], "the replan was unreadable"
        return revised, str(parsed.data.get("note") or "")

    async def _maybe_prose_pass(self, rec: StageRecord, stage: dict, data: dict) -> None:
        if data["user_text"] and rec.kind == "code":
            await self._prose_pass(rec, stage, data)

    async def _prose_pass(self, rec: StageRecord, stage: dict, data: dict) -> None:
        """The prose seat rewrites the builder's user-visible text; skipped when the builder was the prose seat."""
        prose = self._prose_seat()
        if prose is None or prose == rec.builder:
            return
        surfaces: list[tuple[str, str]] = []
        for path in data["user_text"]:
            full = os.path.join(self.project, path)
            with contextlib.suppress(OSError):
                with open(full, encoding="utf-8", errors="replace") as handle:
                    surfaces.append((path, handle.read()[:_FILE_CAP]))
        if not surfaces:
            return
        task = impl_text.prose_task(
            goal=self.goal,
            stage=stage,
            index=rec.index,
            total=len(self.stages),
            surfaces=surfaces,
            gate_output=rec.gate_output,  # the builder's report of the gate from its last pass
            messages=self._drain(prose) or None,
            seats=self._seats(prose),
        )
        result = await self._port(self.crews["builder"]).builder_work(prose, task, round_=rec.index, phase="prose")
        if result.failure:
            raise Abort(result.failure, member=prose, round_=rec.index)
        parsed = parse_work(result.text or "")
        if parsed.invalid:
            self.console.say(f"the prose pass was unreadable: {'; '.join(parsed.errors)} — the builder's text stands")
            return
        rec.prose_pass = True
        data["changed"].extend(parsed.data.get("changed") or [])
        rec.changed.extend(dict.fromkeys(parsed.data.get("changed") or []))
        self.emit("implement_prose_pass", stage=rec.index, seat=prose, surfaces=len(surfaces))
        self.console.say(f"the prose pass rewrote {len(surfaces)} surface(s) — {self.names.get(prose, prose)}")

    # ------------------------------------------------------------- review

    @staticmethod
    def _holds_stage(item: Item, pass_number: int) -> bool:
        """Whether a finding holds the stage open at this rung: anything through the first two builder
        passes; from the third on, `minor` or higher only, so the ladder cannot churn on nits."""
        return pass_number <= _NIT_PASSES or item.severity != "trivial"

    async def _stage_review(
        self,
        rec: StageRecord,
        *,
        stage: dict,
        changed: list[str],
        pass_number: int,
        addressed: list[dict] | None = None,
    ) -> list[str]:
        """The review crew's scoped pass: one review round over the stage's diff — and the gate, which the crew runs —
        the findings onto the shared docket, the gate verdict onto the stage record. `pass_number` is the
        builder pass just reviewed: a finding holds the stage open only while its severity holds it there."""
        crew = self.crews.reviewer
        if crew is None or crew.brief is None:
            await self._settle_gate(rec, stage, {})
            return []
        brief = crew.brief
        diff = await self._stage_diff(changed)
        sections = [
            "",
            "## The stage",
            "",
            f"Stage {rec.index}/{len(self.stages)} — final state: {stage.get('state', '')}",
            self._review_gate_section(rec, stage),
        ]
        open_findings = self._standing_items(rec)
        if open_findings:
            sections += ["", "## The findings still standing"]
            sections += [
                f"- {item['id']}: {item['claim']}" + (f" — {item['evidence']}" if item["evidence"] else "") for item in open_findings
            ]
            sections.append(
                "Re-report any that are still wrong, with the same claim: the re-report joins the standing item."
            )
        sections += [
            "",
            "## What the builder did since the last review",
            "",
            f"{self._activity_line(rec.builder)} · changed: {', '.join(changed) if changed else 'nothing'}",
            "",
            "## The diff since the last review",
            "",
            "```",
            diff or "(no changes to show)",
            "```",
        ]
        if addressed:
            sections += ["", "## What the builder says it addressed"]
            sections += [f"- {a['id']}: {a['how']}" for a in addressed]
            sections.append("Re-review against those claims: report only what is still wrong.")
        goal = f"Review the work of stage {rec.index} of {len(self.stages)} against its final state: {stage.get('state', '')}"
        base = brief.phase0(mode="review", goal=goal, project=self.project) + "\n".join(sections) + "\n" + impl_text.SAY_TO
        port = self._port(crew)
        self.emit("implement_stage_review", stage=rec.index, crew=crew.label, seats=len(crew.council.seat_ids))
        batch = []
        for agent in crew.council.seat_ids:
            extra = "\n" + impl_text.seat_roster(self._seats(agent))
            if messages := self._drain(agent):
                extra += "\n" + impl_text.messages_section(messages)
            batch.append((agent, base + extra))
        results = await port.ask_seats(batch, round_=rec.index, phase="phase0", classify_unusable=True)
        new_ids: list[str] = []
        re_reported: list[str] = []
        for agent in crew.council.seat_ids:
            result = results.get(agent)
            if result is None or result.failure:
                raise Abort(str((result and result.failure) or "no answer"), member=agent, round_=rec.index)
            if result.rejected:
                self.emit("stage_review_rejected", member=agent, stage=rec.index)
                continue
            parsed = result.data
            assert isinstance(parsed, Extraction), "an answered, kept verdict always carries its parse"
            for entry in parsed.data.get("new_items") or []:
                claim = str(entry.get("claim") or "")[:600]
                standing = self._open_item_for(claim)
                if standing is not None:
                    # the same defect under the same words: one item, the re-report joins it
                    if agent not in standing.also_reported_by:
                        standing.also_reported_by.append(agent)
                    if standing.id in rec.findings and standing.id not in re_reported:
                        re_reported.append(standing.id)
                    continue
                item = Item(
                    id=self._new_c(),
                    claim=claim,
                    detail=str(entry.get("detail") or "")[:2000],
                    evidence=str(entry.get("evidence") or "")[:2000],
                    severity=str(entry.get("severity") or "minor"),
                    raised_by=[agent],
                    opened_round=rec.index,
                )
                self.docket.add_item(item)
                new_ids.append(item.id)
            for ask in parsed.data.get("human_input") or []:
                self._raise_ask(agent, ask, round_=rec.index)
            for note in parsed.data.get("notes") or []:
                raise_note(self.docket, by=agent, text=str(note)[:600], item_id=None, round_=rec.index)
            for line in parsed.data.get("say_to") or []:
                self.docket.queue_message(to=line["to"], from_=agent, text=line["text"], stage=rec.index)
        # every seat's verdict came back unreadable: the review saw nothing, and the gate with it
        rec.review_blind = all(result.rejected for result in results.values())
        if rec.review_blind:
            self.emit("implement_stage_review_unreadable", stage=rec.index)
        await self._settle_gate(rec, stage, results)
        held_new = [i for i in new_ids if self._holds_stage(self.docket.items[i], pass_number)]
        held_standing = [i for i in re_reported if self._holds_stage(self.docket.items[i], pass_number)]
        if new_ids:
            self.emit("implement_stage_findings", stage=rec.index, findings=new_ids)
            self.console.say(f"stage {rec.index}: the review crew found {', '.join(new_ids)}")
        if held_new:
            rec.findings.extend(held_new)
        for item_id in [i for i in new_ids + re_reported if i not in held_new + held_standing]:
            # the finding is on the docket and in the report, but it no longer holds the stage: out of the stage's own record
            if item_id in rec.findings:
                rec.findings.remove(item_id)
            item = self.docket.items[item_id]
            self.emit("implement_finding_not_holding", stage=rec.index, finding=item_id, severity=item.severity)
            self.console.say(
                f"stage {rec.index}: {item_id} is {item.severity} — past the first {_NIT_PASSES} passes it does not hold "
                "the stage open; it stays on the docket"
            )
        if held_standing:
            self.emit("implement_stage_findings_standing", stage=rec.index, findings=held_standing)
            self.console.say(f"stage {rec.index}: the review crew still finds {', '.join(held_standing)}")
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
    def _review_gate_section(rec: StageRecord, stage: dict) -> str:
        """The review's gate ask: run the stage's gate yourself, and report the verdict in the reply."""
        gate = stage.get("gate", "")
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

    async def _settle_gate(self, rec: StageRecord, stage: dict, results: dict[str, RawAsk]) -> None:
        """The stage's gate, as its verifier read it: the crew's verdicts, or the chair's when none read.

        The gate item follows the verdict: closed when it passes, standing while it fails.
        """
        gate = stage.get("gate", "")
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
            rec.gate_ok, rec.gate_rerun = await self._chair_gate_check(stage)
        if rec.gate_ok:
            self._close_gate_item(rec)
        else:
            self._ensure_gate_item(rec)

    async def _chair_gate_check(self, stage: dict) -> tuple[bool, str]:
        """The gate's fallback verifier: the run's chair, when the crew's verdicts did not read."""
        port = self._port(self.crews["builder"])
        ok, ran, evidence = await port.chair_gate_check(
            impl_text.chair_gate_task(gate=stage.get("gate", ""), state=stage.get("state", ""), project=self.project),
            round_=self.docket.round,
        )
        return ok, _fold(" ".join(part for part in (ran, evidence) if part), _GATE_EVIDENCE_CAP)

    def _drain(self, agent_id: str) -> list[dict]:
        return [{"to": m.to, "from_": m.from_, "text": m.text} for m in self.docket.drain_messages(agent_id)]

    def _activity_line(self, agent_id: str) -> str:
        member = self.crews["builder"].seat(agent_id)
        if member is None or member.rpc is None:
            return "(the builder is out of reach)"
        activity = member.rpc.activity
        tool = f"last tool: {activity.last_tool}" if activity.last_tool else "no tool in flight"
        return f"{activity.events} event(s) · {tool}"

    # ------------------------------------------------------------- blocking a stage

    async def _block_stage(self, rec: StageRecord) -> bool:
        """The stage is done for this run. The reasons are the stage's items still standing on the
        docket — not just the last pass's — and, when the gate still fails, the gate its own line.
        In a blocking run the human is put to the call: hold or carry on."""
        rec.status = "blocked"
        reasons = [f"{item['id']}: {item['claim']}" for item in self._standing_items(rec) if item["id"] != rec.gate_item]
        if not rec.gate_ok:
            reasons.append(f"the gate still fails: {rec.gate or '(none named)'}")
        rec.blocked_reasons = reasons
        self.emit("implement_stage_blocked", stage=rec.index, reasons=reasons)
        self.console.say(
            f"stage {rec.index}/{len(self.stages)} is blocked after {self.fix_budget} fix pass(es) and {self.replans} replan(s)"
        )
        if self.human != "blocking":
            return False
        question, _ = raise_question(
            self.docket,
            text=f"stage {rec.index} is blocked: hold the run here, or carry on with the next stage?",
            asker=CHAIR_ID,
            item_id=None,
            round_=rec.index,
            options=["carry on", "hold"],
            context="the stage's findings are on the docket; the later stages still build on top of what has landed",
        )
        # the run's own gate, like the approval: the seconding bar is for the seats' asks, so the
        # question is shown whether or not it would clear it
        await self._checkpoint(terminal=False, waiting=True, forced=question)
        held = question.state == ANSWERED and self._picked(question, 2)
        if held:
            self.console.say("the human held the run: the remaining stages stay unbuilt")
        return held

    # ------------------------------------------------------------- work replies

    def _new_c(self) -> str:
        item_id = f"C{self._next_c}"
        while item_id in self.docket.items:
            self._next_c += 1
            item_id = f"C{self._next_c}"
        self._next_c += 1
        return item_id

    def _record_user_text(self, rec: StageRecord, data: dict) -> None:
        """user_text must name files in the tree: the prose seat rewrites them, and the report prints them."""
        kept, rejected = _user_text_surfaces(self.project, data.get("user_text") or [])
        for entry in rejected:
            self.emit("implement_user_text_rejected", stage=rec.index, seat=rec.builder, entry=entry)
            self.docket.queue_message(
                to=rec.builder,
                from_=CHAIR_ID,
                text=f"stage {rec.index}: user_text entry {entry!r} is not a file in the project — "
                "name the paths of the files holding user-visible text, or omit the field",
                stage=rec.index,
            )
            self.console.say(f"stage {rec.index}: user_text entry {entry!r} is not a file in the project — dropped, the builder is told")
        data["user_text"] = kept
        rec.user_text = list(dict.fromkeys([*rec.user_text, *kept]))

    def _apply_work(self, rec: StageRecord, parsed, *, fix_pass: int | None = None) -> dict:
        """What a work reply puts on the record: the docket side effects, whatever the shape check said."""
        data = parsed.data
        rec.gate_output = str(data.get("gate_output") or "")
        for ask in data.get("human_input") or []:
            self._raise_ask(rec.builder, ask, round_=rec.index)
        for note in data.get("notes") or []:
            raise_note(self.docket, by=rec.builder, text=str(note)[:600], item_id=None, round_=rec.index)
        for line in data.get("say_to") or []:
            self.docket.queue_message(to=line["to"], from_=rec.builder, text=line["text"], stage=rec.index)
        rec.open_items = [str(o) for o in data.get("open_issues") or []]
        rec.changed.extend(dict.fromkeys(data.get("changed") or []))
        self._record_user_text(rec, data)
        if fix_pass is not None:
            rec.fix_passes.append({"pass": fix_pass, "addressed": data.get("addressed") or []})
        return data

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

    def _ensure_gate_item(self, rec: StageRecord) -> None:
        if rec.gate_item:
            item = self.docket.items.get(rec.gate_item)
            if item is not None and not item.closed:
                return
            rec.gate_item = ""  # it closed when the gate last passed, and it has failed again: raise a fresh one
        item = Item(
            id=self._new_c(),
            claim=f"the stage's gate fails: {rec.gate or '(none named)'}",
            detail="the stage's gate was verified and it did not pass",
            evidence=rec.gate_rerun,
            severity="major",
            raised_by=[CHAIR_ID],
            opened_round=rec.index,
        )
        self.docket.add_item(item)
        rec.findings.append(item.id)
        rec.gate_item = item.id

    def _standing_items(self, rec: StageRecord) -> list[dict]:
        """The stage's review items still open on the docket: what a re-review is judged against."""
        return [
            {"id": i.id, "claim": i.claim, "evidence": i.evidence}
            for i in (self.docket.items[i] for i in self.docket.order)
            if not i.closed and i.id in rec.findings
        ]

    def _stage_items(self, rec: StageRecord) -> list[dict]:
        """The stage's standing items, for the builder's fix prompt."""
        out = self._standing_items(rec)
        if not rec.gate_ok and not any(i["id"] == rec.gate_item for i in out):
            out.append({"id": "—", "claim": f"the stage's gate fails: {rec.gate}", "evidence": rec.gate_rerun})
        return out

    def _close_gate_item(self, rec: StageRecord) -> None:
        if not rec.gate_item:
            return
        item = self.docket.items.get(rec.gate_item)
        if item is not None and not item.closed:
            item.disposition = AGREED
            item.closed_round = rec.index

    def _close_stage_items(self, rec: StageRecord) -> None:
        for item_id in rec.findings:
            item = self.docket.items.get(item_id)
            if item is not None and not item.closed:
                item.disposition = AGREED
                item.closed_round = rec.index

    # ------------------------------------------------------------- the diff

    def _git(self, *args: str, env: dict | None = None, timeout: float = 60) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                ["git", "-C", self.project, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **(env or {})},
            )
        except OSError, subprocess.TimeoutExpired:
            return False, ""
        return proc.returncode == 0, (proc.stdout or "").strip()

    def _snapshot_tree(self) -> str | None:
        """The project's worktree as a tree object, in the run directory's private index and object store.

        The add is scoped to the project directory: a project that is a subdirectory of a larger
        repo snapshots only what the run may touch. The index is fresh, never a copy of the repo's:
        a copied stat cache can claim a file unchanged when its mtime matches to the second, and the
        tree must hold what is on disk.
        """
        env = snapshot_env(self.run_dir)
        with contextlib.suppress(OSError):
            os.remove(env["GIT_INDEX_FILE"])
        # git refuses a repo whose object directory does not exist yet, so the private store is created first
        os.makedirs(env["GIT_OBJECT_DIRECTORY"], exist_ok=True)
        if not self._git("add", "-A", "--", ".", env=env, timeout=_SNAPSHOT_TIMEOUT_S)[0]:
            return None
        ok, tree = self._git("write-tree", env=env, timeout=_SNAPSHOT_TIMEOUT_S)
        return tree or None

    async def _stage_diff(self, changed: list[str]) -> str:
        """What the tree changed since the last review, or the changed files where the project has no git.

        The git calls walk the worktree, so they run off the loop.
        """
        if self.last_tree is not None:
            tree = await asyncio.to_thread(self._snapshot_tree)
            if tree is not None:
                prev = self.last_tree
                ok, diff = await asyncio.to_thread(
                    self._git, "diff", prev, tree, env=snapshot_env(self.run_dir), timeout=_SNAPSHOT_TIMEOUT_S
                )
                if ok:
                    # only a diff the review actually saw becomes the next review's baseline
                    self.last_tree = tree
                    return (diff or "(no changes to show)")[:_DIFF_CAP]
        parts: list[str] = []
        for path in dict.fromkeys(p for p in changed if p):
            full = os.path.join(self.project, path)
            with contextlib.suppress(OSError):
                with open(full, encoding="utf-8", errors="replace") as handle:
                    parts.append(f"### {path}\n```\n{handle.read()[:_FILE_CAP]}\n```")
        return "\n".join(parts)[:_DIFF_CAP]

    def _read_plan_file(self) -> str:
        with open(self.plan_file, encoding="utf-8", errors="replace") as handle:
            return handle.read()
