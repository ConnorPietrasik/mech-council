from __future__ import annotations

from dataclasses import dataclass, field

AGREE, DISAGREE, NEEDS_HUMAN, ABSTAIN = "agree", "disagree", "needs_human", "abstain"
STANCES = (AGREE, DISAGREE, NEEDS_HUMAN, ABSTAIN)

OPEN = "open"
AGREED, UNHEARD, UNSEEN, DISPUTED, HUMAN, REJECTED, SPLIT = ("agreed", "unheard", "unseen", "disputed", "human", "rejected", "split")

STANDING, WITHDRAWN, RETIRED, DEMOTED = "standing", "withdrawn", "retired", "demoted"
ASKED, ANSWERED, SKIPPED = "asked", "answered", "skipped"
RETRACTED = "retracted"

CONSENSUS, NEEDS_OUTCOME, STALEMATE, ROUND_LIMIT, ABORTED = ("consensus", "needs_human", "stalemate", "round_limit_reached", "aborted")
#: everyone signed off and nothing is disputed, but an open item reached no disposition: a result, not an agreement
UNVETTED = "unvetted"

#: implement: every stage passed its gate and review
IMPLEMENTED = "implemented"
#: implement: some stages landed, some are blocked
PARTIAL = "partial"

#: fix: every selected finding landed, and the final check passed
FIXED = "fixed"
#: fix: the review found nothing to fix
NOTHING_TO_FIX = "nothing_to_fix"

NO_PREFERENCE = "no_preference"
DECLINED = "declined"
#: a `backing` naming a merge one seat wrote down: `merged: <label>`
MERGED = "merged:"

#: the termination order's "keep going", which is not an outcome
GO = "go"

#: the chair's protocol label; never one of `AgentN`
CHAIR_ID = "chair"

#: the docket's phase while the checkpoint holds for an answer
WAITING = "waiting for the human"


#: hard cap on a single recorded text field (claim, note, answer, evidence): an emergency
#: bound on a runaway seat's output, not a style limit
MAX_FIELD_CHARS = 4000


def one_line(text: str) -> str:
    """Free text as it prints: whitespace collapsed, so it holds a single line."""
    return " ".join((text or "").split())


def normalize_text(text: str) -> str:
    return one_line(text).casefold()


def note_key(text: str) -> str:
    """A note's identity, punctuation aside."""
    return normalize_text(text).rstrip(".!?;")


@dataclass
class Stance:
    stance: str = ABSTAIN
    note: str = ""
    evidence: str = ""
    round: int = 0
    declined: bool = False  # written by the run for a survived gap

    @property
    def reasoned(self) -> bool:
        return bool(self.evidence.strip() or self.note.strip())


@dataclass
class Item:
    id: str
    claim: str
    detail: str = ""
    evidence: str = ""
    severity: str = "minor"
    raised_by: list[str] = field(default_factory=list)
    also_reported_by: list[str] = field(default_factory=list)
    opened_round: int = 0
    disposition: str = OPEN
    closed_round: int = 0
    history: list[str] = field(default_factory=list)  # "consensus in 2, reopened in 4 by Agent3"
    markers: list[str] = field(default_factory=list)  # sandbox_artifact / sandbox_limit
    artifact_by: str = ""  # the member whose upper holds an artifact
    #: how many times this item has been reopened: its challenge budget
    reopen_count: int = 0
    #: the round a current disputed episode began, 0 when not disputed
    disputed_since: int = 0

    @property
    def closed(self) -> bool:
        return self.disposition in (AGREED, REJECTED, SPLIT)


@dataclass
class Escalation:
    item_id: str
    asker: str
    state: str = STANDING
    question_id: str = ""
    raised_round: int = 0
    challenges: list[dict] = field(default_factory=list)  # {by, round, note, evidence, defended}
    defense: str | None = None
    defended_round: int = 0
    undefended: bool = False
    end_round: int = 0


