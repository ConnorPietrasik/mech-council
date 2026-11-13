"""What a run leaves behind, and what it says while it does."""

from __future__ import annotations

import json
import os

import pytest

from .conftest import DEFAULT_CONFIG, FAKE_PI, no_user_config, write_project_config

pytestmark = [pytest.mark.timeout(200)]

_NOTED = "the retention window is a contract, not a config choice"

SCRIPT = json.dumps(
    [
        {
            "contains": "Phase 0",
            "reply": {
                "findings": [{"claim": "the cache ignores tenants", "severity": "major", "evidence": "src/app.py:1"}],
                "open_questions": ["Which retention policy applies?"],
                "notes": [_NOTED],
            },
        },
        {"contains": "Round ", "reply": {"positions": [{"id": "F1", "stance": "agree", "note": "read the key"}], "sign_off": True}},
        {"contains": "# Deduplicate", "reply": {"groups": [{"canonical": "F1", "duplicates": ["F2"]}], "human_input": []}},
    ]
)


def runs_env(project: str) -> dict:
    return {"COUNCIL_RUNS_DIR": os.path.join(os.path.dirname(project), "runs")}


@pytest.fixture
def finished(cli, project):
    """One converged review run, its artifacts read once. A headless run takes `none`, so the human's lines need the posture stated."""
    write_project_config(project, DEFAULT_CONFIG.format(pi_cli=FAKE_PI).replace('verbosity = "debug"', 'verbosity = "full"'))
    env = dict(runs_env(project), FAKE_PI_SCRIPT=SCRIPT)
    result = cli(["review", "--human", "blocking"], cwd=project, stdin="\n", env=env, timeout=200)
    assert result.returncode == 0, result.console
    return result


# ------------------------------------------------------- run.json, the manifest


def test_run_json_carries_every_field_of_its_two_phases(finished):
    """Created fields at creation, finalize fields at the end — and both are present."""
    created = [
        "created",
        "config_path",
        "config_layers",
        "council",
        "mode",
        "goal",
        "project",
        "run_dir",
        "runs_dir",
        "roster",
        "chair",
        "settings",
        "pi_cli",
        "pi_version",
    ]
    finalized = ["outcome", "rounds", "report", "write_journal", "survivor_pids", "survivor_ns", "finished"]
    manifest = finished.manifest
    for field_name in created + finalized:
        assert field_name in manifest, f"run.json is missing `{field_name}`"
    assert manifest["outcome"] == "consensus"
    assert manifest["report"].endswith("report.md")
    assert manifest["survivor_pids"] == [] and manifest["survivor_ns"] == []


def test_run_json_records_the_per_member_identity_the_sweep_and_report_need(finished):
    """`file_base` plus *every* namespace inode recorded for that member."""
    members = finished.manifest["members"]
    assert sorted(members) == ["chair", "slot01-metic", "slot02-senior"]
    for base, record in members.items():
        assert record["file_base"] == base
        assert record["namespaces"], f"{base} recorded no namespace to sweep"
        assert record["namespace"] == record["namespaces"][-1]
        assert record["kind"] == ("chair" if base == "chair" else "seat")
        assert "prompts" in record, "the per-member prompt count is read from here"
    assert [row["slot"] for row in finished.manifest["roster"]] == [1, 2]


def test_the_write_journal_is_per_member_and_excludes_scratch(finished):
    """Created/modified/deleted per member, `.council-scratch` never listed."""
    journal = finished.manifest["write_journal"]
    assert {row["file_base"] for row in journal} == {"chair", "slot01-metic", "slot02-senior"}
    for row in journal:
        assert {"created", "modified", "deleted", "bytes", "sandbox_mib"} <= set(row)
        for path in row["created"] + row["modified"] + row["deleted"]:
            assert ".council-scratch" not in path


# ------------------------------------------------------ The run directory's artifacts


def test_events_jsonl_is_one_object_per_line_and_covers_the_whole_run(finished):
    kinds = finished.kinds()
    for expected in ("run_started", "phase0_start", "chair_merge", "round_summary", "human_checkpoint", "termination"):
        assert expected in kinds, f"{expected} never happened: {kinds}"
    assert kinds.index("run_started") == 0
    assert kinds[-1] == "termination", kinds[-3:]
    assert all(isinstance(row, dict) and "at" in row for row in finished.events)


