"""The engine over a scripted port."""

from __future__ import annotations

import re

import pytest

from council.deliberation.engine import Abort, Engine
from council.deliberation.state import DEMOTED, STANDING, WITHDRAWN, Docket
from council.prompts.brief import Brief

from .support.harness import RawAsk, ScriptPort, events_collector, payload

NAMES = {"Agent1": "Metic", "Agent2": "Senior", "Agent3": "Coworker"}


def engine(n=3, *, mode="review", max_rounds=4, reply=None, answers=None, skip=None, selections=None):
    docket = Docket(mode=mode, goal="g", roster=[f"Agent{i}" for i in range(1, n + 1)])
    port = ScriptPort(docket, reply, answers=answers or {}, skip=skip or [], selections=selections or {})
    records, emit = events_collector()
    built = Engine(
        docket,
        port,
        goal="g",
        project="/project",
        brief=Brief(mode=mode, project="/project"),
        max_rounds=max_rounds,
        emit=emit,
    )
    return built, port, records


def finding(claim, severity="major", evidence="x.py:1"):
    return {"claim": claim, "severity": severity, "evidence": evidence}


def phase0(*findings, questions=()):
    return payload(findings=[finding(f) for f in findings], open_questions=[{"text": q} for q in questions])


def phase0_first(who, *findings, questions=()):
    """Only one seat reports, so the docket is exactly as long as the claims are."""
    return phase0(*findings, questions=questions) if who == "Agent1" else payload(findings=[])


def shown_items(message: str) -> list[str]:
    """The ids this seat was actually shown — a scripted agent answers its own docket."""
    return re.findall(r"^### ([A-Z]\d+)", message, flags=re.MULTILINE)


def stance_all(ask, value, *, signoff=True, **extra):
    return payload(
        positions=[{"item": item, "stance": value} for item in shown_items(ask.text)], backing="no_preference", signoff=signoff, **extra
    )


# --------------------------------------------------------------------------- phase 0


def run(engine):
    import asyncio

    return asyncio.run(engine.run())


def test_phase_0_briefing_is_byte_identical_and_names_no_one():
    import asyncio

    async def main():
        built, port, _records = engine(reply=lambda ask: phase0("a bug"))
        await built.phase0()
        return port

    port = asyncio.run(main())
    briefings = [ask.text for ask in port.asks if ask.phase == "phase0"]
    assert len(briefings) == 3
    assert len(set(briefings)) == 1, "one message, sent three times"
    for text in briefings:
        for leak in ("Agent1", "Agent2", "Agent3", "Metic", "Senior", "Coworker"):
            assert leak not in text, f"the briefing carries no roster IDs, but {leak} leaked in"


def test_no_agent_ever_reads_a_name():
    """The numeric ID is the bias control: a seat weighs a claim's evidence, never its reporter's reputation."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the cache key ignores the tenant")
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply)
        outcome = await built.run()
        return outcome, port

    outcome, port = asyncio.run(main())
    assert outcome == "consensus"
    assert port.asks, "the council actually asked"
    for ask in port.asks:
        for name in NAMES.values():
            assert name not in ask.text, f"{name} reached {ask.who} in a {ask.phase} ask"
        if ask.who.startswith("Agent"):
            assert "Agent" in ask.text, "a peer is still addressable — by number"


def test_the_merge_listing_carries_the_detail_the_chair_judges_causes_on():
    """Claim and evidence alone invite merging two causes that share a symptom, so the listing prints the detail too."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[])
            return payload(
                findings=[
                    {
                        "claim": "the cache returns another tenant's rows",
                        "detail": "the key omits the tenant id",
                        "evidence": "cache.py:12",
                    },
                    {
                        "claim": "the cache returns another tenant's rows",
                        "detail": "eviction drops the whole table",
                        "evidence": "cache.py:40",
                    },
                ],
                open_questions=[],
            )
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply)
        await built.run()
        return port

    port = asyncio.run(main())
    listings = [ask.text for ask in port.asks if ask.kind == "merge"]
    assert listings, "new items arrived, so a merge pass ran"
    assert "the key omits the tenant id" in listings[0] and "eviction drops the whole table" in listings[0]
    assert "cache.py:12" in listings[0], "the evidence still rides along"


