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


def engine(n=3, *, mode="review", max_rounds=4, grace_rounds=1, seconding=True, reply=None, answers=None, skip=None, selections=None):
    docket = Docket(mode=mode, goal="g", roster=[f"Agent{i}" for i in range(1, n + 1)], seconding=seconding)
    port = ScriptPort(docket, reply, answers=answers or {}, skip=skip or [], selections=selections or {})
    records, emit = events_collector()
    built = Engine(
        docket,
        port,
        goal="g",
        project="/project",
        brief=Brief(mode=mode, project="/project"),
        max_rounds=max_rounds,
        grace_rounds=grace_rounds,
        names=NAMES,
        emit=emit,
    )
    return built, port, records


def finding(claim, severity="major", evidence="x.py:1"):
    return {"claim": claim, "severity": severity, "evidence": evidence}


def phase0(*findings, questions=()):
    return payload(findings=[finding(f) for f in findings], open_questions=list(questions))


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


def test_every_briefing_carries_the_goal_the_seat_works_to():
    for mode in ("review", "plan"):
        brief = Brief(mode=mode, project="/project")
        for text in (
            brief.phase0(mode=mode, goal="ship the migration", project="/project"),
            brief.round(mode=mode, goal="ship the migration", project="/project", me="Agent1", docket="", phase0_summary=""),
        ):
            assert "Goal: ship the migration" in text


def test_a_mode_s_extra_briefing_reaches_the_seats_it_was_written_for():
    """`EXTRA_BRIEFING` reaches the seats its prose was written for and no others."""
    briefs = {
        mode: Brief(mode=mode, project="/project").round(mode=mode, goal="g", project="/project", me="Agent1", docket="", phase0_summary="")
        for mode in ("review", "plan")
    }
    assert plan_text.EXTRA_BRIEFING.strip() in briefs["plan"], "plan declares prose and never briefs it"
    assert plan_text.EXTRA_BRIEFING.strip() not in briefs["review"], "a review seat reads planning prose"


def test_the_merge_schema_stamps_the_ids_the_mode_s_docket_hands_out():
    assert '"canonical": "F1"' in common.merge_task("review", "- F1: the claim", "g")
    assert '"canonical": "T1"' in common.merge_task("plan", "- T1: the claim", "g")


def test_no_chair_schema_offers_it_a_question_to_raise():
    for mode in ("review", "plan"):
        merge = common.merge_task(mode, "- X1: the claim", "g")
        assert "human_input" not in merge and "blocking" not in merge, f"the {mode} merge schema carries a question field"
    assert "human_input" not in common.comparison_task("### Agent1\n\ndo it", "g"), "the comparison offers a question field"
    direction = common.direction_task("### Agent1\n\n- Agent1, backing `the original plan`:\ndo it", "g")
    assert "blocking" not in direction, "the chair was handed a stop button"
    fold = common.ask_merge_task("### H1 (on F1)\nwhich name wins?\nasked by: Agent1")
    assert "human_input" not in fold and "blocking" not in fold, "the ask fold was handed a question to raise"


def test_the_ask_fold_brief_shows_the_chair_asks_and_their_ids():
    """Ask ids are `H1` in both modes, so the fold's schema must not take the mode's item prefix."""
    brief = common.ask_merge_task("### H1 (on F1)\nwhich tenant owns the key?\nasked by: Agent1")
    assert '"canonical": "H1"' in brief and "which tenant owns the key?" in brief
    assert common.schema_hint("review", phase="asks") == common.schema_hint("plan", phase="asks")


def test_every_seat_phase_repairs_with_the_verdict_shape():
    """A repair re-ask quotes the shape of the pass it repairs: the gap, defense, explain and seconding passes had the chair's."""
    verdict = common.schema_hint("review", phase="round")
    for phase in common.SEAT_PHASES:
        assert common.schema_hint("review", phase=phase) == verdict
    for phase in ("merge", "asks", "compare", "direction"):
        assert common.schema_hint("review", phase=phase) != verdict, "a chair publication is not a seat's verdict"


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
            return payload(positions=[{"item": "F1", "stance": "needs_human", "note": decision}], blocking_for=["H1"], signoff=False)
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, max_rounds=3)
        await built.run()
        return built, port, records

    built, port, records = asyncio.run(main())
    assert built.docket.question_order == ["H1"], "the phase-0 request and the escalation are one question"
    question = built.docket.questions["H1"]
    assert question.item_id == "F1", "the entry that named the item gave the question its item"
    assert question.askers == ["Agent1", "Agent2"], "the second escalation is that ask's second seat"
    assert effective_class(built.docket, question) == "blocking", "both holders declared the stop, which is the bar of 3"
    opened = next(r for r in records if r["event"] == "human_checkpoint" and r["round"] == 0)
    assert opened["ask_set"] == [] and not opened["waited"], "one seat asking over no item puts nothing in front of the human"
    waited = {c["round"]: c["waiting"] for c in port.checkpoints}
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
        built, _port, _records = engine(reply=reply, answers={"H1": "one tenant forever"}, selections={"H1": 2}, seconding=False)
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
        built, _port, records = engine(reply=reply, answers={"H1": "the only way"}, selections={"H1": 9}, seconding=False)
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
        built, _port, records = engine(reply=reply, answers={"H1": "the only way"}, selections={"H1": 2}, seconding=False)
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
        if ask.round == 2:
            # the human's answer settles F1; the same defect shows up one layer further out
            return payload(
                new_items=[{"claim": "the retry loop ignores the tenant too"}] if ask.who == "Agent1" else [],
                positions=[{"item": "F1", "stance": "agree", "note": "the answer settles it"}],
                signoff=True,
            )
        return payload(
            positions=[{"item": "F1", "stance": "agree"}, {"item": "F2", "stance": "needs_human", "note": decision}],
            human_input=[{"question": decision, "item": "F2", "blocking": True}],
            signoff=True,
        )

    async def main():
        built, port, _records = engine(reply=reply, max_rounds=4, answers={"H1": "put the tenant id in the key"})
        outcome = await built.run()
        return outcome, built, port

    outcome, built, port = asyncio.run(main())
    assert outcome == "needs_human", "two seats escalate F2 and want the run to wait on it"
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
            # two seats ask the same thing, and that is what earns it a place at the checkpoint
            return payload(
                findings=[{"claim": "a bug"}] if ask.who == "Agent1" else [],
                open_questions=["what is the retention policy?"],
            )
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


