"""`mech-council doctor`: what a run needs here, checked without convening a council."""

from __future__ import annotations

import os
import sys

from .conftest import FAKE_PI, write_project_config


def test_doctor_passes_on_a_working_host(cli, project):
    result = cli(["doctor"], cwd=project)
    assert result.returncode == 0, result.console
    out = result.stdout
    assert "ok:   pi fake-pi 0.0" in out
    assert "ok:   config: " in out
    assert "2 convenable council(s): review, plan" in out
    if sys.platform == "linux":
        assert "ok:   namespaces: unshare(CLONE_NEWUSER|CLONE_NEWNS) works" in out
    assert "ok:   runs root: " in out
    assert "everything a run needs is in place" in out


def test_doctor_fails_when_pi_is_missing(cli, project, ground):
    write_project_config(
        project,
        """\
[councils.review]
mode = "review"

[councils.review.chair]
name = "Chair"

[[councils.review.agents]]
name = "A"

[[councils.review.agents]]
name = "B"
""",
    )
    empty = os.path.join(ground, "empty-bin")
    os.makedirs(empty, exist_ok=True)
    result = cli(["doctor"], cwd=project, env={"PATH": empty})
    assert result.returncode == 1
    assert "fail: pi: no pi CLI found" in result.stdout
    assert "a run would refuse here" in result.stdout


def test_doctor_fails_on_broken_config(cli, project):
    write_project_config(project, "[defaults\nnot toml")
    result = cli(["doctor"], cwd=project)
    assert result.returncode == 1
    assert "fail: config:" in result.stdout
    assert "not valid TOML" in result.stdout


def test_doctor_fails_when_every_council_is_a_template(cli, project):
    write_project_config(
        project,
        """\
[councils._base]
mode = "review"

[councils._base.chair]
name = "Chair"

[[councils._base.agents]]
name = "A"

[[councils._base.agents]]
name = "B"
""",
    )
    result = cli(["doctor"], cwd=project)
    assert result.returncode == 1, result.console
    assert "fail: config:" in result.stdout
    assert "no council convenes" in result.stdout
    assert "a run would refuse here" in result.stdout


def test_doctor_fails_when_the_only_council_does_not_convene(cli, project):
    write_project_config(
        project,
        """\
[councils.solo]
mode = "review"

[councils.solo.chair]
name = "Chair"

[[councils.solo.agents]]
name = "Only"
""",
    )
    result = cli(["doctor"], cwd=project)
    assert result.returncode == 1, result.console
    assert "fail: config:" in result.stdout
    assert "no council convenes" in result.stdout
    assert "a council needs" in result.stdout
    assert "a run would refuse here" in result.stdout


def test_doctor_notes_a_council_that_does_not_convene(cli, project):
    write_project_config(
        project,
        f"""\
[defaults]
pi_cli = "{FAKE_PI}"

[councils.review]
mode = "review"
goal = "Is this right?"

[councils.review.chair]
name = "Chair"

[[councils.review.agents]]
name = "Metic"

[[councils.review.agents]]
name = "Senior"

[councils.solo]
mode = "review"

[councils.solo.chair]
name = "Chair"

[[councils.solo.agents]]
name = "Only"

[councils._base]
mode = "review"

[councils._base.chair]
name = "Chair"

[[councils._base.agents]]
name = "A"

[[councils._base.agents]]
name = "B"
""",
    )
    result = cli(["doctor"], cwd=project)
    assert result.returncode == 0, result.console
    assert "1 convenable council(s): review + 1 template(s)" in result.stdout
    assert "note: [councils.solo] convenes 1 agent(s)" in result.stdout
    assert "a run would refuse here" not in result.stdout


def test_doctor_notes_a_missing_config_without_failing(cli, ground, child_env):
    bare = os.path.join(ground, "bare")
    os.makedirs(bare, exist_ok=True)
    bindir = os.path.join(ground, "bin")
    os.makedirs(bindir, exist_ok=True)
    os.symlink(FAKE_PI, os.path.join(bindir, "pi"))
    result = cli(["doctor"], cwd=bare, env={"PATH": bindir + os.pathsep + child_env["PATH"]})
    assert result.returncode == 0, result.console
    assert "note: config: no council.toml" in result.stdout
    assert "ok:   pi fake-pi 0.0" in result.stdout


def test_doctor_fails_when_the_runs_root_is_inside_the_project(cli, project):
    result = cli(["doctor"], cwd=project, env={"COUNCIL_RUNS_DIR": os.path.join(project, "runs")})
    assert result.returncode == 1
    assert "fail: runs root:" in result.stdout
    assert "inside the project" in result.stdout


def test_doctor_notes_a_nested_council(cli, project):
    result = cli(["doctor"], cwd=project, env={"COUNCIL_RUN_DIR": "a-run"}, scrub=False)
    assert result.returncode == 0, result.console
    assert "refusing to start a nested council" in result.stdout


def test_doctor_help_and_unknown_flag(cli, ground):
    assert cli(["doctor", "--help"], cwd=ground).returncode == 0
    result = cli(["doctor", "--nope"], cwd=ground)
    assert result.returncode == 2
    assert "unknown flag" in result.stderr
