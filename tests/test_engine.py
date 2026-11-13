"""The engine over a scripted port."""

from __future__ import annotations

import re

import pytest

from mech_council.deliberation.engine import Abort, Engine
from mech_council.deliberation.state import ABSTAIN, AGREE, AGREED, DECLINED, DEMOTED, RETRACTED, STANDING, WITHDRAWN, Docket, Item, Stance
from mech_council.prompts import common
from mech_council.prompts import plan as plan_text
from mech_council.prompts.brief import Brief

from .support.harness import RawAsk, ScriptPort, events_collector, payload

NAMES = {"Agent1": "Metic", "Agent2": "Senior", "Agent3": "Coworker"}


def engine(
    n=3,
    *,
    mode="review",
    max_rounds=4,
    grace_rounds=1,
    seconding=True,
    human="blocking",
    reply=None,
    answers=None,
    skip=None,
    selections=None,
    off_cycle=None,
):
    docket = Docket(mode=mode, goal="g", roster=[f"Agent{i}" for i in range(1, n + 1)], seconding=seconding, human=human)
    port = ScriptPort(docket, reply, answers=answers or {}, skip=skip or [], selections=selections or {}, off_cycle=off_cycle or {})
    records, emit = events_collector()
    built = Engine(
        docket,
        port,
        goal="g",
        project="/project",
        brief=Brief(mode=mode, project="/project", human=human),
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


def needs_human(ask, note="needs the product owner"):
    return payload(
        positions=[{"item": item, "stance": "needs_human", "note": note} for item in shown_items(ask.text)],
        human_input=[{"question": note, "item": item, "blocking": True} for item in shown_items(ask.text)],
        backing="no_preference",
        signoff=True,
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
    assert plan_text.extra_briefing("blocking").strip() in briefs["plan"], "plan declares prose and never briefs it"
    assert plan_text.extra_briefing("blocking").strip() not in briefs["review"], "a review seat reads planning prose"


def test_the_merge_schema_stamps_the_ids_the_mode_s_docket_hands_out():
    assert '"canonical": "F1"' in common.merge_task("review", "- F1: the claim", "g")
    assert '"canonical": "T1"' in common.merge_task("plan", "- T1: the claim", "g")


def test_every_schema_the_seats_are_shown_is_json_they_can_echo():
    """Shape hints are spliced on the human's posture, so every fenced block must parse as the seats will echo it."""
    import json

    for mode in ("review", "plan"):
        for human in ("blocking", "non_blocking", "none"):
            brief = Brief(mode=mode, project="/project", human=human)
            texts = [
                common.schema_hint(mode, phase=phase, human=human)
                for phase in ("phase0", "round", "merge", "asks", "defense", "direction", "watchdog")
            ]
            texts += [
                brief.phase0(mode=mode, goal="g", project="/project"),
                brief.round(mode=mode, goal="g", project="/project", me="Agent1", docket="", phase0_summary=""),
                brief.peer_pass("### H1 (on F1)\nwhich tenant wins?\nasked by: Agent1"),
                common.repair_ask(mode, "round", "bad", "{}", human),
            ]
            for text in texts:
                blocks = re.findall(r"```json\n(.*?)```", text, re.S)
                assert blocks, f"{mode} {human}: a brief with no fenced example"
                for block in blocks:
                    try:
                        json.loads(block)
                    except json.JSONDecodeError as error:
                        pytest.fail(f"{mode} {human}: the seats are shown an unparseable schema: {error}\n{block}")


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


def test_the_note_fold_brief_shows_the_chair_notes_and_their_ids():
    """Note ids are `N1` in both modes, so the fold's schema must not take the mode's item prefix."""
    brief = common.note_merge_task("### N1\nall gates green\nby: Agent1")
    assert '"canonical": "N1"' in brief and "all gates green" in brief and "by: Agent1" in brief
    assert common.schema_hint("review", phase="notes") == common.schema_hint("plan", phase="notes")


def test_the_seat_prompt_keeps_a_note_from_saying_a_peers_note_again():
    """The docket shows a seat a peer's fresh notes, so a repeat of one is a choice the prompt closes."""
    for human in ("blocking", "non_blocking"):
        assert "a peer's standing note" in common.question_shape(human)


def test_every_seat_phase_repairs_with_the_verdict_shape():
    """A repair re-ask quotes the shape of the pass it repairs: the gap, defense and seconding passes had the chair's.
    The builder's passes are the exception: `work` and `replan` repair with the builder's own shapes."""
    verdict = common.schema_hint("review", phase="round")
    for phase in ("round", "gap", "defense", "seconding"):
        assert common.schema_hint("review", phase=phase) == verdict
    assert {"work", "replan"} <= set(common.SEAT_PHASES)
    assert common.schema_hint("implement", phase="work") != verdict, "work repairs with the work shape, not the verdict"
    assert common.schema_hint("implement", phase="replan") != verdict, "replan repairs with the replan shape, not the verdict"
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


def test_a_round_merge_is_shown_the_open_docket_so_a_re_reported_claim_folds_once():
    """A new duplicate of an older entry folds into it: the listing owes the chair both sides of that call."""
    import asyncio

    from mech_council.deliberation.state import Item

    async def main():
        built, port, records = engine(reply=lambda ask: payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}]))
        built.docket.add_item(Item(id="F1", claim="the cache ignores tenants", severity="major", opened_round=0))
        built.docket.add_item(Item(id="F2", claim="tenant key missing from the cache", severity="major", opened_round=1))
        await built._merge_new_items(round_=1)
        return built, port, records

    built, port, records = asyncio.run(main())
    assert "F2" not in built.docket.items and "F2" not in built.docket.order, "the round's duplicate of an older entry stood twice"
    assert [r for r in records if r["event"] == "chair_merge"][-1]["groups"] == 1
    listing = [ask.text for ask in port.asks if ask.kind == "merge"][-1]
    assert "F2: tenant key missing from the cache" in listing.split("Already on the docket")[0], "what to group is listed first"
    assert "F1: the cache ignores tenants" in listing.split("Already on the docket")[1], "what to fold into is listed too"


def test_a_declined_stance_on_a_duplicate_does_not_block_its_fold():
    """The gap ladder's own declines are non-answers: a duplicate it ran out on still folds."""
    import asyncio

    async def main():
        built, _port, records = engine(reply=lambda ask: payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}]))
        built.docket.add_item(Item(id="F1", claim="the cache ignores tenants", severity="major", opened_round=0))
        built.docket.add_item(Item(id="F2", claim="the cache ignores tenants", severity="major", opened_round=1))
        for agent in built.docket.roster:
            built.docket.stances["F2"][agent] = Stance(stance=ABSTAIN, round=1, declined=True)
        await built._merge_new_items(round_=1)
        return built, records

    built, records = asyncio.run(main())
    assert "F2" not in built.docket.items, "the ladder's declined abstains vetoed the fold"
    assert [r for r in records if r["event"] == "chair_merge"][-1]["groups"] == 1


