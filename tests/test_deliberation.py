"""The deliberation units, pure: no I/O, no pi, no clock."""

from __future__ import annotations

import pytest

from mech_council import naming
from mech_council.deliberation import dispositions, escalations, notes, stalemate, termination
from mech_council.deliberation.state import (
    ABSTAIN,
    AGREE,
    AGREED,
    ANSWERED,
    ASKED,
    DISAGREE,
    DISPUTED,
    HUMAN,
    NEEDS_HUMAN,
    NO_PREFERENCE,
    OPEN,
    REJECTED,
    SKIPPED,
    STANDING,
    UNHEARD,
    UNSEEN,
    WITHDRAWN,
    Docket,
    Item,
    Stance,
)


def docket_with(n: int = 3, *, mode: str = "review", items: int = 1) -> Docket:
    docket = Docket(mode=mode, goal="g", roster=[f"Agent{i}" for i in range(1, n + 1)])
    for index in range(1, items + 1):
        docket.add_item(Item(id=f"F{index}", claim=f"claim {index}", severity="major", opened_round=0))
    return docket


def stance(docket: Docket, item: str, agent: str, value: str, **kw) -> None:
    escalations.record_stance(docket, item_id=item, agent=agent, stance=value, round_=kw.pop("round", 1), **kw)


# ---------------------------------------------------------------------------
# The derivation of an open item, from current stances and nothing else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stances, expected",
    [
        # 3 seats: the bar is 2.
        (("agree", "agree", "agree"), AGREED),
        (("agree", "agree", "abstain"), AGREED),
        (("agree", "agree", None), UNSEEN),  # not complete
        (("agree", "agree", "disagree"), DISPUTED),  # disputed beats the bar
        (("agree", "disagree", "abstain"), DISPUTED),
        (("disagree", "disagree", "disagree"), UNHEARD),  # heard, but nothing refutes
        (("disagree", "disagree", "abstain"), UNHEARD),
        (("abstain", "abstain", "abstain"), UNHEARD),
        (("needs_human", "agree", "agree"), HUMAN),  # a standing escalation wins
        # `declined` is inert, never a dissent: 2 `agree` + 1 `declined` → `agreed`.
        (("agree", "agree", "declined"), AGREED),
        (("declined", "declined", "declined"), UNHEARD),
        ((None, None, None), UNSEEN),
    ],
)
def test_derivation_table(stances, expected):
    docket = docket_with(3)
    for agent, value in zip(docket.roster, stances):
        if value is None:
            continue
        if value == "declined":
            docket.stances["F1"][agent] = Stance(stance=ABSTAIN, declined=True)
        else:
            stance(docket, "F1", agent, value)
    assert dispositions.derive(docket, "F1") == expected


def test_unanimous_reasoned_rejection_is_rejected():
    docket = docket_with(3)
    for agent in docket.roster:
        stance(docket, "F1", agent, DISAGREE, note="refuted by X")
    assert dispositions.derive(docket, "F1") == REJECTED


def test_rejection_needs_a_reason_and_is_not_majority():
    docket = docket_with(3)
    for agent in docket.roster:
        stance(docket, "F1", agent, DISAGREE)  # no note, no evidence
    assert dispositions.derive(docket, "F1") == UNHEARD  # heard, and unheard


def test_a_declined_record_is_inert_in_every_count():
    docket = docket_with(3)
    stance(docket, "F1", "Agent1", AGREE)
    stance(docket, "F1", "Agent2", AGREE)
    before = dispositions.support(docket, "F1")
    docket.stances["F1"]["Agent3"] = Stance(stance=DISAGREE, note="ignored", declined=True)
    assert dispositions.support(docket, "F1") == before
    assert dispositions.objections(docket, "F1") == []
    # Inert means it neither dissents nor withholds a stance, so it cannot hold the item open.
    assert dispositions.derive(docket, "F1") == AGREED
    docket.stances["F1"]["Agent3"] = Stance(stance=AGREE, round=1)
    assert dispositions.derive(docket, "F1") == AGREED


def test_a_declined_record_lifts_support_without_reaching_the_bar():
    """The same row's second half: 2 seats (bar 2), 1 `agree` + 1 `declined` → `unheard`."""
    docket = docket_with(2)
    stance(docket, "F1", "Agent1", AGREE)
    docket.stances["F1"]["Agent2"] = Stance(stance=ABSTAIN, round=1, declined=True)
    assert dispositions.derive(docket, "F1") == UNHEARD


def test_a_seat_that_declined_carries_no_objection_from_the_round_before():
    """The dispositions and the gap ladder: disagree in round 2, declined in round 3 → `unheard`, **not** `disputed`."""
    docket = docket_with(3)
    stance(docket, "F1", "Agent1", AGREE, round=2)
    stance(docket, "F1", "Agent2", ABSTAIN, round=2)
    stance(docket, "F1", "Agent3", DISAGREE, note="wrong on X", round=2)
    assert dispositions.derive(docket, "F1") == DISPUTED
    docket.stances["F1"]["Agent3"] = Stance(stance=ABSTAIN, round=3, declined=True)
    assert dispositions.derive(docket, "F1") == UNHEARD


# ---------------------------------------------------------------------------
# Closure is stored; reopening is the only way it changes
# ---------------------------------------------------------------------------


def test_closed_items_are_never_re_derived():
    docket = docket_with(3)
    for agent in docket.roster:
        stance(docket, "F1", agent, AGREE)
    dispositions.closure_pass(docket, 2)
    assert docket.items["F1"].disposition == AGREED
    for agent in docket.roster:
        stance(docket, "F1", agent, DISAGREE, note="now we disagree", round=3)
    dispositions.closure_pass(docket, 5)
    assert docket.items["F1"].disposition == AGREED, "a closed item holds"


