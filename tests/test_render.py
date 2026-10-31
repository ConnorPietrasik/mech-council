"""The renderers: pure functions of (state, context), so they compare exactly."""

from __future__ import annotations

import re

from mech_council.deliberation import dispositions, escalations, state
from mech_council.render import docket as docket_render
from mech_council.render import preview, report_plan, report_review
from mech_council.render.context import Interrupted, ReportContext, RosterRow, minimal_report


def build(mode="review", n=3, goal="g"):
    docket = state.Docket(mode=mode, goal=goal, roster=[f"Agent{i}" for i in range(1, n + 1)])
    docket.chair_name = "Gavel"
    return docket


def item(docket, id_, claim, *, severity="major", raised=("Agent1",), round_=0, evidence="", detail=""):
    entry = state.Item(
        id=id_, claim=claim, evidence=evidence, detail=detail, severity=severity, raised_by=list(raised), opened_round=round_
    )
    docket.add_item(entry)
    return entry


def stance(docket, item_id, agent, value, note="", evidence="", round_=1):
    escalations.record_stance(docket, item_id=item_id, agent=agent, stance=value, note=note, evidence=evidence, round_=round_)


def context(**kw):
    kw.setdefault("project", "/srv/app")
    kw.setdefault("config_path", "/srv/app/council.toml")
    kw.setdefault("council", "review")
    kw.setdefault("outcome", "round_limit_reached")
    kw.setdefault("rounds", 2)
    kw.setdefault("date", "2026-01-01 00:00:00")
    kw.setdefault("sandbox_mib", 512)
    kw.setdefault("member_count", 4)
    kw.setdefault("pi_cli", "/usr/sbin/pi")
    kw.setdefault("pi_version", "pi 1.4.0")
    kw.setdefault(
        "roster",
        [
            RosterRow("Agent1", "Metic", "anthropic/claude-opus-4", 2),
            RosterRow("Agent2", "Senior", "openai/gpt-5.3-codex", 1),
            RosterRow("Agent3", "Coworker", "google/gemini-3.5", 0),
        ],
    )
    kw.setdefault("captions", {"Agent1": "Metic", "Agent2": "Senior", "Agent3": "Coworker"})
    return ReportContext(**kw)


def section(text: str, heading: str) -> str:
    """What one `## ` heading holds, or nothing when the renderer left it out."""
    head = f"## {heading}"
    return "" if head not in text else text[text.index(head) :].split("\n## ")[0]


def listed(text: str, heading: str) -> list[str]:
    """The ask ids a docket section puts in front of the seat."""
    return re.findall(r"^- (H\d+)", section(text, heading), flags=re.MULTILINE)


def ledger(text: str, qid: str) -> str:
    """The report's header line for one ask: its id and every marker it carries."""
    return next(line for line in text.splitlines() if line.startswith(f"### {qid}"))


# ------------------------------------------------------ The single renderer's docket


def test_the_docket_reads_you_and_tags_your_own_entries():
    docket = build()
    item(docket, "F1", "the cache ignores tenants", raised=("Agent1",))
    mine = docket_render.render(docket, "Agent1")
    theirs = docket_render.render(docket, "Agent2")
    assert "[yours]" in mine and "you" in mine  # the prompt text, literally
    assert "[yours]" not in theirs
    assert "Agent1" in theirs, "a peer reads as a bare number, never as a name"


def test_a_closed_item_still_prints_its_id_so_it_can_be_reopened():
    """`agreed`/`rejected` collapse to one *addressable* line."""
    docket = build()
    entry = item(docket, "F1", "a settled claim")
    entry.disposition, entry.closed_round = state.AGREED, 1
    text = docket_render.render(docket, "Agent2")
    assert "F1" in text and "agreed" in text


def test_open_human_requests_print_with_their_effective_class():
    docket = build()
    item(docket, "F1", "a call only a human can make")
    stance(docket, "F1", "Agent1", state.NEEDS_HUMAN, note="which tenant?")
    text = docket_render.render(docket, "Agent2")
    qid = docket.escalations[("F1", "Agent1")].question_id
    assert qid in text, "the open request prints with its H id"
    assert "deferred" in text, "one seat of three is below the bar"


