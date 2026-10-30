"""The extraction suite — the leniency owed to model prose, and the cases where it must stop."""

from __future__ import annotations

import json

from mech_council.extraction import (
    map_backing,
    map_severity,
    map_stance,
    parse_ask_merge,
    parse_comparison,
    parse_direction,
    parse_merge_plan,
    parse_phase0,
    parse_verdict,
    parse_watchdog,
)

KNOWN = {"F1", "F2", "F3"}


def verdict(**payload) -> str:
    return "```json\n" + json.dumps(payload) + "\n```"  # prose-wrapped, as models do


def positions(*entries) -> dict:
    return {"positions": [{"item": i, "stance": s} for i, s in entries]}


def parse(text, **kw):
    kw.setdefault("mode", "review")
    kw.setdefault("me", "Agent1")
    kw.setdefault("known_items", set(KNOWN))
    return parse_verdict(text, **kw)


# --------------------------------------------------------------------------- finding the JSON


def test_a_payload_inside_prose_and_fences_is_found():
    text = "I looked at the diff.\n\n```json\n" + json.dumps(positions(("F1", "agree"))) + "\n```\nHope that helps!"
    out = parse(text)
    assert not out.invalid and out.data["positions"][0]["item"] == "F1"


def test_a_prose_only_answer_is_invalid_and_owes_a_repair():
    out = parse("The code looks fine to me, no issues.")
    assert out.invalid and out.errors


def test_the_first_balanced_object_wins_and_a_trailing_brace_does_not_swallow_it():
    text = 'noise {"positions": [{"item": "F1", "stance": "agree"}]} trailing }'
    out = parse(text)
    assert out.data["positions"][0]["item"] == "F1"


def test_a_brace_left_open_in_the_prose_does_not_doom_the_object_after_it():
    out = parse("the `{` character opens an object.\n" + json.dumps(positions(("F1", "agree"))))
    assert not out.invalid and out.data["positions"][0]["item"] == "F1"


def test_an_answer_cut_off_mid_object_owes_a_repair_and_never_becomes_the_object_nested_in_it():
    """A truncated reply leaves a complete position nested in it, which parses as "nothing to say" — the one reading
    that must never be taken, because the member's whole verdict is gone and the round would carry on without it."""
    out = parse(json.dumps(positions(("F1", "agree"), ("F2", "disagree")))[:-1])
    assert out.invalid and out.errors


# --------------------------------------------------------------------------- case and separators


def test_case_and_separators_are_not_identity():
    text = json.dumps({"PoSiTiOnS": [{"ITEM": "F1", "STANCE": "AGREE"}]})
    out = parse(text)
    assert out.data["positions"] == [{"item": "F1", "stance": "agree", "note": "", "evidence": ""}]


def test_synonyms_land_on_the_canonical_field():
    text = json.dumps({"verdicts": [{"finding": "F2", "position": "disagree", "reason": "it is guarded", "proof": "x.py:9"}]})
    entry = parse(text).data["positions"][0]
    assert entry == {"item": "F2", "stance": "disagree", "note": "it is guarded", "evidence": "x.py:9"}


# --------------------------------------------------------------------------- stances


def test_stance_paraphrases_are_mapped_and_never_dropped():
    """A dropped stance is a lost dissent."""
    for word, want in [
        ("human", "needs_human"),
        ("ask_human", "needs_human"),
        ("neutral", "abstain"),
        ("skip", "abstain"),
        ("ask_the_user", "needs_human"),
        ("no_opinion", "abstain"),
    ]:
        assert map_stance(word) == want, word


def test_withdrawing_is_an_abstain_and_never_an_agree():
    for word in ("withdraw", "withdraw_human", "no_human_needed", "drop_escalation"):
        assert map_stance(word) == "abstain", word


def test_an_unknown_stance_invalidates_only_that_position():
    out = parse(verdict(**positions(("F1", "agree"), ("F2", "maybe_later")), signoff=True))
    assert [p["item"] for p in out.data["positions"]] == ["F1"]
    assert out.data["dropped_positions"] == 1
    assert not out.invalid, "the rest of the verdict survives"


def test_empty_positions_is_valid_but_an_all_invalid_array_is_a_repair():
    assert not parse(verdict(positions=[], signoff=True)).errors
    assert not parse(verdict(positions=[])).repaired
    out = parse(verdict(positions=[{"item": "F1", "stance": "whatever"}]))
    assert out.repaired and out.errors, "present but every entry invalid"