def test_a_reasoned_challenge_reopens_and_the_ladder_applies_again():
    docket = docket_with(3)
    for agent in docket.roster:
        stance(docket, "F1", agent, AGREE, round=1)
    dispositions.closure_pass(docket, 1)
    stance(docket, "F1", "Agent2", DISAGREE, note="wrong on X", round=2)
    events = dispositions.closure_pass(docket, 2)
    assert [name for name, _ in events] == ["item_reopened"]
    assert docket.items["F1"].disposition == OPEN
    assert docket.stances["F1"] == {}, "reopen clears every stance record, declined included"
    assert dispositions.derive(docket, "F1") == UNSEEN


def test_the_report_s_closure_pass_closes_and_never_reopens():
    """The last pass before the report settles what the seats settled; reopening is a round's business."""
    docket = docket_with(3)
    stance(docket, "F1", "Agent1", NEEDS_HUMAN, note="which tenant?", round=1)
    stance(docket, "F1", "Agent2", AGREE, round=1)
    stance(docket, "F1", "Agent3", AGREE, round=1)
    assert dispositions.derive(docket, "F1") == HUMAN
    stance(docket, "F1", "Agent1", AGREE, round=2)  # the seat's own release, as the pass before the report records it
    assert [name for name, _ in dispositions.closure_pass(docket, 2, reopen=False)] == ["item_closed"]
    assert (docket.items["F1"].closed, docket.items["F1"].disposition) == (True, AGREED)
    stance(docket, "F1", "Agent2", DISAGREE, note="wrong on X", round=2)
    assert dispositions.closure_pass(docket, 2, reopen=False) == [], "an objection after the last round reopens nothing"
    assert (docket.items["F1"].closed, docket.items["F1"].disposition) == (True, AGREED)


def test_reopen_reclose_moves_the_stalemate_signature():
    docket = docket_with(3)
    for agent in docket.roster:
        stance(docket, "F1", agent, AGREE, round=1)
    dispositions.closure_pass(docket, 1)
    first = stalemate.signature(docket)
    stance(docket, "F1", "Agent2", DISAGREE, note="wrong on X", round=2)
    dispositions.closure_pass(docket, 2)
    second = stalemate.signature(docket)
    assert first != second
    for agent in docket.roster:
        stance(docket, "F1", agent, AGREE, round=3)
    dispositions.closure_pass(docket, 3)
    assert stalemate.signature(docket) != second


def test_stalemate_needs_two_identical_rounds_and_no_checkpoint_movement():
    docket = docket_with(3)
    stance(docket, "F1", "Agent1", DISAGREE, note="because")
    sig = stalemate.signature(docket)
    docket.round = 1
    assert stalemate.stalemate(docket, sig, sig, False) is False, "never in round 1"
    docket.round = 2
    assert stalemate.stalemate(docket, sig, sig, False) is True
    assert stalemate.stalemate(docket, sig, ("other",), False) is False
    assert stalemate.stalemate(docket, sig, sig, True) is False, "the human moved it"


# ---------------------------------------------------------------------------
# The escalation ledger is a second axis
# ---------------------------------------------------------------------------


def test_escalation_states_and_the_effective_class():
    docket = docket_with(3)
    stance(docket, "F1", "Agent1", NEEDS_HUMAN)
    assert docket.is_standing("F1", "Agent1")
    assert dispositions.derive(docket, "F1") == HUMAN
    # The escalation *is* a question, phrased by the asker's own `needs_human` note.
    question = docket.questions[docket.escalations[("F1", "Agent1")].question_id]
    assert question.item_id == "F1" and question.askers == ["Agent1"]
    assert escalations.effective_class(docket, question) == "deferred", "one of three"
    stance(docket, "F1", "Agent2", NEEDS_HUMAN)
    stance(docket, "F1", "Agent3", NEEDS_HUMAN)
    assert escalations.effective_class(docket, question) == "deferred", "three stances are still not a declared stop"
    escalations.stop_for(docket, qid=question.id, agent="Agent1", round_=1)
    escalations.stop_for(docket, qid=question.id, agent="Agent2", round_=1)
    assert escalations.effective_class(docket, question) == "blocking"
    assert termination.blocking_questions(docket) == [question]


def test_the_split_ask_blocks_only_while_the_split_it_reported_stands():
    """Nobody holds the split the way seats hold an escalation, so nothing but the split itself can hold the run."""
    docket = docket_with(3, mode="plan")
    ask = escalations.raise_direction_ask(
        docket, text="which plan do you want?", context="two seats, two orders of work", options=["Agent1: a", "Agent2: b"], round_=2
    )
    assert ask.direction_ask and ask.item_id is None and ask.askers == ["chair"]
    assert escalations.effective_class(docket, ask) == "deferred", "nobody has named a direction yet"
    docket.backing.update({"Agent1": "Agent1", "Agent2": "Agent2", "Agent3": NO_PREFERENCE})
    assert escalations.effective_class(docket, ask) == "blocking"
    # Item-less and nobody's escalation, so it reaches the human on the ask set alone.
    assert termination.blocking_questions(docket) == [ask] and termination.unsettled_questions(docket) == [ask]
    docket.backing["Agent2"] = "Agent1"
    assert escalations.effective_class(docket, ask) == "deferred", "the council converged: the ask is moot with no keystroke"


def test_a_declined_split_ask_is_a_needs_human_while_two_directions_stand():
    docket = docket_with(3, mode="plan", items=0)
    for agent in docket.roster:
        docket.signoffs[agent] = 1
    ask = escalations.raise_direction_ask(docket, text="which plan?", context="", options=["a", "b"], round_=2)
    docket.backing.update({"Agent1": "Agent1", "Agent2": "merged: both"})
    ask.state = SKIPPED
    assert termination.declined_blocking(docket) == [ask]
    assert termination.evaluate(docket)[0] == termination.NEEDS_OUTCOME


