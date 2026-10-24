"""§13.3 — §5.3's leniency, and the cases where leniency must stop.

Leniency is scoped to model output: a paraphrase is mapped, a value that maps to nothing
invalidates *its own field* and is never invented by the parser.
"""
from __future__ import annotations

import json

from council.extraction import (map_backing, map_severity, map_stance,
                                parse_comparison, parse_merge_plan, parse_phase0,
                                parse_verdict, parse_watchdog)

KNOWN = {"F1", "F2", "F3"}


def verdict(**payload) -> str:
    return "```json\n" + json.dumps(payload) + "\n```"   # prose-wrapped, as models do


def positions(*entries) -> dict:
    return {"positions": [{"item": i, "stance": s} for i, s in entries]}


def parse(text, **kw):
    kw.setdefault("mode", "review")
    kw.setdefault("me", "Agent1")
    kw.setdefault("known_items", set(KNOWN))
    return parse_verdict(text, **kw)


# --------------------------------------------------------------------------- finding the JSON

def test_a_payload_inside_prose_and_fences_is_found():
    text = ("I looked at the diff.\n\n```json\n"
            + json.dumps(positions(("F1", "agree")))
            + "\n```\nHope that helps!")
    out = parse(text)
    assert not out.invalid and out.data["positions"][0]["item"] == "F1"


def test_a_prose_only_answer_is_invalid_and_owes_a_repair():
    out = parse("The code looks fine to me, no issues.")
    assert out.invalid and out.errors


def test_the_first_balanced_object_wins_and_a_trailing_brace_does_not_swallow_it():
    text = 'noise {"positions": [{"item": "F1", "stance": "agree"}]} trailing }'
    out = parse(text)
    assert out.data["positions"][0]["item"] == "F1"


# --------------------------------------------------------------------------- case and separators

def test_case_and_separators_are_not_identity():
    text = json.dumps({"PoSiTiOnS": [{"ITEM": "F1", "STANCE": "AGREE"}]})
    out = parse(text)
    assert out.data["positions"] == [{"item": "F1", "stance": "agree", "note": "",
                                      "evidence": ""}]


def test_synonyms_land_on_the_canonical_field():
    text = json.dumps({"verdicts": [{"finding": "F2", "position": "disagree",
                                     "reason": "it is guarded", "proof": "x.py:9"}]})
    entry = parse(text).data["positions"][0]
    assert entry == {"item": "F2", "stance": "disagree", "note": "it is guarded",
                     "evidence": "x.py:9"}


# --------------------------------------------------------------------------- stances

def test_stance_paraphrases_are_mapped_and_never_dropped():
    """A dropped stance is a lost dissent (§5.3)."""
    for word, want in [("human", "needs_human"), ("ask_human", "needs_human"),
                       ("neutral", "abstain"), ("skip", "abstain"),
                       ("ask_the_user", "needs_human"), ("no_opinion", "abstain")]:
        assert map_stance(word) == want, word


def test_withdrawing_is_an_abstain_and_never_an_agree():
    for word in ("withdraw", "withdraw_human", "no_human_needed", "drop_escalation"):
        assert map_stance(word) == "abstain", word


def test_an_unknown_stance_invalidates_only_that_position():
    out = parse(verdict(**positions(("F1", "agree"), ("F2", "maybe_later")),
                        signoff=True))
    assert [p["item"] for p in out.data["positions"]] == ["F1"]
    assert out.data["dropped_positions"] == 1
    assert not out.invalid, "the rest of the verdict survives"


def test_empty_positions_is_valid_but_an_all_invalid_array_is_a_repair():
    assert not parse(verdict(positions=[], signoff=True)).errors
    assert not parse(verdict(positions=[])).repaired
    out = parse(verdict(**{"positions": [{"item": "F1", "stance": "whatever"}]}))
    assert out.repaired and out.errors, "present but every entry invalid"


def test_a_position_against_an_unknown_id_is_logged_not_dropped():
    """§5.2.4: it could be the dissent that should have blocked consensus."""
    out = parse(verdict(positions=[{"item": "F9", "stance": "disagree"},
                                   {"item": "F1", "stance": "agree"}]))
    assert out.data["unknown_items"] == ["F9"]
    assert [p["item"] for p in out.data["positions"]] == ["F1"]


def test_a_position_on_a_closed_item_is_kept_and_marked():
    out = parse_verdict(verdict(**positions(("F1", "disagree"))), mode="review",
                        me="Agent1", known_items=set(KNOWN), closed_items={"F1"})
    assert out.data["positions"][0]["on_closed"] is True, "§5.2.3's reopen path owns it"


# --------------------------------------------------------------------------- severity

def test_unknown_severities_degrade_to_minor_and_known_words_map():
    assert map_severity("catastrophic") == "minor"
    assert map_severity("Critical") == "blocker"
    assert map_severity("nit") == "trivial"
    assert map_severity(None) == "minor"


# --------------------------------------------------------------------------- backing (plan)

def test_backing_paraphrases_map_onto_the_three_literals_and_nowhere_else():
    assert map_backing("either", "Agent2") == ("no_preference", False)
    assert map_backing("whatever", "Agent2") == ("no_preference", False)
    assert map_backing("me", "Agent2") == ("Agent2", False)
    assert map_backing("my plan", "Agent3") == ("Agent3", False)
    assert map_backing("Agent 2", "Agent1") == ("Agent2", False)
    assert map_backing("agent:2", "Agent1") == ("Agent2", False)
    assert map_backing("merged: the staged one", "Agent1") == ("merged: the staged one",
                                                               False)