def test_a_second_seat_is_what_puts_an_ask_in_front_of_the_human():
    """`second` is the gate: one seat asks, another backs it, and the ask reaches the checkpoint."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[])
            return payload(findings=[{"claim": "a bug"}], open_questions=["what is the retention policy?"])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent2":
            return stance_all(ask, "agree", second=["H1"])
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, answers={"H1": "30 days"})
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    assert outcome == "consensus"
    assert [r for r in records if r["event"] == "question_seconded"][0]["seat"] == "Agent2"
    assert any(c["waiting"] and c["ask_set"] == ["H1"] for c in port.checkpoints), "the backed ask is on the ask set"
    assert built.docket.questions["H1"].answer == "30 days"


def test_an_unbacked_ask_is_weighed_by_the_council_and_never_asked():
    """Nobody backs it: the seats get one last look, the human gets the report, and the ask stays unanswered."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[])
            return payload(findings=[{"claim": "a bug"}], open_questions=["what is the retention policy?"])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, answers={"H1": "30 days"})
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    assert outcome == "consensus", "an ask nobody backs holds nothing"
    assert port.count(phase="seconding") == 3, "every seat is asked once more before the report"
    assert [r for r in records if r["event"] == "peer_pass_start"][0]["asks"] == ["H1"]
    assert all("H1" not in c["ask_set"] for c in port.checkpoints), "an unbacked ask is never put to the human"
    question = built.docket.questions["H1"]
    assert question.answer == "" and "unseconded" in question.markers
    note = next(r for r in records if r["event"] == "console_note")["text"]
    assert "no second seat" in note and "answered" not in note, "nothing was settled among itself: say why it went unasked"


def test_a_seat_that_answers_a_peer_ask_keeps_the_human_out_of_it():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(
                findings=[{"claim": "a bug"}] if ask.who == "Agent1" else [],
                open_questions=["which tenant owns the key?"] if ask.who == "Agent1" else [],
            )
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent2":
            return stance_all(ask, "agree", answer_question=[{"id": "H1", "answer": "config names it: tenant_id"}])
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, answers={"H1": "the tenant is fixed by contract"})
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    assert outcome == "consensus"
    assert [r for r in records if r["event"] == "question_answered_by_seat"][0]["seat"] == "Agent2"
    assert built.docket.questions["H1"].peer_answers == {"Agent2": "config names it: tenant_id"}
    assert not any(c["waiting"] and "H1" in c["ask_set"] for c in port.checkpoints), "a seat answered it: nobody waits on the human"
    assert built.docket.questions["H1"].answer == ""
    assert "a seat answered this from the workspace" in next(r for r in records if r["event"] == "console_note")["text"]


def test_the_seat_that_raised_an_ask_can_call_a_peer_s_answer_not_enough_on_the_last_pass():
    """The pass that decides what the human is asked cannot be closed to the seat whose own ask was answered away."""
    import asyncio

    decision = "which tenant owns the key?"

    def reply(ask):
        if ask.phase == "phase0":
            return payload(
                findings=[{"claim": "a bug"}] if ask.who == "Agent1" else [],
                open_questions=[decision] if ask.who == "Agent1" else [],
            )
        if ask.kind in {"merge", "asks"}:
            return {"groups": []}
        if ask.phase == "seconding":
            if ask.who == "Agent1":
                # a sentence here asks nothing: that pass reads ids, and raises nothing
                return payload(insist=["H1"], human_input=[{"question": "and which key store?"}])
            return payload()
        if ask.who == "Agent2":
            return stance_all(ask, "agree", answer_question=[{"id": "H1", "answer": "config names it: tenant_id"}])
        if ask.who == "Agent3":
            return stance_all(ask, "agree", second=["H1"])
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, answers={"H1": "the tenant is fixed by contract"})
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    question = built.docket.questions["H1"]
    assert [r["event"] for r in records if r["event"].startswith("question_seconded")] == ["question_seconded_but_held_back"]
    assert [r for r in records if r["event"] == "question_insisted"][0]["seat"] == "Agent1"
    assert list(built.docket.questions) == ["H1"], "the last pass raises no ask, whatever else a seat sends"
    assert any(c["ask_set"] == ["H1"] and c["waiting"] for c in port.checkpoints), "the raiser's word reopens the gate"
    assert (question.state, question.insisted) == ("answered", True) and outcome == "consensus"


