"""`mech-council init`: the starter user config, where it lands, and that it convenes."""

from __future__ import annotations

import json
import os
import tomllib

import pytest

from mech_council.init_config import STARTER

from .conftest import FAKE_PI, canary_entries, write_project_config


def user_config_path(config_home: str) -> str:
    return os.path.join(config_home, "council", "council.toml")


def bare_project(ground: str) -> str:
    """A project with source in it and no council.toml of its own."""
    path = os.path.join(ground, "bare")
    os.makedirs(os.path.join(path, "src"), exist_ok=True)
    with open(os.path.join(path, "hello.py"), "w", encoding="utf-8") as handle:
        handle.write("print('hello')\n")
    return path


@pytest.mark.parametrize(
    "env, extra_scrub, expected_at",
    [
        # $COUNCIL_CONFIG wins when it is set at all …
        ({"COUNCIL_CONFIG": "custom.toml"}, (), "custom.toml"),
        # … relative or not, relative against the invoking cwd …
        ({"COUNCIL_CONFIG": "rel.toml"}, (), "rel.toml"),
        # … unset → $XDG_CONFIG_HOME/council/council.toml …
        (None, (), os.path.join("xdg-config", "council", "council.toml")),
        # … unset and no XDG_CONFIG_HOME → the home default
        (None, ("XDG_CONFIG_HOME",), None),
    ],
)
def test_the_starter_lands_at_the_user_location(cli, child_env, ground, env, extra_scrub, expected_at):
    xdg = child_env["XDG_CONFIG_HOME"]
    home = os.path.join(ground, "home")
    expected = os.path.join(ground, expected_at) if expected_at else user_config_path(os.path.join(home, ".config"))
    full_env = dict(env or {})
    if expected_at is None:
        full_env["HOME"] = home
    result = cli(["init"], cwd=ground, env=full_env or None, extra_scrub=extra_scrub)
    assert result.returncode == 0, result.console
    assert f"wrote {expected}" in result.stdout, "the narration names the file it wrote"
    with open(expected, encoding="utf-8") as handle:
        assert handle.read() == STARTER
    for decoy in {user_config_path(xdg), user_config_path(home), os.path.join(ground, "custom.toml"), os.path.join(ground, "rel.toml")}:
        if decoy != expected:
            assert not os.path.exists(decoy), f"nothing but the target may exist: {decoy}"


def test_init_refuses_with_no_user_location(cli, child_env, ground):
    result = cli(["init"], cwd=ground, extra_scrub=("XDG_CONFIG_HOME", "HOME"))
    assert result.returncode == 1
    assert "no user config location" in result.stderr
    assert not os.path.exists(user_config_path(child_env["XDG_CONFIG_HOME"]))


def test_the_written_file_is_a_default_provider_starter(cli, child_env, ground):
    assert cli(["init"], cwd=ground).returncode == 0
    path = user_config_path(child_env["XDG_CONFIG_HOME"])
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    assert set(data["councils"]) == {"review", "plan", "build", "fixit"}
    for table in data["councils"].values():
        chair = table["chair"]
        assert chair.get("name")
        agents = table["agents"]
        assert len(agents) >= 1
        for seat in [chair, *agents]:  # the whole roster: default provider, nothing pinned
            assert "provider" not in seat and "model" not in seat
    assert data["councils"]["build"]["planner"] == "plan" and data["councils"]["build"]["reviewer"] == "review"
    assert data["councils"]["fixit"]["review"] == "review" and data["councils"]["fixit"]["reviewer"] == "review"


PLAN_SCRIPT = json.dumps(
    [
        {
            "contains": "Phase 0",
            "reply": {
                "approach": {
                    "claim": "add the missing bound",
                    "stages": [{"state": "state the bound", "gate": "a test fails without it"}],
                }
            },
        },
        {
            "contains": "Round ",
            "reply": {
                "positions": [{"id": "F1", "stance": "agree", "note": "the bound is the smallest safe fix"}],
                "questions": [],
                "new_items": [],
                "sign_off": True,
                "backing": "agree",
            },
        },
        {"contains": "# Deduplicate", "reply": {"groups": []}},
        {"contains": "# Compare the plans", "reply": {"agreed": ["all seats want the same bound"], "conflicts": []}},
    ]
)


def test_the_starter_convenes_from_any_project(cli, child_env, ground):
    """The point of the user layer: init once, convene in a project that has no council.toml of its own."""
    assert cli(["init"], cwd=ground).returncode == 0
    bindir = os.path.join(ground, "bin")  # the starter states no pi_cli: `pi` on PATH is what the seats take
    os.makedirs(bindir, exist_ok=True)
    os.symlink(FAKE_PI, os.path.join(bindir, "pi"))
    env = {"PATH": bindir + os.pathsep + child_env["PATH"]}
    project = bare_project(ground)
    user_config = user_config_path(child_env["XDG_CONFIG_HOME"])
    for council in ("review", "plan"):
        full_env = dict(env, **({"FAKE_PI_SCRIPT": PLAN_SCRIPT} if council == "plan" else {}))
        result = cli([council, "--verbosity", "summary"], cwd=project, env=full_env, timeout=150)
        assert result.returncode == 0, result.console
        assert result.manifest["config_layers"] == [user_config], "the only layer is the file init wrote"
        assert result.manifest["pi_cli"] == os.path.realpath(FAKE_PI)
        assert sorted(row["name"] for row in result.manifest["roster"]) == ["Coworker", "Metic", "Pro", "Senior"]
        assert result.report(), "the report landed"


def test_init_refuses_to_replace_an_existing_user_config(cli, child_env, ground):
    path = user_config_path(child_env["XDG_CONFIG_HOME"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = "[defaults]\nmax_rounds = 1\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(existing)
    result = cli(["init"], cwd=ground)
    assert result.returncode == 1
    assert "already exists" in result.stderr
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == existing, "the refusal leaves the file alone"
    assert cli(["init", "--overwrite"], cwd=ground).returncode == 0
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == STARTER


def test_a_council_named_init_is_reserved(cli, project):
    write_project_config(
        project,
        """\
[councils.review]
goal = "g"
[councils.review.chair]
name = "Chair"
[[councils.review.agents]]
name = "A"
[[councils.review.agents]]
name = "B"

[councils.init]
goal = "g"
[councils.init.chair]
name = "Chair"
[[councils.init.agents]]
name = "A"
[[councils.init.agents]]
name = "B"
""",
    )
    result = cli(["review"], cwd=project)
    assert result.returncode == 1
    assert "reserved" in result.console


def test_init_help_and_unknown_argument(cli, ground):
    assert cli(["init", "--help"], cwd=ground).returncode == 0
    result = cli(["init", "--nope"], cwd=ground)
    assert result.returncode == 2
    assert len(result.stderr.strip().splitlines()) == 2, result.stderr


def test_init_creates_nothing_but_the_config(cli, canary, ground):
    result = cli(["init"], cwd=ground)
    assert result.returncode == 0
    assert canary_entries(canary) == [], "init never creates a run directory"