def test_human_input_json_holds_every_question_whatever_became_of_it(finished):
    record = json.loads(finished.artifact("human_input.json"))
    assert set(record) == {"questions", "notes", "escalations", "messages"}
    assert record["messages"] == [], "no human is at the keyboard in that run"
    asked = [q for q in record["questions"] if q["askers"]]
    assert asked, "phase 0 raised a question that never reached the record"
    for question in record["questions"]:
        assert question["raised_by"], "the wire record keeps who raised the ask, even after a fold"
    for question in asked:
        for field_name in ("id", "askers", "raised_round", "question", "effective", "state", "answer", "recommendation", "markers"):
            assert field_name in question, f"{question['id']} is missing {field_name}"
        assert not {"item_id", "text"} & set(question), f"{question['id']} carries the docket's field names, not the wire's"
    assert asked[0]["item"] is None, "a phase-0 open question is item-less"


def test_every_note_is_recorded_with_where_it_was_shown(finished):
    notes = json.loads(finished.artifact("human_input.json"))["notes"]
    assert [note["note"] for note in notes] == [_NOTED, _NOTED], "two seats saying one thing is two notes"
    assert {note["seat"] for note in notes} == {"Agent1", "Agent2"}
    # round 0 is a real checkpoint: `null` would mean the human was never shown it
    assert [note["shown_round"] for note in notes] == [0, 0]
    assert all(note["state"] == "standing" for note in notes)


def test_full_transcripts_write_one_verbatim_file_per_member(finished):
    agents = os.path.join(finished.run_dir, "agents")
    written = sorted(os.listdir(agents))
    assert written == ["chair.transcripts.jsonl", "slot01-metic.transcripts.jsonl", "slot02-senior.transcripts.jsonl"], written
    with open(os.path.join(agents, "chair.transcripts.jsonl"), encoding="utf-8") as handle:
        first = json.loads(handle.readline())
    assert "type" in first, "the transcript is the protocol stream, not a summary"


def test_transcripts_off_writes_nothing_until_a_child_produces_stderr(cli, project):
    """Sink rule: `agents/` appears only if a child said something."""
    result = cli(["review"], cwd=project, stdin="\n", env=dict(runs_env(project), FAKE_PI_SCRIPT=SCRIPT), timeout=200)
    assert result.returncode == 0, result.console
    assert not os.path.exists(os.path.join(result.run_dir, "agents")), "`off` wrote transcripts anyway"
    assert "bytes beyond the report" in result.console, "the exit line tallies what the run kept"
    rows = [row for row in result.report().splitlines() if "verbosity:" in row]
    assert len(rows) == 1 and rows[0].endswith("verbosity: debug"), f"nothing was written, so the header names no sink: {rows}"
    sinks = [line for line in result.report().splitlines() if line.startswith("- `agents/")]
    assert not sinks, f"nothing was written under agents/, so the report names no sink there: {sinks}"


def test_the_header_names_a_stderr_only_run_for_what_it_is(cli, project):
    """`off` still writes stderr: the counter saying so has to be one a restart does not lose."""
    entries = json.loads(SCRIPT)
    entries[0]["stderr"] = "a warning from the child\n"
    result = cli(["review"], cwd=project, stdin="\n", env=dict(runs_env(project), FAKE_PI_SCRIPT=json.dumps(entries)), timeout=200)
    assert result.returncode == 0, result.console
    written = sorted(os.listdir(os.path.join(result.run_dir, "agents")))
    assert written and all(name.endswith(".stderr.txt") for name in written), "`off` wrote a sink besides stderr"
    rows = [row for row in result.report().splitlines() if "verbosity:" in row]
    assert len(rows) == 1 and rows[0].endswith("verbosity: debug (stderr only, under `agents/`)"), rows
    sinks = [line for line in result.report().splitlines() if line.startswith("- `agents/")]
    assert len(sinks) == 1 and sinks[0].startswith("- `agents/<file_base>.stderr.txt`"), "the report names the one sink it kept"
    assert "transcripts.jsonl" not in sinks[0], "no stream was written, so no stream bullet"


def test_the_report_header_counts_the_stream_it_used_to_deny(finished):
    """The report is what the human keeps, so it may not call the bytes under `agents/` 'stderr only'."""
    rows = [row for row in finished.report().splitlines() if "verbosity:" in row]
    assert len(rows) == 1, rows
    assert "stderr only" not in rows[0], f"the header denies the verbatim stream it lists two lines later: {rows[0]}"
    assert "bytes" in rows[0], f"the header counts what it kept under `agents/`: {rows[0]}"