def test_an_answered_stance_on_a_duplicate_still_blocks_its_fold():
    """A seat that looked and chose not to take a position still chose: only declines fold away, and the skip is logged."""
    import asyncio

    async def main():
        built, _port, records = engine(reply=lambda ask: payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}]))
        built.docket.add_item(Item(id="F1", claim="the cache ignores tenants", severity="major", opened_round=0))
        built.docket.add_item(Item(id="F2", claim="the cache ignores tenants", severity="major", opened_round=1))
        built.docket.stances["F2"]["Agent1"] = Stance(stance=ABSTAIN, round=1)
        await built._merge_new_items(round_=1)
        return built, records

    built, records = asyncio.run(main())
    assert "F2" in built.docket.items, "the fold discarded a seat's verdict"
    assert [r for r in records if r["event"] == "merge_duplicate_stanced"][0]["into"] == "F1", records
    assert [r for r in records if r["event"] == "chair_merge"][-1]["groups"] == 0


def test_a_landed_fold_is_not_offered_again():
    """A fold that landed owes nothing: its canonical is not re-offered when the next round raises nothing."""
    import asyncio

    async def main():
        built, port, records = engine(reply=lambda ask: payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}]))
        built.docket.add_item(Item(id="F1", claim="the cache ignores tenants", severity="major", opened_round=0))
        built.docket.add_item(Item(id="F2", claim="the cache ignores tenants", severity="major", opened_round=0))
        await built._merge_pass(round_=0, ids=["F1", "F2"])
        await built._merge_new_items(round_=1)
        return built, port, records

    built, port, records = asyncio.run(main())
    assert "F2" not in built.docket.items, "the fold never landed"
    assert [a.round for a in port.asks if a.kind == "merge"] == [0], "a landed fold was re-offered"


def test_a_stance_on_a_folded_item_id_lands_on_the_survivor():
    """The fold keeps no item under the old id, but a seat stancing it from memory still counts, under the id that survives."""
    import asyncio

    async def main():
        built, _port, _records = engine(reply=lambda ask: payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}]))
        built.docket.add_item(Item(id="F1", claim="the cache ignores tenants", severity="major", opened_round=0))
        built.docket.add_item(Item(id="F2", claim="tenant key missing from the cache", severity="major", opened_round=1))
        await built._merge_new_items(round_=1)
        built._apply_verdict("Agent3", payload(positions=[{"item": "F2", "stance": "agree"}], signoff=True), round_=2)
        return built

    built = asyncio.run(main())
    assert built.docket.item_aliases.get("F2") == "F1", "the fold owes a mapping for the id it deleted"
    assert built.docket.stance("F1", "Agent3") is not None, "the stale id stanced the claim, under its surviving id"
    assert built.docket.stance("F2", "Agent3") is None, "no stance sits under an id with no item"


def test_a_gap_answer_on_a_folded_item_id_lands_on_the_survivor():
    """The gap ladder asks only for ids that still stand: a seat answering a folded one from memory still counts, once."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug", "another bug")
        if ask.kind == "merge":
            return payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}])
        if ask.who == "Agent2" and ask.phase == "gap":
            return payload(positions=[{"item": "F2", "stance": "agree"}], signoff=True)
        if ask.who == "Agent2":
            return payload(positions=[], signoff=True)  # silent on the whole docket
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=1)
        await built.run()
        return built, records

    built, records = asyncio.run(main())
    assert built.docket.item_aliases.get("F2") == "F1", "the fold never landed"
    record = built.docket.stance("F1", "Agent2")
    assert record is not None and not record.declined, "a memory of the old id is an answer to the survivor, not a recorded silence"
    assert record.stance == AGREE
    assert built.docket.stance("F2", "Agent2") is None, "no stance sits under an id with no item"
    assert not [r for r in records if r["event"] == "gap_declined" and r["member"] == "Agent2"], "the gap was answered, not exhausted"


def test_a_group_that_grouped_nothing_is_offered_again():
    """A fold the guard blocked grouped nothing: the canonical is re-offered, the answered duplicate is not."""
    import asyncio

    async def main():
        built, port, records = engine(reply=lambda ask: payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}]))
        built.docket.add_item(Item(id="F1", claim="the cache ignores tenants", severity="major", opened_round=0))
        built.docket.add_item(Item(id="F2", claim="the cache ignores tenants", severity="major", opened_round=1))
        built.docket.stances["F2"]["Agent1"] = Stance(stance=AGREE, round=1)
        await built._merge_new_items(round_=1)
        await built._merge_new_items(round_=2)
        return built, port, records

    built, port, records = asyncio.run(main())
    assert "F2" in built.docket.items, "a fold the guard blocked was folded anyway"
    asks = [a for a in port.asks if a.kind == "merge"]
    assert [a.round for a in asks] == [1, 2], "a group that folded nothing was not re-offered"
    offered = asks[1].text.split("Already on the docket")[0]
    assert "F1:" in offered and "F2:" not in offered, "the answered duplicate was re-offered"


def test_a_plan_mode_duplicate_conflict_folds_in_the_round_0_merge():
    """Conflicts open in round 0, so only the comparison's own merge pass can offer them."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(approach=dict(PLANS))
        if ask.kind == "compare":
            positions = [{"agent": "Agent1", "claim": "gate first"}, {"agent": "Agent2", "claim": "ship first"}]
            conflict = {"claim": "gate before ship", "positions": positions}
            return payload(agreed=[], conflicts=[conflict, dict(conflict)], uncovered=[])
        if ask.kind == "merge":
            return payload(groups=[{"canonical": "T1", "duplicates": ["T2"]}])
        return payload(positions=[], backing="no_preference", signoff=True)

    async def main():
        built, port, records = engine(reply=reply, mode="plan", max_rounds=1)
        await built.phase0()
        await built.publish_plan()
        return built, port, records

    built, port, records = asyncio.run(main())
    assert "T2" not in built.docket.items, "the duplicate conflict was never offered to fold"
    assert [a.round for a in port.asks if a.kind == "merge"] == [0], "the comparison never ran its merge pass"
    listing = [a.text for a in port.asks if a.kind == "merge"][0]
    assert "T1: gate before ship" in listing and "T2: gate before ship" in listing, "both conflicts were on the chair's scope"
    assert [r for r in records if r["event"] == "chair_merge"][0]["groups"] == 1


def test_a_folded_duplicate_is_a_co_reporter_not_a_co_claimant():
    """Folding a duplicate credits its raiser as a co-reporter; the claim stays the original raiser's."""
    import asyncio

    from mech_council.deliberation.state import Item

    async def main():
        built, _port, _records = engine(reply=lambda ask: payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}]))
        built.docket.add_item(Item(id="F1", claim="the cache ignores tenants", severity="major", opened_round=0, raised_by=["Agent1"]))
        built.docket.add_item(Item(id="F2", claim="the cache ignores tenants", severity="major", opened_round=1, raised_by=["Agent2"]))
        await built._merge_new_items(round_=1)
        return built

    item = asyncio.run(main()).docket.items["F1"]
    assert item.raised_by == ["Agent1"], "the fold widened the claim to the duplicate's raiser"
    assert item.also_reported_by == ["Agent2"], "the duplicate's raiser is only a co-reporter"