def test_backing_that_maps_to_nothing_invalidates_only_backing():
    """Never defaulted to `no_preference`: a value the parser invented is not a shrug."""
    out = parse_verdict(verdict(**positions(("F1", "agree")), backing="abstain"),
                        mode="plan", me="Agent1", known_items=set(KNOWN))
    assert out.data["backing"] is None
    assert out.data["positions"], "the rest of the verdict stands"
    assert any("backing" in error for error in out.errors)
    for word in ("declined", "abstain", "maybe", ""):
        assert map_backing(word, "Agent1") == (None, True), word


# --------------------------------------------------------------------------- human_input, Q&A

def test_every_verdict_carries_human_input_in_both_modes():
    text = verdict(**positions(("F1", "needs_human")),
                   human_input=[{"question": "which tenant?", "item": "F1",
                                 "blocking": True}])
    for mode in ("review", "plan"):
        out = parse_verdict(text, mode=mode, me="Agent1", known_items=set(KNOWN))
        assert out.data["human_input"] == [{"question": "which tenant?", "item": "F1",
                                            "blocking": True, "context": "",
                                            "options": []}]


def test_an_item_less_human_input_entry_is_a_question_not_an_escalation():
    out = parse(verdict(**positions(("F1", "agree")),
                        human_input=[{"question": "what is the retention policy?"}]))
    assert out.data["human_input"][0]["item"] is None


def test_an_answer_keeps_its_text_and_its_question_id_apart():
    """§5.3: an answer routes on `to` + `Q<n>`; its prose is its own field."""
    out = parse(verdict(**positions(("F1", "agree")),
                        answers=[{"to": "Agent2", "question": "Q9", "answer": "no"}]))
    assert out.data["answers"] == [{"to": "Agent2", "question": "Q9", "text": "no"}]


def test_a_question_to_a_seat_carries_its_text():
    out = parse(verdict(**positions(("F1", "agree")),
                        questions=[{"to": "Agent3", "question": "which gate?"}]))
    assert out.data["questions"] == [{"to": "Agent3", "text": "which gate?"}]


# --------------------------------------------------------------------------- phase 0

def test_phase0_findings_carry_severity_evidence_and_detail():
    text = json.dumps({"findings": [{"title": "the cache ignores tenants",
                                     "impact": "major", "proof": "cache.py:12",
                                     "body": "keys are global"}],
                       "open_questions": ["is a tenant fixed?"]})
    out = parse_phase0(text, mode="review")
    finding = out.data["findings"][0]
    assert finding["claim"] == "the cache ignores tenants"
    assert finding["severity"] == "major" and finding["evidence"] == "cache.py:12"
    assert finding["detail"] == "keys are global"
    assert out.data["open_questions"] == ["is a tenant fixed?"]


def test_phase0_plan_mode_reads_the_approach_schema():
    text = json.dumps({"approach": {"claim": "two steps",
                                    "stages": [{"state": "one", "gate": "green",
                                                "scope": "src", "exceptions": ["tests"]}],
                                    "risks": ["rebase"]}})
    out = parse_phase0(text, mode="plan")
    assert out.data["plan"]["text"] == "two steps"
    assert out.data["plan"]["stages"][0] == {"state": "one", "gate": "green",
                                             "scope": "src", "exceptions": ["tests"]}
    assert out.data["plan"]["risks"] == ["rebase"]


def test_a_plan_with_no_claim_and_no_stages_is_a_repair():
    out = parse_phase0(json.dumps({"approach": {}}), mode="plan")
    assert out.repaired or out.errors


# --------------------------------------------------------------------------- chair payloads

def test_a_merge_plan_is_read_through_the_synonym_table():
    text = json.dumps({"merges": [{"keep": "F1", "dupes": ["F2", "F3"]}],
                       "questions_for_human": [{"question": "which name wins?"}]})
    groups, questions, errors = parse_merge_plan(text)
    assert groups["groups"] == [{"canonical": "F1", "duplicates": ["F2", "F3"]}]
    assert questions[0]["question"] == "which name wins?" and not errors


def test_a_comparison_carries_agreed_conflicts_uncovered_and_questions():
    text = json.dumps({"agreed": [{"point": "the staging"}],
                       "disagreements": [{"claim": "step two",
                                          "positions": [{"agent": "Agent1",
                                                         "claim": "now"},
                                                        {"agent": "Agent2",
                                                         "claim": "later"}]}],
                       "gaps": ["the rollback"],
                       "human": [{"question": "who signs off?"}]})
    data, errors = parse_comparison(text)
    assert not errors
    assert data["agreed"][0]["point"] == "the staging"
    assert data["conflicts"][0]["claim"] == "step two"
    assert data["uncovered"] == ["the rollback"]
    assert data["human_input"][0]["question"] == "who signs off?"


def test_a_watchdog_reply_reads_both_action_spellings():
    """§9: `restart: true` and `action: "keep"` are the same schema, leniently read."""
    text = json.dumps({"decisions": [{"agent": "Senior", "restart": True},
                                     {"agent": "Coworker", "action": "keep",
                                      "reason": "idle is fine"}]})
    decisions, questions, errors = parse_watchdog(text)
    assert not errors and not questions
    assert decisions == [{"agent": "Senior", "action": "restart", "reason": ""},
                         {"agent": "Coworker", "action": "keep",
                          "reason": "idle is fine"}]


def test_a_watchdog_reply_that_is_not_a_decision_list_is_an_error():
    _decisions, _unknown, errors = parse_watchdog("the council looks fine to me")
    assert errors