def test_an_ask_needs_a_second_seat_before_the_human_sees_it():
    docket = docket_with(3)
    question, _ = escalations.raise_question(docket, text="which brand palette?", asker="Agent1", item_id=None, blocking=False, round_=1)
    assert escalations.held_back(docket, question) == escalations.UNBACKED
    assert termination.held_back_questions(docket) == [question]
    backed, reason = escalations.second(docket, qid="H1", agent="Agent2", round_=2)
    assert (backed, reason) == (question, "") and question.askers == ["Agent1", "Agent2"]
    assert escalations.showable(docket, question) and termination.held_back_questions(docket) == []
    assert escalations.second(docket, qid="H9", agent="Agent3", round_=2)[0] is None, "no such ask to back"
    escalations.second(docket, qid="H1", agent="Agent1", round_=2)
    assert question.askers == ["Agent1", "Agent2"], "a seat that asks it already cannot second it"


def test_only_the_split_ask_and_the_knob_bypass_the_second():
    plan = docket_with(3, mode="plan", items=0)
    split = escalations.raise_direction_ask(plan, text="which order?", context="", options=["a", "b"], round_=1)
    assert escalations.showable(plan, split), "the split ask is backed by the camps it reports"
    three = docket_with(3)
    only, _ = escalations.raise_question(three, text="which palette?", asker="Agent1", item_id=None, blocking=False, round_=1)
    assert not escalations.showable(three, only)
    three.seconding = False
    assert escalations.showable(three, only), "the knob turns the gate off"


def test_a_declared_stop_needs_the_bar_and_letting_go_releases_it():
    docket = docket_with(3)
    escalations.raise_question(docket, text="which tenant owns the key?", asker="Agent1", item_id=None, round_=1)
    escalations.stop_for(docket, qid="H1", agent="Agent1", round_=1)
    assert escalations.effective_class(docket, docket.questions["H1"]) == "deferred", "one seat cannot stop the council"
    escalations.stop_for(docket, qid="H1", agent="Agent2", round_=2)
    assert escalations.effective_class(docket, docket.questions["H1"]) == "blocking", "the bar of 3 is two"
    escalations.retract(docket, qid="H1", agent="Agent2", round_=3)
    assert docket.questions["H1"].blocked_by == ["Agent1"], "walking away from the ask walks away from the stop"
    assert escalations.effective_class(docket, docket.questions["H1"]) == "deferred"


def test_no_seat_backs_drops_or_answers_the_split_ask():
    """The camps are what the split ask reports, so no seat's `second`, `retract` or `answer_question` reaches it."""
    plan = docket_with(3, mode="plan", items=0)
    split = escalations.raise_direction_ask(plan, text="which order?", context="", options=["a", "b"], round_=1)
    for refuse in (escalations.second, escalations.retract, escalations.stop_for):
        question, reason = refuse(plan, qid=split.id, agent="Agent1", round_=1)
        assert question is split and reason, "a seat has no claim on the camps' ask"
    answered, reason = escalations.answer_by_seat(plan, qid=split.id, agent="Agent1", text="one tenant, decided")
    assert answered is split and reason and split.peer_answers == {}, "answering the camps from the workspace is no claim either"
    insisted, reason = escalations.insist(plan, qid=split.id, agent="Agent1", round_=1, seen=1)
    assert insisted is split and reason, "echoing the camps' ask asks nothing of them"
    assert split.state == ASKED and "Agent1" not in split.askers, "the guard did not sneak a seat into the camps"


def test_a_peer_answer_holds_an_ask_back_until_a_raiser_insists():
    """`insist` names the ask by id, and only a seat that raised it can mean it."""
    docket = docket_with(3)
    question, _ = escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, blocking=False, round_=1)
    escalations.second(docket, qid="H1", agent="Agent2", round_=1)
    assert escalations.showable(docket, question)
    answered, reason = escalations.answer_by_seat(docket, qid="H1", agent="Agent3", text="the tokens file names brand-primary", round_=1)
    assert (answered, reason) == (question, "")
    assert escalations.held_back(docket, question) == escalations.SETTLED, "a seat answered it: the human is not asked"

    assert escalations.insist(docket, qid="H9", agent="Agent1", round_=2, seen=1)[0] is None, "an id that names nothing"
    assert escalations.insist(docket, qid="H1", agent="Agent1", round_=2, seen=0)[1], "nothing has been answered to refuse"
    backed, reason = escalations.insist(docket, qid="H1", agent="Agent2", round_=2, seen=1)
    assert (backed, question.insisted) == (question, False) and reason, "a backer has no claim on an answer it did not get"
    again, _ = escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, round_=2)
    assert again is question and not question.insisted, "restating an ask only keeps it alive"

    raised, reason = escalations.insist(docket, qid="H1", agent="Agent1", round_=2, seen=1)
    assert (raised, reason) == (question, "") and question.insisted, "only the seat that raised it can say so"
    assert escalations.insist(docket, qid="H1", agent="Agent1", round_=3, seen=2)[1], "it is said once"
    assert escalations.showable(docket, question), "backed and insisted: the human is asked"


def test_an_ask_raised_by_two_seats_in_two_wordings_is_insisted_on_by_either():
    """The wording is not the ask: a co-raiser calls a peer's answer not enough by id, with no sentence to echo."""
    docket = docket_with(3)
    stance(docket, "F1", "Agent1", NEEDS_HUMAN, note="which palette wins?")
    stance(docket, "F1", "Agent2", NEEDS_HUMAN, note="which colour file wins?")
    ask = docket.questions["H1"]
    assert ask.text == "which palette wins?" and ask.also_asked == {"Agent2": "which colour file wins?"}
    escalations.answer_by_seat(docket, qid="H1", agent="Agent3", text="tokens.json names it", round_=1)
    insisted, reason = escalations.insist(docket, qid="H1", agent="Agent2", round_=2, seen=1)
    assert (insisted, reason) == (ask, "") and ask.insisted, "its own wording was never the ask"


