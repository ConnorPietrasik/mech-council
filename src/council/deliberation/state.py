"""The domain model as data.  Pure: no subprocess, no asyncio, no I/O.

Wire names are the field names: what a dataclass attribute says is what `run.json`, a verdict and a report call it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

AGREE, DISAGREE, NEEDS_HUMAN, ABSTAIN = "agree", "disagree", "needs_human", "abstain"
STANCES = (AGREE, DISAGREE, NEEDS_HUMAN, ABSTAIN)

OPEN = "open"
AGREED, UNHEARD, UNSEEN, DISPUTED, HUMAN, REJECTED = ("agreed", "unheard", "unseen", "disputed", "human", "rejected")
DISPOSITIONS = (AGREED, UNHEARD, UNSEEN, DISPUTED, HUMAN, REJECTED)
# The disposition evaluation order — the line in `dispositions.py`, not a table's row order.
EVALUATION_ORDER = (HUMAN, DISPUTED, AGREED, REJECTED, UNHEARD, UNSEEN)

STANDING, WITHDRAWN, RETIRED, DEMOTED = "standing", "withdrawn", "retired", "demoted"
ESCALATION_STATES = (STANDING, WITHDRAWN, RETIRED, DEMOTED)
ASKED, ANSWERED, SKIPPED = "asked", "answered", "skipped"

CONSENSUS, NEEDS_OUTCOME, STALEMATE, ROUND_LIMIT, ABORTED = ("consensus", "needs_human", "stalemate", "round_limit_reached", "aborted")
OUTCOMES = (CONSENSUS, NEEDS_OUTCOME, STALEMATE, ROUND_LIMIT)

NO_PREFERENCE = "no_preference"
DECLINED = "declined"

#: The termination order's "keep going" is not an outcome, so it needs its own literal.
GO = "go"

#: The chair has a protocol presence but no roster ID: this is the only label it is ever addressed by, and it is never one of `AgentN`.
CHAIR_ID = "chair"


def normalize_text(text: str) -> str:
    """The human checkpoint's dedup key: whitespace-collapsed, case-folded question text."""
    return " ".join((text or "").split()).casefold()


@dataclass
class Stance:
    """One seat's stance on one item, recorded with the round it was given."""

    stance: str = ABSTAIN
    note: str = ""
    evidence: str = ""
    round: int = 0
    declined: bool = False  # the record the *run* wrote for a survived gap

    @property
    def reasoned(self) -> bool:
        """A disagree with cited evidence *or* a refuting note."""
        return bool(self.evidence.strip() or self.note.strip())


@dataclass
class Item:
    """The unit of deliberation: a review finding or a plan decision point."""

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

    @property
    def closed(self) -> bool:
        return self.disposition in (AGREED, REJECTED)


@dataclass
class Escalation:
    """One seat's `needs_human` on one item, keyed (item, asker)."""

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
    """A human checkpoint request: asked at most once, deduplicated by normalized text."""

    id: str
    text: str
    context: str = ""
    options: list[str] = field(default_factory=list)
    item_id: str | None = None
    askers: list[str] = field(default_factory=list)
    chair_raised: bool = False
    blocking_requested: bool = False
    raised_round: int = 0
    updated_round: int = 0
    state: str = ASKED  # asked / answered / skipped
    answer: str = ""
    answer_round: int = 0
    recommendation: str = ""  # an `explain` reply: advice, never authority
    recommendation_round: int = 0
    markers: list[str] = field(default_factory=list)  # undefended / skipped / unasked …

    @property
    def key(self) -> str:
        return normalize_text(self.text)


@dataclass
class QA:
    """Lenient extraction's agent-to-agent question with an orchestrator-assigned `Q<n>` id."""

    id: str
    asker: str
    to: str
    text: str
    round: int = 0
    answer: str = ""
    answered_round: int = 0


@dataclass
class Approach:
    """(plan) one seat's phase-0 plan and any revisions of it."""

    agent: str
    original: str = ""
    stages: list[dict] = field(default_factory=list)
    rejected_alternatives: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    revisions: list[dict] = field(default_factory=list)  # {name, round, text, stages}

    def artifact(self) -> dict:
        """The plan this seat stands behind — latest revision, else the original."""
        return self.revisions[-1] if self.revisions else {"name": f"{self.agent} original", "text": self.original, "stages": self.stages}


@dataclass
class RoundRecord:
    """The round-by-round verdict section of the report."""

    round: int
    summary: str = ""
    lines: list[str] = field(default_factory=list)


@dataclass
class Docket:
    """The orchestrator-owned shared state."""

    mode: str = "review"
    goal: str | None = None
    roster: list[str] = field(default_factory=list)  # protocol ids, Agent1..N
    items: dict[str, Item] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    stances: dict[str, dict[str, Stance]] = field(default_factory=dict)
    signoffs: dict[str, int] = field(default_factory=dict)
    escalations: dict[tuple[str, str], Escalation] = field(default_factory=dict)
    questions: dict[str, Question] = field(default_factory=dict)
    question_order: list[str] = field(default_factory=list)
    qa: dict[str, QA] = field(default_factory=dict)
    qa_order: list[str] = field(default_factory=list)
    approaches: dict[str, Approach] = field(default_factory=dict)  # plan
    agreed_points: list[dict] = field(default_factory=list)  # plan: compare
    backing: dict[str, str] = field(default_factory=dict)  # plan
    round: int = 0
    rounds: list[RoundRecord] = field(default_factory=list)
    phase0_counts: dict[str, int] = field(default_factory=dict)  # the report's roster column
    chair_name: str = "Chair"
    outcome: str | None = None
    next_question: int = 1
    next_qa: int = 1

    @property
    def n(self) -> int:
        """The denominator: the roster as convened, frozen."""
        return len(self.roster)

    def bar(self) -> int:
        return self.n // 2 + 1

    def open_items(self) -> list[Item]:
        return [self.items[i] for i in self.order if not self.items[i].closed]

    def closed_items(self) -> list[Item]:
        return [self.items[i] for i in self.order if self.items[i].closed]

    def add_item(self, item: Item) -> Item:
        if item.id in self.items:
            return self.items[item.id]
        self.items[item.id] = item
        self.order.append(item.id)
        self.stances.setdefault(item.id, {})
        return item

    def stance(self, item_id: str, agent: str) -> Stance | None:
        return self.stances.get(item_id, {}).get(agent)

    def stated_stance(self, item_id: str, agent: str) -> str | None:
        """The seat's latest **stated** stance: a `declined` record states nothing."""
        record = self.stance(item_id, agent)
        if record is None or record.declined:
            return None
        return record.stance

    def effective_stance(self, item_id: str, agent: str) -> str | None:
        """The count view: what the dispositions measure, or None if the seat holds nothing.

        Two records are abstain-equivalent rather than absent.  A `declined` record the run wrote for a survived gap is a **current
        stance** (the completeness rule), so it carries the item to `agreed`/`unheard` instead of holding it at `unseen`; a
        retired or demoted asker's `needs_human` is inert too.  Only `None` — never shown it — is what makes an item `unseen`.
        """
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

    def question(self, qid: str) -> Question | None:
        return self.questions.get(qid)

    def questions_for_item(self, item_id: str) -> list[Question]:
        return [q for q in (self.questions[i] for i in self.question_order) if q.item_id == item_id]

    def item_for_question(self, question: Question) -> Item | None:
        return self.items.get(question.item_id) if question.item_id else None