def test_a_fold_of_an_already_folded_item_keeps_its_earlier_co_reporters():
    """A fold carries the duplicate's own co-reporters, so a second fold loses nobody."""
    import asyncio

    from mech_council.deliberation.state import Item

    def reply(ask):
        if ask.round == 0:
            return payload(groups=[{"canonical": "F2", "duplicates": ["F3"]}])
        return payload(groups=[{"canonical": "F1", "duplicates": ["F2", "F4"]}])

    async def main():
        built, _port, _records = engine(reply=reply)
        for item_id, round_, raiser in (("F1", 0, "Agent1"), ("F2", 0, "Agent2"), ("F3", 0, "Agent3"), ("F4", 1, "Agent4")):
            built.docket.add_item(Item(id=item_id, claim="the same claim", severity="major", opened_round=round_, raised_by=[raiser]))
        await built._merge_pass(round_=0, ids=["F1", "F2", "F3"])
        await built._merge_pass(round_=1, ids=["F1", "F2", "F4"], new=["F4"])
        return built

    item = asyncio.run(main()).docket.items["F1"]
    assert item.raised_by == ["Agent1"], "a fold widened the claim to a duplicate's raiser"
    assert item.also_reported_by == ["Agent2", "Agent3", "Agent4"], "the second fold lost the first fold's co-reporter"


def test_a_merge_of_an_id_the_chair_was_never_shown_retires_nothing():
    """The listing is the chair's whole scope: an id outside it retires nothing, on either side of the group."""
    import asyncio

    from mech_council import run as run_mod
    from mech_council.deliberation.state import Item

    async def merge(group):
        built, _port, records = engine(reply=lambda ask: payload(groups=[group]))
        built.docket.add_item(Item(id="F1", claim="an open claim", severity="major", opened_round=0))
        built.docket.add_item(Item(id="F2", claim="the same claim again", severity="major", opened_round=1))
        await built._merge_pass(round_=1, ids=["F2"])
        return built, records

    built, records = asyncio.run(merge({"canonical": "F2", "duplicates": ["F1"]}))
    assert "F1" in built.docket.items and "F1" in built.docket.order, "an id the chair was never shown folded an item away"
    unlisted = [r for r in records if r["event"] == "merge_duplicate_unlisted"]
    assert unlisted and unlisted[0]["duplicate"] == "F1", "an out-of-listing duplicate is refused out loud"
    assert run_mod._summary_line("merge_duplicate_unlisted", unlisted[0], {}), "the operator is told, not only the log"

    built, records = asyncio.run(merge({"canonical": "F1", "duplicates": ["F2"]}))
    assert "F2" in built.docket.items and "F2" in built.docket.order, "a shown item folded into a canonical the chair never saw"
    unlisted = [r for r in records if r["event"] == "merge_canonical_unlisted"]
    assert unlisted and unlisted[0]["canonical"] == "F1", "an out-of-listing canonical is refused out loud"
    assert run_mod._summary_line("merge_canonical_unlisted", unlisted[0], {}), "the operator is told, not only the log"


def test_asks_and_notes_named_on_a_folded_duplicate_repoint_to_the_survivor():
    """The duplicate's ask is about the claim that survives: the fold re-points it, so the canonical's closure can moot it."""
    import asyncio

    from mech_council import run as run_mod
    from mech_council.deliberation.escalations import moot, moot_item_asks, raise_question
    from mech_council.deliberation.notes import raise_note

    async def main():
        built, _port, records = engine(reply=lambda ask: payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}]))
        built.docket.add_item(Item(id="F1", claim="the cache ignores tenants", severity="major", opened_round=0))
        built.docket.add_item(Item(id="F2", claim="tenant key missing from the cache", severity="major", opened_round=1))
        question, _ = raise_question(built.docket, text="may we change the tenant key?", asker="Agent2", item_id="F2", round_=1)
        note, _ = raise_note(built.docket, by="Agent3", text="the key format is documented", item_id="F2", round_=1)
        await built._merge_new_items(round_=1)
        return built, records, question, note

    built, records, question, note = asyncio.run(main())
    assert "F2" not in built.docket.items, "the duplicate did not fold"
    assert question.item_id == "F1", "the ask still names an id the docket no longer has"
    assert note.item_id == "F1", "the note still names an id the docket no longer has"
    folded = [r for r in records if r["event"] == "question_item_folded"]
    assert folded == [{"event": "question_item_folded", "id": "H1", "item": "F2", "into": "F1", "round": 1}]
    note_folded = [r for r in records if r["event"] == "note_item_folded"]
    assert note_folded == [{"event": "note_item_folded", "id": "N1", "item": "F2", "into": "F1", "round": 1}]
    assert run_mod._summary_line("question_item_folded", folded[0], {}), "the operator is told, not only the log"
    assert run_mod._summary_line("note_item_folded", note_folded[0], {}), "the operator is told, not only the log"

    built.docket.items["F1"].disposition = AGREED
    mooted = moot_item_asks(built.docket, item_id="F1", disposition="agreed", round_=2)
    assert [fields["id"] for _event, fields in mooted] == ["H1"], "the survivor's closure could not moot the re-pointed ask"
    assert question.state == WITHDRAWN and moot(built.docket, question)


def test_a_group_that_names_the_later_entry_canonical_folds_it_into_the_earlier_one():
    """The chair is told the earliest id is canonical; a group ignoring that must fold, not no-op."""
    import asyncio

    from mech_council import run as run_mod
    from mech_council.deliberation import escalations
    from mech_council.deliberation.state import AGREE, Item

    async def merge(group):
        built, _port, records = engine(reply=lambda ask: payload(groups=[group]))
        built.docket.add_item(Item(id="F1", claim="the cache ignores tenants", severity="major", opened_round=0))
        built.docket.add_item(Item(id="F2", claim="tenant key missing from the cache", severity="major", opened_round=1))
        for agent in built.docket.roster:
            escalations.record_stance(built.docket, item_id="F1", agent=agent, stance=AGREE, round_=1)
        await built._merge_new_items(round_=1)
        return built, records

    built, records = asyncio.run(merge({"canonical": "F2", "duplicates": ["F1"]}))
    assert "F2" not in built.docket.items and "F1" in built.docket.items, "the inverted group folded nothing"
    replaced = [r for r in records if r["event"] == "merge_canonical_replaced"]
    assert replaced and replaced[0]["canonical"] == "F1" and replaced[0]["chair_named"] == "F2", replaced
    assert run_mod._summary_line("merge_canonical_replaced", replaced[0], {}), "the operator is told, not only the log"
    assert [r for r in records if r["event"] == "chair_merge"][-1]["groups"] == 1

    built, _records = asyncio.run(merge({"canonical": "F1", "duplicates": ["F2", "F2"]}))
    assert "F2" not in built.docket.items, "an id the chair echoed twice retires once, not twice"


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