def test_a_phase_0_human_input_request_arrives_with_its_context_and_options():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[])
            return payload(
                findings=[{"claim": "the cache ignores tenants"}],
                human_input=[
                    {
                        "question": "is the tenant fixed?",
                        "item": "F1",
                        "context": "no caller passes a tenant id",
                        "options": ["fixed by contract", "one tenant forever"],
                    }
                ],
            )
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, _port, _records = engine(reply=reply)
        await built.run()
        return built

    built = asyncio.run(main())
    question = built.docket.questions["H1"]
    assert question.item_id is None, "phase 0 raises no escalation over an item it may not have yet"
    assert question.context == "no caller passes a tenant id"
    assert question.options == ["fixed by contract", "one tenant forever"]
    assert question.state == "asked", "asked at the checkpoint, never a way to hold the run"


def test_a_phase_0_one_liner_the_seat_later_escalates_still_stops_the_run():
    """Phase 0 raises the request item-less; the escalation that re-sends those words brings the item with it."""
    import asyncio

    from council.deliberation.escalations import effective_class

    decision = "is the tenant fixed by contract?"

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[{"claim": "the cache ignores tenants"}])
            return payload(
                findings=[{"claim": "the cache ignores tenants"}],
                human_input=[{"question": decision, "options": ["fixed by contract", "one tenant forever"]}],
            )
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who in ("Agent1", "Agent2"):
            return payload(positions=[{"item": "F1", "stance": "needs_human", "note": decision}], signoff=False)
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply, max_rounds=3)
        await built.run()
        return built, port

    built, port = asyncio.run(main())
    assert built.docket.question_order == ["H1"], "the phase-0 request and the escalation are one question"
    question = built.docket.questions["H1"]
    assert question.item_id == "F1", "the entry that named the item gave the question its item"
    assert effective_class(built.docket, question) == "blocking", f"{built.docket.bar()} of 3 seats hold the escalation"
    waited = {c["round"]: c["waiting"] for c in port.checkpoints}
    assert waited[0] is False, "at phase 0 it is a request over no item, and holds nothing"
    assert waited[1] and waited[2], "once a majority escalates it on the item, the run stops and asks"


def test_an_answer_that_took_an_option_records_the_line_it_took():
    """`H1: 2` is expanded by the Checkpoint into the option's own words; the engine records which one it was."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[])
            return payload(
                findings=[{"claim": "the cache ignores tenants"}],
                human_input=[{"question": "is the tenant fixed?", "options": ["fixed by contract", "one tenant forever"]}],
            )
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, _port, _records = engine(reply=reply, answers={"H1": "one tenant forever"}, selections={"H1": 2})
        outcome = await built.run()
        return outcome, built

    outcome, built = asyncio.run(main())
    assert outcome == "consensus"
    question = built.docket.questions["H1"]
    assert question.answer == "one tenant forever" and question.answer_option == 2
    assert question.answer_option_text == "one tenant forever", "the record proves which offered line was chosen"


def test_an_option_number_the_menu_never_offered_is_refused_and_the_answer_stands():
    """A selection the menu could not have produced is a bug in the port, and the run says which bug — and carries on."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[])
            return payload(findings=[{"claim": "a bug"}], human_input=[{"question": "which way?", "options": ["the only way"]}])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply, answers={"H1": "the only way"}, selections={"H1": 9})
        outcome = await built.run()
        return outcome, built, records

    outcome, built, records = asyncio.run(main())
    assert outcome == "consensus", "the run finishes with the answer it was given"
    question = built.docket.questions["H1"]
    assert question.state == "answered" and question.answer == "the only way"
    assert (question.answer_option, question.answer_option_text) == (0, ""), "no menu line is claimed for a number never shown"
    refused = [record for record in records if record["event"] == "answer_option_rejected"]
    assert len(refused) == 1
    assert refused[0]["id"] == "H1" and refused[0]["option"] == 9 and refused[0]["menu"] == 1


