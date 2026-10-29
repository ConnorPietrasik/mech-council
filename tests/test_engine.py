"""The engine over a scripted port."""

from __future__ import annotations

import re

import pytest

from mech_council.deliberation.engine import Abort, Engine
from mech_council.deliberation.state import DEMOTED, STANDING, WITHDRAWN, Docket
from mech_council.prompts import common
from mech_council.prompts import plan as plan_text
from mech_council.prompts.brief import Brief

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
        names=NAMES,
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


def test_the_shared_briefing_works_for_either_mode():
    """No shared briefing line tells a planning seat it is reviewing somebody's code."""
    for mode in ("review", "plan"):
        brief = Brief(mode=mode, project="/project")
        texts = [
            brief.phase0(mode=mode, goal="ship the migration", project="/project"),
            brief.round(mode=mode, goal="ship the migration", project="/project", me="Agent1", docket="", phase0_summary=""),
        ]
        for text in texts:
            assert "Goal: ship the migration" in text, "the goal is the brief the seat works to"
            for leak in ("independent reviewers", "A finding survives", "never a finding", "summary of your finding"):
                assert leak not in text, f"the shared briefing says {leak!r} to a {mode} seat"
        # The mode still says what the seat produces, in its own words.
        assert ("plan you would execute" if mode == "plan" else "fix or improve") in "\n".join(texts)


def test_a_mode_s_extra_briefing_reaches_the_seats_it_was_written_for():
    """`EXTRA_BRIEFING` reaches the seats its prose was written for and no others."""
    briefs = {
        mode: Brief(mode=mode, project="/project").round(mode=mode, goal="g", project="/project", me="Agent1", docket="", phase0_summary="")
        for mode in ("review", "plan")
    }
    assert plan_text.EXTRA_BRIEFING.strip() in briefs["plan"], "plan declares prose and never briefs it"
    assert plan_text.EXTRA_BRIEFING.strip() not in briefs["review"], "a review seat reads planning prose"


def test_each_mode_tells_the_chair_what_makes_two_entries_one():
    """The dedup discipline is the chair's and mode-blind; the test for one entry twice is the mode's."""
    tests = {"review": "Forbid merging partial overlaps", "plan": "two forks that merely share a word"}
    for mode, its_own in tests.items():
        ask = common.merge_task(mode, "- X1: the claim\n    what breaks: the detail", "g")
        assert its_own in ask, f"the {mode} chair is never told its own test for one entry twice"
        assert tests["plan" if mode == "review" else "review"] not in ask, f"the {mode} chair is read the other mode's test"
        for hedge in ("same underlying thing", "same underlying item", "wrong or undecided"):
            assert hedge not in ask, f"the {mode} merge ask hedges with {hedge!r}"
        # What the modes share stays shared: the chair's own discipline, in both asks.
        for shared in ("Conservative beats tidy", "Echo those IDs exactly"):
            assert shared in ask
    # A chair told to echo IDs exactly gets the IDs its own docket hands out, not review mode's.
    assert '"canonical": "F1"' in common.merge_task("review", "- F1: the claim", "g")
    assert '"canonical": "T1"' in common.merge_task("plan", "- T1: the claim", "g")


def test_the_chair_is_told_it_raises_nothing_and_words_only_a_split():
    for mode in ("review", "plan"):
        merge = common.merge_task(mode, "- X1: the claim", "g")
        assert "You raise nothing to the human" in merge, f"the {mode} chair is still offered a question to raise"
        assert "human_input" not in merge and "blocking" not in merge, f"the {mode} merge schema still carries a question field"
    comparison = common.comparison_task("### Agent1\n\ndo it", "g")
    assert "You ask the human nothing from this pass" in comparison
    assert "human_input" not in comparison, "the comparison still offers the chair a question field"
    direction = common.direction_task("### Agent1\n\n- Agent1, backing `the original plan`:\ndo it", "g")
    assert "the ID each seat answers with" in direction, "the chair may word a direction the run never listed"
    assert "blocking" not in direction, "the chair was handed a stop button again"