def test_a_round_summary_with_nothing_open_has_no_dangling_separator():
    """A round that closes every item reports only the sign-off line, no trailing separator."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who != "Agent1":
                return payload(findings=[])
            return payload(findings=[{"claim": "a bug"}])
        return stance_all(ask, "agree")

    async def main():
        built, _port, _records = engine(reply=reply)
        outcome = await built.run()
        return outcome, built

    outcome, built = asyncio.run(main())
    assert outcome == "consensus"
    summary = built.docket.rounds[0].summary
    assert "3/3" in summary, summary  # the sign-off count is still reported
    assert " · " not in summary, summary  # no states left, no separator


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
    """An ask is a seat's stuck state: once it works the item out, the human is not asked about it."""
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


def test_the_chair_fold_makes_two_wordings_one_note():
    """Phase 0 is independent, so the same note often reaches the docket twice: the chair folds it before the human reads it."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who == "Agent3":
                return payload(findings=[])
            note = "all gates green: ruff, mypy, pytest"
            return payload(findings=[finding("a bug")] if ask.who == "Agent1" else [], notes=[note])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.kind == "notes":
            return {"groups": [{"canonical": "N1", "duplicates": ["N2"]}]}
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(max_rounds=2, reply=reply)
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    assert outcome == "consensus"
    first, second = built.docket.human_notes["N1"], built.docket.human_notes["N2"]
    assert (second.folded_into, first.also_noted_by) == ("N1", ["Agent2"]), "the earliest note survives, named with the other"
    shown = [c["notes"] for c in port.checkpoints if c["notes"]]
    assert shown == [["N1:note"]], "the human reads the note once"
    fold = next(r for r in records if r["event"] == "note_merged")
    assert (fold["id"], fold["into"]) == ("N2", "N1")


def test_the_chair_is_not_asked_to_fold_notes_when_there_is_nothing_to_fold():
    """A chair pass costs a lane's whole turn: it runs when there is something to fold."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            if ask.who == "Agent1":
                return payload(findings=[finding("a bug")], notes=["all gates green"])
            return payload(findings=[])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(max_rounds=2, reply=reply)
        return await built.run(), built, port, records

    outcome, _built, port, _records = asyncio.run(main())
    assert outcome == "consensus"
    assert not [a for a in port.asks if a.kind == "notes"], "one note: nothing for the chair to fold"


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
            # a reasoned objection keeps `evaluate` at GO, so the round limit is what ends the run
            return payload(
                positions=[{"item": item, "stance": "disagree", "evidence": "app.py:9"} for item in shown_items(ask.text)],
                backing="no_preference",
                signoff=True,
            )
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
    """The last pass reads only releases: a restance there is recorded nowhere, and an objection lands nowhere."""
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


def test_the_park_is_the_only_thing_that_says_the_run_is_waiting():
    """A run cut short reports the asks it was parked on when the checkpoint closed."""
    import asyncio

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
        return await built.run(), built, port

    _outcome, built, port = asyncio.run(main())
    waiting = [c for c in port.checkpoints if c["waiting"]]
    assert waiting and all(c["parked_on"] == c["ask_set"] for c in waiting), "the park names what it waits on"
    assert all(c["parked_on"] == [] for c in port.checkpoints if not c["waiting"]), "a checkpoint that waits on nothing"
    assert built.docket.parked_on == [], "and nothing at all once the checkpoint is done"


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


def test_a_restated_stop_is_not_announced_twice():
    """A stop is sticky, so the seat that declared it resends `blocking_for`; the event names the stop, not the restatement."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1":
            return stance_all(ask, "agree", human_input=[{"question": "which tenant owns the cache key?"}])
        if ask.who == "Agent2":
            return stance_all(ask, "agree", blocking_for=["H1"], **({"second": ["H1"]} if ask.round == 1 else {}))
        return stance_all(ask, "disagree" if ask.round == 1 else "agree")

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=2)
        return await built.run(), records

    outcome, records = asyncio.run(main())
    assert outcome == "consensus"
    stops = [r for r in records if r["event"] == "question_stop_requested"]
    assert [(r["id"], r["seat"]) for r in stops] == [("H1", "Agent2")], "the second round restates the stop, it does not declare one"


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
    """The answer came first, so the second changed nothing: one event names the answer that landed."""
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


def test_an_answer_typed_while_the_seats_work_lands_before_the_next_brief():
    """The human answers a deferred ask mid-round: the run does not wait for a checkpoint to take it."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1" and ask.round == 1:
            return stance_all(ask, "agree", human_input=[{"question": "which tenant owns the cache key?"}])
        if ask.who == "Agent2" and ask.round == 1:
            return stance_all(ask, "agree", second=["H1"])
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, off_cycle={"H1": "the tenant is fixed by contract"})
        return await built.run(), built, port, records

    outcome, built, port, records = asyncio.run(main())
    off = [r for r in records if r["event"] == "human_answer_off_cycle"]
    assert [(r["round"], r["answered"]) for r in off] == [(1, ["H1"])]
    question = built.docket.questions["H1"]
    assert (question.state, question.answer, question.answer_round) == ("answered", "the tenant is fixed by contract", 1)
    assert any(r["event"] == "answer_buys_a_round" for r in records), "an answer is movement wherever it arrives"
    assert not any(c["waiting"] and "H1" in c["ask_set"] for c in port.checkpoints), "nothing was put in front of the human"
    briefs = [ask.text for ask in port.asks if ask.phase == "round" and ask.round == 2]
    assert briefs and all("ANSWER (authoritative): the tenant is fixed by contract" in text for text in briefs)
    assert outcome == "consensus", "the answer took the question off the table"


def test_the_grace_budget_caps_the_run_however_the_answers_arrive():
    """Answers from both the checkpoint and mid-round spend the one grace round, and nothing beyond it."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1":
            asked = ["which tenant owns the cache key?"] + (["which palette?"] if ask.round == 1 else [])
            return stance_all(ask, "agree", human_input=[{"question": text} for text in asked])
        if ask.who == "Agent2":
            return stance_all(ask, "agree", second=["H1", "H2", "H3", "H4"])
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(
            reply=reply,
            max_rounds=1,
            grace_rounds=2,
            off_cycle={"H1": "the fixed one", "H3": "the pale one", "H4": "the plain one"},
            answers={"H2": "the house one"},
        )
        return await built.run(), built, port, records

    _outcome, built, port, records = asyncio.run(main())
    bought = [r["grace_left"] for r in records if r["event"] == "grace_round_bought"]
    assert bought == [1, 0], f"one grace round per round of answers, not one per answer: {bought}"
    assert sorted({ask.round for ask in port.asks if ask.phase == "round"}) == [1, 2, 3], "max_rounds + grace_rounds, never more"
    answered = {qid: q.answer_round for qid, q in built.docket.questions.items() if q.state == "answered"}
    assert answered == {"H1": 1, "H2": 1, "H3": 2, "H4": 3}, "every answer is on the docket, the one that bought nothing included"
    assert [r["round"] for r in records if r["event"] == "answer_buys_no_round"] == [3], "and that one is said aloud"


def test_an_off_cycle_answer_at_the_ceiling_leaves_the_checkpoint_open():
    """Paying for the grace round off-cycle must not close the checkpoint that asks about everything else."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1" and ask.round == 1:
            return stance_all(ask, "agree", human_input=[{"question": "which tenant?"}, {"question": "which palette?"}])
        if ask.who == "Agent2" and ask.round == 1:
            return stance_all(ask, "agree", second=["H1", "H2"])
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(
            reply=reply, max_rounds=1, grace_rounds=1, off_cycle={"H1": "the fixed one"}, answers={"H2": "the house one"}
        )
        return await built.run(), built, port, records

    _outcome, built, port, _records = asyncio.run(main())
    assert [c["waiting"] for c in port.checkpoints] == [True], "an answer typed early does not spend the human's turn"
    assert [built.docket.questions[i].state for i in ("H1", "H2")] == ["answered", "answered"]
    assert "unasked" not in built.docket.questions["H2"].markers


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