def test_a_negative_option_number_names_no_line_and_costs_the_run_nothing():
    """`options[-2]` would otherwise record the wrong line as the human's choice, silently — and still end no run."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[])
            return payload(findings=[{"claim": "a bug"}], human_input=[{"question": "which way?", "options": ["a", "b", "c"]}])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply, answers={"H1": "b"}, selections={"H1": -2})
        outcome = await built.run()
        return outcome, built, records

    outcome, built, records = asyncio.run(main())
    question = built.docket.questions["H1"]
    assert outcome == "consensus" and question.state == "answered" and question.answer == "b"
    assert (question.answer_option, question.answer_option_text) == (0, ""), "a negative index proves nothing about the menu"
    assert [record["event"] for record in records].count("answer_option_rejected") == 1


def test_a_selection_whose_line_is_not_the_answer_records_no_line():
    """`answer_option` claims the human chose a particular line, so that line had to **be** the answer."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[])
            return payload(findings=[{"claim": "a bug"}], human_input=[{"question": "which way?", "options": ["the only way", "another"]}])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply, answers={"H1": "the only way"}, selections={"H1": 2})
        outcome = await built.run()
        return outcome, built, records

    outcome, built, records = asyncio.run(main())
    question = built.docket.questions["H1"]
    assert outcome == "consensus" and question.answer == "the only way"
    assert (question.answer_option, question.answer_option_text) == (0, ""), "option 2 is another, and the human did not say that"
    refused = [record for record in records if record["event"] == "answer_option_rejected"]
    assert len(refused) == 1 and "not those words" in refused[0]["reason"]


def test_a_seat_standing_by_its_ask_moves_nothing_on_the_docket():
    """A seat re-sending its entry verbatim is standing by the ask, not updating it."""
    import asyncio

    decision = "does policy allow this?"
    entry = {
        "question": decision,
        "item": "F1",
        "blocking": True,
        "context": "the policy predates the tenant work",
        "options": ["yes", "no"],
    }

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who == "Agent1":
                return payload(findings=[{"claim": "the cache ignores tenants"}])
            return payload(findings=[])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1":
            return payload(
                positions=[{"item": item, "stance": "needs_human", "note": decision} for item in shown_items(ask.text)],
                human_input=[entry],
                signoff=False,
            )
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=3)
        outcome = await built.run()
        return outcome, built, records

    outcome, built, records = asyncio.run(main())
    assert outcome == "round_limit_reached" and built.docket.question_order == ["H1"], "one ask, re-filed three times"
    rounds = [record["round"] for record in records if record["event"] == "question_updated"]
    assert rounds == [1], f"round 1 gave the entry its context and menu; rounds 2 and 3 changed nothing, and said {rounds}"


def test_answered_words_do_not_silence_a_new_escalation():
    """A majority escalation reaches the human even when its exact words were answered about another item."""
    import asyncio

    decision = "which tenant key?"

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who == "Agent1":
                return payload(findings=[{"claim": "the cache ignores tenants"}])
            return payload(findings=[])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent3":
            return payload(positions=[{"item": item, "stance": "agree"} for item in shown_items(ask.text)], signoff=True)
        if ask.round == 1:
            return payload(
                positions=[{"item": item, "stance": "needs_human", "note": decision} for item in shown_items(ask.text)],
                human_input=[{"question": decision, "item": "F1", "blocking": True}],
                signoff=False,
            )
        return payload(
            new_items=[{"claim": "the retry loop ignores the tenant too"}] if ask.who == "Agent1" else [],
            positions=[{"item": "F1", "stance": "agree", "note": "the answer settles it"}],
            human_input=[{"question": decision, "item": "F2", "blocking": True}],
            signoff=True,
        )

    async def main():
        built, port, _records = engine(reply=reply, max_rounds=3, answers={"H1": "put the tenant id in the key"})
        outcome = await built.run()
        return outcome, built, port

    outcome, built, port = asyncio.run(main())
    assert outcome == "needs_human", "two seats hold F2, and the human has never been asked about F2"
    first, second = built.docket.questions["H1"], built.docket.questions["H2"]
    assert (first.state, first.item_id) == ("answered", "F1") and (second.state, second.item_id) == ("asked", "F2")
    for (item_id, _asker), escalation in built.docket.escalations.items():
        if item_id == "F2":
            assert escalation.state == STANDING and escalation.question_id == "H2", "the new escalation owns a question that gets asked"
    asked = [checkpoint for checkpoint in port.checkpoints if checkpoint["waiting"]]
    assert asked[-1]["ask_set"] == ["H2"], f"the terminal checkpoint asked {asked[-1]['ask_set']}, not the new escalation"
    assert asked[0]["ask_set"] == ["H1"]