def test_the_asker_calling_a_peer_s_answer_not_enough_puts_the_ask_back_on_the_table():
    """A peer's answer holds a seconded ask back until the asker says it was not enough — then it is the human's to settle."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[])
            return payload(findings=[{"claim": "a bug"}], open_questions=["which tenant owns the key?"])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.round == 1:
            if ask.who == "Agent2":
                # the item stays open, so the council gets a round to settle the ask among itself
                return payload(
                    positions=[{"item": "F1", "stance": "disagree", "evidence": "app.py:9"}],
                    signoff=False,
                    second=["H1"],
                )
            if ask.who == "Agent3":
                return stance_all(ask, "agree", answer_question=[{"id": "H1", "answer": "config names it"}])
            return stance_all(ask, "agree")
        if ask.who == "Agent1":
            return stance_all(ask, "agree", insist=["H1"])
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply, answers={"H1": "the tenant is fixed by contract"})
        return await built.run(), built, port

    outcome, built, port = asyncio.run(main())
    question = built.docket.questions["H1"]
    assert question.peer_answers and question.insisted, "the re-ask says config names it was not an answer"
    assert question.state == "answered" and outcome == "consensus"
    assert any(c["ask_set"] == ["H1"] and c["waiting"] for c in port.checkpoints), "a second seat plus insistence opens the gate"


def test_a_seat_that_works_it_out_stances_the_item_and_frees_it():
    """The pass reads a stance for this alone: the asker worked it out, so the hold goes and the item closes."""
    import asyncio

    from mech_council.deliberation.dispositions import derive
    from mech_council.deliberation.state import AGREED

    def reply(ask):
        if ask.phase == "phase0":
            return payload(findings=[{"claim": "the cache ignores tenants"}] if ask.who == "Agent1" else [])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "seconding":
            return payload(positions=[{"item": "F1", "stance": "agree", "note": "the config names the tenant"}], retract=["H1"])
        if ask.who == "Agent1":
            return payload(
                positions=[{"item": "F1", "stance": "needs_human", "note": "which tenant?"}],
                human_input=[{"question": "which tenant?", "item": "F1"}],
                signoff=True,
            )
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply)
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    item, question = built.docket.items["F1"], built.docket.questions["H1"]
    assert outcome == "consensus"
    assert (question.state, question.retracted_by) == ("withdrawn", "Agent1")
    assert [r["event"] for r in records if r["event"] == "hold_released"] == ["hold_released"]
    assert not built.docket.is_standing("F1", "Agent1"), "the stance let the hold go, never the dropped ask"
    assert any(r["event"] == "escalation_withdrawn" and r["cause"] == "sent agree" for r in records)
    assert derive(built.docket, "F1") == AGREED and (item.closed, item.disposition, item.closed_round) == (True, AGREED, 1)
    assert all("H1" not in c["ask_set"] for c in port.checkpoints)


def test_dropping_an_ask_leaves_the_hold_and_the_item_exactly_where_they_were():
    """An ask is a proposal and a hold is a stance: dropping the one cannot move the other."""
    import asyncio

    from mech_council.deliberation.dispositions import derive
    from mech_council.deliberation.state import HUMAN

    def reply(ask):
        if ask.phase == "phase0":
            return payload(findings=[{"claim": "the cache ignores tenants"}] if ask.who == "Agent1" else [])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "seconding":
            return payload(retract=["H1"])
        if ask.who == "Agent1":
            return payload(
                positions=[{"item": "F1", "stance": "needs_human", "note": "which tenant?"}],
                human_input=[{"question": "which tenant?", "item": "F1"}],
                signoff=True,
            )
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply)
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    question = built.docket.questions["H1"]
    assert outcome == "consensus", "one seat holding an item below the stop bar ends nothing"
    assert (question.state, question.retracted_by) == ("withdrawn", "Agent1")
    assert built.docket.is_standing("F1", "Agent1") and derive(built.docket, "F1") == HUMAN
    assert not built.docket.items["F1"].closed, "the item waits on the seat's own stance, not on its ask"
    assert not [r for r in records if r["event"] in ("escalation_withdrawn", "hold_released", "item_closed")]


def test_the_ask_a_hold_raised_goes_when_the_seat_lets_go_of_the_item():
    """The ask was the sound of a seat being stuck: once it works the item out, the human is not asked about it."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the cache ignores tenants")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.round == 1 and ask.who in ("Agent1", "Agent2"):
            return payload(
                positions=[{"item": "F1", "stance": "needs_human", "note": "which tenant owns the key?"}],
                human_input=[{"question": "which tenant owns the key?", "item": "F1", "blocking": True}],
                backing="no_preference",
                signoff=False,
            )
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, max_rounds=2)
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    question = built.docket.questions["H1"]
    assert any(c["waiting"] and c["ask_set"] == ["H1"] for c in port.checkpoints), "while both seats hold F1 the human is asked"
    assert outcome == "consensus" and built.docket.items["F1"].closed, "round 2: both worked it out from the workspace"
    assert (question.state, question.askers) == ("withdrawn", []), "each release took its ask with it"
    assert not any(c["round"] == 2 and "H1" in c["ask_set"] for c in port.checkpoints), "the settled item is not put to the human"
    assert [r["cause"] for r in records if r["event"] == "question_retracted"] == ["released the hold", "released the hold"]