def test_a_position_against_an_unknown_id_is_logged_not_dropped():
    """It could be the dissent that should have blocked consensus."""
    out = parse(verdict(positions=[{"item": "F9", "stance": "disagree"}, {"item": "F1", "stance": "agree"}]))
    assert out.data["unknown_items"] == ["F9"]
    assert [p["item"] for p in out.data["positions"]] == ["F1"]


def test_a_position_on_a_closed_item_is_kept_and_marked():
    out = parse_verdict(verdict(**positions(("F1", "disagree"))), mode="review", me="Agent1", known_items=set(KNOWN), closed_items={"F1"})
    assert out.data["positions"][0]["on_closed"] is True, "the reopen path owns it"


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
    assert map_backing("merged: the staged one", "Agent1") == ("merged: the staged one", False)


def test_backing_that_maps_to_nothing_invalidates_only_backing():
    """Never defaulted to `no_preference`: a value the parser invented is not a shrug."""
    out = parse_verdict(verdict(**positions(("F1", "agree")), backing="abstain"), mode="plan", me="Agent1", known_items=set(KNOWN))
    assert out.data["backing"] is None
    assert out.data["positions"], "the rest of the verdict stands"
    assert any("backing" in error for error in out.errors)
    for word in ("declined", "abstain", "maybe", ""):
        assert map_backing(word, "Agent1") == (None, True), word


# --------------------------------------------------------------------------- human_input, Q&A


def test_every_verdict_carries_human_input_in_both_modes():
    text = verdict(**positions(("F1", "needs_human")), human_input=[{"question": "which tenant?", "item": "F1", "blocking": True}])
    for mode in ("review", "plan"):
        out = parse_verdict(text, mode=mode, me="Agent1", known_items=set(KNOWN))
        assert out.data["human_input"] == [{"question": "which tenant?", "item": "F1", "blocking": True, "context": "", "options": []}]


def test_an_item_less_human_input_entry_is_a_question_not_an_escalation():
    out = parse(verdict(**positions(("F1", "agree")), human_input=[{"question": "what is the retention policy?"}]))
    assert out.data["human_input"][0]["item"] is None


def test_an_answer_keeps_its_text_and_its_question_id_apart():
    """An answer routes on `to` + `Q<n>`; its prose is its own field."""
    out = parse(verdict(**positions(("F1", "agree")), answers=[{"to": "Agent2", "question": "Q9", "answer": "no"}]))
    assert out.data["answers"] == [{"to": "Agent2", "question": "Q9", "text": "no"}]


def test_a_question_to_a_seat_carries_its_text():
    out = parse(verdict(**positions(("F1", "agree")), questions=[{"to": "Agent3", "question": "which gate?"}]))
    assert out.data["questions"] == [{"to": "Agent3", "text": "which gate?"}]


def test_the_verdict_names_who_backs_an_ask_who_drops_it_and_who_answers_it():
    """The ask fields are ids a seat reads off the docket, so the reader normalizes what it types."""
    out = parse(
        verdict(
            **positions(("F1", "agree")),
            second="h1",
            Retract=["#H2", "H2", "  "],
            answer_question=[{"id": "H3", "answer": "config names it"}],
            insisted=["h4"],
        )
    )
    assert out.data["second"] == ["H1"]
    assert out.data["retract"] == ["H2"]
    assert out.data["answer_question"] == [{"id": "H3", "text": "config names it"}]
    assert out.data["insist"] == ["H4"]


def test_the_verdict_names_the_asks_it_wants_the_run_to_wait_for():
    """The stop is declared per seat, so the ids it names are normalized like every other ask field."""
    out = parse(verdict(**positions(("F1", "agree")), blocking_for=["h1", "#H2", "H2", "  "]))
    assert out.data["blocking_for"] == ["H1", "H2"]
    spelled = parse(verdict(**positions(("F1", "agree")), blocking=["H3"]))
    assert spelled.data["blocking_for"] == ["H3"], "`blocking` at the top level is the stop, not the entry's flag"


def test_an_ask_answer_written_as_a_bare_object_still_names_its_ask():
    out = parse(verdict(**positions(("F1", "agree")), answer_question={"question_id": "H1", "text": "the tokens file"}))
    assert out.data["answer_question"] == [{"id": "H1", "text": "the tokens file"}]


def test_an_ask_answer_can_name_its_ask_where_its_siblings_put_their_prose():
    """`question` is the prose key everywhere else, so a seat reaches for it even when the answer went to `answer`."""
    named = parse(verdict(**positions(("F1", "agree")), answer_question=[{"question": "h2", "answer": "the tokens file"}]))
    assert named.data["answer_question"] == [{"id": "H2", "text": "the tokens file"}]
    prose = parse(verdict(**positions(("F1", "agree")), answer_question=[{"question": "which palette?", "answer": "the tokens file"}]))
    assert prose.data["answer_question"] == [], "a sentence is not an ask id, and inventing one would answer the wrong ask"