# ------------------------------------------------------------- The event stream console


def test_the_console_says_what_a_member_could_touch_before_one_runs(finished):
    """Config, run dir, sandbox shape, transcript level and trust are all lines."""
    console = finished.console
    for wanted in ("config ", "run directory ", "sandbox ", "tmpfs ", "MiB", "verbosity full", "project trust approve"):
        assert wanted in console, f"console line missing: {wanted!r}"
    assert "3 members" in console, "the sandbox line counts N+1, not N"


def test_the_console_prints_each_spawn_with_its_namespace_and_each_ask_with_a_duration(finished):
    console = finished.console
    for name in ("Metic", "Senior", "Chair"):
        assert f"{name} up ·" in console, f"{name} never reported up"
    assert console.count("ns ") >= 3, "every spawn prints the namespace it landed in"
    assert " answered in " in console, "per-ask durations are printed"
    assert "s (round 1)" in console


def test_the_console_names_members_and_the_roster_line_keys_the_names_to_ids(finished):
    """Names are for whoever is watching; `events.jsonl`, `run.json` and `agents/` keep the ids this line is the key for."""
    console = finished.console
    assert "Metic: verdict answered in" in console, "a seat's ask line carries its name"
    assert "asked by Metic, Senior" in console, "even the checkpoint's listing names the asker and nothing more"
    narration = console.split("# Code review", 1)[0]  # the report follows the narration and speaks ids beside names
    keyed = [line for line in narration.splitlines() if "Agent" in line]
    assert len(keyed) == 1 and "Metic (Agent1)" in keyed[0], "the roster line is the one place a name meets its id"


def test_the_console_says_who_declined_a_gap_by_name_only():
    """The event carries the id the report is written with; the line says the name."""
    from mech_council.run import _summary_line

    line = _summary_line("gap_declined", {"member": "Agent2", "items": ["F1", "F2"]}, {"Agent2": "Senior"})
    assert "Senior" in line and "Agent2" not in line and "2 item" in line


def test_the_console_names_a_split_the_way_the_menu_does():
    from mech_council.run import _summary_line

    line = _summary_line("direction_ask", {"directions": ["Agent1", "merged: both"]}, {"Agent1": "Metic"})
    assert "Metic" in line and "Agent1" not in line and "both" in line


def test_the_console_tells_a_dropped_ask_from_the_last_asker_walking_away():
    """`still_asking` empty is the only signal that nobody asks it any more."""
    from mech_council.run import _summary_line

    names = {"Agent1": "Metic", "Agent2": "Senior"}

    def line(askers):
        return _summary_line("question_retracted", {"id": "H1", "seat": "Agent1", "still_asking": askers, "round": 2}, names)

    assert "still asked by Senior" in line(["Agent2"]) and "nobody" not in line(["Agent2"])
    assert "nobody asks it" in line([])


def test_the_console_says_what_a_second_that_bought_nothing_was_up_against():
    """The one event names the answer that actually held the ask back; a moot ask must not be told it was answered."""
    from mech_council.run import _summary_line

    names = {"Agent1": "Metic", "Agent2": "Senior", "Agent3": "Coworker"}

    def line(held, answered_by):
        fields = {"id": "H1", "seat": "Agent3", "askers": ["Agent1", "Agent3"], "held": held, "answered_by": answered_by, "round": 2}
        return _summary_line("question_seconded_but_held_back", fields, names)

    settled = line("settled", ["Agent2"])
    assert "Senior answered it" in settled and "not enough" in settled
    moot = line("moot", [])
    assert "answered" not in moot, "nothing answered this ask"
    assert "stop" in moot