def test_a_stance_on_a_closed_item_does_not_make_the_verdict_whole():
    """A seat that re-voices a settled item and leaves one of two open items silent is still partial: mechanical, not deliberate."""
    from mech_council.deliberation.engine import _partial

    docket = Docket(mode="review", roster=["Agent1", "Agent2"])
    docket.add_item(Item(id="F1", claim="open one", raised_by=["Agent1"]))
    docket.add_item(Item(id="F2", claim="open two", raised_by=["Agent1"]))
    docket.add_item(Item(id="F3", claim="settled", raised_by=["Agent1"], disposition=AGREED, closed_round=1))
    docket.stances["F2"]["Agent1"] = Stance(stance=AGREE, round=2)
    docket.stances["F3"]["Agent1"] = Stance(stance=AGREE, round=2)
    assert _partial(docket, "Agent1", 2), "a re-stance of a closed item is not an answer to an open one"


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


def test_a_fold_that_deletes_a_shown_item_leaves_no_gap_for_the_removed_id():
    """The chair's merge deletes F2 mid-round; the gap ladder must not ask for it, nor decline it under an id with no item."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the cache ignores tenants", "tenant key missing from the cache")
        if ask.kind == "merge":
            if ask.round == 0:
                return payload(groups=[])  # round 0 leaves both entries open; the fold comes in round 1
            return payload(groups=[{"canonical": "F1", "duplicates": ["F2", "F3"]}])
        if ask.phase == "gap":
            return payload(positions=[{"item": item, "stance": "agree"} for item in gap_items(ask.text)], signoff=True)
        if ask.who == "Agent3":
            # stances F1, stays silent on F2, re-files the claim as a fresh entry
            return payload(positions=[{"item": "F1", "stance": "agree"}], new_items=[finding("the cache ignores tenants")], signoff=True)
        return payload(positions=[{"item": "F1", "stance": "agree"}], signoff=True)

    async def main():
        built, port, records = engine(reply=reply, max_rounds=1)
        await built.run()
        return built, port, records

    built, port, records = asyncio.run(main())
    assert "F2" not in built.docket.items, "the fold deleted the shown duplicate, as the chair meant"
    assert port.count(phase="gap") == 0, "the ladder asked a seat about an item the chair deleted mid-round"
    assert "F2" not in built.docket.stances, "a declined stance sits under an id with no item"
    assert not [r for r in records if r["event"] == "gap_declined"], "the decline named the removed id"


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


def test_a_restarted_defense_re_asks_the_round_with_the_challenge():
    """The defense fragment presumes a session, so a restarted seat is re-asked the round with the
    challenge appended — not the fragment alone (no session) nor the round alone (hides the challenge)."""
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
        built, port, _records = engine(reply=reply, max_rounds=1)
        await built.run()
        return port

    port = asyncio.run(main())
    round_ask = [a for a in port.asks if a.who == "Agent1" and a.phase == "round"][0]
    defense_ask = [a for a in port.asks if a.who == "Agent1" and a.phase == "defense"][0]
    assert defense_ask.context is not None, "a restarted seat is owed a self-contained re-ask"
    assert defense_ask.text in defense_ask.context, "the challenge is in the re-ask"
    assert round_ask.text in defense_ask.context, "the round it continues is in the re-ask"


def test_every_challenge_in_a_round_reaches_the_defense():
    """Two reasoned objections in one round: the holder answers the set, not just the first."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "defense":
            return payload(
                positions=[{"item": "F1", "stance": "needs_human", "note": "the policy predates the tenant work"}],
                signoff=True,
            )
        items = shown_items(ask.text)
        if ask.who == "Agent1":
            return payload(
                positions=[{"item": i, "stance": "needs_human", "note": "does policy allow this"} for i in items],
                signoff=True,
            )
        if ask.who == "Agent2":
            return payload(
                positions=[
                    {"item": i, "stance": "disagree", "note": "the contract fixes it", "evidence": "docs/contract.md:12"} for i in items
                ],
                signoff=True,
            )
        if ask.who == "Agent3":
            return payload(
                positions=[
                    {"item": i, "stance": "disagree", "note": "the test suite covers it", "evidence": "tests/test_tenant.py:44"}
                    for i in items
                ],
                signoff=True,
            )
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply, max_rounds=1)
        await built.run()
        return built, port

    built, port = asyncio.run(main())
    defense = [ask.text for ask in port.asks if ask.phase == "defense"]
    assert len(defense) == 1, "one round of challenges costs one defense ask"
    for snippet in ("the contract fixes it", "docs/contract.md:12", "the test suite covers it", "tests/test_tenant.py:44"):
        assert snippet in defense[0], f"the seat never saw {snippet!r}"
    assert built.docket.escalations[("F1", "Agent1")].state == STANDING


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


def test_a_refused_fold_is_offered_again_until_it_lands():
    """A duplicate whose fold was refused is re-offered in later rounds; the ladder's declines do not stop it."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "the token cache is wrong")
        if ask.kind == "merge":
            if ask.round == 0:
                return payload(groups=[])
            if ask.round <= 2:
                # one id reused across groups refuses the whole plan
                return payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}, {"canonical": "F2", "duplicates": []}])
            return payload(groups=[{"canonical": "F1", "duplicates": ["F2"]}])
        positions = []
        for item in shown_items(ask.text):
            if item == "F2":
                continue  # the seats stay out of the duplicate until the chair folds it
            if ask.who == "Agent2":
                positions.append({"item": item, "stance": "disagree", "note": "not the only cause"})
            else:
                positions.append({"item": item, "stance": "agree"})
        extra = {"new_items": [finding("the token cache is wrong too")]} if ask.round == 1 and ask.who == "Agent1" else {}
        return payload(positions=positions, backing="no_preference", signoff=True, **extra)

    async def main():
        built, port, records = engine(reply=reply, max_rounds=3)
        await built.run()
        return built, port, records

    built, port, records = asyncio.run(main())
    assert "F2" not in built.docket.items, "the refused fold was never re-offered"
    assert [a.round for a in port.asks if a.kind == "merge"] == [0, 1, 2, 3], "the re-offer skipped a round"
    listing = [a.text for a in port.asks if a.kind == "merge" and a.round == 2][0]
    assert "F2: the token cache is wrong too" in listing.split("Already on the docket")[0], "the carried id is what the chair groups"
    declined = [r for r in records if r["event"] == "gap_declined" and "F2" in r["items"]]
    assert declined and all(r["round"] == 2 for r in declined), "the fold landed before the round-3 ladder could nag"
    assert [r for r in records if r["event"] == "chair_merge" and r["round"] == 3][0]["groups"] == 1
    assert not built.docket.items["F1"].closed


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
    """A seat laying a fresh plan is still trying to win a camp over: the split ask waits out its rounds, three at the latest."""
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
    """The ceiling asks regardless of how much work that round was doing."""
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
    """The clock counts camps, not labels: renaming the merge every round moves nothing, and the split is asked at three."""
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
    """The menu is stored in the ids the seats read and printed in names; every line carries the plan's own claim and stages."""
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
    """The round limit asks rather than silencing: an answer at the ceiling is on the grace budget."""
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
    """The termination order's direction rule, conditions 1-3."""
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


