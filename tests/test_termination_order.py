"""The row order of `termination.evaluate`, held by what a reordered row would cost."""

from __future__ import annotations

from council.deliberation import escalations, termination
from council.deliberation.state import AGREE, NEEDS_HUMAN, Docket, Item
from council.deliberation.termination import CONSENSUS, GO, NEEDS_OUTCOME, NO_PREFERENCE


def _docket(mode: str, items: list[str], *, signed: list[str] | None = None) -> Docket:
    board = Docket(mode=mode, goal="g", roster=["Agent1", "Agent2", "Agent3"])
    for item in items:
        board.add_item(Item(id=item, claim=f"claim {item}", severity="major", opened_round=0))
    for agent in signed if signed is not None else board.roster:
        board.signoffs[agent] = 1
    return board


def _two_seats_need_a_human(board: Docket, item: str) -> None:
    """Two of three seats escalate the item, which is at the bar, and the third agrees with it."""
    escalations.record_stance(board, item_id=item, agent="Agent1", stance=NEEDS_HUMAN, note="the premise is a product decision", round_=1)
    escalations.record_stance(board, item_id=item, agent="Agent2", stance=NEEDS_HUMAN, note="the ordering is not ours to pick", round_=1)
    escalations.record_stance(board, item_id=item, agent="Agent3", stance=AGREE, round_=1)


def test_a_decision_one_seat_never_signed_cannot_be_reported() -> None:
    """A missing sign-off outranks both ways the docket ends by itself, in either mode."""
    review = _docket("review", ["F1"])
    for agent in review.roster:
        escalations.record_stance(review, item_id="F1", agent=agent, stance=AGREE, round_=1)
    del review.signoffs["Agent3"]

    outcome, reason = termination.evaluate(review)
    assert outcome == GO, f"a review run ended without Agent3's sign-off: {outcome}, {reason}"
    assert "Agent3" in reason

    plan = _docket("plan", ["T1"])
    for agent in plan.roster:
        escalations.record_stance(plan, item_id="T1", agent=agent, stance=AGREE, round_=1)
    plan.backing.update({"Agent1": "plan-a", "Agent2": "plan-a", "Agent3": NO_PREFERENCE})
    del plan.signoffs["Agent3"]

    outcome, reason = termination.evaluate(plan)
    assert outcome == GO, f"the direction rule named a direction while Agent3's sign-off was missing: {outcome}, {reason}"
    assert "Agent3" in reason

    plan.signoffs["Agent3"] = 1  # the signature is the only thing that changed
    assert termination.evaluate(plan)[0] == CONSENSUS, "with the signature in place this docket must be consensus-ready"
    review.signoffs["Agent3"] = 1
    assert termination.evaluate(review)[0] == CONSENSUS, "and the same for review, or the cases above tested the wrong row"


def test_plan_mode_never_works_topics_under_an_escalated_premise() -> None:
    """Plan answers a premise question before it grinds any topic; review defers the same question, on the same facts."""
    plan = _docket("plan", ["T1", "T2"])
    _two_seats_need_a_human(plan, "T1")
    assert termination.unsettled_questions(plan), "the fixture has lost the question this ordering is about"

    outcome, reason = termination.evaluate(plan)
    assert outcome == NEEDS_OUTCOME, f"plan worked topics under an unanswered premise question: {outcome}, {reason}"
    assert "T1" in reason or reason.startswith("unanswered effective-blocking question")

    review = _docket("review", ["F1", "F2"])
    _two_seats_need_a_human(review, "F1")
    assert termination.evaluate(review)[0] == GO, "the same facts must defer in review, or plan's row is not what is tested"


def test_review_settles_the_work_it_can_before_it_needs_human() -> None:
    """Review grinds disputed and unseen items first, and the question it defers is asked anyway that same round."""
    board = _docket("review", ["F1", "F2"])
    _two_seats_need_a_human(board, "F1")
    asked_before = termination.unsettled_questions(board)
    assert termination.blocking_questions(board), "the fixture has lost the blocking question this ordering is about"

    outcome, reason = termination.evaluate(board)
    assert outcome == GO, f"review ended the run over a question instead of settling F2: {outcome}, {reason}"
    assert "F2" in reason
    assert termination.unsettled_questions(board) == asked_before, "settling F2 must not silently answer the standing question"
