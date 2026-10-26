"""Startup and an unusable member, through the real CLI."""

from __future__ import annotations

import json
import os

import pytest

from .conftest import FAKE_PI, write_project_config

pytestmark = [pytest.mark.timeout(180)]


def runs_env(project: str) -> dict:
    return {"COUNCIL_RUNS_DIR": os.path.join(os.path.dirname(project), "runs")}


def errored(**extra) -> str:
    """A first prompt that settles the way pi settles a provider failure."""
    return json.dumps([dict({"contains": "", "stop_reason": "error", "error_message": "429 provider quota exhausted"}, **extra)])


# ------------------------------------------------------------ The config loader shape 1


def test_an_unusable_seat_leaves_only_the_minimal_report_and_a_finalized_manifest(cli, project):
    """The minimal-artifacts rule, in the direction that catches an over-eager exit path.

    One `errored()` run carries the whole unusable-member contract, so it asserts all of it: it
    exits one naming the member and pi's own message, it spends exactly one prompt and never
    reaches the repair path or a deliberation round, and it leaves three files and a finalized
    manifest behind. Three separate runs of this same scenario killed the same mutants.
    """
    result = cli(["review"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=errored()), timeout=150)
    assert result.returncode == 1
    console = result.console.lower()
    for named in ("agent1", "unusable", "429 provider quota exhausted"):
        assert named in console, f"the line names the member and pi's message: {console}"
    records = result.manifest["members"]
    seats = [record for record in records.values() if record["kind"] == "seat"]
    assert len(seats) == 2
    assert all(record["prompts"] == 1 for record in seats), f"an unusable seat spends exactly one prompt: {[r['prompts'] for r in seats]}"
    chair = [record for record in records.values() if record["kind"] == "chair"][0]
    assert chair["prompts"] <= 1, "the chair is never asked to repair a dead council"
    assert result.manifest["outcome"] == "aborted"
    kinds = [row["event"] for row in result.events]
    assert "round_summary" not in kinds, "nobody deliberates after a seat is found unusable"
    assert "verdict_rejected" not in kinds, "a dead council is not asked to repair itself"
    written = sorted(name for name in os.listdir(result.run_dir) if os.path.isfile(os.path.join(result.run_dir, name)))
    assert written == ["events.jsonl", "report.md", "run.json"], written
    assert result.manifest["finished"], "run.json is finalized, not left as created"
    report = result.report()
    assert "aborted" in report.lower() and "429" in report, "the minimal report says why"
    assert "consensus findings" not in report.lower()


# ------------------------------------------------------------ The config loader shape 2


def test_a_member_whose_launcher_never_reaches_pi_exits_one_with_minimal_artifacts(cli, project):
    """A launcher that answers `--version` and dies on the RPC form: exit 1, and the run dir still holds the report and `run.json`."""
    broken = os.path.join(os.path.dirname(project), "half-a-pi")
    with open(broken, "w", encoding="utf-8") as handle:
        handle.write('#!/bin/sh\nif [ "$1" = "--version" ]; then echo 9.9.9; exit 0; fi\nexit 1\n')
    os.chmod(broken, 0o755)
    config = os.path.join(project, "council.toml")
    with open(config, encoding="utf-8") as handle:
        text = handle.read()
    with open(config, "w", encoding="utf-8") as handle:
        handle.write(text.replace(f'pi_cli = "{FAKE_PI}"', f'pi_cli = "{broken}"'))
    result = cli(["review"], cwd=project, env=runs_env(project), timeout=150)
    assert result.returncode == 1, result.console
    if result.run_dir:  # the run dir exists once setup ran
        written = sorted(name for name in os.listdir(result.run_dir) if os.path.isfile(os.path.join(result.run_dir, name)))
        assert "run.json" in written and "report.md" in written, written
        assert result.manifest["outcome"] == "aborted"


def test_an_unresolvable_pi_cli_leaves_no_run_directory(cli, project):
    write_project_config(project, text=None)
    config = os.path.join(project, "council.toml")
    with open(config, encoding="utf-8") as handle:
        text = handle.read()
    with open(config, "w", encoding="utf-8") as handle:
        handle.write(text.replace(f'pi_cli = "{FAKE_PI}"', 'pi_cli = "no-such-pi-binary"'))
    result = cli(["review"], cwd=project, env=runs_env(project))
    assert result.returncode == 1
    assert "pi" in result.console.lower()
    assert result.run_dir is None


# ------------------------------------------------------------ The config loader shape 3


def test_an_unknown_model_warns_on_stderr_and_the_run_goes_anyway(cli, project):
    """`Model "X" not found for provider "Y"` is a warning, never a refusal."""
    warning = 'Model "vision-pro" not found for provider "pi"; using the default'
    result = cli(["review"], cwd=project, env=dict(runs_env(project), FAKE_PI_STARTLE=warning), timeout=150)
    assert result.returncode == 0, result.console
    assert warning in result.console, "the warning reaches the operator, once per member"
    assert result.manifest["outcome"] in ("consensus", "needs_human", "stalemate", "round_limit_reached")
    warned = [base for base, record in result.manifest["members"].items() if record.get("model_warning")]
    assert warned, "the report records the warning per seat"


# -------------------------------------------------------------------- The chair


def test_a_chair_that_errors_its_first_publication_exits_one_after_one_prompt(cli, project):
    """The chair's eager spawn: the chair's unusable shape is presented on its *publication* pass."""
    script = json.dumps(
        [
            {"contains": "Phase 0", "reply": {"findings": [{"claim": "a real finding", "severity": "major", "evidence": "src/app.py:1"}]}},
            {"contains": "# Deduplicate", "stop_reason": "error", "error_message": "500 upstream unavailable"},
        ]
    )
    result = cli(["review"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 1, result.console
    assert "chair" in result.console.lower() and "500 upstream unavailable" in result.console
    chair = [r for r in result.manifest["members"].values() if r["kind"] == "chair"][0]
    assert chair["prompts"] == 1, f"the chair spent {chair['prompts']} prompts to be unusable"