def test_an_answer_landing_in_its_own_round_is_not_rebuttable_in_it():
    """A verdict cannot refuse an answer that was not on the docket it was written against; the pass before the
    report is the one pass that does show a seat the answers it carries.
    """
    docket = docket_with(3)
    question, _ = escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, round_=1)
    escalations.second(docket, qid="H1", agent="Agent2", round_=1)
    escalations.answer_by_seat(docket, qid="H1", agent="Agent3", text="tokens.json names brand-primary", round_=2)
    assert escalations.held_back(docket, question) == escalations.SETTLED

    same_round, reason = escalations.insist(docket, qid="H1", agent="Agent1", round_=2, seen=1)
    assert (same_round, question.insisted) == (question, False) and reason, "the round's own answer is not one it has read"
    assert not escalations.showable(docket, question)

    next_round, reason = escalations.insist(docket, qid="H1", agent="Agent1", round_=3, seen=2)
    assert (next_round, reason) == (question, "") and question.insisted, "the next round is where a rebuttal lands"
    assert escalations.showable(docket, question)


def test_the_last_asker_to_walk_away_withdraws_the_ask():
    docket = docket_with(3)
    question, _ = escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, blocking=False, round_=1)
    escalations.second(docket, qid="H1", agent="Agent2", round_=1)
    assert escalations.retract(docket, qid="H1", agent="Agent1", round_=2) == (question, "")
    assert question.state == ASKED and question.askers == ["Agent2"], "another seat still asks it"
    assert escalations.retract(docket, qid="H1", agent="Agent3", round_=2)[1], "a seat cannot drop an ask it does not ask"
    assert (question.state, question.askers) == (ASKED, ["Agent2"])
    escalations.retract(docket, qid="H1", agent="Agent2", round_=3)
    assert question.state == WITHDRAWN and question.retracted_by == "Agent2"
    assert termination.unsettled_questions(docket) == []
    again, updated = escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, blocking=False, round_=4)
    assert not updated and again.id == "H2", "a withdrawn ask is history, so the same words raise a new one"


def test_letting_go_of_the_item_releases_the_stop_declared_on_its_ask():
    """A stop that outlives the hold it was declared with parks the run on a question nobody is asking."""
    docket = docket_with(3)
    for agent in ("Agent1", "Agent2"):
        stance(docket, "F1", agent, NEEDS_HUMAN, note="which tenant owns the key?")
    question = docket.questions["H1"]
    for agent in ("Agent1", "Agent2"):
        escalations.stop_for(docket, qid="H1", agent=agent, round_=1)
    assert escalations.effective_class(docket, question) == "blocking"
    for agent in ("Agent1", "Agent2"):
        stance(docket, "F1", agent, AGREE)
    assert question.blocked_by == [], "the hold is gone, and the stop it declared goes with it"
    assert escalations.effective_class(docket, question) == "deferred"
    assert termination.blocking_questions(docket) == []


def test_the_chair_folds_asks_that_want_one_decision():
    """Two seats wanting one decision word it differently: apart, neither is backed and the human is asked twice."""
    docket = docket_with(3)
    escalations.raise_question(docket, text="which tenant owns the cache key?", asker="Agent1", item_id="F1", round_=1)
    escalations.raise_question(docket, text="who is accountable for tenancy?", asker="Agent2", item_id="F1", round_=1)
    first, second = docket.questions["H1"], docket.questions["H2"]
    escalations.stop_for(docket, qid="H1", agent="Agent1", round_=1)
    escalations.stop_for(docket, qid="H2", agent="Agent2", round_=1)
    escalations.offer_options(second, "Agent2", ["the billing service", "the storage team"], replace=True)
    assert not escalations.showable(docket, first) and not escalations.showable(docket, second), "two wordings, neither backed"

    events, reason = escalations.merge_asks(docket, groups=[{"canonical": "H1", "duplicates": ["H2"]}], round_=2)
    assert reason == "" and [name for name, _ in events] == ["question_merged"]
    assert first.askers == ["Agent1", "Agent2"] and first.blocked_by == ["Agent1", "Agent2"]
    assert escalations.effective_class(docket, first) == "blocking", "one ask with a majority wanting the stop"
    assert [entry[0] for entry in first.menu.values()] == ["the billing service", "the storage team"]
    assert (second.state, second.merged_into, second.askers) == (WITHDRAWN, "H1", [])
    assert "merged" in second.markers
    assert termination.unsettled_questions(docket) == [first], "one ask reaches the human, not two"
    assert escalations.retract(docket, qid="H2", agent="Agent2", round_=3)[1].startswith("that ask is now H1")


def test_the_fold_keeps_the_words_of_the_ask_raised_first():
    """The ask raised first survives whichever one the chair names; a chair that reads the order backwards loses that
    wording, not the fold — and not the groups it named properly.
    """
    docket = docket_with(3)
    escalations.raise_question(docket, text="which tenant owns the cache key?", asker="Agent1", item_id="F1", round_=1)
    escalations.raise_question(docket, text="who is accountable for tenancy?", asker="Agent2", item_id="F1", round_=2)
    escalations.raise_question(docket, text="which palette wins?", asker="Agent1", item_id=None, round_=2)
    escalations.raise_question(docket, text="which colour file?", asker="Agent3", item_id=None, round_=2)
    escalations.stop_for(docket, qid="H2", agent="Agent2", round_=2)

    events, reason = escalations.merge_asks(
        docket, groups=[{"canonical": "H2", "duplicates": ["H1"]}, {"canonical": "H3", "duplicates": ["H4"]}], round_=3
    )
    assert reason == ""
    assert [(f["id"], f["into"], f["chair_named"]) for _name, f in events] == [("H2", "H1", "H2"), ("H4", "H3", "H3")]
    first = docket.questions["H1"]
    assert (first.text, first.askers, first.blocked_by) == ("which tenant owns the cache key?", ["Agent1", "Agent2"], ["Agent2"])
    assert (docket.questions["H2"].state, docket.questions["H2"].merged_into) == (WITHDRAWN, "H1")
    assert docket.questions["H4"].merged_into == "H3", "the groups the chair worded well still fold"


