"""The renderers: pure functions of (state, context), so they compare exactly."""

from __future__ import annotations

from council.deliberation import dispositions, escalations, state
from council.render import docket as docket_render
from council.render import preview, report_plan, report_review
from council.render.context import ReportContext, RosterRow


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


# ------------------------------------------------------ The single renderer's docket


def test_the_docket_reads_you_and_tags_your_own_entries():
    docket = build()
    item(docket, "F1", "the cache ignores tenants", raised=("Agent1",))
    mine = docket_render.render(docket, "Agent1")
    theirs = docket_render.render(docket, "Agent2")
    assert "[yours]" in mine and "you" in mine  # the prompt text, literally
    assert "[yours]" not in theirs
    assert "Agent1" in theirs, "a peer reads as a bare number, never as a name"


def test_the_docket_renderer_cannot_be_handed_names():
    """The bias control is the signature: no parameter of the renderer could carry a name into a seat's docket."""
    import inspect

    assert "names" not in inspect.signature(docket_render.render).parameters


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