def test_the_console_lines_know_the_run_s_human_posture():
    """`non_blocking` never says the run waits or stops, and `none` never says the human is asked at all."""
    from mech_council.run import _summary_line

    names = {"Agent1": "Metic", "Agent2": "Senior", "Agent3": "Coworker"}
    seconded = {"id": "H1", "seat": "Agent2", "askers": ["Agent1", "Agent2"], "round": 2}
    moot = {"id": "H1", "seat": "Agent3", "askers": ["Agent1", "Agent3"], "held": "moot", "answered_by": [], "round": 2}
    answered = {"id": "H1", "seat": "Agent2", "round": 2}

    def line(event, fields, human):
        return _summary_line(event, fields, names, human=human)

    assert "the human is asked" in line("question_seconded", seconded, "blocking"), "the default still puts the ask to the human"
    assert "stop" in line("question_seconded_but_held_back", moot, "blocking")
    assert "in the human's place" in line("question_answered_by_seat", answered, "blocking")
    for human in ("non_blocking", "none"):
        assert "the human is asked" not in line("question_seconded", seconded, human), human
        assert "asking the human" not in line("direction_ask", {"directions": ["Agent1"]}, human), human
        assert "stop" not in line("question_seconded_but_held_back", moot, human), "no stop machinery to name"
        assert "before the council asks you" not in line("peer_pass_start", {"asks": ["H1"]}, human), human
    assert "keeps working" in line("question_seconded", seconded, "non_blocking")
    assert "keeps working" in line("direction_ask", {"directions": ["Agent1"]}, "non_blocking")
    assert "recorded" in line("question_seconded", seconded, "none")
    assert "no human" in line("direction_ask", {"directions": ["Agent1"]}, "none")
    assert "in the human's place" not in line("question_answered_by_seat", answered, "none")


def test_the_console_names_the_ask_whose_words_survive_a_fold_the_chair_misnamed():
    from mech_council.run import _summary_line

    names = {"Agent1": "Metic", "Agent2": "Senior"}
    fields = {"id": "H2", "into": "H1", "askers": ["Agent1", "Agent2"], "chair_named": "H2", "round": 3}
    named = _summary_line("question_merged", fields, names)
    assert "H1 keeps its words" in named and "the chair named H2" in named
    assert "keeps its words" not in _summary_line("question_merged", {**fields, "chair_named": "H1"}, names)


def test_the_write_journal_names_the_member_rather_than_its_directory(cli, project):
    """The directory is named by `file_base` and lives in `run.json`; the line about it names the member."""
    script = json.loads(SCRIPT)
    script[0]["write"] = [{"path": "seen-by-every-seat.md", "text": "one write per seat"}]
    result = cli(["review"], cwd=project, stdin="\n", env=dict(runs_env(project), FAKE_PI_SCRIPT=json.dumps(script)), timeout=200)
    assert result.returncode == 0, result.console
    journal = [line for line in result.console.splitlines() if line.startswith("journal:")]
    assert any(line.startswith("journal: Metic wrote") for line in journal), f"no journal line naming a member: {journal}"
    assert not any("slot" in line for line in journal), "the line names the member, not the directory it wrote into"
    assert result.manifest["write_journal"], "the record still says which `file_base` wrote"


def test_the_run_ends_by_printing_the_termination_and_the_disk_tally(finished):
    console = finished.console
    assert "outcome: consensus" in console
    assert "bytes beyond the report" in console, "every run says what it kept on disk"
    assert "disposing 3 member(s)" in console
    assert "no survivors" in console, "the sweep reports itself on every run"


def test_a_disabled_watchdog_says_so(cli, project):
    silent = cli(["review"], cwd=project, stdin="\n", env=dict(runs_env(project), FAKE_PI_SCRIPT=SCRIPT), timeout=200)
    assert "watchdog disabled" in silent.console


# -------------------------------------------------------- The aborted run's report


def test_an_aborted_run_still_writes_a_report_that_explains_itself(cli, project):
    """The minimal report names the reason, and `run.json` is still finalized."""
    result = cli(
        ["review"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=json.dumps([{"contains": "", "die": True}])), timeout=200
    )
    assert result.returncode == 1
    assert result.manifest["outcome"] == "aborted"
    assert result.manifest["finished"], "an abort finalizes run.json like any other exit"
    report = result.report()
    assert "aborted" in report.lower()
    assert "# Why nothing ran" in report, "the minimal report explains itself rather than announcing an outcome"
    reason = report.split("# Why nothing ran", 1)[1].strip().splitlines()[0]
    assert len(reason) > 10, f"no reason given: {reason!r}"


# ------------------------------------------------------ The review report's two markers


def _docket_with_item(evidence: str):
    from mech_council.deliberation.state import Docket, Item

    docket = Docket(mode="review", goal="g", roster=["Agent1", "Agent2"])
    docket.add_item(Item(id="F1", claim="the guard is missing", evidence=evidence))
    return docket