def test_an_answered_question_is_still_shown_to_the_seats_that_must_follow_it():
    """The human's answer is an instruction the council reads, not a line only the report ever gets."""
    docket = build()
    item(docket, "F1", "a call only a human can make")
    stance(docket, "F1", "Agent1", state.NEEDS_HUMAN, note="which tenant key?")
    question = docket.questions[docket.escalations[("F1", "Agent1")].question_id]
    assert "ANSWER" not in docket_render.render(docket, "Agent2")
    question.state, question.answer, question.answer_round = (state.ANSWERED, "the tenant is fixed by contract", 2)
    text = docket_render.render(docket, "Agent2")
    assert "ANSWER (authoritative): the tenant is fixed by contract" in text
    open_asks, answers = text.split("## Human answers", 1)
    assert question.id in answers and "answered, round 2" in answers
    assert question.id not in open_asks, "an answered ask prints as an answer, not as a question still holding the run"


# ------------------------------------------------------ The review report


def test_the_report_is_a_pure_function_of_state_and_context():
    docket = build()
    item(docket, "F1", "the cache ignores tenants", evidence="cache.py:12")
    for agent in docket.roster:
        stance(docket, "F1", agent, state.AGREE)
    first = report_review.render(docket, context())
    assert first == report_review.render(docket, context()), "no clock, no randomness"
    assert "cache.py:12" in first


def test_every_bucket_lands_where_the_spec_puts_it():
    docket = build()
    item(docket, "F1", "everyone agrees")
    for agent in docket.roster:
        stance(docket, "F1", agent, state.AGREE)
    dispositions.closure_pass(docket, 1)
    item(docket, "F2", "two seats clash")
    stance(docket, "F2", "Agent1", state.AGREE)
    stance(docket, "F2", "Agent2", state.DISAGREE, evidence="x.py:1")
    item(docket, "F3", "nobody looked", raised=("Agent3",))
    text = report_review.render(docket, context(outcome="round_limit_reached"))
    agreed_line, disputed_line, unseen_line = (text.index("everyone agrees"), text.index("two seats clash"), text.index("nobody looked"))
    assert text.index("## Consensus findings") < agreed_line
    assert text.index("## Disputed findings") < disputed_line
    assert text.index("## Never vetted") < unseen_line
    assert "F3" in text[text.index("## Never vetted") :]


def test_an_item_nobody_stanced_says_who_never_saw_it():
    docket = build()
    item(docket, "F1", "an unexamined claim", raised=("Agent1",))
    stance(docket, "F1", "Agent1", state.AGREE)
    text = report_review.render(docket, context())
    section = text[text.index("## Never vetted") :]
    assert "F1" in section
    assert "Senior (Agent2)" in section and "Coworker (Agent3)" in section, "the gap ladder: the seats that never saw it are named"


def test_an_item_every_seat_declined_is_reported_as_a_decline_not_a_miss():
    """A survived gap is an inert abstention, so the item is `unheard`."""
    docket = build()
    item(docket, "F1", "nobody would touch it")
    for agent in docket.roster:
        record = state.Stance(stance=state.ABSTAIN, round=1)
        record.declined = True
        docket.stances.setdefault("F1", {})[agent] = record
    text = report_review.render(docket, context(outcome="stalemate"))
    heard = text[text.index("## Not fully vetted") : text.index("## Disputed")]
    assert "nobody would touch it" in heard, "an unheard item is still reported"
    assert "`declined`" in heard and "not vetted by" in heard
    assert "Every item reached every seat" in text, "nothing was unseen"


def test_the_docket_keeps_an_unbacked_ask_where_a_seat_can_back_it():
    docket = build()
    escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, blocking=False, round_=1)
    before = docket_render.render(docket, "Agent2")
    assert listed(before, "Asks not put to the human") == ["H1"] and listed(before, "Questions for the human") == []
    assert "until you `second` it" in before, "a peer is the seat that can back it"
    assert "only a peer can `second` it" in docket_render.render(docket, "Agent1"), "the seat that asks it has nothing to second"
    escalations.second(docket, qid="H1", agent="Agent2", round_=1)
    after = docket_render.render(docket, "Agent2")
    assert listed(after, "Questions for the human") == ["H1"] and listed(after, "Asks not put to the human") == []


