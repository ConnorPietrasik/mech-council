"""In place: no namespaces, no journal, and one project every seat shares. Nothing here needs a sandbox."""

from __future__ import annotations

import json
import os

import pytest

from mech_council.process.spawn import member_env
from mech_council.prompts import common

pytestmark = [pytest.mark.timeout(180)]


def runs_env(project: str) -> dict:
    return {"COUNCIL_RUNS_DIR": os.path.join(os.path.dirname(project), "runs")}


# ------------------------------------------------------------- the run, end to end


def test_in_place_runs_the_council_and_names_what_it_gives_up(cli, project):
    result = cli(["review", "--in-place"], cwd=project, env=runs_env(project), timeout=150)
    assert result.returncode == 0, result.console
    console = result.console
    assert "--in-place" in console and "in place · members share" in console, console
    assert "· ns " not in console, "no member prints a namespace it never left"
    assert "no namespace sweep" in console, "teardown says which sweep did not run"
    assert result.manifest["settings"]["in_place"] is True
    assert result.manifest["namespace_sweep"] == "none (in place)"
    assert {record["namespace"] for record in result.manifest["members"].values()} == {None}
    assert result.manifest["write_journal"] == []


def test_an_in_place_member_writes_where_its_peers_can_see_it(cli, project):
    """The proof that nothing stands between a seat and the project, and that the report says so rather than
    printing a journal the run never read."""
    script = json.dumps([{"contains": "Phase 0", "write": [{"path": "phase0-note.md", "text": "seen by every seat"}]}])
    result = cli(["review", "--in-place"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    with open(os.path.join(project, "phase0-note.md"), encoding="utf-8") as handle:
        assert handle.read() == "seen by every seat", "the write landed in the project, where a peer reads it"
    assert not os.path.exists(os.path.join(project, ".council-scratch")), "in place, scratch lives outside the tree"
    report = result.report()
    assert "## Project writes" in report and "## Sandbox writes" not in report
    assert "- workspace: in place" in report and "sandbox_mib" not in report
    assert result.manifest["write_journal"] == [], "the write is real, and the run still journaled nothing"


def test_the_scratch_of_each_seat_is_its_own_directory_under_the_run(cli, project):
    result = cli(["review", "--in-place"], cwd=project, env=runs_env(project), timeout=150)
    assert result.returncode == 0, result.console
    slots = os.path.join(os.path.dirname(result.run_dir), "sandbox", os.path.basename(result.run_dir))
    assert sorted(os.listdir(slots)) == sorted(result.manifest["members"]), "one slot per member, chair included"
    for seat in result.manifest["members"]:
        assert os.path.isdir(os.path.join(slots, seat, "scratch")), f"{seat} has no scratch"


# ------------------------------------------------------- the two things in place teaches


def test_the_in_place_member_env_carries_no_launcher_paths():
    env = member_env(
        base={"COUNCIL_LAUNCH_PROJECT": "/left/over/from/another/run", "PATH": "/bin"},
        run_dir="/runs/run1",
        run_path="/runs/run1",
        project="/proj",
        slot_dir="/runs/sandbox/run1/seat1-a",
        runs_root="/runs",
        tmpdir="/runs/sandbox/run1/seat1-a/scratch",
        in_place=True,
    )
    assert env["TMPDIR"] == "/runs/sandbox/run1/seat1-a/scratch"
    assert not [key for key in env if key.startswith("COUNCIL_LAUNCH_")], "in place there is no launcher to instruct"
    assert env["COUNCIL_RUN_PATH"] == "/runs/run1", "the nested-council guard still travels with the member"
    sandboxed = member_env(base={}, run_dir="/r", run_path="/r", project="/proj", slot_dir="/s/seat1-a", runs_root="/runs")
    assert sandboxed["TMPDIR"] == "/proj/.council-scratch"
    assert sandboxed["COUNCIL_LAUNCH_SLOT_DIR"] == "/s/seat1-a"


def test_the_in_place_briefing_never_promises_a_private_copy():
    for text in (
        common.phase0_briefing(mode="review", goal="g", project="/p", in_place=True),
        common.round_briefing(mode="review", goal="g", project="/p", me="Agent1", docket="", phase0_summary="", in_place=True),
    ):
        assert text.strip()
        for claim in ("private", "invisible to them", "overlay", ".council-scratch"):
            assert claim not in text, f"the briefing still teaches {claim!r}, which in place is false"