def test_the_chair_fold_makes_two_wordings_one_blocking_ask():
    """The second seat the gate asks for is often there all along, in another wording."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the cache ignores tenants")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.kind == "asks":
            return {"groups": [{"canonical": "H1", "duplicates": ["H2"]}]}
        if ask.who in ("Agent1", "Agent2"):
            wording = "which tenant owns the key?" if ask.who == "Agent1" else "who is accountable for tenancy?"
            return payload(
                positions=[{"item": "F1", "stance": "agree"}],
                human_input=[{"question": wording, "item": "F1", "blocking": True}],
                backing="no_preference",
                signoff=False,
            )
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(max_rounds=2, reply=reply, answers={"H1": "the billing service owns it"})
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    first, second = built.docket.questions["H1"], built.docket.questions["H2"]
    assert (first.askers, first.blocked_by) == (["Agent1", "Agent2"], ["Agent1", "Agent2"]), "the fold carries the stops too"
    assert second.merged_into == "H1" and first.also_asked == {"Agent2": "who is accountable for tenancy?"}, "no wording is lost"
    assert any(c["waiting"] and c["ask_set"] == ["H1"] for c in port.checkpoints), "asked once, not twice"
    assert first.answer == "the billing service owns it", "the human's answer lands on the ask both seats wanted"
    fold = next(r for r in records if r["event"] == "question_merged")
    assert fold["into"] == "H1" and fold["askers"] == ["Agent1", "Agent2"]
    moved = [r for r in records if r["event"] == "question_effective_class" and r["id"] == "H1"][-1]
    assert (moved["prior"], moved["effective"]) == ("deferred", "blocking"), "one ask, a majority wanting the stop"


def test_two_seats_holding_one_item_ask_the_human_without_the_chair_noticing():
    """Two `needs_human` holds on one item are one ask: no chair, no second seat typed by anyone."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the cache ignores tenants")
        if ask.kind in {"merge", "asks"}:
            return {"groups": []}
        if ask.round == 1 and ask.who in ("Agent1", "Agent2"):
            wording = "which tenant owns the key?" if ask.who == "Agent1" else "who is accountable for tenancy?"
            return payload(
                positions=[{"item": "F1", "stance": "needs_human", "note": wording}],
                human_input=[{"question": wording, "item": "F1", "blocking": True}],
                backing="no_preference",
                signoff=False,
            )
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(max_rounds=2, reply=reply, answers={"H1": "the billing service owns it"})
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    asked = list(built.docket.questions.values())
    assert [q.id for q in asked] == ["H1"], "the second hold joined the first ask instead of raising a second one"
    first = asked[0]
    assert (first.askers, first.raised_by, first.blocked_by) == (
        ["Agent1", "Agent2"],
        ["Agent1", "Agent2"],
        ["Agent1", "Agent2"],
    ), "two wordings, one ask, both stops"
    assert [(built.docket.escalations[("F1", seat)].question_id) for seat in ("Agent1", "Agent2")] == ["H1", "H1"]
    assert not [a for a in port.asks if a.kind == "asks"], "one ask: nothing for the chair to fold"
    assert any(c["waiting"] and c["ask_set"] == ["H1"] for c in port.checkpoints), "what two seats hold goes to the human"
    assert (first.answer, outcome) == ("the billing service owns it", "consensus")


