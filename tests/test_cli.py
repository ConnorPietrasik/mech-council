"""The CLI contract, driven as a subprocess against the fake pi."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from .conftest import FAKE_PI, _kill_group_pid, canary_entries, write_project_config

TWO_COUNCILS = """\
[defaults]
pi_cli = "{pi}"
max_rounds = 1
watchdog_sec = 0

[councils.review]
goal = "g"
[councils.review.chair]
name = "Chair"
[[councils.review.agents]]
name = "A"
[[councils.review.agents]]
name = "B"

[councils.second]
goal = "g2"
[councils.second.chair]
name = "Chair"
[[councils.second.agents]]
name = "A"
[[councils.second.agents]]
name = "B"
"""


def runs_env(project: str) -> dict:
    return {"COUNCIL_RUNS_DIR": os.path.join(os.path.dirname(project), "runs")}


# --------------------------------------------------------------- The CLI contract usage


@pytest.mark.parametrize(
    "argv, wanted",
    [
        (["nosuchcouncil"], "no council 'nosuchcouncil'"),
        (["review", "plan"], "one positional is accepted"),
    ],
)
def test_usage_errors_exit_two_before_anything_is_spawned(cli, project, canary, argv, wanted):
    result = cli(argv, cwd=project)
    assert result.returncode == 2
    assert wanted in result.console
    assert canary_entries(canary) == [], "a usage error creates nothing anywhere"
    assert "run 'council --help'" in result.console


def test_two_lines_not_a_usage_dump(cli, project):
    result = cli(["--nope"], cwd=project)
    assert result.returncode == 2
    assert len(result.console.strip().splitlines()) == 2, result.console


def test_unknown_flag_names_itself(cli, project):
    result = cli(["review", "--dry-run-ok"], cwd=project)
    assert result.returncode == 2
    assert "unknown flag '--dry-run-ok'" in result.console


def test_a_multi_council_file_needs_a_name(cli, project, canary):
    write_project_config(project, TWO_COUNCILS.format(pi=FAKE_PI))
    result = cli([], cwd=project)
    assert result.returncode == 2
    assert "name the council" in result.console
    assert canary_entries(canary) == []


def test_help_and_version_exit_zero(cli, project, canary):
    assert cli(["--help"], cwd=project).returncode == 0
    assert cli(["--version"], cwd=project).returncode == 0
    assert canary_entries(canary) == []


def test_a_repeat_that_changes_value_is_noted(cli, project):
    env = runs_env(project)
    result = cli(["review", "--rounds", "1", "--transcripts", "full", "--transcripts", "off"], cwd=project, env=env, timeout=120)
    assert any("overrides" in line for line in result.console.splitlines()), result.console


# ---------------------------------------------------- The exit-code table


def test_a_round_limited_run_exits_zero(cli, project):
    """The outcome is a result, never an exit code: 0 for every member of the four."""
    script = json.dumps(
        [
            {
                "contains": "Phase 0",
                "reply": {"findings": [{"claim": "the cache ignores tenants", "severity": "major", "evidence": "src/app.py:1"}]},
            },
            {"contains": "# Deduplicate", "reply": {"groups": [{"canonical": "F1", "duplicates": ["F2"]}], "human_input": []}},
            {
                "contains": "raised by you",
                "reply": {"positions": [{"id": "F1", "stance": "agree", "note": "I read the key myself"}], "sign_off": False},
            },
            {
                "contains": "Round ",
                "reply": {
                    "positions": [{"id": "F1", "stance": "disagree", "note": "the tenant guard sits upstream", "evidence": "src/app.py:9"}],
                    "sign_off": True,
                },
            },
        ]
    )
    env = dict(runs_env(project), FAKE_PI_SCRIPT=script)
    result = cli(["review"], cwd=project, env=env, timeout=120)
    assert result.returncode == 0, result.console
    assert result.manifest.get("outcome") == "round_limit_reached"
    assert os.path.exists(os.path.join(result.run_dir, "report.md"))
    assert os.path.exists(os.path.join(result.run_dir, "human_input.json"))
    assert result.events, "events.jsonl holds the run"
    assert "disputed" in result.report().lower(), "the item is reported, not hidden"


def test_a_seat_that_goes_silent_does_not_hold_the_run_open(cli, project):
    """End to end: a survived gap is inert, so the run reaches a result."""
    script = json.dumps(
        [
            {
                "contains": "Phase 0",
                "reply": {"findings": [{"claim": "the cache ignores tenants", "severity": "major", "evidence": "src/app.py:1"}]},
            },
            {"contains": "# Deduplicate", "reply": {"groups": [{"canonical": "F1", "duplicates": ["F2"]}], "human_input": []}},
            {"contains": "raised by you", "reply": {"positions": [{"id": "F1", "stance": "agree", "note": "read it"}], "sign_off": True}},
            {"contains": "Round ", "reply": {"positions": [], "sign_off": True}},
        ]
    )
    env = dict(runs_env(project), FAKE_PI_SCRIPT=script)
    result = cli(["review"], cwd=project, env=env, timeout=120)
    assert result.returncode == 0, result.console
    assert result.manifest.get("outcome") == "consensus", result.report()
    report = result.report()
    assert "declined" in report, "the review report says which seat the run could not reach"
    assert "the cache ignores tenants" in report


def test_an_aborted_run_exits_one_with_a_minimal_report(cli, project):
    script = json.dumps([{"contains": "", "die": True}])
    env = dict(runs_env(project), FAKE_PI_SCRIPT=script)
    result = cli(["review"], cwd=project, env=env, timeout=120)
    assert result.returncode == 1, result.console
    assert result.manifest.get("outcome") == "aborted"
    report = result.report()
    assert report and "aborted" in report.lower()


def test_a_config_error_exits_one_and_leaves_the_canary_empty(cli, project, canary):
    write_project_config(project, '[councils.review]\nmode = "planning"\n')
    result = cli(["review"], cwd=project)
    assert result.returncode == 1
    assert "did you mean 'plan'" in result.console
    assert canary_entries(canary) == []


def test_a_bad_project_is_a_usage_error(cli, canary):
    result = cli(["review", "--project", "/nonexistent-here"], cwd=os.getcwd())
    assert result.returncode == 2
    assert canary_entries(canary) == []


def test_sigterm_leaves_on_128_plus_signum(cli, project, child_env):
    env = dict(child_env, **runs_env(project), FAKE_PI_SCRIPT=json.dumps([{"contains": "", "sleep": 60}]))
    child = subprocess.Popen(
        [sys.executable, "-m", "council", "review"],
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert child.stdout is not None, "stdout was PIPEd above: it is how the run directory is awaited"
        # Wait until the run exists, then signal the group the way a terminal would.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = child.stdout.readline()
            if "run directory" in line:
                break
            if not line:
                break
        time.sleep(0.3)
        os.killpg(child.pid, signal.SIGTERM)
        code = child.wait(timeout=60)
        assert code == 128 + signal.SIGTERM, (code, child.stdout.read())
    finally:
        _kill_group_pid(child.pid)


# ------------------------------------------------------- The nested-council guard


def test_council_run_dir_refuses_and_prints_the_parent_verbatim(cli, project):
    parent = "/nowhere/this-suite-never-made-this-run"
    env = {"COUNCIL_RUN_DIR": "/also/nowhere", "COUNCIL_RUN_PATH": parent}
    result = cli(["review"], cwd=project, env=env, scrub=False)
    assert result.returncode == 1
    assert "nested council" in result.console
    assert parent in result.console, "verbatim, never re-resolved from COUNCIL_RUNS_DIR"


def test_allow_nested_overrides_the_guard(cli, project):
    env = dict(runs_env(project), COUNCIL_RUN_DIR="/also/nowhere")
    result = cli(["review", "--allow-nested"], cwd=project, env=env, timeout=120)
    assert "nested council" not in result.console
    assert result.run_dir


# -------------------------------------------------- The runs-root resolution


@pytest.mark.parametrize(
    "runs, xdg, home, expected",
    [
        # COUNCIL_RUNS_DIR wins when it is set at all …
        ("runs-env", "xdg", "home", "runs-env"),
        # … unset → $XDG_CACHE_HOME/council/runs …
        (None, "xdg", "home", os.path.join("xdg", "council", "runs")),
        # … unset and XDG unset → the built-in default, asserted so a change is a visible edit
        (None, None, "home", os.path.join("home", ".cache", "council", "runs")),
        # … and a relative XDG_CACHE_HOME is invalid, so the default is used anyway
        (None, "relative-xdg", "home", os.path.join("home", ".cache", "council", "runs")),
    ],
)
def test_runs_root_resolution(ground, runs, xdg, home, expected):
    from council.cli import _runs_root_from_env

    env = {"CWD": ground}
    for key, name in (("COUNCIL_RUNS_DIR", runs), ("XDG_CACHE_HOME", xdg), ("HOME", home)):
        if name is None:
            continue
        # `relative-xdg` stays relative on purpose: the resolution rules say it is ignored.
        env[key] = name if name == "relative-xdg" else os.path.join(ground, name)
    assert _runs_root_from_env(env) == os.path.realpath(os.path.join(ground, expected))


def test_a_relative_runs_root_is_absolute_against_the_invoking_cwd(ground):
    from council.cli import _runs_root_from_env

    assert _runs_root_from_env({"COUNCIL_RUNS_DIR": "side", "CWD": ground}) == os.path.realpath(os.path.join(ground, "side"))


def test_a_runs_root_inside_the_project_is_exit_one_naming_both(cli, project):
    env = {"COUNCIL_RUNS_DIR": os.path.join(project, "runs")}
    result = cli(["review"], cwd=project, env=env)
    assert result.returncode == 1
    assert "inside the project" in result.console and project in result.console
    assert not os.path.exists(os.path.join(project, "runs")), "no run directory at all"


# ---------------------------------------------------------- pi's argv


def test_every_spawn_carries_approve_and_the_untrusted_flip_carries_no_approve(cli, project):
    marker = os.path.join(os.path.dirname(project), "argv.jsonl")
    env = dict(runs_env(project), FAKE_PI_ARGV=marker)
    cli(["review"], cwd=project, env=env, timeout=120)
    with open(marker, encoding="utf-8") as handle:
        first = [json.loads(line)["argv"] for line in handle]
    assert len(first) >= 3, "two seats and the chair all exec pi"
    assert all("--approve" in argv and "--no-approve" not in argv for argv in first)
    assert all("--mode" in argv and "rpc" in argv for argv in first)
    os.unlink(marker)

    cli(["review", "--untrusted"], cwd=project, env=env, timeout=120)
    with open(marker, encoding="utf-8") as handle:
        second = [json.loads(line)["argv"] for line in handle]
    assert all("--no-approve" in argv and "--approve" not in argv for argv in second)
    assert sorted(len(a) for a in second) == sorted(len(a) for a in first), "the only difference is the approval flag"


# -------------------------------------------- The smoke test / the plan phase machine

PLAN_SCRIPT = json.dumps(
    [
        {
            "contains": "Phase 0",
            "reply": {
                "approach": {
                    "claim": "retry the sync call with a bounded backoff",
                    "stages": [
                        {"state": "wrap the call in a retry helper", "gate": "the helper has a test for each failure kind"},
                        {"state": "point the sync path at the helper", "gate": "the existing tests still pass"},
                    ],
                    "constraints": ["no new dependency"],
                    "risks": ["a retry can double a non-idempotent write"],
                    "rejected_alternatives": ["queue the writes"],
                }
            },
        },
        {
            "contains": "Round ",
            "reply": {
                "positions": [{"id": "F1", "stance": "agree", "note": "the retry is the smallest safe fix"}],
                "questions": [],
                "new_items": [],
                "sign_off": True,
                "backing": "agree",
            },
        },
        {
            "contains": "# Deduplicate",
            "reply": {
                "items": [
                    {"claim": "retry the sync call with a bounded backoff", "severity": "major", "evidence": "app.py:1", "sources": [1]}
                ],
                "rules": ["A retry must never repeat a non-idempotent call"],
                "direction": "bounded backoff",
                "dropped": [],
            },
        },
        {"contains": "# Compare the plans", "reply": {"agreed": ["both seats want the same retry"], "conflicts": []}},
    ]
)


def test_a_plan_council_writes_plan_md_and_says_plainly_when_no_direction_was_chosen(cli, project):
    """plan mode offline: the same protocol, over plan-mode data — and an unsettled direction is said, not glossed."""
    env = dict(runs_env(project), FAKE_PI_SCRIPT=PLAN_SCRIPT)
    result = cli(["plan"], cwd=project, env=env, timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["mode"] == "plan"
    assert os.path.exists(os.path.join(result.run_dir, "plan.md")), "plan mode writes plan.md, not report.md"
    assert not os.path.exists(os.path.join(result.run_dir, "report.md"))
    report = result.report()
    # The plan report's sections, each earned by something in this run rather than printed empty.
    for heading in (
        "## Rules for the implementer",
        "## Chosen direction",
        "## Stages",
        "## Assumptions",
        "## Agreed points",
        "## Alternatives on the table",
    ):
        assert heading in report, f"{heading} missing from\n{report[:600]}"
    # The offline plan fixture never converges, so the direction section must say so out loud rather than
    # quietly printing a heading nobody chose: a named direction carries its backer, an absent one is named.
    direction = report.split("## Chosen direction", 1)[1].split("\n## ", 1)[0]
    assert "did not settle a direction" in direction, f"the report chose a direction this run never agreed:\n{direction[:400]}"
    assert "backed by" not in direction.split("The candidates")[0], "no `backed by` line stands where no direction was chosen"
    # The plan report opens with the standing reading rules, and the stages carry
    # the gates the seats wrote; nothing here invents a chair-authored rule list.
    assert "prescriptive" in report and "descriptive" in report
    assert "wrap the call in a retry helper" in report, "the stage gates reach the plan"
    assert "bounded backoff" in report
    assert result.manifest["outcome"] in ("consensus", "round_limit_reached")


def test_a_plan_council_needs_a_backer_and_the_direction_rule_holds(cli, project):
    """No seat backs a direction, so there is no consensus however it reads."""
    script = json.dumps(
        [
            {
                "contains": "Phase 0",
                "reply": {
                    "approach": {
                        "claim": "rewrite the sync path as a queue",
                        "stages": [{"state": "stand up a queue", "gate": "one round-trip per job"}],
                    }
                },
            },
            {
                "contains": "Round ",
                "reply": {
                    "positions": [{"id": "F1", "stance": "agree", "note": "the retry is the smallest safe fix"}],
                    "questions": [],
                    "new_items": [],
                    "sign_off": True,
                    "backing": "no_preference",
                },
            },
            {
                "contains": "# Deduplicate",
                "reply": {
                    "items": [{"claim": "rewrite the sync path as a queue", "severity": "major", "evidence": "app.py:1", "sources": [1]}],
                    "rules": [],
                    "dropped": [],
                },
            },
        ]
    )
    result = cli(["plan"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] != "consensus", "endorsement with no backer is not a chosen direction"


def test_a_question_at_the_round_limit_is_printed_unasked_and_the_run_never_waits(cli, project):
    """The human checkpoint's wait rule: `round_limit_reached` is the one way a question ends unasked."""
    # Round 2 raises something new so the docket keeps moving: the run must end on the round
    # limit, not on termination's stalemate, which is a different rule and another test.
    script = json.dumps(
        [
            {
                "contains": "Round ",
                "index": 2,
                "reply": {
                    "new_items": [{"claim": "and the same cache never expires", "severity": "minor", "evidence": "src/app.py:2"}],
                    "sign_off": False,
                },
            },
            {
                "contains": "Phase 0",
                "reply": {
                    "findings": [{"claim": "the cache ignores tenants", "severity": "major", "evidence": "src/app.py:1"}],
                    "open_questions": ["Which retention policy applies?"],
                },
            },
            {"contains": "Round ", "reply": {"positions": [], "sign_off": False}},
            {"contains": "# Deduplicate", "reply": {"groups": [], "human_input": []}},
        ]
    )
    result = cli(["review"], cwd=project, stdin="H1: 30 days\n", env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "round_limit_reached"
    asked = json.loads(result.artifact("human_input.json"))["questions"]
    assert asked and asked[0]["state"] == "asked" and asked[0]["answer"] == "", (
        "the question was never waited for, so the typed line cannot be its answer"
    )
    assert "unasked" in result.report(), "the review report marks the question unasked"
    checkpoint = [row for row in result.events if row["event"] == "human_checkpoint"][-1]
    assert checkpoint["terminal"] and not checkpoint["waited"]


# ---------------------------------------------- The config loader / an unusable seat


def test_a_seat_that_fails_its_first_prompt_is_unusable_and_names_the_provider(cli, project, canary):
    """A provider error on the first prompt is an unusable member, not a retry."""
    script = json.dumps([{"contains": "", "stop_reason": "error", "error_message": "401 invalid api key"}])
    result = cli(["review"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 1
    assert "is unusable" in result.console, "the seat is named unusable, not retried"
    assert "401 invalid api key" in result.console, "the provider's own words are said, not a generic failure"
    assert "round 1" not in result.console.lower(), "nobody deliberates after that"
    assert result.manifest["outcome"] == "aborted", "the run ends naming the unusable member"
