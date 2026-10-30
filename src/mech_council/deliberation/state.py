from __future__ import annotations

from dataclasses import dataclass, field

AGREE, DISAGREE, NEEDS_HUMAN, ABSTAIN = "agree", "disagree", "needs_human", "abstain"
STANCES = (AGREE, DISAGREE, NEEDS_HUMAN, ABSTAIN)

OPEN = "open"
AGREED, UNHEARD, UNSEEN, DISPUTED, HUMAN, REJECTED = ("agreed", "unheard", "unseen", "disputed", "human", "rejected")

STANDING, WITHDRAWN, RETIRED, DEMOTED = "standing", "withdrawn", "retired", "demoted"
ASKED, ANSWERED, SKIPPED = "asked", "answered", "skipped"

CONSENSUS, NEEDS_OUTCOME, STALEMATE, ROUND_LIMIT, ABORTED = ("consensus", "needs_human", "stalemate", "round_limit_reached", "aborted")

NO_PREFERENCE = "no_preference"
DECLINED = "declined"
#: a `backing` naming a merge one seat wrote down: `merged: <label>`
MERGED = "merged:"

#: the termination order's "keep going", which is not an outcome
GO = "go"

#: the chair's protocol label; never one of `AgentN`
CHAIR_ID = "chair"


def normalize_text(text: str) -> str:
    return " ".join((text or "").split()).casefold()


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

    @property
    def closed(self) -> bool:
        return self.disposition in (AGREED, REJECTED)


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
    blocking_requested: bool = False
    #: the council's own split: the one ask that blocks with nobody holding it
    direction_ask: bool = False
    raised_round: int = 0
    updated_round: int = 0
    state: str = ASKED  # asked / answered / skipped
    answer: str = ""
    answer_round: int = 0
    answer_option: int = 0  # the number the human typed to take an offered option; 0 means their own words
    answer_option_text: str = ""  # that option verbatim
    recommendation: str = ""  # an `explain` reply
    recommendation_round: int = 0
    markers: list[str] = field(default_factory=list)  # undefended / skipped / unasked …

    @property
    def key(self) -> str:
        return normalize_text(self.text)

    @property
    def options(self) -> list[str]:
        return [wording for wording, _asker in self.menu.values()]


@dataclass
class QA:
    id: str
    asker: str
    to: str
    text: str
    round: int = 0
    answer: str = ""
    answered_round: int = 0


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
    phase0_counts: dict[str, int] = field(default_factory=dict)
    chair_name: str = "Chair"
    outcome: str | None = None
    next_question: int = 1
    next_qa: int = 1

    @property
    def n(self) -> int:
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
        record = self.stance(item_id, agent)
        if record is None or record.declined:
            return None
        return record.stance

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

    def directions(self) -> tuple[str, ...]:
        """The directions the seats actually named."""
        named = {self.backing[a] for a in self.roster if self.backing.get(a) not in (None, NO_PREFERENCE, DECLINED)}
        return tuple(sorted(named))

    def camps(self) -> tuple[tuple[str, ...], ...]:
        """The seats grouped by the direction they name."""
        groups: dict[str, list[str]] = {}
        for agent in self.roster:
            token = self.backing.get(agent)
            if token not in (None, NO_PREFERENCE, DECLINED):
                groups.setdefault(token, []).append(agent)
        return tuple(sorted(tuple(group) for group in groups.values()))

    def direction_ask(self) -> Question | None:
        return next((q for q in (self.questions[i] for i in self.question_order) if q.direction_ask), None)

    def question(self, qid: str) -> Question | None:
        return self.questions.get(qid)

    def questions_for_item(self, item_id: str) -> list[Question]:
        return [q for q in (self.questions[i] for i in self.question_order) if q.item_id == item_id]

    def item_for_question(self, question: Question) -> Item | None:
        return self.items.get(question.item_id) if question.item_id else None