def test_open_questions_read_whether_written_as_lines_or_as_objects():
    out = parse(verdict(**positions(("F1", "agree")), open_questions=[{"question": "which palette?"}, "who owns the key?"]))
    assert out.data["open_questions"] == ["which palette?", "who owns the key?"]


def test_notes_read_whether_written_as_lines_or_objects_in_both_modes():
    text = verdict(**positions(("F1", "agree")), notes=["retention is a contract", {"note": "the key lives in env", "item": "F1"}])
    for mode in ("review", "plan"):
        out = parse_verdict(text, mode=mode, me="Agent1", known_items=set(KNOWN))
        assert out.data["notes"] == [
            {"text": "retention is a contract", "item": None},
            {"text": "the key lives in env", "item": "F1"},
        ]


def test_a_note_that_says_nothing_is_dropped_and_owes_no_repair():
    out = parse(verdict(**positions(("F1", "agree")), notes=["", "   ", 7, {"item": "F1"}, {"note": "kept"}]))
    assert out.data["notes"] == [{"text": "kept", "item": None}]
    assert not out.errors and not out.invalid and not out.repaired


def test_retract_note_names_the_note_ledger_and_normalizes_like_an_ask_id():
    out = parse(verdict(**positions(("F1", "agree")), retract_note=["n1", "#N2", "  "]))
    assert out.data["retract_note"] == [{"id": "N1", "reason": ""}, {"id": "N2", "reason": ""}]


def test_a_retraction_carries_the_reason_the_human_is_told():
    """`note` is a synonym of `reason`: under it an id names the note and prose says what was wrong with it."""
    out = parse(
        verdict(
            **positions(("F1", "agree")),
            retract_note=[
                {"id": "n1", "why": "the contract says 90 days"},
                {"note": "N2"},
                {"note": "N3", "reason": "wrong clause"},
                {"id": "N4", "note": "the clause moved, it is not 90 days"},
                {"id": "N5", "comment": "I never read the contract"},
            ],
        )
    )
    assert out.data["retract_note"] == [
        {"id": "N1", "reason": "the contract says 90 days"},
        {"id": "N2", "reason": ""},
        {"id": "N3", "reason": "wrong clause"},
        {"id": "N4", "reason": "the clause moved, it is not 90 days"},
        {"id": "N5", "reason": "I never read the contract"},
    ]


def test_a_top_level_note_is_a_note_while_a_finding_s_is_still_its_detail():
    """`notes` is a synonym of a finding's `detail`; the two are read out of different tables."""
    payload = json.dumps({"findings": [{"claim": "x", "notes": "keys are global"}], "notes": ["the human should know"]})
    out = parse_phase0(payload, mode="review")
    assert out.data["findings"][0]["detail"] == "keys are global"
    assert out.data["notes"] == [{"text": "the human should know", "item": None}]


def test_phase_0_reads_notes_and_has_nothing_to_retract():
    out = parse_phase0(json.dumps({"findings": [], "notes": [{"note": "env holds the key"}], "retract_note": ["n1"]}), mode="review")
    assert out.data["notes"] == [{"text": "env holds the key", "item": None}]
    assert out.data["retract_note"] == [], "phase 0 mints the first notes: it reads no retraction, and the field keeps the round's shape"


# --------------------------------------------------------------------------- phase 0


def test_phase0_findings_carry_severity_evidence_and_detail():
    text = json.dumps(
        {
            "findings": [{"title": "the cache ignores tenants", "impact": "major", "proof": "cache.py:12", "body": "keys are global"}],
            "open_questions": ["is a tenant fixed?"],
        }
    )
    out = parse_phase0(text, mode="review")
    finding = out.data["findings"][0]
    assert finding["claim"] == "the cache ignores tenants"
    assert finding["severity"] == "major" and finding["evidence"] == "cache.py:12"
    assert finding["detail"] == "keys are global"
    assert out.data["open_questions"] == ["is a tenant fixed?"]


def test_phase_0_reads_human_input_with_its_context_and_options():
    """The phase-0 brief asks the seat for the human's menu; extraction is what keeps that brief from being a lie."""
    payload = json.dumps(
        {
            "findings": [],
            "human_input": [{"question": "which way?", "item": "F1", "context": "no caller passes a tenant", "options": ["a", "b", "a"]}],
        }
    )
    out = parse_phase0(payload, mode="review")
    assert out.data["human_input"] == [
        {"question": "which way?", "item": "F1", "blocking": False, "context": "no caller passes a tenant", "options": ["a", "b", "a"]}
    ]