def test_a_fold_cannot_launder_the_answer_a_seat_gave_and_then_asked_about():
    """A seat answers a peer's ask and wants the human's word in its own words: the fold makes it an asker, so its
    answer stops being a reason to keep the ask from the human — and stays on the record all the same.
    """
    docket = docket_with(3)
    escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, round_=1)
    escalations.raise_question(docket, text="which colour file?", asker="Agent2", item_id=None, round_=1)
    escalations.answer_by_seat(docket, qid="H1", agent="Agent2", text="tokens.json, and nothing reads it", round_=2)
    assert escalations.held_back(docket, docket.questions["H1"]) == escalations.SETTLED

    escalations.merge_asks(docket, groups=[{"canonical": "H1", "duplicates": ["H2"]}], round_=2)
    first = docket.questions["H1"]
    assert first.peer_answers == {"Agent2": "tokens.json, and nothing reads it"}, "the answer the human should still get"
    assert escalations.settlements(first) == {}, "an answer from a seat that asks it is not a settlement"
    assert escalations.showable(docket, first) and escalations.held_back(docket, first) == ""

    escalations.retract(docket, qid="H1", agent="Agent2", round_=3)
    assert escalations.held_back(docket, first) == escalations.SETTLED, "walk away and the answer counts again"


def test_answering_an_ask_and_then_backing_it_is_asking_for_the_human_s_word():
    """The one way a seat's own answer stops holding an ask back: it wants the human's word on it too."""
    docket = docket_with(3)
    question, _ = escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, round_=1)
    escalations.answer_by_seat(docket, qid="H1", agent="Agent2", text="tokens.json, and nothing reads it", round_=1)
    assert escalations.held_back(docket, question) == escalations.SETTLED

    escalations.second(docket, qid="H1", agent="Agent2", round_=2)
    assert escalations.showable(docket, question), "a seat asking it is no answer to it"
    assert question.peer_answers == {"Agent2": "tokens.json, and nothing reads it"}, "the answer is kept, not undone"


def test_two_seats_holding_one_item_raise_one_ask_together():
    """The second seat is usually there already: two seats holding one item want the same decision."""
    docket = docket_with(3)
    stance(docket, "F1", "Agent1", NEEDS_HUMAN, note="which tenant owns the cache key?")
    events = escalations.record_stance(docket, item_id="F1", agent="Agent2", stance=NEEDS_HUMAN, note="who owns it?", round_=1)

    asked = list(docket.questions.values())
    assert [q.id for q in asked] == ["H1"], "one item, one ask"
    first = asked[0]
    assert (first.askers, first.raised_by) == (["Agent1", "Agent2"], ["Agent1", "Agent2"]), "each asked for it, in their own words"
    assert first.also_asked == {"Agent2": "who owns it?"}, "no wording is lost"
    assert escalations.showable(docket, first), "the second seat was there all along"
    assert [fields["asked_with"] for _name, fields in events] == [["Agent1"]]
    assert docket.escalations[("F1", "Agent2")].question_id == "H1", "the second hold points at the same ask"
    escalations.stop_for(docket, qid="H1", agent="Agent1", round_=1)
    escalations.stop_for(docket, qid="H1", agent="Agent2", round_=1)
    assert escalations.effective_class(docket, first) == "blocking", "two declared stops on the one ask about their item"


def test_a_seat_that_shares_an_ask_does_not_reword_it():
    """Two seats, one ask: the words the human reads belong to the seat that raised it first."""
    docket = docket_with(3)
    stance(docket, "F1", "Agent1", NEEDS_HUMAN, note="which tenant owns the cache key?")
    stance(docket, "F1", "Agent2", NEEDS_HUMAN, note="who is accountable for tenancy?")
    first = docket.questions["H1"]
    changed = escalations.refresh_question(first, asker="Agent2", text="who is accountable for tenancy?", round_=2)
    assert not changed and first.text == "which tenant owns the cache key?", "this seat has no words to take the ask's place"
    assert escalations.refresh_question(first, asker="Agent1", text="config or KMS, which?", round_=3), "the raiser rewords its own ask"
    assert first.text == "config or KMS, which?"


def test_a_folded_ask_keeps_the_peer_answer_that_was_holding_it_back():
    """Folding must not launder a settled question back in front of the human, nor lose the round it was settled in."""
    docket = docket_with(3)
    first, _ = escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, round_=1)
    escalations.raise_question(docket, text="which colour file?", asker="Agent2", item_id=None, round_=1)
    escalations.answer_by_seat(docket, qid="H2", agent="Agent3", text="tokens.json, and nothing reads it", round_=2)
    escalations.merge_asks(docket, groups=[{"canonical": "H1", "duplicates": ["H2"]}], round_=2)
    assert first.peer_answers == {"Agent3": "tokens.json, and nothing reads it"}
    assert not escalations.showable(docket, first), "backed by two seats, and still settled by a peer"
    assert escalations.held_back(docket, first) == escalations.SETTLED

    same_round, reason = escalations.insist(docket, qid="H1", agent="Agent1", round_=2, seen=1)
    assert not first.insisted and reason, "the answer's round travels with the answer through the fold"
    next_round, reason = escalations.insist(docket, qid="H1", agent="Agent1", round_=3, seen=2)
    assert (next_round, reason) == (first, "") and first.insisted and escalations.showable(docket, first)