def test_the_docket_tells_an_asker_which_peer_answered_it():
    docket = build()
    escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, blocking=False, round_=1)
    escalations.second(docket, qid="H1", agent="Agent2", round_=1)
    escalations.answer_by_seat(docket, qid="H1", agent="Agent3", text="the tokens file names it")
    assert "Agent3: the tokens file names it" in docket_render.render(docket, "Agent1")
    assert "only you can call that answer not enough" in docket_render.render(docket, "Agent1"), "only the raiser reopens it"
    assert "only Agent1 can call it not enough" in docket_render.render(docket, "Agent2"), "and this seat cannot"


def test_the_docket_shows_an_answer_the_fold_stopped_counting():
    """The seat that answered a peer's ask from the workspace now asks it too: the ask comes to the human with the
    answer it was built on still printed, not quietly dropped.
    """
    docket = build()
    escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, round_=1)
    escalations.raise_question(docket, text="which colour file?", asker="Agent2", item_id=None, round_=1)
    escalations.answer_by_seat(docket, qid="H1", agent="Agent2", text="the tokens file names it", round_=1)
    escalations.merge_asks(docket, groups=[{"canonical": "H1", "duplicates": ["H2"]}], round_=2)
    text = docket_render.render(docket, "Agent1")
    assert listed(text, "Questions for the human") == ["H1"]
    assert "the tokens file names it" in text and "answered it and asks it still" in text


def test_the_docket_never_asks_a_seat_to_second_one_the_human_answered():
    """The second-seat advice is for an open ask; this one is settled and only its asker walked off."""
    docket = build()
    escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, blocking=False, round_=1)
    escalations.second(docket, qid="H1", agent="Agent2", round_=1)
    docket.questions["H1"].state, docket.questions["H1"].answer = state.ANSWERED, "brand-primary"
    escalations.retract(docket, qid="H1", agent="Agent1", round_=2)
    text = docket_render.render(docket, "Agent2")
    assert "ANSWER (authoritative): brand-primary" in text
    assert "no second seat" not in text


def test_open_questions_print_with_answer_or_skip_and_their_effective_class():
    docket = build()
    item(docket, "F1", "a call")
    stance(docket, "F1", "Agent1", state.NEEDS_HUMAN, note="which tenant?")
    question = docket.questions[docket.escalations[("F1", "Agent1")].question_id]
    text = report_review.render(docket, context(outcome="needs_human"))
    assert question.id in text and "standing" in text.lower(), "the review report's end state"
    question.state, question.answer, question.answer_round = (state.ANSWERED, "the tenant is fixed", 2)
    answered = report_review.render(docket, context(outcome="consensus"))
    assert "the tenant is fixed" in answered


def test_an_item_less_question_prints_without_an_item_line():
    """A question never stands in for the finding it was raised on."""
    docket = build()
    escalations.raise_question(docket, text="what is the retention policy?", asker="Agent2", item_id=None, blocking=False, round_=1)
    text = report_review.render(docket, context(outcome="round_limit_reached"))
    section = text[text.index("## Open questions") : text.index("## Round-by-round")]
    assert "what is the retention policy?" in section
    assert "item:" not in section, "no item line for an item-less question"


def test_the_report_tells_the_human_which_ask_no_seat_backed():
    docket = build()
    escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, blocking=False, round_=1)
    text = report_review.render(docket, context(outcome="consensus"))
    assert "unseconded" in ledger(text, "H1")
    assert "no second seat" in section(text, "Open questions")


def test_an_ask_a_seat_answered_is_not_reported_as_short_a_second_seat():
    """Two seats asked and a third settled it: telling the human nobody backed it is a lie about the council."""
    docket = build()
    escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, blocking=False, round_=1)
    escalations.second(docket, qid="H1", agent="Agent2", round_=1)
    escalations.answer_by_seat(docket, qid="H1", agent="Agent3", text="the tokens file names it")
    text = report_review.render(docket, context(outcome="consensus"))
    assert "settled" in ledger(text, "H1") and "unseconded" not in ledger(text, "H1")
    assert "the tokens file names it" in section(text, "Open questions")