def test_the_chair_is_not_asked_to_fold_asks_when_only_one_seat_has_asks():
    """A chair pass costs a lane's whole turn: it runs when there is something to fold."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the cache ignores tenants", questions=["which tenant owns the key?"])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(max_rounds=2, reply=reply)
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    assert outcome == "consensus"
    assert [q.id for q in built.docket.questions.values()] == ["H1"] and built.docket.questions["H1"].askers == ["Agent1"]
    assert not [a for a in port.asks if a.kind == "asks"], "one ask from one seat: nothing to fold"


def test_the_chair_reads_the_asks_once_and_only_the_round_after_it_fails_to_read_them():
    """A fold is a chair's whole turn: an ask set it has read is read again when it moves, or when the read failed."""
    import asyncio

    reads = {"n": 0}

    def reply(ask):
        if ask.phase == "phase0":
            words = {"Agent1": "which tenant owns the key?", "Agent2": "which palette wins?"}
            return payload(
                findings=[{"claim": "a bug"}] if ask.who == "Agent1" else [],
                open_questions=[words[ask.who]] if ask.who in words else [],
            )
        if ask.kind == "merge":
            return {"groups": []}
        if ask.kind == "asks":
            reads["n"] += 1
            if reads["n"] == 1:
                return {"groups": [{"canonical": "H1", "duplicates": ["H2"]}, {"canonical": "H2", "duplicates": ["H1"]}]}
            return {"groups": []}
        if ask.who == "Agent3":
            # one disputed item keeps the run to the round limit
            return stance_all(ask, "disagree", signoff=False)
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(max_rounds=4, grace_rounds=0, reply=reply)
        return await built.run(), built, port, records

    _outcome, _built, port, records = asyncio.run(main())
    assert [a.round for a in port.asks if a.kind == "asks"] == [0, 1], "one read that failed, one that landed, and no third"
    assert len([r for r in records if r["event"] == "chair_ask_merge_refused"]) == 1


def test_a_question_shown_at_the_grace_checkpoint_is_unanswered_never_unasked():
    """The grace round asks: passing it over is not the same silence as never being asked."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(
                findings=[{"claim": "a bug"}] if ask.who == "Agent1" else [],
                open_questions=["what is the retention policy?"] if ask.who == "Agent1" else [],
            )
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent2":
            # an open dispute keeps `evaluate` at GO, so the round limit is what ends the run
            return stance_all(ask, "disagree")
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, max_rounds=1, seconding=False)
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    assert outcome == "round_limit_reached"
    assert any(c["waiting"] and c["ask_set"] == ["H1"] for c in port.checkpoints), "the grace round put it in front of the human"
    question = built.docket.questions["H1"]
    assert "unanswered" in question.markers and "unasked" not in question.markers, "the ledger says you were asked"
    assert next(r for r in records if r["event"] == "termination")["unasked"] == [], "nothing was swallowed by the round limit"


def test_the_pass_before_the_report_opens_no_hold_and_moves_none():
    """One verb there, said out loud when a seat reaches past it."""
    import asyncio

    from mech_council.deliberation.state import AGREE, NEEDS_HUMAN

    def reply(ask):
        if ask.phase == "phase0":
            return payload(findings=[{"claim": "the cache ignores tenants"}] if ask.who == "Agent1" else [])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "seconding":
            if ask.who == "Agent1":
                return payload(positions=[{"item": "F1", "stance": "needs_human", "note": "I still hold it"}])
            if ask.who == "Agent2":
                return payload(positions=[{"item": "F1", "stance": "disagree", "evidence": "app.py:9"}])
            return payload(positions=[{"item": "F1", "stance": "agree"}])
        if ask.who == "Agent1":
            return payload(
                positions=[{"item": "F1", "stance": "needs_human", "note": "which tenant?"}],
                human_input=[{"question": "which tenant?", "item": "F1"}],
                signoff=True,
            )
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply)
        return await built.run(), built, records

    outcome, built, records = asyncio.run(main())
    assert outcome == "consensus"
    assert built.docket.stance("F1", "Agent1").stance == NEEDS_HUMAN, "a pass restance kept nothing and recorded nothing"
    assert built.docket.stance("F1", "Agent2").stance == AGREE, "an objection there neither landed nor replaced its agree"
    assert built.docket.is_standing("F1", "Agent1")
    assert not built.docket.items["F1"].closed
    assert not [r for r in records if r["event"] == "hold_released"]
    refused = [r for r in records if r["event"] == "verdict_ask_refused" and r["field"] == "positions"]
    assert sorted(r["member"] for r in refused) == ["Agent1", "Agent2"], "the two stances with consequence were refused out loud"


def test_a_needs_human_at_the_bar_holds_the_run_until_the_human_answers():
    import asyncio

    def needs_human(ask, note="needs the product owner"):
        return payload(
            positions=[{"item": item, "stance": "needs_human", "note": note} for item in shown_items(ask.text)],
            human_input=[{"question": note, "item": item, "blocking": True} for item in shown_items(ask.text)],
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
    assert any(c["waiting"] for c in port.checkpoints), "a declared stop at the bar makes the checkpoint wait"


def test_one_seat_cannot_stop_the_run_by_wanting_to():
    """The bar counts declared stops, so one seat's insistence ends nothing: the council finishes and reports the ask."""
    import asyncio

    from mech_council.deliberation.dispositions import derive
    from mech_council.deliberation.state import HUMAN

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1":
            return payload(
                positions=[{"item": item, "stance": "needs_human", "note": "which tenant owns it?"} for item in shown_items(ask.text)],
                human_input=[{"question": "which tenant owns it?", "item": item, "blocking": True} for item in shown_items(ask.text)],
                signoff=True,
            )
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply, answers={"H1": "the contract names it"})
        return await built.run(), built, port

    outcome, built, port = asyncio.run(main())
    assert outcome == "consensus", "one declared stop is below the bar of two"
    assert not any(c["waiting"] for c in port.checkpoints)
    assert derive(built.docket, "F1") == HUMAN, "the stance still holds the item, which is the other half of the story"
    question = built.docket.questions["H1"]
    assert question.answer == "" and question.blocked_by == ["Agent1"] and "unseconded" in question.markers


