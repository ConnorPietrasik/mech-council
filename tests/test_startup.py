"""Startup and an unusable member, through the real CLI."""

from __future__ import annotations

import json
import os

import pytest

from mech_council import startup
from mech_council.sandbox import mount

from .conftest import FAKE_PI, write_project_config

pytestmark = [pytest.mark.timeout(180)]


def runs_env(project: str) -> dict:
    return {"COUNCIL_RUNS_DIR": os.path.join(os.path.dirname(project), "runs")}


# ------------------------------------------------------------ Who asked to run in place


def convened(argv: list[str], project: str, environ: dict[str, str] | None = None):
    """`startup.convene` with no user config in reach: the notes are what it hands the CLI to print."""
    env = runs_env(project)
    env["XDG_CONFIG_HOME"] = os.path.join(os.path.dirname(project), "no-user-config")  # a directory nothing creates
    env.update(environ or {})
    _args, launch = startup.convene([*argv, "-p", project], environ=env)
    return launch.notes, launch.council


def state_the_knobs(project: str, *lines: str) -> str:
    path = os.path.join(project, "council.toml")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text.replace("[defaults]\n", "[defaults]\n" + "".join(f"{line}\n" for line in lines), 1))
    return path


def test_the_sandbox_is_still_the_default_on_a_host_that_has_namespaces(project):
    notes, council = convened(["review"], project)
    assert council.in_place is False
    assert not any("in place" in note for note in notes), notes


def test_the_flag_says_what_in_place_costs(project):
    notes, council = convened(["review", "--in-place"], project)
    assert council.in_place is True
    said = next(note for note in notes if "--in-place" in note)
    assert "share one project" in said and "journaled" in said, said


def test_a_council_asking_for_in_place_names_the_file_that_asked(project):
    config = state_the_knobs(project, "in_place = true", "sandbox_mib = 64")
    notes, council = convened(["review"], project)
    assert council.in_place is True
    said = next(note for note in notes if "asked for by" in note)
    assert config in said, f"the note must name who asked: {said}"
    assert "sandbox_mib = 64 is ignored" in said, "a knob in place cannot honor is said so"


def test_a_host_with_no_namespaces_runs_in_place_instead_of_refusing(project, monkeypatch):
    monkeypatch.setattr(mount, "supported", lambda: False)
    notes, council = convened(["review"], project)
    assert council.in_place is True, "no namespaces is a reason to run in place, not to refuse"
    assert any("no namespaces" in note and "running in place" in note for note in notes), notes


def errored(**extra) -> str:
    """A first prompt that settles the way pi settles a provider failure."""
    return json.dumps([dict({"contains": "", "stop_reason": "error", "error_message": "429 provider quota exhausted"}, **extra)])


# ------------------------------------------------------------ The config loader shape 1


def test_an_unusable_seat_leaves_only_the_minimal_report_and_a_finalized_manifest(cli, project):
    """An `errored()` run: exits one naming the member, exactly one prompt spent, no repair and no
    round, and three files plus a finalized manifest.
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


# ------------------------------------------------------------ extends, through the CLI


def test_a_council_that_extends_another_convenes_the_inherited_roster(cli, project):
    """`review-staged` states a goal and nothing else: the seats, the chair and the knobs come from `review`."""
    config = os.path.join(project, "council.toml")
    with open(config, encoding="utf-8") as handle:
        text = handle.read()
    with open(config, "w", encoding="utf-8") as handle:
        handle.write(text + '[councils.review-staged]\nextends = "review"\ngoal = "Did whoever staged this do a good job?"\n')
    result = cli(["review-staged"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=errored()), timeout=150)
    assert result.returncode == 1, result.console
    assert "extends review" in result.console, "the console says whose roster is convening"
    assert result.manifest["extends"] == ["review"], "and the manifest carries the chain"
    assert [row["name"] for row in result.manifest["roster"]] == ["Metic", "Senior"], "the roster is the parent's"
    assert result.manifest["goal"] == "Did whoever staged this do a good job?", "the goal is the child's"
    assert "extends review" in result.report(), "the report names the lineage it ran under"


def test_a_template_is_not_a_council_you_name(cli, project):
    config = os.path.join(project, "council.toml")
    with open(config, encoding="utf-8") as handle:
        text = handle.read()
    with open(config, "w", encoding="utf-8") as handle:
        handle.write(text + '[councils._bench]\n\n[[councils._bench.agents]]\nname = "Metic"\n')
    result = cli(["_bench"], cwd=project, env=runs_env(project))
    assert result.returncode == 2, result.console
    assert "template" in result.console and "_bench" in result.console
    assert result.run_dir is None, "a refused selection creates nothing"


def test_a_file_of_nothing_but_templates_names_no_council_to_convene(cli, project):
    """Selection offers only convenable councils, so a file of templates is refused outright rather
    than with an offer list of nothing."""
    write_project_config(project, '[councils._bench]\n\n[[councils._bench.agents]]\nname = "Metic"\n')
    result = cli([], cwd=project, env=runs_env(project))
    assert result.returncode == 2, result.console
    assert "states no council to convene" in result.console and "template" in result.console, result.console
    assert result.run_dir is None, "a refused selection creates nothing"


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