def test_a_plan_verdict_that_omits_backing_is_said_and_the_ladder_owns_the_rest():
    """The omission is the only case the emit can see: a token that maps to none of the three is refused before the verdict is applied."""
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
            return payload(agreed=[], conflicts=[], uncovered=[])
        if ask.who == "Agent2":
            return RawAsk(rejected=True)  # what the port does with a backing that maps to none of the three
        if ask.who == "Agent3":
            return payload(positions=[], backing="no_preference", signoff=True)
        return payload(positions=[], signoff=True)  # Agent1: the verdict names no backing

    async def main():
        built, _port, records = engine(reply=reply, mode="plan", max_rounds=1)
        await built.run()
        return built, records

    built, records = asyncio.run(main())
    missing = [r for r in records if r["event"] == "verdict_missing_backing"]
    assert {(r["member"], r["round"]) for r in missing} == {("Agent1", 1)}, "the omission is said once, by the seat that omitted"
    assert not [r for r in records if r["event"] == "verdict_unknown_findings"], "a missing field is not an unknown finding"
    declined = [r for r in records if r["event"] == "gap_declined"]
    assert sorted(r["member"] for r in declined) == ["Agent1", "Agent2"], "the ladder owns what the omission and the refusal leave behind"
    assert all(r["items"] == ["backing"] for r in declined)
    assert built.docket.backing["Agent1"] == DECLINED and built.docket.backing["Agent2"] == DECLINED


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


def test_a_seat_cannot_answer_a_question_the_docket_addressed_to_a_peer():
    """An answer is printed under a name: a seat answering a question it was not asked answers for a peer."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.who == "Agent1" and ask.round == 1:
            return {"positions": [], "questions": [{"to": "Agent3", "question": "why is the key safe?"}]}
        if ask.who == "Agent2" and ask.round == 1:
            return {"positions": [], "answers": [{"to": "Agent1", "question": "Q1", "answer": "no, by inode"}]}
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply)
        await built.run()
        return built, records

    built, records = asyncio.run(main())
    assert not [qa for qa in built.docket.qa.values() if qa.answer], "Agent3 was answered over"
    refused = [r for r in records if r["event"] == "qa_answer_not_addressed"]
    assert refused and refused[0]["member"] == "Agent2", "the attempt is logged, not swallowed"


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


def test_an_answer_with_no_id_among_several_open_questions_lands_nowhere():
    """Several open questions from one peer: without a q-id the harness refuses, it does not pick the newest."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.who == "Agent1" and ask.round == 1:
            return {
                "positions": [],
                "questions": [{"to": "Agent2", "question": "why is the key safe?"}, {"to": "Agent2", "question": "which host runs it?"}],
            }
        if ask.who == "Agent2" and ask.round == 2:
            return {"positions": [], "answers": [{"to": "Agent1", "answer": "no, by inode"}]}
        return stance_all(ask, "agree" if ask.who != "Agent3" else "disagree")

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=2)
        await built.run()
        return built, records

    built, records = asyncio.run(main())
    assert not [qa for qa in built.docket.qa.values() if qa.answer], "a guess landed the answer on one of the open questions"
    refused = [r for r in records if r["event"] == "qa_answer_unmatched" and r["member"] == "Agent2"]
    assert refused, records
    assert sorted(refused[0]["candidates"]) == ["Q1", "Q2"], "the refusal names what the seat can answer instead"


def test_an_answer_named_by_its_text_lands_on_the_question_that_text_is():
    """Several open questions: a quoted question text settles the one it is, no refusal."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.who == "Agent1" and ask.round == 1:
            return {
                "positions": [],
                "questions": [{"to": "Agent2", "question": "why is the key safe?"}, {"to": "Agent2", "question": "which host runs it?"}],
            }
        if ask.who == "Agent2" and ask.round == 2:
            return {"positions": [], "answers": [{"to": "Agent1", "question": "which host runs it?", "answer": "the panel host"}]}
        return stance_all(ask, "agree" if ask.who != "Agent3" else "disagree")

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=2)
        await built.run()
        return built, records

    built, records = asyncio.run(main())
    by_id = {qa.id: qa for qa in built.docket.qa.values()}
    assert by_id["Q2"].answer == "the panel host", "the quoted text names the second question"
    assert not by_id["Q1"].answer
    assert not [r for r in records if r["event"] == "qa_answer_unmatched"], records


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


def test_a_seat_demoted_in_one_round_wins_its_defense_in_the_next():
    """A demotion in one round does not reach the next: the seat's defense is read and the report says it answered the challenge."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.phase == "defense":
            repeats = "does policy allow this" if ask.round == 1 else "that document predates the tenant work, so a person must decide"
            return payload(positions=[{"item": "F1", "stance": "needs_human", "note": repeats}], signoff=True)
        items = shown_items(ask.text)
        if ask.who == "Agent1":
            return payload(
                positions=[{"item": i, "stance": "needs_human", "note": "does policy allow this"} for i in items],
                human_input=[{"question": "does policy allow this", "item": i} for i in items],
                signoff=True,
            )
        if ask.who == "Agent2":
            return payload(positions=[{"item": i, "stance": "disagree", "evidence": "docs/policy.md:4"} for i in items], signoff=True)
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=2)
        await built.run()
        return built, records

    built, records = asyncio.run(main())
    events = [r["event"] for r in records]
    assert events.count("escalation_demoted") == 1, f"round 2 must not demote a defense that answered: {events}"
    escalation = built.docket.escalations[("F1", "Agent1")]
    assert escalation.state == STANDING and escalation.defended_round == 2, escalation
    assert escalation.defense and not escalation.undefended, "the defense was read, and is reported as one"
    assert [r["item"] for r in records if r["event"] == "escalation_defended"] == ["F1"]


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


# --------------------------------------------------------------------------- the note ledger


def test_a_note_needs_no_second_seat_and_ends_the_run_like_silence():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(findings=[], notes=["retention is a contract, not a config choice"])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply)
        return await built.run(), port, built

    outcome, port, built = asyncio.run(main())
    assert outcome == "consensus", "an unseconded note ends nothing: it asks nothing"
    assert all(cp["ask_set"] == [] for cp in port.checkpoints), "a note is never put to the human as a question"
    assert [n.shown_round for n in built.docket.human_notes.values()] == [0, 0, 0], "and every one still reaches the human"