def test_the_ask_fold_is_one_plan_or_none():
    docket = docket_with(3)
    for agent, text in (("Agent1", "which palette?"), ("Agent2", "which colour file?"), ("Agent3", "which theme?")):
        escalations.raise_question(docket, text=text, asker=agent, item_id=None, round_=1)
    groups = [{"canonical": "H1", "duplicates": ["H2"]}, {"canonical": "H3", "duplicates": ["H1"]}]
    events, reason = escalations.merge_asks(docket, groups=groups, round_=2)
    assert events == [] and "reused" in reason
    assert [q.state for q in docket.questions.values()] == [ASKED, ASKED, ASKED], "half a fold is no fold"
    events, _reason = escalations.merge_asks(docket, groups=[{"canonical": "H9", "duplicates": ["H2"]}], round_=2)
    assert events == [] and docket.questions["H2"].state == ASKED, "an id that is not an open ask is skipped"


def test_a_seat_cannot_answer_its_own_ask():
    """An answer holds an ask back from the human, so the asker writing one is not how it goes away."""
    docket = docket_with(3)
    question, _ = escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, round_=1)
    escalations.second(docket, qid="H1", agent="Agent2", round_=1)
    assert escalations.answer_by_seat(docket, qid="H1", agent="Agent1", text="I would guess the tokens file")[1]
    assert question.peer_answers == {} and escalations.showable(docket, question)


def test_retracting_an_answered_ask_leaves_the_human_s_word_standing():
    docket = docket_with(3)
    question, _ = escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, blocking=False, round_=1)
    question.state, question.answer = ANSWERED, "brand-primary"
    escalations.retract(docket, qid="H1", agent="Agent1", round_=2)
    assert question.state == ANSWERED and question.answer == "brand-primary", "the human's word outlives the ask"
    assert "moot" in question.markers


def test_an_item_less_ask_blocks_only_on_a_declared_stop():
    docket = docket_with(3)
    for agent in docket.roster:
        escalations.raise_question(docket, text="a rule question", asker=agent, item_id=None, round_=1)
    question = docket.questions["H1"]
    assert question.askers == docket.roster
    assert escalations.effective_class(docket, question) == "deferred", "three askers are not a declared stop"
    assert termination.blocking_questions(docket) == []
    for agent in ("Agent1", "Agent2"):
        escalations.stop_for(docket, qid="H1", agent=agent, round_=1)
    assert escalations.effective_class(docket, question) == "blocking", "the bar counts declared stops"


def test_an_ask_about_an_item_the_council_settled_goes_moot_with_it():
    """The closure is the council's answer about its item; a deferred ask about it is no question left over."""
    docket = docket_with(3)
    for agent in docket.roster:
        stance(docket, "F1", agent, AGREE)
    deferred, _ = escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id="F1", round_=1)
    blocking, _ = escalations.raise_question(docket, text="who owns the key?", asker="Agent1", item_id="F1", round_=1)
    for agent in ("Agent1", "Agent2"):
        escalations.stop_for(docket, qid="H2", agent=agent, round_=1)
    assert [event for event, _ in dispositions.closure_pass(docket, 1)] == ["item_closed"]

    events = escalations.moot_item_asks(docket, item_id="F1", disposition=AGREED, round_=1)
    assert [(event, detail["id"]) for event, detail in events] == [("question_mooted", "H1")]
    assert deferred.state == WITHDRAWN and deferred.moot_with == "F1" and deferred.blocked_by == []
    assert blocking.state == ASKED and blocking.moot_with == "", "a declared stop outranks the closure"
    assert termination.unsettled_questions(docket) == [blocking]


def test_the_entry_that_names_an_item_gives_the_deduped_question_its_item():
    """The same words, first with no item and then bound to one: the item and the intent land on one question."""
    docket = docket_with(3)
    question, _ = escalations.raise_question(docket, text="which tenant key?", asker="Agent1", item_id=None, blocking=False, round_=0)
    assert escalations.effective_class(docket, question) == "deferred", "one ask is not a stop"

    again, _ = escalations.raise_question(docket, text="Which tenant key?", asker="Agent1", item_id="F1", blocking=True, round_=1)
    assert again is question, "one question, deduplicated as ever"
    assert question.item_id == "F1" and question.blocked_by == ["Agent1"], "the entry's blocking flag is one seat's claim"

    stance(docket, "F1", "Agent2", NEEDS_HUMAN)
    assert escalations.effective_class(docket, question) == "deferred", "one declared stop is below the bar"
    stance(docket, "F1", "Agent3", NEEDS_HUMAN)
    assert escalations.effective_class(docket, question) == "deferred", "a majority of stances is still not a majority of stops"
    escalations.stop_for(docket, qid="H1", agent="Agent2", round_=2)
    assert escalations.effective_class(docket, question) == "blocking", "the bar counts declared stops"


def test_an_item_is_adopted_once_and_never_lost():
    """Nothing later demotes an escalation back to a request: a co-asker naming no item changes nothing."""
    docket = docket_with(3)
    question, _ = escalations.raise_question(docket, text="which tenant key?", asker="Agent1", item_id="F1", blocking=True, round_=1)
    again, _ = escalations.raise_question(docket, text="which tenant key?", asker="Agent2", item_id=None, blocking=False, round_=2)
    assert again is question and question.item_id == "F1"
    assert question.askers == ["Agent1", "Agent2"]
    assert escalations.effective_class(docket, question) == "deferred", "still one seat of three"