def test_a_retracted_ask_is_reported_as_dropped_by_named_seat():
    docket = build()
    escalations.raise_question(docket, text="which palette?", asker="Agent1", item_id=None, blocking=False, round_=1)
    escalations.retract(docket, qid="H1", agent="Agent1", round_=2)
    text = report_review.render(docket, context(outcome="consensus"))
    assert "withdrawn" in ledger(text, "H1") and "Metic" in section(text, "Open questions")


def test_a_human_answer_is_attributed_to_the_human_never_to_a_seat():
    docket = build()
    item(docket, "F1", "a call")
    stance(docket, "F1", "Agent2", state.NEEDS_HUMAN, note="which gate?")
    question = docket.questions[docket.escalations[("F1", "Agent2")].question_id]
    question.state, question.answer, question.answer_round = (state.ANSWERED, "the e2e suite", 2)
    text = report_review.render(docket, context())
    line = [row for row in text.splitlines() if "the e2e suite" in row][0]
    assert "Agent" not in line, f"a seat is credited with the human's answer: {line}"
    assert "the e2e suite" in text.split("## Open questions for the human")[1], "it is reported under the human's own section"


def test_the_sandbox_journal_is_rendered_per_member_with_its_markers():
    docket = build()
    item(docket, "F1", "a finding from an artifact")
    docket.items["F1"].markers.append("sandbox_artifact")
    ctx = context(
        journal=[
            {"member": "Metic", "file_base": "seat1-metic", "sandbox_mib": 512, "created": ["src/x.py"], "modified": [], "deleted": []}
        ]
    )
    for agent in docket.roster:
        stance(docket, "F1", agent, state.AGREE)
    dispositions.closure_pass(docket, 1)
    text = report_review.render(docket, ctx)
    assert "seat1-metic" in text and "src/x.py" in text
    assert "sandbox_artifact" in text, "a finding citing a sandbox artifact is marked"


def test_an_in_place_report_never_claims_a_sandbox():
    """Nothing was journaled and nothing was quarantined, so neither section may say otherwise."""
    docket = build()
    entry = item(docket, "F1", "the write failed", evidence="mv: Invalid cross-device link")
    entry.markers.append("sandbox_limit")
    text = report_review.render(docket, context(in_place=True))
    assert "- workspace: in place" in text and "sandbox_mib" not in text, "the header names the shared tree, not a cap"
    assert "## Project writes" in text and "## Sandbox writes" not in text
    assert "git status" in text, "the report says where what the members wrote went"
    assert "## Filesystem refusals" in text and "## Sandbox limits" not in text
    assert "F1" in text[text.index("## Filesystem refusals") :], "the refusal is still reported, under its real name"
    assert "no write journal, no namespaces, no sweep" in text, "the manifest's own description says what it cannot hold"


def test_an_overlay_refusal_prints_under_sandbox_limits_not_as_evidence():
    """An overlay refusal is evidence of the *limit*, never of a finding."""
    docket = build()
    entry = item(
        docket, "F1", "the write failed", raised=("Agent1",), evidence="mkdir '/project/.council-scratch/x': Read-only file system"
    )
    entry.markers.append("sandbox_limit")
    text = report_review.render(docket, context())
    section = text[text.index("## Sandbox limits") : text.index("## Open questions")]
    assert "F1" in section and "Read-only file system" in section


def test_an_aborted_run_still_renders_a_report_naming_the_reason():
    docket = build()
    item(docket, "F1", "a finding the run never finished vetting")
    text = report_review.render(docket, context(outcome="aborted", aborted_reason="Agent2: no answer"))
    assert text.startswith("# Code review")
    assert "aborted" in text and "Agent2: no answer" in text
    assert "a finding the run never finished vetting" in text