@dataclass
class Question:
    id: str
    text: str
    context: str = ""
    # option key (the question's dedup key) → (wording, the asker that offered it)
    menu: dict[str, tuple[str, str]] = field(default_factory=dict)
    item_id: str | None = None
    askers: list[str] = field(default_factory=list)
    #: seats that asked for this themselves, rather than seconded it: the only ones who can call a peer's answer not enough
    raised_by: list[str] = field(default_factory=list)
    #: seat → the words they asked with when another seat's ask already stood for the decision
    also_asked: dict[str, str] = field(default_factory=dict)
    #: seats that asked the council to wait on this ask
    blocked_by: list[str] = field(default_factory=list)
    #: the council's own split: a blocking ask with no seat holding it
    direction_ask: bool = False
    raised_round: int = 0
    updated_round: int = 0
    state: str = ASKED  # asked / answered / skipped / withdrawn
    answer: str = ""
    answer_round: int = 0
    answer_option: int = 0  # the number the human typed to take an offered option; 0 means their own words
    answer_option_text: str = ""  # that option verbatim
    recommendation: str = ""  # an `explain` reply
    recommendation_round: int = 0
    #: seat id → what that seat answered in place of the human
    peer_answers: dict[str, str] = field(default_factory=dict)
    #: the round the latest peer answer landed: an insistence rebuts only answers from a round the seat has read
    peer_answer_round: int = 0
    #: a seat that raised it said a peer's answer was not enough (`insist`): that answer no longer holds the ask back
    insisted: bool = False
    retracted_round: int = 0
    retracted_by: str = ""
    #: the ask the chair folded this one into
    merged_into: str = ""
    #: the item whose closure mooted this ask
    moot_with: str = ""
    markers: list[str] = field(default_factory=list)  # undefended / skipped / unasked / unanswered / unseconded / moot / merged …

    @property
    def key(self) -> str:
        return normalize_text(self.text)

    @property
    def options(self) -> list[str]:
        return [wording for wording, _asker in self.menu.values()]


@dataclass
class Note:
    """What a seat wants the human to know and not answer: it asks, holds and blocks nothing."""

    id: str
    by: str
    text: str
    #: info | correction — a correction restates an item's claim or detail; the report prints it under the item, marked as a correction
    kind: str = "info"
    item_id: str | None = None
    round: int = 0
    updated_round: int = 0
    state: str = STANDING
    #: the round whose checkpoint printed this text
    shown_round: int | None = None
    retracted_round: int = 0
    retract_reason: str = ""
    #: the round whose checkpoint printed the retraction
    retraction_shown_round: int | None = None
    #: the note the chair folded this one into: the human reads the survivor, this one beside it
    folded_into: str | None = None
    #: the seats whose notes folded into this one
    also_noted_by: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return note_key(self.text)


@dataclass
class HumanMessage:
    """A direct message from the human to a seat or the chair: no stance, no docket change, prose back."""

    id: str
    to: str  # the roster key: a seat's protocol id, or the chair's file_base
    to_name: str  # the display name the message went to
    round: int
    text: str
    reply: str = ""
    failure: str = ""


@dataclass
class QA:
    id: str
    asker: str
    to: str
    text: str
    round: int = 0
    answer: str = ""


@dataclass
class PeerMessage:
    """A `say_to` line: queued fire-and-forget, printed at the target's next prompt."""

    to: str
    from_: str
    text: str
    stage: int = 0


@dataclass
class StageRecord:
    """One stage of the adopted plan, as the run built it."""

    index: int
    state: str
    gate: str
    scope: str = ""
    kind: str = "code"  # code | prose
    builder: str = ""  # the global id that held the baton
    review_blind: bool = False  # the last review pass was unreadable: the gate's chair check is the only verdict
    prose_pass: bool = False
    status: str = "pending"  # pending | passed | blocked
    gate_output: str = ""  # what the builder reported
    gate_rerun: str = ""  # what the gate's verifier last said
    gate_ok: bool = False  # the last gate verdict
    findings: list[str] = field(default_factory=list)  # the review item ids that hold the stage open, in the order raised
    gate_item: str = ""  # the synthetic "the gate fails" item, while it stands
    open_items: list[str] = field(default_factory=list)  # what the builder left open
    changed: list[str] = field(default_factory=list)  # the paths the builder named as created or modified
    user_text: list[str] = field(default_factory=list)  # the stage's user-visible surfaces, as the builder named them
    fix_passes: list[dict] = field(default_factory=list)  # {pass, addressed: [{id, how}]}
    replans: list[dict] = field(default_factory=list)  # {pass, note, stages: int}
    blocked_reasons: list[str] = field(default_factory=list)


@dataclass
class FindingRecord:
    """One finding of the fix run's list, as the run fixed it or failed at it."""

    index: int
    finding_id: str  # the docket id the finding stands on
    severity: str
    claim: str
    builder: str = ""  # the global id that held the baton
    status: str = "pending"  # pending | fixed | blocked
    plan: dict = field(default_factory=dict)  # state / gate / scope / exceptions
    pre_tree: str = ""  # the tree the finding started from
    commit_sha: str = ""  # the run's commit of the passed fix; empty when commit = false or no tree change
    diff_path: str = ""  # a blocked finding's pre→post diff, in the run directory
    gate_ok: bool = False  # the gate, as the verifier last read it
    gate_output: str = ""  # the builder's report of the gate from its last pass
    gate_rerun: str = ""  # the verifier's report of the gate from its last pass
    gate_item: str = ""  # the docket item standing on a still-failing gate
    changed: list[str] = field(default_factory=list)  # the paths the passes named as created or modified
    fix_passes: list[dict] = field(default_factory=list)  # {pass, addressed: [{id, how}]}
    replans: list[dict] = field(default_factory=list)  # {pass, note, stages: int}
    review_findings: list[str] = field(default_factory=list)  # the docket ids the verifier raised against the fix
    review_blind: bool = False  # every verifier verdict was unreadable: the gate stood alone
    blocked_reasons: list[str] = field(default_factory=list)
    verify_note: str = ""  # how the fix was verified, when not by the crew's full pass