def test_phase0_fewer_than_two_valid_answers_aborts():
    """Briefings that are not payloads are rejected, and fewer than two is no council."""
    import asyncio

    async def main():
        built, _port, records = engine(reply=lambda ask: "prose, and no payload at all")
        with pytest.raises(Abort) as raised:
            await built.phase0()
        return raised.value, records

    failure, records = asyncio.run(main())
    assert "fewer than 2" in failure.reason
    assert sum(1 for r in records if r["event"] == "phase0_rejected") == 3


# ----------------------------------------------- The termination order's direction rule


def test_consensus_needs_no_standing_escalation_and_the_terminal_checkpoint_comes_first():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the cache key ignores the tenant")
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply)
        outcome = await built.run()
        return outcome, port, records

    outcome, _port, records = asyncio.run(main())
    assert outcome == "consensus", [r["event"] for r in records]
    kinds = [r["event"] for r in records]
    assert "human_checkpoint" in kinds
    # The checkpoint that ended the run came *before* the termination decision.
    assert kinds.index("human_checkpoint") < kinds.index("termination")


def test_an_open_question_never_blocks_consent_but_is_asked_at_the_terminal_checkpoint():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug", questions=["what is the retention policy?"])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply)
        outcome = await built.run()
        return outcome, port, records

    outcome, port, _records = asyncio.run(main())
    assert outcome == "consensus", "an item-less question is never effective-blocking"
    terminal = [c for c in port.checkpoints if c["terminal"]]
    assert terminal, "the checkpoint runs before the termination check"
    # The human checkpoint's ask set is every unsettled question the asker still holds, deferred included; the terminal
    # checkpoint waits for all of it. Consensus still arrives because being asked is not the same as being effective-blocking.
    assert terminal[-1]["listing"] == ["H1"]
    assert terminal[-1]["ask_set"] == ["H1"]
    assert terminal[-1]["waiting"] is True


def test_a_needs_human_at_the_bar_holds_the_run_until_the_human_answers():
    import asyncio

    def needs_human(ask, note="needs the product owner"):
        return payload(
            positions=[{"item": item, "stance": "needs_human", "note": note} for item in shown_items(ask.text)],
            backing="no_preference",
            signoff=True,
        )

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who in ("Agent1", "Agent2"):
            return needs_human(ask)
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply)
        return await built.run(), port

    outcome, port = asyncio.run(main())
    assert outcome == "needs_human"
    assert any(c["waiting"] for c in port.checkpoints), "a bar-held needs_human makes the checkpoint wait"


def test_an_answer_at_the_terminal_checkpoint_buys_a_full_round():
    import asyncio

    def blocked(ask):
        return payload(
            positions=[{"item": item, "stance": "needs_human", "note": "blocked"} for item in shown_items(ask.text)],
            backing="no_preference",
            signoff=True,
        )

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who in ("Agent1", "Agent2"):
            return blocked(ask)
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, max_rounds=5, answers={"H1": "the tenant is fixed by contract"})
        outcome = await built.run()
        return outcome, port, records

    outcome, port, records = asyncio.run(main())
    assert any(r["event"] == "answer_buys_a_round" for r in records)
    rounds = sorted({ask.round for ask in port.asks if ask.phase == "round"})
    assert rounds == [1, 2], f"a full round after the answer, not a partial one: {rounds}"


def test_declining_the_whole_ask_set_ends_the_run_immediately():
    import asyncio

    def blocked(ask):
        return payload(
            positions=[{"item": item, "stance": "needs_human", "note": "blocked"} for item in shown_items(ask.text)],
            backing="no_preference",
            signoff=True,
        )

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who in ("Agent1", "Agent2"):
            return blocked(ask)
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply, skip=["H1"])
        return await built.run(), port

    outcome, _port = asyncio.run(main())
    assert outcome == "needs_human"


# ------------------------------------------------------------------ The gap ladder gaps


def test_abstain_and_no_preference_are_stances_and_never_gaps():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug", "another")
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "abstain")

    async def main():
        built, port, records = engine(reply=reply, max_rounds=1)
        await built.run()
        return port, records

    port, records = asyncio.run(main())
    assert port.count(phase="gap") == 0, "abstaining is an answer, not a gap"
    assert not any(r["event"] == "gap_declined" for r in records)