def test_a_cut_short_run_leads_with_where_it_stopped_in_every_report():
    cut = Interrupted(
        name="SIGINT",
        elapsed=41.2,
        round=2,
        phase="the gap ladder",
        waiting_on=["H1"],
        in_flight=[{"agent": "Agent1", "elapsed": 9.0, "idle": 1.0, "tool_name": "edit", "last_tool": "path=src/app.py"}],
    )

    docket = build()
    item(docket, "F1", "the guard is missing")
    review = report_review.render(docket, context(outcome="aborted", interrupted=cut))
    head = review.split("## Roster")[0]
    assert "## Cut short by SIGINT" in head and "round 2 · the gap ladder" in head
    assert "it was asking you: H1" in head, "what the run wanted from the human is the first thing it says"
    assert "Metic (Agent1)" in head and "`edit`" in head, "the seat mid-answer is named with what it was doing"

    plan = build(mode="plan")
    plan.backing = {"Agent1": "Agent1", "Agent2": "Agent1", "Agent3": "Agent2"}
    text = report_plan.render(plan, context(mode="plan", outcome="aborted", interrupted=cut), converged="Agent1")
    assert "Cut short by SIGINT" in text.split("## Rules for the implementer")[0]
    assert "not settled" in section(text, "Chosen direction"), "a majority a signal cut short is not a chosen direction"

    sparse = minimal_report(reason="interrupted by SIGINT 41.2 s in (phase 0)", context=context(outcome="aborted", interrupted=cut))
    assert "## Cut short by SIGINT" in sparse.split("# Why nothing ran")[0], "a docket with nothing on it still says where it stood"


def test_the_artifacts_section_names_the_real_files():
    docket = build()
    text = report_review.render(docket, context(), run_dir="/runs/2026-01-01-app")
    for name in ("run.json", "report.md", "events.jsonl", "human_input.json"):
        assert name in text


# --------------------------------------------------------------------------- Plan report


def test_the_plan_report_shows_the_chosen_plan_the_backing_and_the_shrugs():
    docket = build(mode="plan", n=3, goal="ship the migration")
    docket.backing = {"Agent1": "Agent1", "Agent2": "no_preference", "Agent3": "no_preference"}
    docket.agreed_points = [{"point": "the staging", "agents": ["Agent1", "Agent2"]}]
    docket.approaches["Agent1"] = state.Approach(
        agent="Agent1",
        original="land it in two steps",
        stages=[{"state": "the old table is gone", "gate": "the e2e suite", "scope": "src/db", "exceptions": []}],
        rejected_alternatives=["big bang"],
    )
    ctx = context(mode="plan", outcome="consensus", report_name="plan.md", goal="ship the migration")
    text = report_plan.render(docket, ctx)
    assert text.startswith("# Implementation plan")
    assert "rules for the implementer" in text.lower(), "the plan report opens with the rules"
    assert "no_preference" in text.replace("no preference", "no_preference"), "the roster prints the shrug, never 'abstained'"
    assert "abstained" not in text.lower(), "that word belongs to item stances"
    assert "the staging" in text and "big bang" in text
    assert "land it in two steps" in text, "Agent1's plan is the chosen direction"


def test_the_plan_report_prints_deferred_questions_as_named_assumptions():
    """A deferred question touching the direction is a named assumption."""
    docket = build(mode="plan", n=2)
    docket.backing = {"Agent1": "Agent1", "Agent2": "no_preference"}
    docket.approaches["Agent1"] = state.Approach(agent="Agent1", original="do it")
    escalations.raise_question(
        docket, text="is the old table read by anything else?", asker="chair", item_id=None, blocking=False, round_=1
    )
    text = report_plan.render(docket, context(mode="plan", outcome="round_limit_reached"))
    assert "is the old table read by anything else?" in text
    assert "assumption" in text.lower(), "never dropped, never presented as settled"


def test_the_plan_report_prints_the_split_ask_where_the_direction_is_reported():
    """The council's split belongs beside the direction it could not choose, not among the plan's assumptions."""
    docket = build(mode="plan", n=3)
    docket.backing = {"Agent1": "Agent1", "Agent2": "Agent2", "Agent3": "no_preference"}
    ask = escalations.raise_direction_ask(
        docket, text="which plan do you want?", context="two seats, two orders of work", options=["Agent1: a", "Agent2: b"], round_=2
    )
    text = report_plan.render(docket, context(mode="plan", outcome="needs_human"))
    chosen = text.split("## Chosen direction", 1)[1].split("\n## ", 1)[0]
    assert "which plan do you want?" in chosen and "never answered" in chosen
    assumptions = text.split("## Assumptions", 1)[1].split("\n## ", 1)[0]
    assert ask.id not in assumptions, "the split the human was asked about is not printed as a guess the council made"