def test_each_note_says_itself_once_and_its_retraction_once():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(findings=[], notes=[] if ask.who != "Agent1" else ["retention is a contract"])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent3" and ask.round == 1:
            return stance_all(ask, "agree", signoff=False)  # keep the council another round
        if ask.who == "Agent2":
            if ask.round == 1:
                return stance_all(ask, "agree", notes=["the lease renews in march"])
            if ask.round == 2:
                return stance_all(ask, "agree", retract_note=["N2"])
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply, max_rounds=3)
        return await built.run(), port, built

    outcome, port, built = asyncio.run(main())
    assert outcome == "consensus"
    printed = [(cp["round"], one) for cp in port.checkpoints for one in cp["notes"]]
    assert printed == [(0, "N1:note"), (1, "N2:note"), (2, "N2:retraction")], "each statement once, at the next checkpoint"
    assert [built.docket.human_notes[i].shown_round for i in ("N1", "N2")] == [0, 1]
    assert built.docket.human_notes["N2"].state == RETRACTED
    assert built.docket.human_notes["N2"].retraction_shown_round == 2


def test_only_the_author_retracts_a_note_and_the_refusal_is_said_not_swallowed():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(findings=[], notes=[] if ask.who != "Agent1" else ["retention is a contract"])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent2":
            if ask.round == 1:
                return stance_all(ask, "agree", signoff=False)
            return stance_all(ask, "agree", retract_note=["N1"])
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=2)
        return await built.run(), records, built

    _outcome, records, built = asyncio.run(main())
    refused = [r for r in records if r["event"] == "note_retract_refused"]
    assert [(r["member"], r["id"]) for r in refused] == [("Agent2", "N1")], "the refusal is recorded, not swallowed"
    assert built.docket.human_notes["N1"].state == STANDING, "a peer's refusal changes nothing"


def test_a_retraction_carries_the_reason_the_human_is_told():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(findings=[], notes=[] if ask.who != "Agent2" else ["the key lives in env"])
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1" and ask.round == 1:
            return stance_all(ask, "agree", signoff=False)  # keep the council another round
        if ask.who == "Agent2" and ask.round == 2:
            return stance_all(ask, "agree", retract_note=[{"id": "N1", "reason": "the config holds it, not env"}])
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply, max_rounds=3)
        return await built.run(), records, built

    _outcome, records, built = asyncio.run(main())
    note = built.docket.human_notes["N1"]
    assert (note.state, note.retract_reason) == (RETRACTED, "the config holds it, not env")
    said = [r for r in records if r["event"] == "note_retracted"]
    assert said and said[0]["reason"] == "the config holds it, not env"


def test_a_note_about_an_item_that_does_not_exist_keeps_the_note_and_drops_the_link():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(findings=[], notes=[{"note": "the lease renews in march", "item": "F9"}] if ask.who == "Agent1" else [])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(reply=reply)
        return await built.run(), records, built

    _outcome, records, built = asyncio.run(main())
    assert built.docket.human_notes["N1"].item_id is None
    assert any(r["event"] == "verdict_unknown_findings" for r in records), "the bad id is said, not silently obeyed"


def test_phase0_note_cannot_link_an_item_the_seat_was_not_shown():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(
                findings=[finding("the cache ignores tenants")],
                notes=["F1: the fix should extend the conditional write"] if ask.who == "Agent4" else [],
            )
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(ask, "agree")

    async def main():
        built, _port, records = engine(4, reply=reply)
        return await built.run(), records, built

    _outcome, records, built = asyncio.run(main())
    note = built.docket.human_notes["N1"]
    assert note.by == "Agent4"
    assert note.item_id is None, "phase 0 shows no docket, so a note's item reference is dropped"
    assert any(r["event"] == "verdict_unknown_findings" and r["id"] == "F1" for r in records), "the dropped link is said"


def test_a_note_that_opens_with_an_item_id_files_under_it_without_being_asked():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(findings=[finding("the cache ignores tenants")])
        if ask.kind == "merge":
            return {"groups": []}
        return stance_all(
            ask,
            "agree",
            notes=["F1: the fix should extend the conditional write", "the lease renews in march, like F1 does"]
            if ask.who == "Agent1"
            else [],
        )

    async def main():
        built, _port, _records = engine(reply=reply)
        return await built.run(), built

    _outcome, built = asyncio.run(main())
    assert built.docket.human_notes["N1"].item_id == "F1", "a note that opens with the item's id is filed under it"
    assert built.docket.human_notes["N2"].item_id is None, "an id mentioned away from the start is not a link"


def test_the_last_pass_reaches_the_human_because_the_asking_follows_it():
    """A peer's answer that proves a note wrong is read on the last pass, so the human is shown the correction."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(
                findings=[{"claim": "a bug"}] if ask.who == "Agent1" else [],
                open_questions=["which tenant owns the key?"] if ask.who == "Agent1" else [],
                notes=["the key lives in env"] if ask.who == "Agent1" else [],
            )
        if ask.kind in {"merge", "asks"}:
            return {"groups": []}
        if ask.phase == "seconding":
            if ask.who == "Agent1":
                return payload(retract_note=[{"id": "N1", "reason": "config holds it after all"}], notes=["config holds the key"])
            return payload()
        if ask.who == "Agent2":
            return stance_all(ask, "agree", answer_question=[{"id": "H1", "answer": "config names it: tenant_id"}])
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply)
        return await built.run(), built, port, records

    _outcome, built, port, records = asyncio.run(main())
    printed = [one for cp in port.checkpoints for one in cp["notes"]]
    assert printed == ["N1:note", "N1:retraction", "N2:note"], "the last pass corrects what the human was told"
    assert [r["event"] for r in records if r["event"].startswith("note_")] == ["note_raised", "note_retracted", "note_raised"]
    notes = built.docket.human_notes
    assert notes["N1"].state == RETRACTED and notes["N1"].retract_reason == "config holds it after all"
    assert notes["N2"].shown_round is not None, "and the fresh note is not left in the report alone"


# ------------------------------------------------------------ The human's posture


def test_the_non_blocking_run_never_waits_but_still_puts_the_asks_in_front_of_the_human():
    """Two seats declare the stop anyway: the run keeps working, and the ledger says the human was shown the ask and never answered it."""
    import asyncio

    from mech_council.deliberation.escalations import effective_class

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who in ("Agent1", "Agent2"):
            return needs_human(ask)
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply, human="non_blocking")
        outcome = await built.run()
        return outcome, built, port

    outcome, built, port = asyncio.run(main())
    question = built.docket.questions["H1"]
    assert outcome == "consensus", "an ask that cannot stop the run does not end the run needs_human"
    assert question.blocked_by == [] and effective_class(built.docket, question) == "deferred", "the stop was declared into the void"
    assert question.state == "asked", "nothing answered it, so it is still open"
    assert "unanswered" in question.markers and "unasked" not in question.markers, "the ledger says the human was shown it"
    assert port.checkpoints, "the terminal checkpoint still runs"
    assert all(not c["waiting"] for c in port.checkpoints), "the run never waited"
    assert all(c["parked_on"] == [] for c in port.checkpoints), "and nothing is parked: a checkpoint that waits on nothing"
    terminal = [c for c in port.checkpoints if c["terminal"]]
    assert terminal and terminal[-1]["ask_set"] == ["H1"], "the asks are still shown, at the terminal"


def test_the_non_blocking_run_still_hears_answers_typed_while_it_works():
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1" and ask.round == 1:
            return stance_all(ask, "agree", human_input=[{"question": "which tenant owns the cache key?"}])
        if ask.who == "Agent2" and ask.round == 1:
            return stance_all(ask, "agree", second=["H1"])
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, human="non_blocking", off_cycle={"H1": "the tenant is fixed by contract"})
        outcome = await built.run()
        return outcome, built, records

    outcome, built, records = asyncio.run(main())
    question = built.docket.questions["H1"]
    assert (question.state, question.answer) == ("answered", "the tenant is fixed by contract")
    assert [r["answered"] for r in records if r["event"] == "human_answer_off_cycle"] == [["H1"]]
    assert outcome == "consensus", "the answer took the question off the table"


def test_the_non_blocking_terminal_ask_is_asked_on_the_way_out_never_parked():
    """The ceiling in non_blocking is not a stop: the asks are shown, nothing waits, and the run ends on its own reading."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(
                findings=[{"claim": "a bug"}] if ask.who == "Agent1" else [],
                open_questions=["what is the retention policy?"],
            )
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent2":
            # a reasoned objection holds the item open, so the ceiling is what ends the run
            return payload(
                positions=[{"item": item, "stance": "disagree", "evidence": "app.py:9"} for item in shown_items(ask.text)],
                backing="no_preference",
                signoff=True,
            )
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, human="non_blocking", max_rounds=1, seconding=False)
        outcome = await built.run()
        return outcome, built, port

    outcome, built, port = asyncio.run(main())
    assert outcome == "round_limit_reached"
    question = built.docket.questions["H1"]
    assert "unanswered" in question.markers and "unasked" not in question.markers, "shown at the ceiling, never parked"
    assert all(not c["waiting"] for c in port.checkpoints)


