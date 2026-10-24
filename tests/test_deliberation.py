"""The deliberation units, pure: no I/O, no pi, no clock.

The disposition rules' judgment table and the escalation rules' ledger states are
the case lists; the invariant rows assert what the tables cannot say by themselves.
"""

from __future__ import annotations

import pytest

from council import naming
from council.deliberation import dispositions, escalations, stalemate, termination
from council.deliberation.state import (
    ABSTAIN,
    AGREE,
    AGREED,
    DISAGREE,
    DISPUTED,
    HUMAN,
    NEEDS_HUMAN,
    OPEN,
    REJECTED,
    STANDING,
    UNHEARD,
    UNSEEN,
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
        # The disposition rules' own row: "a `declined` abstention is inert, never a
        # dissent: 3 seats, 2 `agree` + 1 `declined` → `agreed`". A declined
        # record is a current stance.
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


def test_every_open_item_yields_exactly_one_state():
    """The invariant: no pattern is stateless, and none is two states at once."""
    options = [None, AGREE, DISAGREE, ABSTAIN, NEEDS_HUMAN]
    docket = docket_with(2, items=1)
    seen = set()
    for first in options:
        for second in options:
            docket.stances["F1"] = {}
            for agent, value in zip(docket.roster, (first, second)):
                if value is not None:
                    stance(docket, "F1", agent, value)
            state = dispositions.derive(docket, "F1")
            assert state in (AGREED, REJECTED, DISPUTED, HUMAN, UNHEARD, UNSEEN)
            seen.add(state)
    assert seen >= {AGREED, DISPUTED, HUMAN, UNHEARD, UNSEEN}


def test_no_open_item_derives_short_a_stance_except_unseen():
    """The invariant in the other direction: while a seat holds no current stance, the
    only state reachable is `unseen`; once every seat has one, `unseen` is unreachable."""
    docket = docket_with(3)
    stance(docket, "F1", "Agent1", AGREE)
    stance(docket, "F1", "Agent2", ABSTAIN)
    assert dispositions.derive(docket, "F1") == UNSEEN
    stance(docket, "F1", "Agent3", ABSTAIN)
    assert dispositions.derive(docket, "F1") != UNSEEN


def test_a_declined_record_is_inert_in_every_count():
    docket = docket_with(3)
    stance(docket, "F1", "Agent1", AGREE)
    stance(docket, "F1", "Agent2", AGREE)
    before = dispositions.support(docket, "F1")
    docket.stances["F1"]["Agent3"] = Stance(stance=DISAGREE, note="ignored", declined=True)
    assert dispositions.support(docket, "F1") == before
    assert dispositions.objections(docket, "F1") == []
    # The disposition rules' boundary row, verbatim: 3 seats, 2 `agree` + 1 `declined` → `agreed`. Inert
    # means it neither dissents nor withholds a stance, so it cannot hold the item open.
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
    assert escalations.effective_class(docket, question) == "blocking"

    chair_question, _ = escalations.raise_question(docket, text="chair asks", asker="chair", item_id=None, blocking=False, round_=1)
    assert escalations.effective_class(docket, chair_question) == "blocking"
    assert termination.blocking_questions(docket) == [question, chair_question]


def test_an_item_less_question_is_never_blocking_however_many_ask():
    docket = docket_with(3)
    for agent in docket.roster:
        escalations.raise_question(docket, text="a rule question", asker=agent, item_id=None, blocking=True, round_=1)
    question = docket.questions["H1"]
    assert question.askers == docket.roster
    assert escalations.effective_class(docket, question) == "deferred"
    assert termination.blocking_questions(docket) == []


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
            # A retired or demoted ask is abstain-equivalent: it neither blocks
            # nor objects, and it is not counted as a stance for the bar either.
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
    assert naming.seat_file_base(1, "Metic") == "slot1-metic"
    assert naming.seat_file_base(2, "Coworker") == "slot2-coworker"
    assert naming.seat_file_base(12, "Big Name!") == "slot12-big-name"
    assert naming.chair_file_base() == "chair"
    assert naming.slugify("  ../weird/../name ") not in ("",)
    assert "/" not in naming.slugify("a/b") and ".." not in naming.slugify("a/..b")


def test_docket_shows_you_and_yours_to_the_viewer():
    from council.render import docket as render

    docket = docket_with(3)
    stance(docket, "F1", "Agent1", DISAGREE, note="because")
    # A peer is named, yourself is `you`, and your own entries say `[yours]`.
    other = render.render(docket, "Agent2", names={"Agent1": "Metic"})
    body = other.split("\n", 1)[1]  # the header names the viewer to itself
    assert "Metic" in other and "you" in body and "Agent2" not in body
    own = render.render(docket, "Agent1", names={"Agent1": "Metic"})
    assert "[yours]" in own and "Metic" not in own