def test_the_plan_report_carries_no_review_only_sections():
    docket = build(mode="plan")
    text = report_plan.render(docket, context(mode="plan"))
    assert "## Consensus findings" not in text
    assert "Decision points" in text


# --------------------------------------------------------------------------- the digest's one-line preview


def test_a_wrapped_command_previews_as_the_command_and_not_its_cd():
    assert preview.collapse_cd("cd /srv/app && make test") == "make test"
    assert preview.collapse_cd("cd /srv/app; make") == "make"
    assert preview.collapse_cd("make test") == "make test", "a command with no cd is left exactly as it came"
    assert preview.preview({"command": "cd /srv/app && make test"}, project="/srv/app", tool_name="bash") == "make test"
    assert preview.preview({"command": "make test"}, project="/srv/app", tool_name="exec") == "make test"


def test_a_preview_names_the_argument_that_matters_relative_to_the_project():
    assert preview.preview({"path": "/srv/app/src/main.py", "offset": 10}, project="/srv/app") == "path=src/main.py"
    assert preview.preview({"pattern": "tenant"}, project="") == "pattern=tenant"
    assert preview.preview("free-form argument", project="") == "free-form argument"


def test_a_long_preview_keeps_both_ends_because_the_tail_is_the_part_that_bites():
    command = "npm test " + "x" * 400
    shown = preview.preview({"command": command}, project="", tool_name="bash", limit=60)
    assert shown.startswith("npm test xxxx"), "the head still says what was run"
    assert shown.endswith("xxxx") and "…" in shown, "the tail survives, so a long target is still readable"
    assert len(shown) <= 60


# --------------------------------------------------------------------------- the note ledger


def noted(docket, text, by="Agent1", round_=1, item_id=None):
    from mech_council.deliberation import notes

    note, _updated = notes.raise_note(docket, by=by, text=text, item_id=item_id, round_=round_)
    return note


def test_the_docket_shows_peers_the_notes_they_left_for_the_human():
    docket = build()
    noted(docket, "retention is a contract, not a config choice", by="Agent2", item_id="F1")
    for viewer in ("Agent1", "Agent2"):
        body = section(docket_render.render(docket, viewer), "Notes for the human")
        assert "retention is a contract" in body and "on F1" in body, "every seat sees the note and what it is about"


def test_a_retracted_note_leaves_the_docket_a_seat_reads():
    from mech_council.deliberation import notes

    docket = build()
    noted(docket, "retention is a contract")
    notes.retract_note(docket, nid="N1", agent="Agent1", round_=2)
    assert section(docket_render.render(docket, "Agent2"), "Notes for the human") == ""


def test_the_docket_prints_the_notes_that_are_still_news_and_leaves_the_old_ones_to_the_report():
    docket = build()
    noted(docket, "the old one", round_=1)
    noted(docket, "the fresh one", by="Agent2", round_=10)
    docket.round = 10
    body = section(docket_render.render(docket, "Agent1"), "Notes for the human")
    assert "the fresh one" in body and "the old one" not in body


def test_the_review_report_keeps_a_retracted_note_the_human_was_shown():
    from mech_council.deliberation import notes

    docket = build()
    noted(docket, "retention is a contract, not a config choice", item_id="F1")
    noted(docket, "and the lease renews in march", by="Agent2")
    notes.mark_shown(notes.unshown(docket), 0)
    notes.retract_note(docket, nid="N2", agent="Agent2", round_=3, reason="wrong clause")
    body = section(report_review.render(docket, context()), "Notes for the human")
    assert "N1" in body and "F1" in body and "round 0" in body, "what the human already read says which round showed it"
    assert "retracted" in body and "wrong clause" in body
    assert "retention is a contract" in body, "the report is the whole ledger the checkpoint showed once"


def test_a_note_no_checkpoint_showed_still_reaches_the_report():
    docket = build()
    noted(docket, "retention is a contract, not a config choice")
    body = section(report_plan.render(docket, context(), converged="Agent1"), "Notes for the human")
    assert "N1" in body and "retention is a contract" in body, "the report holds the ledger, shown or not"
