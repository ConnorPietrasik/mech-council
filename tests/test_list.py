"""`mech-council list`: what the config offers, read the way a run would read it."""

from __future__ import annotations

import os

import pytest

from .conftest import write_project_config


def test_list_shows_every_council_with_its_effective_shape(cli, project):
    result = cli(["list"], cwd=project)
    assert result.returncode == 0, result.console
    out = result.stdout
    assert out.index("review · review") < out.index("plan · plan"), "config order"
    assert "goal: Is this right?" in out
    assert "goal: How should we do it?" in out
    assert "roster: Metic, Senior · chair: Chair" in out
    # [defaults] differs from the built-in on exactly these
    assert 'knobs: max_rounds = 2 · watchdog_sec = 0 · verbosity = "debug" · pi_cli = "' in out
    assert f"source: {project}/council.toml" in out


def test_list_shows_an_implement_councils_crew_knobs_and_builder_roles(cli, project):
    """The implement knobs differ from the defaults the way the others do: a build council lists
    its planner and reviewer, a stated fix budget, and a builder seat's non-default role."""
    write_project_config(
        project,
        """\
[councils.planners]
mode = "plan"
goal = "Plan the build"

[councils.planners.chair]
name = "Plan Chair"

[[councils.planners.agents]]
name = "P1"

[[councils.planners.agents]]
name = "P2"

[councils.inspect]
mode = "review"
goal = "Review the stage"

[councils.inspect.chair]
name = "Review Chair"

[[councils.inspect.agents]]
name = "R1"

[[councils.inspect.agents]]
name = "R2"

[councils.build]
mode = "implement"
goal = "Build it"
planner = "planners"
reviewer = "inspect"
fix_budget = 5

[councils.build.chair]
name = "Chair"

[[councils.build.agents]]
name = "Forge"

[[councils.build.agents]]
name = "Wordsmith"
role = "prose"
""",
    )
    result = cli(["list"], cwd=project)
    assert result.returncode == 0, result.console
    out = result.stdout
    assert "build · implement" in out
    assert "roster: Forge, Wordsmith · prose · chair: Chair" in out, "a non-default role is listed with the seat"
    assert 'knobs: fix_budget = 5 · planner = "planners" · reviewer = "inspect" · in_place = true' in out


def test_list_shows_a_fix_councils_crew_knobs(cli, project):
    """The fix knobs list like the implement ones: the review council the findings come from, and a
    stated commit that differs from the default."""
    write_project_config(
        project,
        """\
[councils.inspect]
mode = "review"
goal = "Find the defects"

[councils.inspect.chair]
name = "Review Chair"

[[councils.inspect.agents]]
name = "R1"

[[councils.inspect.agents]]
name = "R2"

[councils.fixit]
mode = "fix"
goal = "Fix the findings"
review = "inspect"
commit = false

[councils.fixit.chair]
name = "Chair"

[[councils.fixit.agents]]
name = "Forge"
""",
    )
    result = cli(["list"], cwd=project)
    assert result.returncode == 0, result.console
    out = result.stdout
    assert "fixit · fix" in out
    assert 'knobs: review = "inspect" · commit = false · in_place = true' in out


def test_list_marks_templates_and_broken_councils(cli, project):
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

[councils.child]
extends = "_base"
goal = "on the base"

[councils.child.chair]
name = "Chair"

[councils.solo]
mode = "review"

[councils.solo.chair]
name = "Chair"

[[councils.solo.agents]]
name = "Only"
""",
    )
    result = cli(["list"], cwd=project)
    assert result.returncode == 0, result.console
    out = result.stdout
    assert "_base · review · template (extended, never convened)" in out
    assert "child · review" in out
    assert "extends: _base" in out
    assert "goal: on the base" in out
    assert "solo · not convenable" in out
    assert "as-is: [councils.solo] convenes 1 agent(s); a council needs" in out


def test_list_shows_both_layers_and_the_replacement_note(cli, project, child_env):
    user = os.path.join(child_env["XDG_CONFIG_HOME"], "council", "council.toml")
    os.makedirs(os.path.dirname(user), exist_ok=True)
    with open(user, "w", encoding="utf-8") as handle:
        handle.write("""\
[councils.review]
mode = "review"
goal = "the user goal"

[councils.review.chair]
name = "Chair"

[[councils.review.agents]]
name = "U1"

[[councils.review.agents]]
name = "U2"

[councils.only-user]
mode = "review"

[councils.only-user.chair]
name = "Chair"

[[councils.only-user.agents]]
name = "U1"

[[councils.only-user.agents]]
name = "U2"
""")
    result = cli(["list"], cwd=project)
    assert result.returncode == 0, result.console
    out = result.stdout
    assert user in out and os.path.join(project, "council.toml") in out, "the header names both files"
    assert "replaces any council of the same name" in out
    assert "goal: Is this right?" in out, "the project's review beats the user's"
    assert "the user goal" not in out
    assert "only-user" in out


def test_list_with_no_config_refuses(cli, ground):
    bare = os.path.join(ground, "bare")
    os.makedirs(bare, exist_ok=True)
    result = cli(["list"], cwd=bare)
    assert result.returncode == 1
    assert "no council.toml" in result.stderr


def test_list_help_and_unknown_flag(cli, ground):
    assert cli(["list", "--help"], cwd=ground).returncode == 0
    result = cli(["list", "--rounds", "3"], cwd=ground)
    assert result.returncode == 2
    assert "unknown flag" in result.stderr
    result = cli(["list", "review"], cwd=ground)
    assert result.returncode == 2
    assert "accepts no positionals" in result.stderr


def test_list_takes_project_and_config_flags(cli, project, ground):
    assert cli(["list", "--project", project], cwd=ground).returncode == 0
    result = cli(["list", "-c", os.path.join(project, "council.toml")], cwd=ground)
    assert result.returncode == 0, result.console
    assert f"source: {os.path.join(project, 'council.toml')}" in result.stdout


@pytest.mark.parametrize("name", ["list", "doctor"])
def test_council_names_list_and_doctor_are_reserved(cli, project, name):
    write_project_config(
        project,
        f"""\
[councils.{name}]
mode = "review"

[councils.{name}.chair]
name = "Chair"

[[councils.{name}.agents]]
name = "A"

[[councils.{name}.agents]]
name = "B"
""",
    )
    result = cli([name], cwd=project)
    assert result.returncode == 1
    assert "reserved" in result.console