def test_options_union_across_askers_and_never_replace():
    """The text belongs to the first asker; the menu belongs to everybody."""
    docket = docket_with(3)
    question, _ = escalations.raise_question(
        docket,
        text="which retention?",
        asker="Agent1",
        item_id=None,
        blocking=False,
        round_=1,
        options=["keep 30 days", "tie it to the job"],
    )
    again, updated = escalations.raise_question(
        docket,
        text="  Which   RETENTION? ",
        asker="Agent2",
        item_id=None,
        blocking=False,
        round_=2,
        options=["KEEP 30 DAYS", "no cache at all"],
    )
    assert again is question and not updated, "the same question from anyone is one entry"
    assert question.options == ["keep 30 days", "tie it to the job", "no cache at all"]
    assert question.askers == ["Agent1", "Agent2"]


def test_a_reasking_seat_replaces_its_own_menu_and_leaves_a_peers_alone():
    """The menu is each asker's own to revise: a re-ask retires the candidates **it** offered, and nobody else's."""
    docket = docket_with(3)
    question, _ = escalations.raise_question(
        docket,
        text="which retention?",
        asker="Agent1",
        item_id="F1",
        blocking=True,
        round_=1,
        options=["keep 30 days", "tie it to the job"],
    )
    escalations.raise_question(
        docket, text="which retention?", asker="Agent2", item_id="F1", blocking=True, round_=1, options=["no cache at all"]
    )
    assert question.askers == ["Agent1", "Agent2"], "one question, two askers, three candidates"

    assert escalations.refresh_question(
        question,
        asker="Agent1",
        text="which retention window?",
        item_id="F1",
        round_=2,
        options=["keep 30 days", "wipe it nightly"],
    )
    assert question.options == ["keep 30 days", "no cache at all", "wipe it nightly"], "the candidate Agent1 abandoned is off the menu"
    assert ("no cache at all", "Agent2") in question.menu.values(), "a peer that never re-asked still stands behind its line"


def test_a_reask_offering_no_menu_takes_it_from_nobody():
    """Re-asking without candidates is not a revision: a menu the human was shown must not vanish because a model went terse."""
    docket = docket_with(3)
    question, _ = escalations.raise_question(
        docket, text="which retention?", asker="Agent1", item_id="F1", blocking=True, round_=1, options=["keep 30 days"]
    )
    assert escalations.refresh_question(question, asker="Agent1", text="which retention, then?", item_id="F1", round_=2)
    assert question.options == ["keep 30 days"], "the human can still take the line they were shown"


def test_a_verbatim_reask_changes_nothing_and_says_no_more_than_that():
    """`updated` is a fact about the docket, and the event log is the machine channel that reads it."""
    docket = docket_with(3)
    question, _ = escalations.raise_question(
        docket,
        text="which retention?",
        asker="Agent1",
        item_id="F1",
        blocking=True,
        round_=1,
        context="the policy predates the tenant work",
        options=["keep 30 days"],
    )
    assert not escalations.refresh_question(
        question,
        asker="Agent1",
        text="which retention?",
        item_id="F1",
        context="the policy predates the tenant work",
        options=["keep 30 days"],
        round_=2,
    ), "the same words, context and menu changed nothing"
    assert escalations.refresh_question(question, asker="Agent1", text="which retention window?", round_=3), "a rephrase did"


def test_a_settled_question_is_not_a_home_for_a_new_ask():
    """An answered entry is the answer to **that** decision; the same words about a new decision are a new question."""
    docket = docket_with(3, items=2)
    stance(docket, "F1", "Agent1", NEEDS_HUMAN, note="which tenant key?")
    first = docket.questions[docket.escalations[("F1", "Agent1")].question_id]
    first.state, first.answer = ANSWERED, "put the tenant id in the key"

    stance(docket, "F2", "Agent2", NEEDS_HUMAN, note="which tenant key?")
    again = docket.questions[docket.escalations[("F2", "Agent2")].question_id]
    assert again is not first and again.id == "H2", "the answered words did not absorb the new ask"
    assert again.state == ASKED and again.item_id == "F2", "counted against F2, the decision actually being held"
    assert termination.unsettled_questions(docket) == [again], "the new ask is back in the human's sight"
    assert escalations.effective_class(docket, again) == "deferred", "one seat of three is below the bar on F2"


def test_the_asker_owns_the_text_and_its_revised_menu_arrives_whole():
    """The latest asker round wins the wording, and the menu that arrives with it **is** the menu now."""
    docket = docket_with(3)
    question, _ = escalations.raise_question(
        docket, text="which retention?", asker="Agent1", item_id=None, blocking=False, round_=1, options=["keep 30 days"]
    )
    again, updated = escalations.raise_question(
        docket, text="  Which RETENTION? ", asker="Agent1", item_id=None, blocking=False, round_=2, options=["no cache at all"]
    )
    assert again is question and updated, "the same key, retitled by its own asker: latest asker round wins"
    assert question.text == "  Which RETENTION? "
    assert question.options == ["no cache at all"], "the re-ask is the menu now"


def test_withdraw_retire_and_demote_never_leave_an_item_human():
    for end in ("withdrawn", "retired", "demoted"):
        docket = docket_with(3)
        stance(docket, "F1", "Agent1", NEEDS_HUMAN)
        escalation = docket.escalations[("F1", "Agent1")]
        if end == "withdrawn":
            stance(docket, "F1", "Agent1", AGREE, round=2)
        elif end == "retired":
            escalations.retire(escalation, 2)
        else:
            escalations.demote(escalation, 2)
        assert escalation.state != STANDING
        stance(docket, "F1", "Agent2", AGREE)
        stance(docket, "F1", "Agent3", AGREE)
        assert dispositions.derive(docket, "F1") == AGREED, end
        if end == "withdrawn":
            # The asker sent a real stance on the way out, so that stance counts.
            assert docket.effective_stance("F1", "Agent1") == AGREE
        else:
            # A retired or demoted ask is abstain-equivalent: it neither blocks nor objects.
            assert docket.effective_stance("F1", "Agent1") == ABSTAIN, end
            assert dispositions.support(docket, "F1") == 2, end
        assert not docket.standing_escalations("F1"), end