def test_an_ask_a_peer_answered_cannot_hold_the_run_even_at_the_bar():
    """Blocking means the human is actually asked. A settled ask defers, however many seats wanted the stop."""
    import asyncio

    from mech_council.deliberation.escalations import SETTLED, held_back

    decision = "which tenant owns it?"

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "seconding":
            return payload()
        if ask.who == "Agent1":
            return payload(
                positions=[{"item": item, "stance": "needs_human", "note": decision} for item in shown_items(ask.text)],
                human_input=[{"question": decision, "item": item, "blocking": True} for item in shown_items(ask.text)],
                signoff=True,
            )
        if ask.who == "Agent2":
            return stance_all(ask, "agree", second=["H1"], blocking_for=["H1"])
        return stance_all(ask, "agree", answer_question=[{"id": "H1", "answer": "config names tenant_id"}])

    async def main():
        built, port, records = engine(reply=reply, answers={"H1": "the contract names it"})
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    question = built.docket.questions["H1"]
    assert outcome == "consensus"
    assert question.blocked_by == ["Agent1", "Agent2"], "at the bar, and still not blocking"
    assert held_back(built.docket, question) == SETTLED, "a seat that does not ask it answered it"
    assert not any(c["waiting"] and "H1" in c["ask_set"] for c in port.checkpoints), "nothing waits on a question nobody is shown"
    assert question.answer == "", "the human was never asked, so the human has not spoken"
    flips = [(r["prior"], r["effective"]) for r in records if r["event"] == "question_effective_class"]
    assert flips == [("deferred", "blocking"), ("blocking", "deferred")], "the answer that released the run is on the feed too"


def test_a_council_that_settles_on_the_last_pass_does_not_end_needs_human():
    """The ending is read after the pass that lets seats go, not before it."""
    import asyncio

    HELD = "who owns the runbook?"
    PALETTE = "which palette wins?"
    DECISION = "which tenant owns the key?"

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the cache ignores tenants")
        if ask.kind in {"merge", "asks"}:
            return {"groups": []}
        if ask.phase == "seconding":
            if ask.who == "Agent3":
                return payload()
            return payload(positions=[{"item": "F1", "stance": "agree", "note": "the config names the tenant"}])
        if ask.who == "Agent3":
            return payload(positions=[{"item": "F1", "stance": "agree"}], human_input=[{"question": HELD}], signoff=True)
        return payload(
            positions=[{"item": "F1", "stance": "needs_human", "note": DECISION}],
            human_input=[{"question": DECISION, "item": "F1", "blocking": True}, {"question": PALETTE}],
            signoff=True,
        )

    async def main():
        built, _port, records = engine(reply=reply)
        return await built.run(), built, records

    outcome, built, records = asyncio.run(main())
    assert [r["asks"] for r in records if r["event"] == "peer_pass_start"] == [["H3"]], "one unbacked ask opens the pass"
    assert built.docket.items["F1"].closed and built.docket.questions["H1"].state == WITHDRAWN
    assert outcome == "consensus", "both holders let go on that pass, so nothing is left to need a human"


def test_a_second_on_an_ask_a_peer_already_answered_buys_nothing_and_says_so_once():
    """The answer came first, so the second settled nothing: the seat gets the one line that says what the second did not buy."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[])
            return payload(findings=[{"claim": "the cache ignores tenants"}], open_questions=["which tenant owns the key?"])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "seconding":
            return payload()
        if ask.round == 1 and ask.who == "Agent3":
            return stance_all(ask, "agree", signoff=False, answer_question=[{"id": "H1", "answer": "the config names tenant_id"}])
        if ask.round == 1 and ask.who == "Agent2":
            return stance_all(ask, "disagree", signoff=False)
        if ask.who == "Agent2":
            return stance_all(ask, "agree", second=["H1"])
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply)
        return await built.run(), records

    outcome, records = asyncio.run(main())
    assert outcome == "consensus"
    backed = [r for r in records if r["event"].startswith("question_seconded")]
    assert [r["event"] for r in backed] == ["question_seconded_but_held_back"], "no line claims the human was asked"
    assert (backed[0]["held"], backed[0]["answered_by"]) == ("settled", ["Agent3"])


def test_the_last_pass_hears_every_answer_before_it_hears_the_insistence():
    """Otherwise the seat that happens to be asked first cannot call an answer not enough: it has not landed yet."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the login form re-hashes on every keystroke", questions=["which palette does the shell take?"])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "seconding":
            if ask.who == "Agent1":
                return payload(insist=["H1"])
            return payload(answer_question=[{"id": "H1", "answer": "tokens.json names brand-primary"}])
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(2, reply=reply)
        return await built.run(), built, records

    outcome, built, records = asyncio.run(main())
    assert outcome == "consensus"
    refused = [r for r in records if r["event"] == "verdict_ask_refused" and r["field"] == "insist"]
    assert not refused, f"the raiser speaks first on that pass: {refused}"
    assert built.docket.questions["H1"].insisted, "the answer it rebuts landed on the same pass"
    assert [r["id"] for r in records if r["event"] == "question_insisted"] == ["H1"]