@dataclass
class Execution:
    """The build run's spine, kept on the shared docket for the report and `run.json`."""

    crews: list[dict] = field(default_factory=list)  # {label, council, mode, seats: [(id, name)], chair: {name, file_base}}
    adopted: str = ""  # the direction token the run builds
    adopted_how: str = ""  # consensus | human | majority | chair | structured
    plan_text: str = ""
    assumptions: list[str] = field(default_factory=list)  # the planning phase's decision points, settled or not: the report's trail
    open_assumptions: list[str] = field(default_factory=list)  # the ones no answer settled: the planning carry the builder prompt takes
    approval: str = ""  # granted | recorded | declined
    baseline: str = ""  # the tree the run started from, when the project is a git repo
    final_tree: str = ""  # the tree the run ended on, for the report's diffstat
    stages: list[StageRecord] = field(default_factory=list)
    #: fix: where the findings came from — {"how": "council" | "file", "council", "date", "outcome", "rounds", "path"}
    source: dict = field(default_factory=dict)
    #: fix: the gate's result, one line per question id — selected | skipped | added
    gate: dict = field(default_factory=dict)
    #: fix: the per-finding ledger, in fix order
    findings: list[FindingRecord] = field(default_factory=list)
    #: fix: the whole-project check over the finished tree — {ok, ran, evidence}
    final_check: dict = field(default_factory=dict)
    #: fix: the run's commits — {sha, finding, message}
    commits: list[dict] = field(default_factory=list)

    @property
    def blocked(self) -> list[StageRecord]:
        return [stage for stage in self.stages if stage.status == "blocked"]

    @property
    def landed(self) -> list[StageRecord]:
        return [stage for stage in self.stages if stage.status == "passed"]


@dataclass
class Approach:
    agent: str
    original: str = ""
    stages: list[dict] = field(default_factory=list)
    rejected_alternatives: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    revisions: list[dict] = field(default_factory=list)  # {name, round, text, stages}

    def artifact(self) -> dict:
        return self.revisions[-1] if self.revisions else {"name": f"{self.agent} original", "text": self.original, "stages": self.stages}