def test_phase0_plan_mode_reads_the_approach_schema():
    text = json.dumps(
        {
            "approach": {
                "claim": "two steps",
                "stages": [{"state": "one", "gate": "green", "scope": "src", "exceptions": ["tests"]}],
                "risks": ["rebase"],
            }
        }
    )
    out = parse_phase0(text, mode="plan")
    assert out.data["plan"]["text"] == "two steps"
    assert out.data["plan"]["stages"][0] == {"state": "one", "gate": "green", "scope": "src", "exceptions": ["tests"]}
    assert out.data["plan"]["risks"] == ["rebase"]


def test_a_plan_with_no_claim_and_no_stages_is_a_repair():
    out = parse_phase0(json.dumps({"approach": {}}), mode="plan")
    assert out.repaired and out.errors == ["the plan carries no stages"], "the repair asks for the one thing missing, by name"


# --------------------------------------------------------------------------- chair payloads


def test_a_merge_plan_is_read_through_the_synonym_table():
    text = json.dumps({"merges": [{"keep": "F1", "dupes": ["F2", "F3"]}]})
    groups, errors = parse_merge_plan(text)
    assert groups is not None and not errors, "the synonym table is what makes this parse"
    assert groups["groups"] == [{"canonical": "F1", "duplicates": ["F2", "F3"]}]


def test_the_ask_fold_reads_the_chair_s_ids_however_it_typed_them():
    groups, errors = parse_ask_merge('{"groups": [{"canonical": "h1", "duplicates": "H2 #h2 h3"}]}')
    assert not errors and groups == {"groups": [{"canonical": "H1", "duplicates": ["H2", "H3"]}]}
    empty, _ = parse_ask_merge(json.dumps({"groups": [{"canonical": "F1", "duplicates": ["H2"]}]}))
    assert empty == {"groups": []}, "an item id names no ask, so the group is dropped"


def test_no_publication_pass_carries_a_question_from_the_chair():
    """A question the chair raised would reach the human with no evidence behind it and nobody to challenge it."""
    merge, _ = parse_merge_plan(json.dumps({"groups": [], "human_input": [{"question": "which name wins?"}]}))
    comparison, _ = parse_comparison(json.dumps({"agreed": [], "human_input": [{"question": "who signs off?"}]}))
    decisions, _ = parse_watchdog(json.dumps({"decisions": [], "human_input": [{"question": "who is wedged?"}]}))
    assert merge is not None and comparison is not None and decisions == []
    assert "questions" not in merge and "human_input" not in comparison


def test_the_direction_reply_is_a_sentence_about_a_split_the_run_found():
    wording, errors = parse_direction(json.dumps({"question": "which cache owns the key?", "context": "two plans, one gate"}))
    assert wording == {"question": "which cache owns the key?", "context": "two plans, one gate"} and not errors
    # The menu is the run's, one line per direction the council held, so a reply can offer none of its own.
    assert parse_direction(json.dumps({"context": "no question here"}))[1], "a wording without a question is unreadable"


def test_a_comparison_carries_agreed_conflicts_and_uncovered():
    text = json.dumps(
        {
            "agreed": [{"point": "the staging"}],
            "disagreements": [
                {"claim": "step two", "positions": [{"agent": "Agent1", "claim": "now"}, {"agent": "Agent2", "claim": "later"}]}
            ],
            "gaps": ["the rollback"],
            "human": [{"question": "who signs off?"}],
        }
    )
    data, errors = parse_comparison(text)
    assert data is not None and not errors
    assert data["agreed"][0]["point"] == "the staging"
    assert data["conflicts"][0]["claim"] == "step two"
    assert data["uncovered"] == ["the rollback"]


def test_a_watchdog_reply_reads_both_action_spellings():
    """`restart: true` and `action: "keep"` are the same schema, leniently read."""
    text = json.dumps(
        {"decisions": [{"agent": "Senior", "restart": True}, {"agent": "Coworker", "action": "keep", "reason": "idle is fine"}]}
    )
    decisions, errors = parse_watchdog(text)
    assert not errors
    assert decisions == [
        {"agent": "Senior", "action": "restart", "reason": ""},
        {"agent": "Coworker", "action": "keep", "reason": "idle is fine"},
    ]


def test_a_watchdog_reply_that_is_not_a_decision_list_is_an_error():
    _decisions, errors = parse_watchdog("the council looks fine to me")
    assert errors