def _journal(created: dict[str, list[str]], modified: dict[str, list[str]] | None = None):
    modified = modified or {}
    return [
        {
            "member": base,
            "file_base": base,
            "bytes": 10,
            "sandbox_mib": 32,
            "created": created.get(base, []),
            "modified": modified.get(base, []),
            "deleted": [],
        }
        for base in sorted(set(created) | set(modified))
    ]


def test_an_item_naming_a_file_only_a_member_wrote_is_marked_as_an_artifact(tmp_path):
    """That file is the seat's own artifact and never project evidence."""
    from mech_council.sandbox.journal import mark_sandbox_items

    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    docket = _docket_with_item("src/repro.py:1")
    marked = mark_sandbox_items(docket, _journal({"slot01-metic": ["src/repro.py"]}), project=str(project))
    assert "F1:sandbox_artifact" in marked
    assert docket.items["F1"].artifact_by == "slot01-metic"


def test_an_item_naming_a_file_the_seat_edited_is_project_evidence(tmp_path):
    """`modified` is not `created`: the journal shows the file as modified, not created."""
    from mech_council.sandbox.journal import mark_sandbox_items

    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("x = 1\n")
    docket = _docket_with_item("src/app.py:12")
    journal = _journal({"slot01-metic": []}, {"slot01-metic": ["src/app.py"]})
    assert mark_sandbox_items(docket, journal, project=str(project)) == []
    assert docket.items["F1"].markers == []


def test_a_path_the_lower_layer_also_holds_is_not_an_artifact(tmp_path):
    """A deleted-then-recreated project file is the project's file, not an invention."""
    from mech_council.sandbox.journal import mark_sandbox_items

    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "config.py").write_text("DEBUG = True\n")
    docket = _docket_with_item("src/config.py sets DEBUG")
    assert mark_sandbox_items(docket, _journal({"chair": ["src/config.py"]}), project=str(project)) == []


def test_a_file_two_seats_both_wrote_is_marked_and_owned_by_neither(tmp_path):
    """Naming one of them as the author would be a guess; the marker is still the truth."""
    from mech_council.sandbox.journal import mark_sandbox_items

    project = tmp_path / "proj"
    project.mkdir()
    docket = _docket_with_item("REPRO.md shows it")
    journal = _journal({"slot01-metic": ["REPRO.md"], "slot02-senior": ["REPRO.md"]})
    marked = mark_sandbox_items(docket, journal, project=str(project))
    assert "F1:sandbox_artifact" in marked
    assert docket.items["F1"].artifact_by == ""


def test_an_item_whose_evidence_is_an_overlay_refusal_is_a_limit_not_a_finding(tmp_path):
    """`rename across the view` is the sandbox talking, not the code."""
    from mech_council.sandbox.journal import mark_sandbox_items

    project = tmp_path / "proj"
    project.mkdir()
    docket = _docket_with_item("os.rename failed: [Errno 18] Invalid cross-device link")
    marked = mark_sandbox_items(docket, _journal({}), project=str(project))
    assert "F1:sandbox_limit" in marked
    assert "sandbox_artifact" not in docket.items["F1"].markers


def _report_bits(project, tmp_path):
    """Enough of a run for `run._report` to write into."""
    from mech_council import runjson
    from mech_council.console import Console
    from mech_council.startup import convene

    _, launch = convene(["review", "-p", project], environ=no_user_config(COUNCIL_RUNS_DIR=str(tmp_path / "runs")))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    console = Console(run_dir=str(run_dir), verbosity=launch.council.verbosity)
    manifest = runjson.create(
        str(run_dir / "run.json"),
        launch.council,
        run_dir=str(run_dir),
        runs_dir=str(tmp_path / "runs"),
        project=project,
        orchestrator_pid=os.getpid(),
    )
    return launch.council, console, manifest, run_dir