def test_a_seat_that_answers_the_gap_with_a_non_answer_is_recorded_declined():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1":
            return stance_all(ask, "agree")
        if ask.phase == "gap":
            return payload(positions=[], signoff=False)  # a mechanical non-answer
        return payload(positions=[], signoff=True)  # silent on the whole docket

    async def main():
        built, port, records = engine(reply=reply, max_rounds=1)
        await built.run()
        return port, records

    port, records = asyncio.run(main())
    assert port.count(who="Agent2", phase="gap") == 1, "one gap ask, then its silence is recorded"
    assert any(r["event"] == "gap_declined" and r["member"] == "Agent2" for r in records), "an exhausted ladder records declined"


def test_a_deliberate_gap_gets_one_ask_not_four():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1":
            return stance_all(ask, "agree")
        return payload(positions=[], signoff=True)  # silent, then refuses

    async def main():
        built, port, _records = engine(reply=reply, max_rounds=1)
        await built.run()
        return port

    port = asyncio.run(main())
    assert port.count(who="Agent2", phase="gap") == 1, "deliberate: one ask, then declined"


def test_a_round_of_only_closed_one_liners_asks_nothing():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply, max_rounds=3)
        await built.run()
        return port

    port = asyncio.run(main())
    assert port.count(phase="round") == 3, "one round only: consensus ends it"
    assert port.count(phase="gap") == 0


# ------------------------------------------------- Escalation challenges


def test_a_challenge_without_evidence_costs_no_defense_ask():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        items = shown_items(ask.text)
        if ask.who == "Agent1":
            return payload(
                positions=[{"item": i, "stance": "needs_human", "note": "this is a judgement call"} for i in items], signoff=True
            )
        if ask.who == "Agent2":
            return payload(positions=[{"item": i, "stance": "disagree"} for i in items], signoff=True)
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, max_rounds=1)
        await built.run()
        return port, records

    port, _records = asyncio.run(main())
    assert port.count(who="Agent1", phase="defense") == 0, "no cited evidence, no ask"


def test_a_challenge_with_evidence_costs_exactly_one_defense_ask():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        items = shown_items(ask.text)
        if ask.who == "Agent1":
            return payload(
                positions=[{"item": i, "stance": "needs_human", "note": "this is a judgement call"} for i in items], signoff=True
            )
        if ask.who == "Agent2" and ask.phase != "defense":
            return payload(positions=[{"item": i, "stance": "disagree", "evidence": "docs/policy.md:4"} for i in items], signoff=True)
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, max_rounds=1)
        await built.run()
        return port, records

    port, _records = asyncio.run(main())
    assert port.count(who="Agent1", phase="defense") == 1


# ------------------------------------------------ Restart-and-re-ask through the port


def test_a_second_ask_failure_ends_the_run_naming_the_member_and_round():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0("a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent2":
            return RawAsk(failure="Agent2's provider returned an error")
        return payload(positions=[{"item": "F1", "stance": "agree"}], signoff=True)

    async def main():
        built, _port, _records = engine(reply=reply)
        with pytest.raises(Abort) as raised:
            await built.run()
        return raised.value

    failure = asyncio.run(main())
    assert failure.member == "Agent2"
    assert failure.round == 1


def test_a_chair_ask_failure_takes_the_identical_path():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind in ("merge", "compare"):
            return RawAsk(failure="chair: the answer never came")
        return stance_all(ask, "agree")

    async def main():
        built, _port, _records = engine(reply=reply)
        with pytest.raises(Abort) as raised:
            await built.run()
        return raised.value

    failure = asyncio.run(main())
    assert failure.member == "chair"


def test_a_refused_merge_plan_leaves_the_docket_unmerged_and_the_run_going():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the token cache is wrong", "the cache token is wrong")
        if ask.kind == "merge":
            # The engine's structural check for a merge plan — a pass both modes run: one ID reused across groups refuses the whole plan.
            return payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}, {"canonical": "F2", "duplicates": ["F3"]}])
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=1)
        outcome = await built.run()
        return outcome, built, records

    outcome, built, records = asyncio.run(main())
    assert len(built.docket.items) == 2, "nothing merged"
    assert any(r["event"] == "merge_refused" for r in records), records
    assert outcome != "aborted"


# --------------------------------------------------------------------------- plan mode