def test_the_merge_listing_words_an_item_s_detail_the_way_the_mode_does():
    """The chair reads an item's detail under the label that mode's schema asks the seat for."""
    import asyncio

    from mech_council.deliberation.state import Item

    def listing(mode: str, detail: str) -> str:
        async def main():
            built, port, _records = engine(mode=mode, reply=payload(groups=[]))
            built.docket.add_item(Item(id=built.new_id(), claim="the claim", detail=detail, raised_by=["Agent1"]))
            await built.publish_review()
            return next(ask.text for ask in port.asks if ask.kind == "merge")

        return asyncio.run(main())

    assert "what breaks: the write is not idempotent" in listing("review", "the write is not idempotent")
    plan = listing("plan", "queue it, or retry in place")
    assert "the answers it admits: queue it, or retry in place" in plan
    assert "what breaks" not in plan, "a decision point's detail line is not a finding's"


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

    from mech_council.deliberation.escalations import effective_class

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
    assert outcome == "stalemate" and built.docket.question_order == ["H1"], "one ask, re-filed until the council stalled on it"
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
    # The terminal checkpoint waits for every unsettled question, deferred included; being asked is not being effective-blocking.
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
            # The structural merge check both modes run: one ID reused across groups refuses the whole plan.
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


PLANS = {"claim": "do it", "stages": [{"state": "done", "gate": "green", "scope": "src/", "exceptions": []}]}