def test_an_ask_about_an_item_no_seat_escalated_leaves_the_item_to_the_seats():
    """Asking about an item is not refusing it: the council closes the item, and the ask goes moot with it."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the login form re-hashes on every keystroke")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "seconding":
            return payload()
        if ask.who == "Agent1":
            return stance_all(ask, "agree", human_input=[{"question": "which brand palette?", "item": "F1"}])
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply, answers={"H1": "the tokens file names it"})
        return await built.run(), built, port

    outcome, built, port = asyncio.run(main())
    assert outcome == "consensus"
    assert built.docket.items["F1"].closed and built.docket.items["F1"].disposition == "agreed"
    assert not built.docket.escalations, "an ask raises no escalation: only the stance does"
    mooted = built.docket.questions["H1"]
    assert mooted.state == "withdrawn" and mooted.moot_with == "F1" and mooted.answer == ""
    assert not port.checkpoints, "a settled item's deferred ask never reached the human"


def test_an_ask_the_seats_settled_on_cannot_be_raised_against_the_item_that_settled_it():
    """The closure is the answer about that item: what a seat still wants is a question of its own."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who == "Agent1":
                return payload(findings=[{"claim": "the login form re-hashes on every keystroke", "where": "src/a.py:1"}])
            if ask.who == "Agent2":
                return payload(findings=[{"claim": "the export loops over every row", "where": "src/b.py:2"}])
            return payload(findings=[])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "seconding":
            return payload()
        if ask.round == 1:
            if ask.who == "Agent3":
                return payload(
                    positions=[{"item": "F1", "stance": "agree"}, {"item": "F2", "stance": "needs_human"}],
                    backing="no_preference",
                    signoff=False,
                )
            return stance_all(ask, "agree", signoff=False)
        return payload(
            positions=[{"item": "F2", "stance": "needs_human"}],
            human_input=[{"question": "and does the retry budget change too?", "item": "F1"}],
            backing="no_preference",
            signoff=False,
        )

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=2, grace_rounds=0)
        return await built.run(), built, records

    outcome, built, records = asyncio.run(main())
    assert built.docket.items["F1"].closed and not built.docket.items["F2"].closed
    refused = [r for r in records if r["event"] == "verdict_ask_refused" and r["field"] == "human_input"]
    assert refused and refused[0]["reason"].startswith("F1 is settled as agreed")
    late = next(q for q in built.docket.questions.values() if q.text.startswith("and does the retry"))
    assert late.item_id is None, "the ask survived as a question of its own, not about a settled item"
    assert "moot" not in late.markers