def test_plan_mode_compares_the_plans_and_no_preference_alone_never_sets_a_direction():
    """The termination order's direction rule, condition 3: a round where nobody proposed goes on."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(
                summary="one paragraph",
                approach={
                    "claim": "do it in one step",
                    "stages": [{"state": "the step is done", "gate": "the tests pass", "scope": "src/", "exceptions": []}],
                },
            )
        if ask.kind == "compare":
            return payload(agreed=["the staging"], conflicts=[], uncovered=[])
        return payload(positions=[], backing="no_preference", signoff=True)

    async def main():
        built, port, _records = engine(reply=reply, mode="plan", max_rounds=1)
        return await built.run(), port

    outcome, port = asyncio.run(main())
    assert port.asked("chair", phase="compare"), "plan mode compares"
    assert outcome == "round_limit_reached", "no_preference never causes consensus"


def test_plan_mode_reaches_consensus_on_one_backer_and_two_shrugs():
    """The termination order's direction rule, conditions 1-3, including what it concedes out loud."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(
                summary="one paragraph",
                approach={
                    "claim": "do it in one step",
                    "stages": [{"state": "the step is done", "gate": "the tests pass", "scope": "src/", "exceptions": []}],
                },
            )
        if ask.kind == "compare":
            return payload(agreed=["the staging"], conflicts=[], uncovered=[])
        backing = "Agent1" if ask.who == "Agent1" else "no_preference"
        return payload(positions=[], backing=backing, signoff=True)

    async def main():
        built, _port, records = engine(reply=reply, mode="plan", max_rounds=3)
        return await built.run(), records

    outcome, records = asyncio.run(main())
    assert outcome == "consensus", [r for r in records if r["event"] == "termination"]


def test_plan_mode_objects_loudly_when_two_seats_name_different_directions():
    """A seat naming another plan is an objection, not a lost vote (the direction rule's second condition)."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(approach={"claim": "do it", "stages": [{"state": "done", "gate": "green", "scope": "src/", "exceptions": []}]})
        if ask.kind == "compare":
            return payload(agreed=[], conflicts=[], uncovered=[])
        backing = {"Agent1": "Agent1", "Agent2": "Agent2", "Agent3": "no_preference"}
        return payload(positions=[], backing=backing[ask.who], signoff=True)

    async def main():
        built, _port, records = engine(reply=reply, mode="plan", max_rounds=2)
        return await built.run(), records

    outcome, records = asyncio.run(main())
    assert outcome == "round_limit_reached", "a different token is an objection"
    reasons = [r.get("reason", "") for r in records if r["event"] == "round_continue"]
    assert any("different directions" in reason for reason in reasons), reasons


def test_an_answer_naming_its_addressee_instead_of_a_q_id_still_lands():
    """Lenient extraction's "never silently dropped", on the shape a live model actually writes."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.who == "Agent1" and ask.round == 1:
            # Agent1 asks Agent2 something; the engine assigns the `Q` id, the model never sees one it must quote back.
            return {"positions": [], "questions": [{"to": "Agent2", "question": "why is the key safe?"}]}
        if ask.who == "Agent2" and ask.round == 1:
            return {"positions": [], "answers": [{"to": "Agent1", "answer": "because it is per-tenant"}]}
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply)
        await built.run()
        return built, records

    built, records = asyncio.run(main())
    asked = list(built.docket.qa.values())
    assert asked, "the peer question never reached the docket"
    answered = [qa for qa in asked if qa.answer]
    assert answered, f"the answer was dropped instead of matched: {asked[0].id} is bare"
    assert answered[0].answer == "because it is per-tenant"
    assert "qa_answer_unmatched" not in [r["event"] for r in records]


