"""Regression: a non_blocking fix run shows the gate. The gate's questions are the chair's alone,
so the seconding bar holds them back — the gate must force them to the human without waiting."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.conftest import FAKE_PI, write_project_config

PI = FAKE_PI

pytestmark = [pytest.mark.timeout(180)]

CONFIG = """\
[defaults]
pi_cli = "{pi}"
watchdog_sec = 0
verbosity = "debug"
human = "non_blocking"

[councils.inspect]
goal = "Review the project"
mode = "review"

[councils.inspect.chair]
name = "Review Chair"

[[councils.inspect.agents]]
name = "Metic"

[[councils.inspect.agents]]
name = "Senior"

[councils.fixit]
goal = "Fix the findings"
mode = "fix"
review = "inspect"
reviewer = "inspect"

[councils.fixit.chair]
name = "Chair"

[[councils.fixit.agents]]
name = "Forge"
"""


def body() -> str:
    return CONFIG.format(pi=PI)


def runs_env(project: str) -> dict:
    return {"COUNCIL_RUNS_DIR": os.path.join(os.path.dirname(project), "runs")}


def commit_all(project: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "baseline"],
        cwd=project,
        check=True,
        capture_output=True,
    )


def test_the_non_blocking_gate_shows_and_does_not_wait(cli, project):
    write_project_config(project, body())
    commit_all(project)
    script = json.dumps(
        [
            {
                "contains": "## The planned fix",
                "reply": {"summary": "clean", "gate": {"ok": True, "ran": "grep", "evidence": "ok"}, "open_questions": []},
            },
            {
                "contains": "Phase 0",
                "once": True,
                "reply": {
                    "findings": [{"claim": "the value is wrong", "severity": "blocker", "evidence": "src/mod.py:1"}],
                    "summary": "1 finding",
                    "open_questions": [],
                },
            },
            {"contains": "Round ", "reply": {"positions": [{"id": "F1", "stance": "agree"}], "signoff": True}},
            {
                "contains": "# Plan the fix",
                "reply": {"stages": [{"state": "the value is right", "gate": "grep -q 'VALUE = 2' src/mod.py", "scope": "src/mod.py"}]},
            },
            {
                "contains": "# Implement the fix",
                "reply": {
                    "summary": "fixed it",
                    "gate_output": "ok",
                    "changed": ["src/mod.py"],
                    "open_issues": [],
                },
                "write": [{"path": "src/mod.py", "text": "VALUE = 2\n"}],
            },
            {"contains": "# The final check", "reply": {"ok": True, "ran": "the checks", "evidence": "ok"}},
        ]
    )
    result = cli(["fixit"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    # the gate's question is the run docket's first, so it is H1: a checkpoint put it in front of the
    # human without waiting on it
    shown = [e for e in result.events if e.get("event") == "human_checkpoint" and "H1" in e.get("ask_set", [])]
    assert shown and not shown[0]["waited"], "the non_blocking gate shows the questions and does not wait"
    assert result.manifest["outcome"] == "fixed"