@dataclass
class RoundRecord:
    round: int
    summary: str = ""
    #: (protocol id, what that seat did)
    lines: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Docket:
    mode: str = "review"
    goal: str | None = None
    roster: list[str] = field(default_factory=list)  # protocol ids, Agent1..N
    items: dict[str, Item] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    #: a folded item's id → the id the chair kept for it: stale references redirect, the report shows no gap
    item_aliases: dict[str, str] = field(default_factory=dict)
    stances: dict[str, dict[str, Stance]] = field(default_factory=dict)
    signoffs: dict[str, int] = field(default_factory=dict)
    escalations: dict[tuple[str, str], Escalation] = field(default_factory=dict)
    questions: dict[str, Question] = field(default_factory=dict)
    question_order: list[str] = field(default_factory=list)
    qa: dict[str, QA] = field(default_factory=dict)
    qa_order: list[str] = field(default_factory=list)
    #: what seats told the human that needs no answer
    human_notes: dict[str, Note] = field(default_factory=dict)
    note_order: list[str] = field(default_factory=list)
    #: direct messages the human sent to a seat or the chair, and what came back
    human_messages: list[HumanMessage] = field(default_factory=list)
    approaches: dict[str, Approach] = field(default_factory=dict)  # plan
    agreed_points: list[dict] = field(default_factory=list)  # plan: compare
    backing: dict[str, str] = field(default_factory=dict)  # plan
    #: an ask reaches the human only once a second seat backs it
    seconding: bool = True
    #: the human's posture: the run waits at the bar, never waits, or never asks
    human: str = "blocking"
    round: int = 0
    #: the pass the engine had reached, for the report of a run that was cut short
    phase: str = ""
    #: the asks the checkpoint is parked on: empty whenever the run is not waiting on the human
    parked_on: list[str] = field(default_factory=list)
    rounds: list[RoundRecord] = field(default_factory=list)
    phase0_counts: dict[str, int] = field(default_factory=dict)
    chair_name: str = "Chair"
    outcome: str | None = None
    next_question: int = 1
    next_qa: int = 1
    next_note: int = 1
    next_message: int = 1
    #: implement: the run's spine — crews, adoption, stages
    execution: Execution | None = None
    #: implement: `say_to` lines waiting for their target's next prompt
    peer_messages: list[PeerMessage] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.roster)

    @property
    def blank(self) -> bool:
        """True when nothing was recorded: there is no state to report, only a reason."""
        return not (self.round or self.order or self.questions or self.human_notes or self.approaches or self.rounds)

    def bar(self) -> int:
        return self.n // 2 + 1

    def open_items(self) -> list[Item]:
        return [self.items[i] for i in self.order if not self.items[i].closed]

    def queue_message(self, *, to: str, from_: str, text: str, stage: int = 0) -> None:
        """A `say_to` line, waiting for its target's next prompt."""
        self.peer_messages.append(PeerMessage(to=to, from_=from_, text=one_line(text), stage=stage))

    def drain_messages(self, agent_id: str) -> list[PeerMessage]:
        """The `say_to` lines waiting for this seat, cleared on the way to its next prompt."""
        kept = [m for m in self.peer_messages if m.to != agent_id]
        drained = [m for m in self.peer_messages if m.to == agent_id]
        self.peer_messages = kept
        return drained

    def closed_items(self) -> list[Item]:
        return [self.items[i] for i in self.order if self.items[i].closed]

    def add_item(self, item: Item) -> Item:
        """Registers the item; its text prints on single-line surfaces, so it enters collapsed."""
        if item.id in self.items:
            return self.items[item.id]
        item.claim, item.detail, item.evidence = one_line(item.claim), one_line(item.detail), one_line(item.evidence)
        self.items[item.id] = item
        self.order.append(item.id)
        self.stances.setdefault(item.id, {})
        return item

    def stance(self, item_id: str, agent: str) -> Stance | None:
        return self.stances.get(item_id, {}).get(agent)

    def effective_stance(self, item_id: str, agent: str) -> str | None:
        """A declined record and a retired or demoted asker count as abstain, not absent."""
        record = self.stance(item_id, agent)
        if record is None:
            return None
        if record.declined:
            return ABSTAIN
        if record.stance == NEEDS_HUMAN and not self.is_standing(item_id, agent):
            return ABSTAIN
        return record.stance

    def is_standing(self, item_id: str, agent: str) -> bool:
        esc = self.escalations.get((item_id, agent))
        return bool(esc and esc.state == STANDING)

    def standing_escalations(self, item_id: str) -> list[Escalation]:
        return [e for (iid, _a), e in self.escalations.items() if iid == item_id and e.state == STANDING]

    def direction_origin(self, agent: str) -> str:
        """The direction a seat's backing stands for, keyed to the seat whose plan it is.

        `merged: <label>` names the plan its writer spelled out, so the direction is that
        seat's plan — the label is its wording, not a new direction.
        """
        token = self.backing.get(agent)
        if token in (None, NO_PREFERENCE, DECLINED):
            return ""
        return self.direction_for(token, agent)

    def direction_for(self, token: str, backer: str) -> str:
        """The direction a raw backing token names: a seat token is itself; a merged label, the seat
        whose plan it is — the label's speller first, then the roster, and the backer where no plan
        spells the label out."""
        if not token.startswith(MERGED):
            return token
        label = token.partition(MERGED)[2].strip()
        own = self.approaches.get(backer)
        if own is not None and any(revision.get("name") == label for revision in own.revisions):
            return backer
        for seat in self.roster:
            approach = self.approaches.get(seat)
            if approach is not None and any(revision.get("name") == label for revision in approach.revisions):
                return seat
        return backer

    def directions(self) -> tuple[str, ...]:
        """The directions the seats actually name, by the seat whose plan each is."""
        named = {origin for origin in (self.direction_origin(a) for a in self.roster) if origin}
        return tuple(sorted(named))

    def camps(self) -> tuple[tuple[str, ...], ...]:
        """The seats grouped by the direction they name."""
        groups: dict[str, list[str]] = {}
        for agent in self.roster:
            origin = self.direction_origin(agent)
            if origin:
                groups.setdefault(origin, []).append(agent)
        return tuple(sorted(tuple(group) for group in groups.values()))

    def direction_ask(self) -> Question | None:
        return next((q for q in (self.questions[i] for i in self.question_order) if q.direction_ask), None)

    def question(self, qid: str) -> Question | None:
        return self.questions.get(qid)

    def resolve_item(self, item_id: str) -> str:
        """The id an item now stands under: a folded id follows its alias to the survivor."""
        seen: set[str] = set()
        while item_id in self.item_aliases and item_id not in seen:
            seen.add(item_id)
            item_id = self.item_aliases[item_id]
        return item_id

    def questions_for_item(self, item_id: str) -> list[Question]:
        return [q for q in (self.questions[i] for i in self.question_order) if q.item_id == item_id]