def test_an_answer_naming_nobody_is_logged_and_kept_out_of_the_docket():
    """Unmatched is *logged*, not guessed at and not swallowed."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.who == "Agent2":
            return {"positions": [], "answers": [{"to": "Agent9", "question": "Q404", "answer": "nothing"}]}
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply)
        await built.run()
        return built, records

    built, records = asyncio.run(main())
    assert [r for r in records if r["event"] == "qa_answer_unmatched"], records
    assert not [qa for qa in built.docket.qa.values() if qa.answer]


# --------------------------------------------------- Escalation defenses


def _escalation_and_defense(asked=None, **defend):
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "defense":
            return payload(positions=[{"item": "F1", **defend}], human_input=asked or [], signoff=True)
        if ask.who == "Agent1":
            return payload(
                positions=[{"item": i, "stance": "needs_human", "note": "does policy allow this"} for i in shown_items(ask.text)],
                signoff=True,
            )
        if ask.who == "Agent2":
            return payload(
                positions=[{"item": i, "stance": "disagree", "evidence": "docs/policy.md:4"} for i in shown_items(ask.text)], signoff=True
            )
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=1)
        await built.run()
        return built, records

    return asyncio.run(main())


def test_a_defense_that_concedes_withdraws_and_is_never_reported_undefended():
    """Restancing ends the escalation; a voluntary change is not a failure."""
    built, records = _escalation_and_defense(stance="agree", note="the policy does settle it")
    escalation = built.docket.escalations[("F1", "Agent1")]
    events = [r["event"] for r in records]
    assert escalation.state == WITHDRAWN
    assert escalation.state != DEMOTED, "a seat that changed its mind was not demoted"
    assert not escalation.undefended
    assert "escalation_demoted" not in events


def test_a_defense_that_only_repeats_the_question_is_demoted():
    """The defense rule's own trigger: repeating the question brings nothing to the item."""
    built, records = _escalation_and_defense(stance="needs_human", note="does policy allow this")
    escalation = built.docket.escalations[("F1", "Agent1")]
    assert escalation.state == DEMOTED
    assert escalation.undefended, "the review report prints it marked undefended"
    assert "escalation_demoted" in [r["event"] for r in records]


def test_a_defense_that_answers_in_other_words_keeps_the_escalation_standing():
    """Nothing in the escalation rules requires the defense to quote the challenger's wording back."""
    built, records = _escalation_and_defense(stance="needs_human", note="that document predates the tenant work, so a person must decide")
    escalation = built.docket.escalations[("F1", "Agent1")]
    assert escalation.state == STANDING
    assert "escalation_demoted" not in [r["event"] for r in records]


def test_a_defense_offering_the_human_a_menu_keeps_it():
    """A defense is a verdict, and the round schema asks every verdict for `context` and `options`."""
    built, _records = _escalation_and_defense(
        asked=[
            {
                "question": "does policy allow this?",
                "item": "F1",
                "blocking": True,
                "context": "the policy predates the tenant work",
                "options": ["key it on the tenant", "drop the cache"],
            }
        ],
        stance="needs_human",
        note="that document predates the tenant work, so a person must decide",
    )
    escalation = built.docket.escalations[("F1", "Agent1")]
    question = built.docket.questions[escalation.question_id]
    assert question.context == "the policy predates the tenant work"
    assert question.options == ["key it on the tenant", "drop the cache"], "the menu the seat offered reaches the checkpoint"


def test_a_defense_that_rephrases_updates_its_own_question_and_never_opens_a_second():
    """One escalation is one question, however many times its asker rewords it."""
    from council.deliberation.termination import unsettled_questions

    built, _records = _escalation_and_defense(
        asked=[
            {
                "question": "does the policy permit a 30-day window?",
                "item": "F1",
                "blocking": True,
                "context": "the policy predates the tenant work",
                "options": ["keep it 30 days", "tie it to the job"],
            }
        ],
        stance="needs_human",
        note="that document predates the tenant work, so a person must decide",
    )
    escalation = built.docket.escalations[("F1", "Agent1")]
    assert built.docket.question_order == [escalation.question_id], "the docket holds one question for one escalation"
    question = built.docket.questions[escalation.question_id]
    assert question.text == "does the policy permit a 30-day window?", "the rephrasing is what the human is asked"
    assert question.options == ["keep it 30 days", "tie it to the job"] and question.context == "the policy predates the tenant work"
    assert [q.id for q in unsettled_questions(built.docket)] == [question.id], "the checkpoint asks it once"


def test_a_rephrased_reask_that_is_not_a_defense_also_updates_rather_than_forks():
    """The same rule on the round path: a seat standing by its escalation with new words moves its own question."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the cache ignores tenants")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1":
            first = ask.round == 1
            return payload(
                positions=[{"item": "F1", "stance": "needs_human", "note": "which tenant key?" if first else "which key, then?"}],
                human_input=[{"question": "which tenant key?" if first else "which key, then?", "item": "F1", "blocking": True}],
                signoff=False,
            )
        return stance_all(ask, "agree")

    async def main():
        built, _port, _records = engine(reply=reply, max_rounds=2)
        await built.run()
        return built

    built = asyncio.run(main())
    escalation = built.docket.escalations[("F1", "Agent1")]
    assert built.docket.question_order == [escalation.question_id], "two rounds, one question"
    question = built.docket.questions[escalation.question_id]
    assert question.text == "which key, then?" and question.item_id == "F1"