def test_a_renderer_that_raises_still_leaves_a_report_and_a_finalized_manifest(project, tmp_path):
    """The owed steps are independent — one failing may not cancel the rest."""
    from unittest import mock

    from mech_council import run as run_mod
    from mech_council import runjson
    from mech_council.deliberation.state import Docket, Item

    council, console, manifest, run_dir = _report_bits(project, tmp_path)
    docket = Docket(mode="review", goal="g", roster=["Agent1", "Agent2"])
    docket.add_item(Item(id="F1", claim="a finding", evidence="src/app.py:1"))

    with mock.patch("mech_council.render.report_review.build", side_effect=RuntimeError("the renderer had a bug")):
        path, _text = run_mod._report(council, docket, console, str(run_dir), "consensus", [], manifest, "")

    assert path and os.path.exists(path), "a failed renderer must not cost the report too"
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    assert "renderer failed" in text and "events.jsonl" in text
    events = console.events()
    assert any(e.get("event") == "report_render_failed" for e in events), (
        "the event stream: the failure is an event, not only a console line"
    )

    runjson.finalize(manifest, outcome="consensus", journal=[], survivor_pids=[], survivor_ns=[], rounds=1, report=path)
    with open(str(run_dir / "run.json"), encoding="utf-8") as handle:
        finalized = json.load(handle)
    assert finalized["outcome"] == "consensus", "the manifest still finalizes"


def test_an_html_renderer_that_raises_still_leaves_a_report(project, tmp_path):
    """The page is one of the report's files: its failure degrades that file to markdown, not the run."""
    from unittest import mock

    from mech_council import run as run_mod
    from mech_council.deliberation.state import Docket, Item

    council, console, manifest, run_dir = _report_bits(project, tmp_path)
    council.report_format = "html"
    docket = Docket(mode="review", goal="g", roster=["Agent1", "Agent2"])
    docket.add_item(Item(id="F1", claim="a finding", evidence="src/app.py:1"))

    with mock.patch("mech_council.render.html.render_html", side_effect=RuntimeError("the page renderer had a bug")):
        path, text = run_mod._report(council, docket, console, str(run_dir), "consensus", [], manifest, "")

    assert path.endswith("report.html") and os.path.exists(path), "a failed page renderer must not cost the report too"
    with open(path, encoding="utf-8") as handle:
        assert "a finding" in handle.read(), "the file lands with the markdown the page could not style"
    assert "a finding" in text, "the console copy keeps the run's substance"
    events = console.events()
    assert any(e.get("event") == "report_render_failed" for e in events), (
        "the event stream: the failure is an event, not only a console line"
    )


def test_a_report_that_cannot_be_replaced_into_place_leaves_no_tmp_behind(project, tmp_path):
    from mech_council import run as run_mod
    from mech_council.deliberation.state import Docket, Item

    council, console, manifest, run_dir = _report_bits(project, tmp_path)
    holder = tmp_path / "reported"
    holder.mkdir()
    target = holder / "report.md"
    target.mkdir()  # os.replace cannot land on a directory
    docket = Docket(mode="review", goal="g", roster=["Agent1", "Agent2"])
    docket.add_item(Item(id="F1", claim="a finding", evidence="src/app.py:1"))

    assert run_mod._report(council, docket, console, str(run_dir), "consensus", [], manifest, "", report_at=str(target)) == ("", "")
    assert os.listdir(holder) == ["report.md"], "only the thing that refused the report is left"


def test_a_file_name_that_is_not_text_costs_neither_the_report_nor_the_manifest(project, tmp_path):
    """One odd name must not lose the human the artifacts, nor the byte-true name."""
    from mech_council import run as run_mod
    from mech_council import runjson
    from mech_council.deliberation.state import Docket, Item

    council, console, manifest, run_dir = _report_bits(project, tmp_path)
    docket = Docket(mode="review", goal="g", roster=["Agent1", "Agent2"])
    docket.add_item(Item(id="F1", claim="a finding", evidence="src/app.py:1"))
    journal = [{"file_base": "slot01-metic", "created": ["bad\udcffname"], "modified": [], "deleted": []}]
    console.event("journal_line", artifact=journal[0]["created"][0])

    path, _text = run_mod._report(council, docket, console, str(run_dir), "consensus", journal, manifest, "")
    runjson.finalize(manifest, outcome="consensus", journal=journal, survivor_pids=[], survivor_ns=[], rounds=1, report=path)

    assert path and os.path.exists(path), "the report the human was promised"
    with open(path, encoding="utf-8") as handle:
        assert "bad\\xffname" in handle.read(), "the report names the artifact in a form a person can read"
    raw = (run_dir / "run.json").read_bytes()
    assert b"bad\\udcffname" in raw and raw.isascii(), "the manifest is written, and reads back as the same name"
    assert (run_dir / "events.jsonl").read_bytes().isascii(), "the event that named it wrote too"
    assert sorted(os.listdir(run_dir)) == ["events.jsonl", "report.md", "run.json"], "no write left a tmp behind"