def test_a_declared_stop_outranks_the_closure_that_would_moot_its_ask():
    """A majority saying the run cannot go on without you outranks the seats settling the item."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the login form re-hashes on every keystroke")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "seconding":
            return payload()
        wording = "who owns the tenant key once the form is fixed?"
        if ask.who == "Agent1":
            return stance_all(ask, "agree", human_input=[{"question": wording, "item": "F1", "blocking": True}], blocking_for=["H1"])
        if ask.who == "Agent2":
            return stance_all(ask, "agree", human_input=[{"question": wording, "item": "F1"}], blocking_for=["H1"])
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply, answers={"H1": "platform keys, per the vault"})
        return await built.run(), built, port

    outcome, built, port = asyncio.run(main())
    assert built.docket.items["F1"].closed, "the seats did settle the item"
    assert any(c["waiting"] and c["ask_set"] == ["H1"] for c in port.checkpoints), "the stop held the run anyway"
    assert outcome == "consensus"


def test_a_moot_ask_carries_the_item_that_answered_it():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the login form re-hashes on every keystroke")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "seconding":
            return payload()
        if ask.who == "Agent1":
            return stance_all(ask, "agree", human_input=[{"question": "which brand palette?", "item": "F1"}])
        if ask.who == "Agent2":
            return stance_all(ask, "agree", second=["H1"])
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply)
        return await built.run(), built, records

    outcome, built, records = asyncio.run(main())
    assert outcome == "consensus", "a backed ask the closure answered is not a question left waiting"
    mooted = [r for r in records if r["event"] == "question_mooted"]
    assert [(r["id"], r["item"], r["disposition"]) for r in mooted] == [("H1", "F1", "agreed")]
    assert built.docket.questions["H1"].markers == ["moot"]


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
            return payload(positions=[], signoff=False)  # a refusal of the ask
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


def gap_items(text: str) -> list[str]:
    """The ids this gap ask names: a scripted seat answers what it was actually asked for."""
    listed = text.split("returned nothing for them: ", 1)[1].split(".", 1)[0]
    return re.findall(r"\b[A-Z]\d+\b", listed)


def test_an_unusable_gap_answer_costs_the_whole_ladder():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug", "another")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who in ("Agent1", "Agent3"):
            return stance_all(ask, "agree")
        if ask.phase == "gap":
            return RawAsk(text="still not JSON", rejected=True)
        return payload(positions=[{"item": shown_items(ask.text)[0], "stance": "agree"}], signoff=True)  # one of two: incomplete

    async def main():
        built, port, records = engine(reply=reply, max_rounds=1)
        await built.run()
        return port, records

    port, records = asyncio.run(main())
    assert port.count(who="Agent2", phase="gap") == 4, "an answer that never parses is retried to the ceiling"
    declined = next(r for r in records if r["event"] == "gap_declined" and r["member"] == "Agent2")
    assert declined["items"] == ["F2"], "what it still owed is declined"


def test_a_gap_longer_than_one_chunk_is_asked_to_the_end():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, *[f"bug {n}" for n in range(14)])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who in ("Agent1", "Agent3"):
            return stance_all(ask, "agree")
        items = gap_items(ask.text) if ask.phase == "gap" else shown_items(ask.text)[:1]
        return payload(positions=[{"item": item, "stance": "agree"} for item in items], signoff=True)

    async def main():
        built, port, records = engine(reply=reply, max_rounds=1)
        await built.run()
        return port, records

    port, records = asyncio.run(main())
    assert port.count(who="Agent2", phase="gap") == 2, "the 13 items it owed take two asks, not one and a decline"
    assert not [r for r in records if r["event"] == "gap_declined" and r["member"] == "Agent2"], "nothing was dropped for want of an ask"


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


def test_a_defense_that_lets_go_of_the_item_does_not_adopt_the_ask_it_raises():
    """A withdrawn hold must not own the ask its own release left standing: that reports a free question as escalated."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "defense":
            return payload(
                positions=[{"item": "F1", "stance": "agree", "note": "the config settles it"}],
                human_input=[{"question": "does the vault rotate that key?", "item": "F1"}],
            )
        if ask.who == "Agent1":
            return payload(positions=[{"item": "F1", "stance": "needs_human", "note": "which tenant?"}], signoff=True)
        if ask.who == "Agent2":
            return payload(positions=[{"item": "F1", "stance": "disagree", "evidence": "docs/policy.md:4"}], signoff=True)
        return stance_all(ask, "agree")

    async def main():
        built, _port, _records = engine(reply=reply, max_rounds=1)
        await built.run()
        return built

    built = asyncio.run(main())
    late = next(q for q in built.docket.questions.values() if q.text.startswith("does the vault"))
    assert late.state == "asked", "the ask the defense raised over the item it released still stands"
    assert all(esc.question_id != late.id for esc in built.docket.escalations.values()), "and no hold, standing or withdrawn, owns it"


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


def test_an_answer_at_the_ceiling_is_asked_on_the_grace_budget():
    """`round_limit_reached` used to be the one way a question ended unasked; the grace round is what makes an answer useful."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(findings=[{"claim": "a bug"}] if ask.who == "Agent1" else [], open_questions=["what is the retention policy?"])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, max_rounds=1, answers={"H1": "30 days"})
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    assert outcome == "consensus"
    assert any(c["waiting"] and c["ask_set"] == ["H1"] for c in port.checkpoints), "the round limit asks rather than silencing"
    assert next(r for r in records if r["event"] == "grace_round_bought")["grace_left"] == 0
    assert [r["round"] for r in records if r["event"] == "round_summary"] == [1, 2], "round 2 is the one the answer paid for"
    assert built.docket.questions["H1"].answer == "30 days"


def test_grace_rounds_zero_ends_the_run_on_the_limit_with_the_question_unasked():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(findings=[{"claim": "a bug"}] if ask.who == "Agent1" else [], open_questions=["what is the retention policy?"])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, max_rounds=1, grace_rounds=0, answers={"H1": "30 days"})
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    assert outcome == "consensus"
    assert not any(c["waiting"] for c in port.checkpoints), "no budget, no wait"
    assert [r["round"] for r in records if r["event"] == "round_summary"] == [1]
    question = built.docket.questions["H1"]
    assert question.answer == "" and "unasked" in question.markers, "the ledger says nobody was asked"


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


def test_a_declared_stop_moves_the_effective_class_and_never_needs_a_reask():
    """The class flips on another seat's verdict, not on a fresh restatement of the question."""
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
            blocking_for=["H1"] if escalating and ask.round > 1 else [],
            backing="no_preference",
            signoff=True,
        )

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=4)
        return await built.run(), built, records

    _outcome, built, records = asyncio.run(main())
    assert built.docket.question_order == ["H1"], "one question, held by two seats"
    assert built.docket.questions["H1"].blocked_by == ["Agent2", "Agent3"]
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