def test_a_declined_record_for_the_asker_leaves_it_standing():
    docket = docket_with(3)
    stance(docket, "F1", "Agent1", NEEDS_HUMAN)
    docket.stances["F1"]["Agent1"] = Stance(stance=ABSTAIN, declined=True, round=2)
    assert docket.is_standing("F1", "Agent1")
    assert dispositions.derive(docket, "F1") == HUMAN


# ---------------------------------------------------------------------------
# The chair's renderings and restart-and-re-ask's names
# ---------------------------------------------------------------------------


def test_file_bases_are_their_own_vocabulary():
    assert naming.seat_file_base(1, "Metic") == "slot01-metic"
    assert naming.seat_file_base(2, "Coworker") == "slot02-coworker"
    assert naming.seat_file_base(12, "Big Name!") == "slot12-big-name"
    assert naming.seat_file_base(9) < naming.seat_file_base(10), "single digits are padded, so a listing is in seat order"
    assert naming.chair_file_base() == "chair"
    assert naming.slugify("  ../weird/../name ") == "weird-name", "path noise collapses to one separator, ends trimmed"
    assert "/" not in naming.slugify("a/b") and ".." not in naming.slugify("a/..b")


def test_docket_shows_you_and_yours_to_the_viewer():
    from mech_council.render import docket as render

    docket = docket_with(3)
    stance(docket, "F1", "Agent1", DISAGREE, note="because")
    # A peer is a number, yourself is `you`, and your own entries say `[yours]`.
    other = render.render(docket, "Agent2")
    body = other.split("\n", 1)[1]  # the header names the viewer to itself
    assert "Agent1" in body and "you" in body and "Agent2" not in body
    own = render.render(docket, "Agent1")
    assert "[yours]" in own


# --------------------------------------------------------------------------- the note ledger


def noted(docket: Docket, text: str, by: str = "Agent1", **kw) -> str:
    note, _updated = notes.raise_note(docket, by=by, text=text, item_id=kw.pop("item_id", None), round_=kw.pop("round_", 1))
    return note.id


def test_a_seat_repeating_itself_updates_its_note_instead_of_forking_it():
    docket = docket_with(3)
    first = noted(docket, "retention is a contract", round_=1)
    again = noted(docket, "Retention   is a   contract.", round_=3)
    assert first == again == "N1" and docket.note_order == ["N1"], "one seat, one sentence, one note"
    assert docket.human_notes["N1"].updated_round == 3


def test_two_seats_saying_one_thing_write_two_notes():
    docket = docket_with(3)
    noted(docket, "retention is a contract", by="Agent1")
    noted(docket, "retention is a contract", by="Agent2")
    assert docket.note_order == ["N1", "N2"], "the docket credits each seat its own statement"


def test_a_note_the_human_never_saw_is_nothing_to_print_or_to_report():
    docket = docket_with(3)
    noted(docket, "retention is a contract")
    notes.retract_note(docket, nid="N1", agent="Agent1", round_=2, reason="the contract says otherwise")
    assert notes.unshown(docket) == [], "a note retracted before it was read never reaches the human"
    assert notes.report_notes(docket) == [], "and the report does not reprint what the council unsaid"


def test_a_retraction_of_a_note_the_human_read_is_its_own_one_shot_finding():
    docket = docket_with(3)
    noted(docket, "retention is a contract")
    notes.mark_shown(notes.unshown(docket), 0)
    notes.retract_note(docket, nid="N1", agent="Agent1", round_=4, reason="the contract says otherwise")
    pending = notes.unshown(docket)
    assert [(n.id, kind) for n, kind in pending] == [("N1", "retraction")]
    notes.mark_shown(pending, 4)
    assert notes.unshown(docket) == [], "the correction is said once, like the note it corrects"
    assert [n.id for n in notes.report_notes(docket)] == ["N1"], "the report keeps what the human was told"


def test_saying_a_note_again_after_retracting_it_is_a_fresh_claim():
    docket = docket_with(3)
    noted(docket, "retention is a contract")
    notes.retract_note(docket, nid="N1", agent="Agent1", round_=2)
    assert noted(docket, "retention is a contract", round_=3) == "N2"


def test_an_id_from_the_other_ledger_moves_nothing_in_either_ledger():
    docket = docket_with(3)
    ask, _ = escalations.raise_question(docket, text="which tenant?", asker="Agent1", item_id=None, round_=1)
    noted(docket, "retention is a contract")
    assert notes.retract_note(docket, nid=ask.id, agent="Agent1", round_=2)[0] is None, "an ask id is no note to unsay"
    for field in (escalations.second, escalations.stop_for, escalations.retract):
        assert field(docket, qid="N1", agent="Agent2", round_=2)[0] is None, "a note id is not an ask to work"
    assert escalations.answer_by_seat(docket, qid="N1", agent="Agent2", text="config holds it", round_=2)[0] is None
    assert escalations.insist(docket, qid="N1", agent="Agent2", round_=2, seen=2)[0] is None
    assert (docket.human_notes["N1"].state, ask.state) == (STANDING, ASKED), "a crossed id changes neither ledger"
    notes.retract_note(docket, nid="N1", agent="Agent1", round_=3)
    assert escalations.second(docket, qid="N1", agent="Agent2", round_=4)[0] is None, "a retracted note is not an open ask either"