def test_the_none_run_asks_nothing_and_marks_the_asks_for_the_report():
    """No human on the run: the asks are never shown, notes still are, and the ledger says `no_human`, never `unasked`."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(
                findings=[{"claim": "a bug"}] if ask.who == "Agent1" else [],
                open_questions=["which tenant owns the key?"] if ask.who == "Agent1" else [],
                notes=["the key lives in env"] if ask.who == "Agent1" else [],
            )
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent2" and ask.round == 1:
            return stance_all(ask, "agree", second=["H1"])
        return stance_all(ask, "agree")

    async def main():
        built, port, records = engine(reply=reply, human="none")
        outcome = await built.run()
        return outcome, built, port, records

    outcome, built, port, _records = asyncio.run(main())
    assert outcome == "consensus"
    question = built.docket.questions["H1"]
    assert question.state == "asked" and "no_human" in question.markers, "the ask is recorded for the report"
    assert "unasked" not in question.markers, "`unasked` is for a human who was never shown it"
    assert all(c["ask_set"] == [] and c["listing"] == [] for c in port.checkpoints), "nothing is ever put in front of the human"
    assert any(c["notes"] == ["N1:note"] for c in port.checkpoints), "the notes are still shown"


def test_the_none_run_marks_every_ask_it_never_showed_even_without_a_terminal():
    """A mid-run showable ask is already the report's, not a future checkpoint's."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return phase0_first(ask.who, "a bug")
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent1":
            return stance_all(ask, "agree", human_input=[{"question": "which tenant owns the cache key?"}], second=[])
        if ask.who == "Agent2":
            return stance_all(ask, "agree", second=["H1"])
        return stance_all(ask, "agree")

    async def main():
        built, port, _records = engine(reply=reply, human="none", max_rounds=2)
        await built.run()
        return built

    built = asyncio.run(main())
    assert "no_human" in built.docket.questions["H1"].markers


def test_the_new_postures_teach_no_stop_machinery_and_the_blocking_one_does():
    """When the run cannot wait, the seats are never handed a stop button — and never told one exists."""
    briefs = {human: Brief(mode="review", project="/project", human=human) for human in ("blocking", "non_blocking", "none")}
    for human, brief in briefs.items():
        texts = (
            brief.phase0(mode="review", goal="g", project="/project"),
            brief.round(mode="review", goal="g", project="/project", me="Agent1", docket="", phase0_summary=""),
            brief.peer_pass("### H1 (on F1)\nwhich tenant wins?\nasked by: Agent1"),
        )
        joined = "\n".join(texts)
        if human == "blocking":
            assert "blocking_for" in joined and "declared stop" in joined, "the default still teaches the stop"
        else:
            for needle in ("blocking_for", "declared stop", "`blocking`"):
                assert needle not in joined, f"{human} teaches {needle!r}"
        # the posture itself is the contract each run states
        assert ("No human is on this run" in joined) == (human == "none")
        assert ("The run never stops for one" in joined) == (human == "non_blocking")


def test_the_non_blocking_plan_split_is_asked_and_the_run_ends_on_its_own_budget():
    """A split that would end the run needs_human instead goes to the human between rounds; no answer, no convergence: the limit ends it."""
    import asyncio

    def reply(ask):
        if ask.kind == "direction":
            return payload(question="which plan do you want?", context="two seats, two orders of work")
        return split_council()(ask)

    async def main():
        built, port, records = engine(reply=reply, mode="plan", max_rounds=6, human="non_blocking")
        outcome = await built.run()
        return outcome, built, port

    outcome, built, port = asyncio.run(main())
    ask = built.docket.direction_ask()
    assert ask is not None and ask.state == "asked"
    assert "unanswered" in ask.markers and "unasked" not in ask.markers, "the split was shown, and never answered"
    assert all(not c["waiting"] for c in port.checkpoints), "the split does not hold the run"
    assert outcome in ("round_limit_reached", "stalemate"), outcome


def test_the_none_run_does_not_mark_a_seconded_ask_held_back():
    """`no_human` is not a held-back reason: a seconded ask on a none-run ends marked no_human, never held back."""
    import asyncio

    def reply(ask):
        if ask.phase == "phase0":
            return payload(
                findings=[{"claim": "a bug"}] if ask.who == "Agent1" else [],
                open_questions=["which tenant owns the key?"] if ask.who == "Agent1" else [],
            )
        if ask.kind == "merge":
            return {"groups": []}
        if ask.who == "Agent2" and ask.round == 1:
            return stance_all(ask, "agree", second=["H1"])
        return stance_all(ask, "agree")

    from mech_council.deliberation.escalations import HELD_BACK, NO_HUMAN

    async def main():
        built, port, records = engine(reply=reply, human="none")
        outcome = await built.run()
        return outcome, built

    outcome, built = asyncio.run(main())
    assert outcome == "consensus"
    markers = built.docket.questions["H1"].markers
    assert NO_HUMAN in markers and not set(HELD_BACK) & set(markers), "the seconded ask is never shown, but never held back either"