def test_no_chair_pass_puts_a_question_to_the_human():
    """The chair sees no code and holds no stance: a question of its own would reach the human unexamined and unchallenged."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            reported = {"claim": "the cache ignores tenants", "severity": "major", "evidence": "x.py:1"}
            return payload(approach=dict(PLANS), findings=[reported] if ask.who == "Agent1" else [])
        if ask.kind == "merge":
            return payload(groups=[], human_input=[{"question": "is the tenant fixed by contract?", "blocking": True}])
        if ask.kind == "compare":
            return payload(agreed=[], conflicts=[], uncovered=[], human_input=[{"question": "which order?"}])
        return stance_all(ask, "agree")

    async def main():
        boards = {}
        for mode in ("review", "plan"):
            built, _port, _records = engine(reply=reply, mode=mode, max_rounds=2)
            boards[mode] = await built.run(), built
        return boards

    boards = asyncio.run(main())
    for mode, (_outcome, built) in boards.items():
        assert built.docket.question_order == [], f"the {mode} chair put a question to the human anyway"


def split_council(following: str | None = None):
    """Two seats, two directions, a third with no preference; `following` is what the seats back once they read an answer."""

    def reply(ask):
        if ask.phase == "phase0":
            return payload(approach=dict(PLANS))
        if ask.kind == "compare":
            return payload(agreed=[], conflicts=[], uncovered=[])
        if ask.kind == "direction":
            return payload(question="which plan do you want?", context="two seats, two orders of work")
        if following and "ANSWER (authoritative)" in ask.text:
            return payload(positions=[], backing=following, signoff=True)
        backing = {"Agent1": "Agent1", "Agent2": "Agent2", "Agent3": "no_preference"}
        return payload(positions=[], backing=backing[ask.who], signoff=True)

    return reply


def test_a_council_that_will_not_converge_asks_instead_of_spending_another_round():
    """The split is a fact of the seats' own signed backings, so the ask needs nobody's permission and the wait is not optional."""
    import asyncio

    from mech_council.deliberation.escalations import effective_class

    async def main():
        built, port, records = engine(reply=split_council(), mode="plan", max_rounds=6)
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    ask = built.docket.direction_ask()
    assert ask is not None and ask.direction_ask and ask.item_id is None
    assert ask.text == "which plan do you want?", "the chair words the ask"
    assert ask.options == ["Agent1: do it → done", "Agent2: do it → done"], "one line per direction the council holds, and no others"
    assert effective_class(built.docket, ask) == "blocking", "no seat can plan against a direction the council has not chosen"
    notice = next(record for record in records if record["event"] == "direction_ask")
    assert notice["directions"] == ["Agent1", "Agent2"] and notice["phrased_by"] == "chair"
    assert notice["round"] == 2 and notice["camps_held"] == 2, "the first round nothing moved in, not one still being argued"
    assert outcome == "needs_human"
    assert port.checkpoints[-1]["waiting"], "the split ask holds the run: the round after it would be waste"
    spent = [r["round"] for r in records if r["event"] == "round_summary"]
    assert spent == [1, 2], f"the run asked instead of spending rounds 3 to 6: {spent}"
    reasons = [r.get("reason", "") for r in records if r["event"] == "round_continue"]
    assert any("different directions" in reason for reason in reasons), "round 1 says what it is still going on about"


def test_a_council_still_laying_plans_buys_its_split_one_more_round():
    """A seat laying a fresh plan is still trying to win a camp over, so the ask waits out the rounds it is working in — three at
    the latest, whatever it is drawing.
    """
    import asyncio

    def reply(ask):
        if ask.kind == "direction":
            return payload(question="which order of work?", context="two gates")
        if ask.who == "Agent2" and ask.phase != "phase0":
            # The same backing restated and the same plan redrawn: nothing the docket's own signature would notice.
            return payload(
                positions=[],
                backing="merged: gate first",
                signoff=True,
                revised_plan={"name": "gate first", "stages": [{"state": f"the gate, drawn {ask.round}"}]},
            )
        return split_council()(ask)

    async def main():
        built, _port, records = engine(reply=reply, mode="plan", max_rounds=6)
        return await built.run(), built, records

    outcome, _built, records = asyncio.run(main())
    notice = next(record for record in records if record["event"] == "direction_ask")
    assert notice["round"] == 3 and notice["camps_held"] == 3, "the two rounds it was working in were not counted against it"
    assert [r["round"] for r in records if r["event"] == "round_summary"] == [1, 2, 3]
    assert outcome == "needs_human"


def test_a_split_on_the_last_round_is_asked_however_busy_the_council_was():
    """Waiting for an idle round buys nothing when there is no round left to wait for: a split that ends unasked is the failure
    mode this ask exists to avoid, so the ceiling asks regardless of how much work that round was doing.
    """
    import asyncio

    def reply(ask):
        if ask.kind == "direction":
            return payload(question="which order of work?", context="two gates")
        if ask.who == "Agent2" and ask.phase != "phase0":
            return payload(
                positions=[],
                backing="merged: gate first",
                signoff=True,
                revised_plan={"name": "gate first", "stages": [{"state": "drawn again"}]},
            )
        return split_council()(ask)

    async def main():
        built, _port, records = engine(reply=reply, mode="plan", max_rounds=2)
        return await built.run(), built, records

    outcome, built, records = asyncio.run(main())
    ask = built.docket.direction_ask()
    assert ask is not None and "unasked" not in ask.markers, "the round limit swallowed the split instead of asking it"
    assert next(r for r in records if r["event"] == "direction_ask")["round"] == 2
    assert outcome == "needs_human"


def test_renaming_a_merge_is_no_movement_to_a_split():
    """The clock counts camps, not labels: a seat re-stamping its merge every round is fidgeting, and fidgeting runs out at three."""
    import asyncio

    def reply(ask):
        if ask.kind == "direction":
            return payload(question="which order of work?", context="two gates")
        if ask.who == "Agent2" and ask.phase != "phase0":
            return payload(positions=[], backing=f"merged: v{ask.round}", signoff=True)
        return split_council()(ask)

    async def main():
        built, _port, records = engine(reply=reply, mode="plan", max_rounds=6)
        return await built.run(), built, records

    outcome, built, records = asyncio.run(main())
    assert next(r for r in records if r["event"] == "direction_ask")["round"] == 3, "the camps never changed, whatever they were called"
    assert built.docket.direction_ask().options == ["Agent1: do it → done", "merged: v3: do it → done"], "the current label is the menu's"
    assert outcome == "needs_human"


def test_a_split_menu_offers_the_directions_in_ids_and_prints_them_in_names():
    """A taken line is recorded verbatim as what the seats plan against, so the menu is stored in the ids they read; the console is
    where a seat becomes a name — over the chair's sentence too, which is worded from a listing headed by those ids.  Every line
    carries the plan's own claim and stages — a `merged:` token alone names nothing.
    """
    import asyncio

    from mech_council.humaninput import render_ask_set

    def reply(ask):
        if ask.phase == "phase0":
            return payload(approach=dict(PLANS))
        if ask.kind == "compare":
            return payload(agreed=[], conflicts=[], uncovered=[])
        if ask.kind == "direction":
            return payload(
                question="Agent1 gates before anything ships; Agent2 ships and gates after",
                context="Agent1 buys a safe first release; Agent2 buys the first week",
            )
        if ask.who == "Agent2":
            return payload(
                positions=[],
                backing="merged: ship the gate first",
                signoff=True,
                revised_plan={"name": "ship the gate first", "stages": [{"state": "the gate is green"}, {"state": "the helper is called"}]},
            )
        return payload(positions=[], backing={"Agent1": "Agent1", "Agent3": "no_preference"}[ask.who], signoff=True)

    async def main():
        built, _port, _records = engine(reply=reply, mode="plan", max_rounds=6)
        return await built.run(), built

    _outcome, built = asyncio.run(main())
    ask = built.docket.direction_ask()
    assert ask.options == [
        "Agent1: do it → done",
        "merged: ship the gate first: the gate is green → the helper is called",
    ]
    menu = render_ask_set(built.docket, [ask], names=NAMES, waiting=True)
    assert "1) Metic: do it → done" in menu, menu
    assert "2) the merge `ship the gate first`: the gate is green" in menu, menu
    assert "Metic gates before anything ships" in menu and "Metic buys a safe first release" in menu, menu
    assert "Agent" not in menu, "the console leaves an id where the human needs a name"
    assert "Agent1" in ask.text, "what the seats are shown keeps the id they answer with"


def test_an_answer_at_the_round_limit_buys_the_round_it_promised():
    """The split ask lands on the last allowed round, so the ceiling has to move or the answer buys nothing."""
    import asyncio

    async def main():
        built, _port, records = engine(
            reply=split_council(following="Agent1"),
            mode="plan",
            max_rounds=2,
            answers={"H1": "Agent1: do it → done"},
            selections={"H1": 1},
        )
        return await built.run(), records

    outcome, records = asyncio.run(main())
    assert outcome == "consensus", [r for r in records if r["event"] == "termination"]
    assert [r["round"] for r in records if r["event"] == "round_summary"] == [1, 2, 3], "round 3 is the one the answer paid for"


def test_an_answer_to_the_split_ask_buys_the_round_that_converges_on_it():
    import asyncio

    async def main():
        built, port, records = engine(
            reply=split_council(following="Agent1"),
            mode="plan",
            max_rounds=6,
            answers={"H1": "Agent1: do it → done"},
            selections={"H1": 1},
        )
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    ask = built.docket.direction_ask()
    assert ask.state == "answered" and ask.answer_option == 1, "a numbered answer names the direction it was shown"
    shown = next(ask_.text for ask_ in port.asks if ask_.round == 3 and ask_.who == "Agent2")
    assert "ANSWER (authoritative): Agent1: do it → done" in shown, "the seat is told the direction in the id it answers with"
    assert "Metic" not in shown, "a name would be a direction the seats have no token for"
    assert built.docket.backing == {"Agent1": "Agent1", "Agent2": "Agent1", "Agent3": "Agent1"}, "the seats followed it"
    assert outcome == "consensus", [r for r in records if r["event"] == "termination"]


def test_a_chair_that_cannot_word_the_split_costs_the_run_neither_the_ask_nor_the_run():
    import asyncio

    def reply(ask):
        if ask.kind == "direction":
            return RawAsk(failure="chair: the answer never came")
        return split_council()(ask)

    async def main():
        built, port, records = engine(reply=reply, mode="plan", max_rounds=3)
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    assert outcome != "aborted", "the split is the docket's own fact; the chair's sentence is a nicety"
    ask = built.docket.direction_ask()
    assert ask is not None and ask.text == "The seats will not converge on one direction: which do you want?"
    assert ask.options == ["Agent1: do it → done", "Agent2: do it → done"], "the menu never depended on the chair"
    assert ask.context == "2 rounds: Metic, Senior still standing, no camp changed.", "the human is told names, not protocol ids"
    assert any(r["event"] == "chair_direction_unreadable" for r in records), "the run says the wording is the docket's"
    assert next(r for r in records if r["event"] == "direction_ask")["phrased_by"] == "docket"
    assert outcome == "needs_human" and port.checkpoints[-1]["ask_set"] == [ask.id]


def test_a_second_needs_human_says_so_on_the_event_feed():
    """A stance is what usually moves an escalation's effective class, and the seat that moved it never re-asks."""
    import asyncio

    decision = "is the tenant fixed by contract?"

    def reply(ask):
        if ask.phase == "phase0":
            reported = [
                {"claim": "the cache ignores tenants", "severity": "major", "evidence": "src/app.py:1"},
                {"claim": "the retry hides 5xx", "severity": "major", "evidence": "src/app.py:9"},
            ]
            return payload(findings=reported if ask.who == "Agent1" else [])
        if ask.kind == "merge":
            return payload(groups=[])
        # F2 stays refuted, so the run is still working while the escalation on F1 gathers its second holder.
        escalating = ask.who == "Agent2" or (ask.who == "Agent3" and ask.round > 1)
        return payload(
            positions=[
                {"item": "F1", "stance": "needs_human" if escalating else "agree", "note": decision if escalating else ""},
                {"item": "F2", "stance": "disagree", "note": "the guard sits upstream", "evidence": "src/app.py:12"}
                if ask.who == "Agent1"
                else {"item": "F2", "stance": "agree"},
            ],
            backing="no_preference",
            signoff=True,
        )

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=4)
        return await built.run(), built, records

    _outcome, built, records = asyncio.run(main())
    assert built.docket.question_order == ["H1"], "one question, held by two seats"
    flips = [r for r in records if r["event"] == "question_effective_class"]
    assert [(r["prior"], r["effective"], r["round"]) for r in flips] == [("deferred", "blocking", 2)], flips
    assert any(r["event"] == "human_checkpoint" and r["waited"] for r in records), "the wait follows the class the run derived"


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
    from mech_council.deliberation.termination import unsettled_questions

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
