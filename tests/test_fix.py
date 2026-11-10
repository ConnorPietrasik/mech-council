"""Fix mode: the config surface, and full runs — a review crew's findings fixed one at a time,
each fix committed, a blocked finding rolled back, and a review file run ungated."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from mech_council import config as config_mod
from mech_council.cli import Args, ConfigError, UsageError
from tests.conftest import FAKE_PI, write_project_config

PI = FAKE_PI

pytestmark = [pytest.mark.timeout(180)]

CONFIG = """\
[defaults]
pi_cli = "{pi}"
watchdog_sec = 0
verbosity = "debug"
human = "none"

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


def convene(tmp_path, text: str, name: str = "fixit", **kw):
    path = os.path.join(str(tmp_path), "council.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    loaded = config_mod.load_config(path)
    chosen = config_mod.select_council(loaded, name)
    return config_mod.convene(loaded, chosen, Args(**kw), str(tmp_path))


def body(**replacements) -> str:
    text = CONFIG.format(pi=PI)
    for find, with_ in replacements.items():
        assert find in text, find
        text = text.replace(find, with_)
    return text


def runs_env(project: str) -> dict:
    return {"COUNCIL_RUNS_DIR": os.path.join(os.path.dirname(project), "runs")}


def commit_all(project: str) -> None:
    """The fixture projects sit inside the dev's repo; a run of its own starts its own tree at a clean baseline."""
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "baseline"],
        cwd=project,
        check=True,
        capture_output=True,
    )


def git_log_lines(project: str) -> list:
    log = subprocess.run(["git", "log", "--format=%an|%s"], cwd=project, check=True, capture_output=True, text=True).stdout
    return log.splitlines()


def review_phase0(**findings):
    return {"findings": findings["findings"], "summary": f"{len(findings['findings'])} finding(s)", "open_questions": []}


# ------------------------------------------------------------- the config surface


def test_a_fix_council_convenes_with_its_crew_keys(tmp_path):
    council = convene(tmp_path, body())
    assert council.mode == "fix"
    assert council.review == "inspect" and council.reviewer == "inspect"
    assert council.commit is True, "a fix run commits each passed finding by default"
    assert council.in_place is True, "a fix run is in place, like implement"
    loaded = config_mod.load_config(os.path.join(str(tmp_path), "council.toml"))
    crews = config_mod.resolve_crews(loaded, council, Args())
    assert [label for label, _ in crews] == ["review", "builder", "reviewer"]


def test_a_fix_council_needs_a_findings_source(tmp_path):
    neither = body().replace('review = "inspect"\nreviewer = "inspect"', 'reviewer = "inspect"')
    with pytest.raises(ConfigError):
        convene(tmp_path, neither)


def test_a_fix_council_states_no_goal_and_the_review_crew_keeps_its_own(tmp_path):
    """The run's goal steers the review crew and the builder's how is its persona: a fix council
    states no goal at all, and the review crew hunts with its own."""
    text = body().replace('goal = "Fix the findings"\n', "")
    council = convene(tmp_path, text)
    assert not (council.goal or "").strip()
    loaded = config_mod.load_config(os.path.join(str(tmp_path), "council.toml"))
    crews = {label: crew for label, crew in config_mod.resolve_crews(loaded, council, Args())}
    assert crews["review"].goal == "Review the project", "the review crew hunts with its own goal"


def test_the_review_file_flag_supersedes_the_review_council(tmp_path):
    """The README's `fixit --review-file` works on a council that names a review council: the
    flag supersedes the config, so the file run convenes no review crew."""
    report = os.path.join(str(tmp_path), "report.json")
    council = convene(tmp_path, body(), review_file=report)
    loaded = config_mod.load_config(os.path.join(str(tmp_path), "council.toml"))
    crews = config_mod.resolve_crews(loaded, council, Args(review_file=report))
    assert [label for label, _ in crews] == ["builder", "reviewer"]


def test_a_planner_is_an_implement_knob_on_a_fix_council(tmp_path):
    with pytest.raises(ConfigError):
        convene(tmp_path, body().replace('mode = "fix"', 'mode = "fix"\nplanner = "inspect"'))


def test_a_commit_knob_is_a_fix_knob(tmp_path):
    with pytest.raises(ConfigError):
        convene(
            tmp_path,
            body().replace(
                '[councils.inspect]\ngoal = "Review the project"',
                '[councils.inspect]\ngoal = "Review the project"\ncommit = false',
            ),
            name="inspect",
        )


def test_a_commit_knob_is_refused_on_an_implement_council(tmp_path):
    """The implement run never commits, so a stated commit is a refusal there, as it is on a review council."""
    text = body().replace('mode = "fix"', 'mode = "implement"\ncommit = false').replace('review = "inspect"\nreviewer = "inspect"\n', "")
    with pytest.raises(ConfigError):
        convene(tmp_path, text, plan=os.path.join(str(tmp_path), "plan.md"))


def test_a_review_knob_is_refused_on_an_implement_council(tmp_path):
    """The findings crew is a fix run's source: on an implement council the knob is a refusal,
    not a silently ignored one."""
    text = body().replace('mode = "fix"', 'mode = "implement"')
    with pytest.raises(ConfigError):
        convene(tmp_path, text, plan=os.path.join(str(tmp_path), "plan.md"))


def test_the_review_file_flag_is_a_fix_flag(tmp_path):
    with pytest.raises(UsageError):
        convene(tmp_path, body(), name="inspect", review_file=os.path.join(str(tmp_path), "report.json"))


def test_a_dirty_tree_is_refused_unless_dirty_is_passed(tmp_path):
    """The fix run's preflight measures the tree the same way implement's does: dirty is a refusal,
    --dirty a note, and an unborn HEAD a dirty tree, not a non-repo."""
    import types
    from typing import cast

    from mech_council import startup

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    with open(os.path.join(str(tmp_path), "a.txt"), "w", encoding="utf-8") as handle:
        handle.write("x\n")
    council = cast(config_mod.Council, types.SimpleNamespace(project=str(tmp_path), commit=True))
    with pytest.raises(UsageError):
        startup._fix_notes(council, Args())
    notes = startup._fix_notes(council, Args(dirty=True))
    assert any("no commits yet" in note for note in notes), notes
    uncommitted = cast(config_mod.Council, types.SimpleNamespace(project=str(tmp_path), commit=False))
    notes = startup._fix_notes(uncommitted, Args(dirty=True))
    assert not any("sweeps in" in note for note in notes), "with commit = false no commit sweeps in the dirt"
    assert any("leaves its fixes uncommitted" in note for note in notes), notes


def test_a_fix_run_needs_git(tmp_path):
    """No repo is a refusal, not a note: the commit is the safety model, and without it the run
    cannot protect or land its work."""
    with open(os.path.join(str(tmp_path), "a.txt"), "w", encoding="utf-8") as handle:
        handle.write("x\n")
    import types
    from typing import cast

    from mech_council import startup

    council = cast(config_mod.Council, types.SimpleNamespace(project=str(tmp_path), commit=True))
    with pytest.raises(UsageError):
        startup._fix_notes(council, Args())


# ------------------------------------------------------------- a full run off the review crew


def test_the_review_crews_findings_are_fixed_and_committed(cli, project):
    """The whole spine: the review crew finds a blocker, the builder plans and lands the fix, the
    reviewer verifies it, and the run commits it as its own commit."""
    write_project_config(project, body())
    commit_all(project)
    script = json.dumps(
        [
            {
                "contains": "## The planned fix",
                "reply": {"summary": "clean", "gate": {"ok": True, "ran": "grep", "evidence": "VALUE = 2"}, "open_questions": []},
            },
            {
                "contains": "Phase 0",
                "once": True,
                "reply": review_phase0(findings=[{"claim": "the value is wrong", "severity": "blocker", "evidence": "src/mod.py:1"}]),
            },
            {"contains": "Round ", "reply": {"positions": [{"id": "F1", "stance": "agree"}], "signoff": True}},
            {
                "contains": "# Plan the fix",
                "reply": {"stages": [{"state": "the value is right", "gate": "grep -q 'VALUE = 2' src/mod.py", "scope": "src/mod.py"}]},
            },
            {
                "contains": "# Implement the fix",
                "reply": {
                    "summary": "fixed the value",
                    "gate_command": "grep -q 'VALUE = 2' src/mod.py",
                    "gate_output": "VALUE = 2",
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
    assert result.manifest["outcome"] == "fixed"
    # the fix is a real file in the tree, and it landed as the run's own commit
    with open(os.path.join(project, "src", "mod.py"), encoding="utf-8") as handle:
        assert handle.read() == "VALUE = 2\n", "the builder's fix is a real file in the tree"
    lines = git_log_lines(project)
    assert len(lines) == 2, "one baseline commit plus the run's commit of the one fixed finding"
    assert "mech-council" in lines[0] and "fix F1" in lines[0], "the newest commit is the run's, on the run's identity"
    # the spine stands in the manifest
    execution = result.manifest["execution"]
    assert execution["source"]["how"] == "council"
    assert [f["status"] for f in execution["findings"]] == ["fixed"]
    assert len(execution["commits"]) == 1 and execution["final_check"]["ok"] is True
    report = result.artifact("fix.md")
    assert "## Crews" in report and "## The findings" in report and "## Findings" in report and "## What landed" in report
    assert "committed" in report and "The whole-project check passed" in report


def test_a_gate_that_passes_as_the_tree_stands_fixes_with_no_commit(cli, project):
    """A builder that proves the gate without changing the tree: the finding passes uncommitted,
    the baseline commit stands, and the run still ends fixed."""
    write_project_config(project, body())
    commit_all(project)
    script = json.dumps(
        [
            {
                "contains": "## The planned fix",
                "reply": {"summary": "clean", "gate": {"ok": True, "ran": "grep", "evidence": "VALUE = 1"}, "open_questions": []},
            },
            {
                "contains": "Phase 0",
                "once": True,
                "reply": review_phase0(findings=[{"claim": "the value is wrong", "severity": "blocker", "evidence": "src/mod.py:1"}]),
            },
            {"contains": "Round ", "reply": {"positions": [{"id": "F1", "stance": "agree"}], "signoff": True}},
            {
                "contains": "# Plan the fix",
                "reply": {"stages": [{"state": "the value is right", "gate": "grep -q 'VALUE = 1' src/mod.py", "scope": "src/mod.py"}]},
            },
            {
                "contains": "# Implement the fix",
                "reply": {
                    "summary": "the gate already holds",
                    "gate_command": "grep -q 'VALUE = 1' src/mod.py",
                    "gate_output": "VALUE = 1",
                    "changed": [],
                    "open_issues": [],
                },
            },
            {"contains": "# The final check", "reply": {"ok": True, "ran": "the checks", "evidence": "ok"}},
        ]
    )
    result = cli(["fixit"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "fixed"
    assert "fixed with no tree change" in result.console, result.console
    assert len(git_log_lines(project)) == 1, "the baseline commit stands: a no-change fix lands as no commit"


def test_severity_orders_the_findings_and_only_the_reviewer_verifies(cli, project):
    """Two agreed findings fix in severity order — the blocker before the major — and a named
    reviewer crew verifies, so the review crew disposes at the phase boundary."""
    write_project_config(project, body())
    commit_all(project)
    script = json.dumps(
        [
            {
                "contains": "## The planned fix",
                "reply": {"summary": "clean", "gate": {"ok": True, "ran": "the gate", "evidence": "ok"}, "open_questions": []},
            },
            {
                "contains": "Phase 0",
                "once": True,
                "reply": review_phase0(
                    findings=[
                        {"claim": "the value is wrong", "severity": "blocker", "evidence": "src/mod.py:1"},
                        {"claim": "hello is not documented", "severity": "major", "evidence": "hello.py"},
                    ]
                ),
            },
            {
                "contains": "Round ",
                "reply": {
                    "positions": [{"id": "F1", "stance": "agree"}, {"id": "F2", "stance": "agree"}],
                    "signoff": True,
                },
            },
            {
                "contains": "# Plan the fix",
                "index": 2,
                "reply": {"stages": [{"state": "hello is documented", "gate": "test -f docs.md", "scope": "docs.md"}]},
            },
            {
                "contains": "# Plan the fix",
                "reply": {"stages": [{"state": "the value is right", "gate": "grep -q 'VALUE = 2' src/mod.py", "scope": "src/mod.py"}]},
            },
            {
                "contains": "# Implement the fix",
                "index": 3,
                "reply": {
                    "summary": "documented hello",
                    "gate_output": "ok",
                    "changed": ["docs.md"],
                    "open_issues": [],
                },
                "write": [{"path": "docs.md", "text": "# Docs\n"}],
            },
            {
                "contains": "# Implement the fix",
                "reply": {
                    "summary": "fixed the value",
                    "gate_output": "ok",
                    "changed": ["src/mod.py"],
                    "open_issues": [],
                },
                "write": [{"path": "src/mod.py", "text": "VALUE = 2\n"}],
            },
            {"contains": "# The final check", "reply": {"ok": True, "ran": "the checks", "evidence": "ok"}},
        ]
    )
    env = dict(runs_env(project), FAKE_PI_SCRIPT=script, FAKE_PI_PROMPTS=os.path.join(os.path.dirname(project), "prompts.log"))
    result = cli(["fixit"], cwd=project, env=env, timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "fixed"
    # both findings were planned, and the blocker's plan came first: the run is in severity order
    with open(env["FAKE_PI_PROMPTS"], encoding="utf-8") as handle:
        prompts = [json.loads(line) for line in handle]
    plans = [p["message"] for p in prompts if "# Plan the fix" in p["message"]]
    assert len(plans) == 2, "each selected finding gets its own plan pass"
    assert "the value is wrong" in plans[0] and "hello is not documented" in plans[1], "the blocker plans before the major"
    assert all("Goal:" not in plan for plan in plans), "the builder's how is its persona, not the run's goal"
    phase0 = [p["message"] for p in prompts if "Phase 0" in p["message"]]
    assert any("Goal: Fix the findings" in m for m in phase0), "the run's goal is the review crew's goal"
    with open(os.path.join(project, "docs.md"), encoding="utf-8") as handle:
        assert handle.read() == "# Docs\n", "the second finding's fix is a real file in the tree"
    assert "the fixes already committed are the tree you start from" in plans[1], "the later plan sees the earlier commit"
    lines = git_log_lines(project)
    assert len(lines) == 3, "each passed finding is its own commit"
    # the review crew disposed at the boundary: the verifier's name in the report is the reviewer crew's
    report = result.artifact("fix.md")
    assert "reviewer" in report


def test_a_blocked_finding_is_rolled_back_and_the_run_ends_partial(cli, project):
    """A fix the verifier will not pass burns the ladder, then rolls back: the failed change is out
    of the tree and out of the commit history, its diff is saved — and the next finding still lands.
    """
    config = body().replace(
        'review = "inspect"\nreviewer = "inspect"',
        'review = "inspect"\nreviewer = "inspect"\nfix_budget = 1\nreplans = 0',
    )
    write_project_config(project, config)
    commit_all(project)
    script = json.dumps(
        [
            # the second finding's own plan and work, before the shared ones (an index floor only lowers)
            {
                "contains": "# Plan the fix",
                "index": 3,
                "reply": {"stages": [{"state": "hello is documented", "gate": "test -f docs.md", "scope": "docs.md"}]},
            },
            {
                "contains": "# Plan the fix",
                "reply": {"stages": [{"state": "the value is right", "gate": "grep -q 'VALUE = 42' src/mod.py", "scope": "src/mod.py"}]},
            },
            {
                "contains": "# Implement the fix",
                "index": 4,
                "reply": {
                    "summary": "documented hello",
                    "gate_output": "ok",
                    "changed": ["docs.md"],
                    "open_issues": [],
                },
                "write": [{"path": "docs.md", "text": "# Docs\n"}],
            },
            {
                "contains": "# Implement the fix",
                "reply": {
                    "summary": "tried",
                    "changed": ["src/mod.py"],
                    "open_issues": ["cannot make the gate pass"],
                },
                "write": [{"path": "src/mod.py", "text": "VALUE = 99\n"}],
            },
            # the passing finding's verify, before the shared one (the verify prompt names its finding)
            {
                "contains": "F2 (major) — fix the defect",
                "reply": {"summary": "clean", "gate": {"ok": True, "ran": "test -f docs.md", "evidence": "docs.md"}, "open_questions": []},
            },
            # the blocked finding's verify (twice: after the unreadable fix pass it fails again)
            {
                "contains": "## The planned fix",
                "reply": {
                    "findings": [{"claim": "the gate still fails", "severity": "major", "evidence": "VALUE = 99"}],
                    "summary": "not done",
                    "gate": {"ok": False, "ran": "grep", "evidence": "VALUE = 99"},
                    "open_questions": [],
                },
            },
            {
                "contains": "Phase 0",
                "once": True,
                "reply": review_phase0(
                    findings=[
                        {"claim": "the value is wrong", "severity": "blocker", "evidence": "src/mod.py:1"},
                        {"claim": "hello is not documented", "severity": "major", "evidence": "hello.py"},
                    ]
                ),
            },
            # both seats raise the same two findings: the chair's dedup folds each pair onto the first
            {
                "contains": "# Deduplicate",
                "reply": {"groups": [{"canonical": "F1", "duplicates": ["F3"]}, {"canonical": "F2", "duplicates": ["F4"]}]},
            },
            {
                "contains": "Round ",
                "reply": {
                    "positions": [{"id": "F1", "stance": "agree"}, {"id": "F2", "stance": "agree"}],
                    "signoff": True,
                },
            },
            {"contains": "# The final check", "reply": {"ok": True, "ran": "the checks", "evidence": "ok"}},
        ]
    )
    result = cli(["fixit"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "partial"
    with open(os.path.join(project, "src", "mod.py"), encoding="utf-8") as handle:
        assert handle.read() == "VALUE = 1\n", "the blocked fix is rolled back: the tree is where the finding found it"
    with open(os.path.join(project, "docs.md"), encoding="utf-8") as handle:
        assert handle.read() == "# Docs\n", "the next finding still lands after a block"
    lines = git_log_lines(project)
    assert len(lines) == 2 and "fix F2" in lines[0], "the blocked finding commits nothing; the next one does"
    execution = result.manifest["execution"]
    assert [f["status"] for f in execution["findings"]] == ["blocked", "fixed"]
    assert len(execution["commits"]) == 1 and execution["commits"][0]["finding"] == "F2"
    assert os.path.isfile(os.path.join(result.run_dir, "finding-01.diff")), "the failed fix's diff is saved"
    with open(os.path.join(result.run_dir, "finding-01.diff"), encoding="utf-8") as handle:
        diff = handle.read()
    assert "+VALUE = 99" in diff, "the saved diff is the pre-tree against the failed pass"
    report = result.artifact("fix.md")
    assert "blocked" in report and "rolled back" in report


def test_a_fix_pass_clears_the_verifier_s_finding_and_the_commit_follows(cli, project):
    """The middle of the ladder: the verify finds a standing item, the builder's fix pass addresses
    it, the verify passes, and the commit lands — one finding, one commit, no replan.
    """
    write_project_config(project, body())
    commit_all(project)
    script = json.dumps(
        [
            {
                "contains": "# Plan the fix",
                "reply": {"stages": [{"state": "the value is right", "gate": "grep -q 'VALUE = 2' src/mod.py", "scope": "src/mod.py"}]},
            },
            {
                "contains": "# Implement the fix",
                "reply": {
                    "summary": "tried",
                    "changed": ["src/mod.py"],
                    "open_issues": ["the gate says otherwise"],
                },
                "write": [{"path": "src/mod.py", "text": "VALUE = 99\n"}],
            },
            # the fix pass: the standing item, addressed, and the file rewritten
            {
                "contains": "# Fix pass",
                "reply": {
                    "summary": "set the value the gate wants",
                    "gate_command": "grep -q 'VALUE = 2' src/mod.py",
                    "gate_output": "VALUE = 2",
                    "changed": ["src/mod.py"],
                    "addressed": [{"id": "C1", "how": "wrote the value the gate checks"}],
                    "open_issues": [],
                },
                "write": [{"path": "src/mod.py", "text": "VALUE = 2\n"}],
            },
            # the first verify (the reviewer's first prompt) finds it; the second, after the fix pass, does not
            {
                "contains": "## The planned fix",
                "index": 1,
                "reply": {"summary": "clean", "gate": {"ok": True, "ran": "grep", "evidence": "VALUE = 2"}, "open_questions": []},
            },
            {
                "contains": "## The planned fix",
                "reply": {
                    "findings": [{"claim": "the gate still fails", "severity": "major", "evidence": "VALUE = 99"}],
                    "summary": "not done",
                    "gate": {"ok": False, "ran": "grep", "evidence": "VALUE = 99"},
                    "open_questions": [],
                },
            },
            {
                "contains": "Phase 0",
                "once": True,
                "reply": review_phase0(findings=[{"claim": "the value is wrong", "severity": "blocker", "evidence": "src/mod.py:1"}]),
            },
            {"contains": "Round ", "reply": {"positions": [{"id": "F1", "stance": "agree"}], "signoff": True}},
            {"contains": "# The final check", "reply": {"ok": True, "ran": "the checks", "evidence": "ok"}},
        ]
    )
    result = cli(["fixit"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "fixed"
    with open(os.path.join(project, "src", "mod.py"), encoding="utf-8") as handle:
        assert handle.read() == "VALUE = 2\n", "the fix pass's write is what committed"
    lines = git_log_lines(project)
    assert len(lines) == 2 and "fix F1" in lines[0], "the finding's one commit follows its passing verify"
    execution = result.manifest["execution"]
    finding = execution["findings"][0]
    assert finding["status"] == "fixed" and finding["fix_passes"] and not finding["replans"]
    assert finding["fix_passes"][0]["addressed"] == [{"id": "C1", "how": "wrote the value the gate checks"}]
    report = result.artifact("fix.md")
    assert "fix pass 1 addressed" in report, "the report names what the fix pass addressed"


def test_a_replans_breakdown_lands_in_full_and_the_findings_gate_still_closes(cli, project):
    """The ladder's last rung: a replan's smaller breakdown lands in full in the next fix pass —
    the finding's own plan stays the contract, so the verify re-runs the finding's gate, not a
    step's, and every step of the breakdown is what commits."""
    config = body().replace(
        'review = "inspect"\nreviewer = "inspect"',
        'review = "inspect"\nreviewer = "inspect"\nfix_budget = 1',
    )
    write_project_config(project, config)
    commit_all(project)
    gate_fail = {
        "findings": [],
        "summary": "the gate fails",
        "gate": {"ok": False, "ran": "grep -q 'VALUE = 2' src/mod.py", "evidence": "no match"},
        "open_questions": [],
    }
    script = json.dumps(
        [
            # the verify prompt carries the phase-0 briefing, so its entries precede the review's
            {"contains": "## The planned fix", "index": 0, "once": True, "reply": gate_fail},
            {"contains": "## The planned fix", "index": 1, "once": True, "reply": gate_fail},
            {
                "contains": "## The planned fix",
                "index": 2,
                "once": True,
                "reply": {
                    "findings": [],
                    "summary": "clean",
                    "gate": {"ok": True, "ran": "grep -q 'VALUE = 2' src/mod.py", "evidence": "VALUE = 2"},
                    "open_questions": [],
                },
            },
            {
                "contains": "Phase 0",
                "once": True,
                "reply": review_phase0(findings=[{"claim": "the value is wrong", "severity": "blocker", "evidence": "src/mod.py:1"}]),
            },
            {"contains": "Round ", "reply": {"positions": [{"id": "F1", "stance": "agree"}], "signoff": True}},
            {
                "contains": "# Plan the fix",
                "reply": {"stages": [{"state": "the value is right", "gate": "grep -q 'VALUE = 2' src/mod.py", "scope": "src/mod.py"}]},
            },
            {
                "contains": "# Implement the fix",
                "reply": {"summary": "tried", "gate_output": "VALUE = 99", "changed": ["src/mod.py"], "open_issues": []},
                "write": [{"path": "src/mod.py", "text": "VALUE = 99\n"}],
            },
            {
                "contains": "# Fix pass — the finding is not done",
                "reply": {
                    "summary": "tried again",
                    "gate_output": "VALUE = 98",
                    "changed": ["src/mod.py"],
                    "open_issues": ["cannot make the gate pass"],
                },
                "write": [{"path": "src/mod.py", "text": "VALUE = 98\n"}],
            },
            # the fix budget is spent: the ladder's last rung replans into a two-step breakdown
            {
                "contains": "# Replan",
                "reply": {
                    "note": "split it in two",
                    "stages": [
                        {"state": "the value is written", "gate": "test -f src/mod.py", "scope": "src/mod.py"},
                        {"state": "the caller is updated", "gate": "grep -q 'from mod import' caller.py", "scope": "caller.py"},
                    ],
                },
            },
            {
                "contains": "# Fix pass — the revised breakdown",
                "reply": {
                    "summary": "landed the breakdown",
                    "gate_output": "VALUE = 2",
                    "changed": ["src/mod.py", "caller.py"],
                    "open_issues": [],
                },
                "write": [
                    {"path": "src/mod.py", "text": "VALUE = 2\n"},
                    {"path": "caller.py", "text": "from mod import VALUE\n"},
                ],
            },
            {"contains": "# The final check", "reply": {"ok": True, "ran": "the checks", "evidence": "ok"}},
        ]
    )
    prompts_log = os.path.join(os.path.dirname(project), "prompts.log")
    env = dict(runs_env(project), FAKE_PI_SCRIPT=script, FAKE_PI_PROMPTS=prompts_log)
    result = cli(["fixit"], cwd=project, env=env, timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "fixed"
    finding = result.manifest["execution"]["findings"][0]
    assert finding["status"] == "fixed"
    assert finding["replans"] == [{"pass": 1, "note": "split it in two", "stages": 2}]
    assert finding["plan"]["state"] == "the value is right", "the finding's plan stays the contract after a replan"
    with open(os.path.join(project, "caller.py"), encoding="utf-8") as handle:
        assert handle.read() == "from mod import VALUE\n", "every step of the breakdown landed in the tree"
    lines = git_log_lines(project)
    assert len(lines) == 2 and "fix F1" in lines[0], "the replanned fix lands as the finding's one commit"
    with open(prompts_log, encoding="utf-8") as handle:
        prompts = [json.loads(line) for line in handle]
    revised = [p["message"] for p in prompts if "# Fix pass — the revised breakdown" in p["message"]]
    assert len(revised) == 1, "the replanned fix is one pass"
    assert "the value is written" in revised[0] and "the caller is updated" in revised[0], "both steps of the breakdown reach the builder"
    verifies = [p["message"] for p in prompts if "## The planned fix" in p["message"]]
    assert verifies, "the verifier re-runs the gate"
    assert all("test -f src/mod.py" not in v for v in verifies), "a step's gate is not the finding's gate"
    assert all("grep -q 'VALUE = 2' src/mod.py" in v for v in verifies), "the finding's own gate is what the verify re-runs"


def test_the_review_file_run_fixes_its_agreed_findings_without_a_gate(cli, project):
    """A curated report.json is the curation: no review crew, no gate — its agreed findings fix
    straight through, a disputed one is left alone, and with no verifier the chair runs the gate."""
    write_project_config(project, body().replace('review = "inspect"\nreviewer = "inspect"\n', ""))
    commit_all(project)
    review_file = os.path.join(os.path.dirname(project), "report.json")
    with open(review_file, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "format": "council-report",
                "version": 1,
                "run": {"council": "inspect", "mode": "review", "date": "2026-02-08 03:00:00", "outcome": "all_agreed"},
                "items": [
                    {
                        "id": "F1",
                        "claim": "the value is wrong",
                        "detail": "",
                        "evidence": "src/mod.py:1",
                        "severity": "blocker",
                        "disposition": "agreed",
                    },
                    {
                        "id": "F2",
                        "claim": "the docs are stale",
                        "detail": "",
                        "evidence": "",
                        "severity": "minor",
                        "disposition": "disputed",
                    },
                ],
                "questions": [],
                "notes": [],
            },
            handle,
        )
    script = json.dumps(
        [
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
                    "notes": ["F1: the gate re-run is the real proof"],
                },
                "write": [{"path": "src/mod.py", "text": "VALUE = 2\n"}],
            },
            {"contains": "# Verify the gate", "reply": {"ok": True, "ran": "grep", "evidence": "ok"}},
        ]
    )
    result = cli(["fixit", "--review-file", review_file], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "fixed"
    assert [crew["label"] for crew in result.manifest["crews"]] == ["builder"], "no review crew is convened for a file run"
    execution = result.manifest["execution"]
    assert execution["source"]["how"] == "file"
    assert len(execution["findings"]) == 1, "only the agreed finding of the file is fixed"
    assert execution["findings"][0]["finding_id"] == "F1"
    with open(os.path.join(project, "src", "mod.py"), encoding="utf-8") as handle:
        assert handle.read() == "VALUE = 2\n"
    report = result.artifact("fix.md")
    assert "No gate was taken" in report
    assert "the gate re-run is the real proof" in report, "the builder's note is its words, not a parse artifact"
    assert "Notes for the human (0)" in report, "a note that opens with the finding's id is filed under it, not the flat ledger"


def test_the_review_file_carries_its_notes_to_the_builder(cli, project):
    """The review's standing notes ride with the finding: the builder reads them in the brief and the report
    files them under the finding, while a retracted one, one on an unfixed finding, and one folded into a
    note that still stands stay out. The agreed finding is the review's F2, so it is the run's F1: a prefix
    that rode would name a finding that does not exist in the run. A note the review already showed the
    human is not shown again in the fix run; one the review left unshown is."""
    write_project_config(project, body().replace('review = "inspect"\nreviewer = "inspect"\n', ""))
    commit_all(project)
    review_file = os.path.join(os.path.dirname(project), "report.json")
    with open(review_file, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "format": "council-report",
                "version": 1,
                "run": {"council": "inspect", "mode": "review", "date": "2026-02-08 03:00:00", "outcome": "all_agreed"},
                "items": [
                    {
                        "id": "F1",
                        "claim": "the cache is wrong",
                        "detail": "",
                        "evidence": "",
                        "severity": "blocker",
                        "disposition": "disputed",
                    },
                    {
                        "id": "F2",
                        "claim": "the value is wrong",
                        "detail": "",
                        "evidence": "src/mod.py:1",
                        "severity": "blocker",
                        "disposition": "agreed",
                    },
                ],
                "questions": [],
                "notes": [
                    {
                        "id": "N1",
                        "seat": "Agent1",
                        "item": "F2",
                        "kind": "correction",
                        "note": "F2: the value is not wrong; it is computed twice",
                        "state": "standing",
                        "shown_round": 2,
                    },
                    {
                        "id": "N2",
                        "seat": "Agent1",
                        "item": "F2",
                        "kind": "correction",
                        "note": "F2: an earlier, retracted guess",
                        "state": "retracted",
                    },
                    {
                        "id": "N3",
                        "seat": "Agent2",
                        "item": "F1",
                        "kind": "correction",
                        "note": "F1: a correction of an unfixed finding",
                        "state": "standing",
                    },
                    {
                        "id": "N4",
                        "seat": "Agent2",
                        "item": "F1",
                        "kind": "info",
                        "note": "F1: a note of an unfixed finding",
                        "state": "standing",
                    },
                    {
                        "id": "N5",
                        "seat": "Agent2",
                        "item": "F2",
                        "kind": "correction",
                        "note": "F2: a correction folded into a standing correction",
                        "state": "standing",
                        "folded_into": "N1",
                    },
                    {
                        "id": "N6",
                        "seat": "Agent2",
                        "item": "F2",
                        "kind": "correction",
                        "note": "F2: a correction folded into a standing note",
                        "state": "standing",
                        "folded_into": "N7",
                    },
                    {
                        "id": "N7",
                        "seat": "Agent2",
                        "item": "F2",
                        "kind": "info",
                        "note": "F2: the standing note it folded into",
                        "state": "standing",
                    },
                ],
            },
            handle,
        )
    script = json.dumps(
        [
            {
                "contains": "# Plan the fix",
                "reply": {
                    "stages": [{"state": "the value is computed once", "gate": "grep -q 'VALUE = 2' src/mod.py", "scope": "src/mod.py"}]
                },
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
            {"contains": "# Verify the gate", "reply": {"ok": True, "ran": "grep", "evidence": "ok"}},
        ]
    )
    prompts_log = os.path.join(os.path.dirname(project), "prompts.log")
    env = dict(runs_env(project), FAKE_PI_SCRIPT=script, FAKE_PI_PROMPTS=prompts_log)
    result = cli(["fixit", "--review-file", review_file], cwd=project, env=env, timeout=150)
    assert result.returncode == 0, result.console
    with open(prompts_log, encoding="utf-8") as handle:
        plan = [p["message"] for p in (json.loads(line) for line in handle) if "# Plan the fix" in p["message"]]
    assert plan, "the builder planned the finding"
    assert "it is computed twice" in plan[0], "the correction reaches the builder beside the original claim"
    assert "the standing note it folded into" in plan[0], "the info note rides too: the review's record of the finding is the builder's"
    assert "an earlier, retracted guess" not in plan[0], "a retracted note does not ride with the finding"
    assert "a correction of an unfixed finding" not in plan[0], "a correction of an unfixed finding stays with it"
    assert "a note of an unfixed finding" not in plan[0], "a note of an unfixed finding stays with it"
    assert "a correction folded into a standing correction" not in plan[0], "the duplicate reads through the standing correction"
    assert "a correction folded into a standing note" not in plan[0], "the duplicate reads through the standing note, whatever its kind"
    assert "F2" not in plan[0], "the review's id does not ride: the finding has its own"
    report = result.artifact("fix.md")
    assert "it is computed twice" in report, "the report files the correction under the finding"
    assert "the standing note it folded into" in report, "the info note prints under the finding"
    assert "a correction folded into a standing correction" not in report, "the folded duplicate does not print under the finding"
    assert "a correction folded into a standing note" not in report
    assert "a correction of an unfixed finding" not in report
    assert "a note of an unfixed finding" not in report
    assert "Notes for the human (0)" in report, "a note about the finding prints under it, not the flat ledger"
    run_console = result.console.split("# Fix")[0]  # the run's own output, before the report is echoed to the console
    assert "it is computed twice" not in run_console, "a note the review already showed is not shown again"
    assert "the standing note it folded into" in run_console, "one the review left unshown is shown in the fix run"


def test_the_review_crews_notes_ride_with_their_findings(cli, project):
    """The review crew's standing notes ride with their findings, like a review file's: the builder reads
    them in the brief and the report files them under the finding. A note on a finding the gate skips stays
    with the review's record."""
    write_project_config(project, body())
    commit_all(project)
    script = json.dumps(
        [
            {
                "contains": "## The planned fix",
                "reply": {"summary": "clean", "gate": {"ok": True, "ran": "grep", "evidence": "VALUE = 2"}, "open_questions": []},
            },
            {
                "contains": "Phase 0",
                "reply": review_phase0(
                    findings=[
                        {"claim": "the value is wrong", "severity": "blocker", "evidence": "src/mod.py:1"},
                        {"claim": "the cache is wrong", "severity": "major", "evidence": "src/mod.py:2"},
                    ]
                ),
            },
            {
                "contains": "Round ",
                "reply": {
                    "positions": [{"id": "F1", "stance": "agree"}],
                    "signoff": True,
                    "notes": [
                        {"note": "F1: the value is not wrong; it is computed twice", "item": "F1", "kind": "correction"},
                        {"note": "F1: the evidence is stale; the code moved since the report", "item": "F1", "kind": "info"},
                        {"note": "F2: the cache is fine as it stands", "item": "F2", "kind": "correction"},
                    ],
                },
            },
            {
                "contains": "# Plan the fix",
                "reply": {
                    "stages": [{"state": "the value is computed once", "gate": "grep -q 'VALUE = 2' src/mod.py", "scope": "src/mod.py"}]
                },
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
    prompts_log = os.path.join(os.path.dirname(project), "prompts.log")
    env = dict(runs_env(project), FAKE_PI_SCRIPT=script, FAKE_PI_PROMPTS=prompts_log)
    result = cli(["fixit"], cwd=project, env=env, timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["execution"]["source"]["how"] == "council"
    with open(prompts_log, encoding="utf-8") as handle:
        plan = [p["message"] for p in (json.loads(line) for line in handle) if "# Plan the fix" in p["message"]]
    assert plan, "the builder planned the finding"
    assert "it is computed twice" in plan[0], "the review crew's correction reaches the builder beside the original claim"
    assert "the evidence is stale; the code moved since the report" in plan[0], "the crew's info note rides too"
    assert "the cache is fine as it stands" not in plan[0], "a note on a finding the gate skips stays with the review's record"
    report = result.artifact("fix.md")
    assert "it is computed twice" in report, "the report files the correction under the finding"
    assert "the evidence is stale; the code moved since the report" in report, "the info note prints under the finding"
    assert "the cache is fine as it stands" not in report
    assert "Notes for the human (0)" in report, "a filed note is not in the flat ledger"


def test_the_gate_shows_the_review_s_corrections(cli, project):
    """The gate's fix-or-skip call weighs the corrected mechanism: the review's standing correction on a
    finding stands in the question's context, so the human fixes or skips the thing it is now said to be."""
    write_project_config(project, body(**{'human = "none"': 'human = "blocking"'}))
    commit_all(project)
    script = json.dumps(
        [
            {
                "contains": "## The planned fix",
                "reply": {"summary": "clean", "gate": {"ok": True, "ran": "grep", "evidence": "VALUE = 2"}, "open_questions": []},
            },
            {
                "contains": "Phase 0",
                "reply": review_phase0(findings=[{"claim": "the value is wrong", "severity": "blocker", "evidence": "src/mod.py:1"}]),
            },
            {"contains": "# Deduplicate", "reply": {"groups": [{"canonical": "F1", "duplicates": ["F2"]}]}},
            {
                "contains": "Round ",
                "reply": {
                    "positions": [{"id": "F1", "stance": "agree"}],
                    "signoff": True,
                    "notes": [{"note": "F1: the value is not wrong; it is computed twice", "item": "F1", "kind": "correction"}],
                },
            },
            {
                "contains": "# Plan the fix",
                "reply": {
                    "stages": [{"state": "the value is computed once", "gate": "grep -q 'VALUE = 2' src/mod.py", "scope": "src/mod.py"}]
                },
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
    result = cli(
        ["fixit"],
        cwd=project,
        env=dict(runs_env(project), FAKE_PI_SCRIPT=script),
        timeout=150,
        stdin="H1: fix\n",
    )
    assert result.returncode == 0, result.console
    assert "correction: F1: the value is not wrong; it is computed twice" in result.console, (
        "the gate's context carries the review's correction"
    )
    assert result.manifest["outcome"] == "fixed"


def test_the_review_file_must_be_a_review_report(cli, project):
    write_project_config(project, body().replace('review = "inspect"\nreviewer = "inspect"\n', ""))
    commit_all(project)
    not_a_report = os.path.join(os.path.dirname(project), "other.json")
    with open(not_a_report, "w", encoding="utf-8") as handle:
        json.dump({"format": "council-report", "run": {"mode": "plan"}, "items": []}, handle)
    result = cli(["fixit", "--review-file", not_a_report], cwd=project, timeout=60)
    assert result.returncode != 0
    assert "review report.json" in result.console


def test_a_review_file_run_spawns_the_builders_once(cli, project, tmp_path):
    """--review-file convenes no review crew, so the builders are up from the start: the fix loop
    must not start their seats a second time, or the first member is orphaned past the teardown."""
    counter = os.path.join(str(tmp_path), "spawns.txt")
    wrapper = os.path.join(str(tmp_path), "counting_pi.py")
    with open(wrapper, "w", encoding="utf-8") as handle:
        handle.write(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "args = sys.argv[1:]\n"
            "name = args[args.index('--name') + 1] if '--name' in args else 'other'\n"
            f"open({counter!r}, 'a').write(name + '\\n')\n"
            f"exec(open({FAKE_PI!r}).read())\n"
        )
    os.chmod(wrapper, 0o755)
    config = body().replace('review = "inspect"\nreviewer = "inspect"\n', "").replace(f'pi_cli = "{PI}"', f'pi_cli = "{wrapper}"')
    write_project_config(project, config)
    commit_all(project)
    review_file = os.path.join(os.path.dirname(project), "report.json")
    with open(review_file, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "format": "council-report",
                "version": 1,
                "run": {"council": "inspect", "mode": "review", "date": "2026-02-08 03:00:00", "outcome": "all_agreed"},
                "items": [
                    {
                        "id": "F1",
                        "claim": "the value is wrong",
                        "detail": "",
                        "evidence": "src/mod.py:1",
                        "severity": "blocker",
                        "disposition": "agreed",
                    },
                ],
                "questions": [],
                "notes": [],
            },
            handle,
        )
    script = json.dumps(
        [
            {
                "contains": "# Plan the fix",
                "reply": {"stages": [{"state": "the value is right", "gate": "grep -q 'VALUE = 2' src/mod.py", "scope": "src/mod.py"}]},
            },
            {
                "contains": "# Implement the fix",
                "reply": {"summary": "fixed it", "gate_output": "ok", "changed": ["src/mod.py"], "open_issues": []},
            },
            {"contains": "# Verify the gate", "reply": {"ok": True, "ran": "grep", "evidence": "ok"}},
        ]
    )
    result = cli(["fixit", "--review-file", review_file], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    with open(counter, encoding="utf-8") as handle:
        members = [line.strip() for line in handle if line.startswith("council-")]
    assert sorted(members) == ["council-chair", "council-slot01-forge"], "the chair and the builder, each spawned once"


def test_a_file_run_with_no_verifier_and_no_gate_never_asks_the_chair(cli, project):
    """No crew verifies and the plan names no gate: the pass stands on the builder's word — the
    chair is never asked to run a gate that is not there, so the finding passes and commits."""
    write_project_config(project, body().replace('review = "inspect"\nreviewer = "inspect"\n', ""))
    commit_all(project)
    review_file = os.path.join(os.path.dirname(project), "report.json")
    with open(review_file, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "format": "council-report",
                "version": 1,
                "run": {"council": "inspect", "mode": "review", "date": "2026-02-08 03:00:00", "outcome": "all_agreed"},
                "items": [
                    {
                        "id": "F1",
                        "claim": "the value is wrong",
                        "detail": "",
                        "evidence": "src/mod.py:1",
                        "severity": "blocker",
                        "disposition": "agreed",
                    },
                ],
                "questions": [],
                "notes": [],
            },
            handle,
        )
    script = json.dumps(
        [
            {"contains": "# Plan the fix", "reply": {"stages": [{"state": "the value is right", "scope": "src/mod.py"}]}},
            {
                "contains": "# Implement the fix",
                "reply": {"summary": "fixed it", "changed": ["src/mod.py"], "open_issues": []},
                "write": [{"path": "src/mod.py", "text": "VALUE = 2\n"}],
            },
            # if the run ever asks the chair to verify a gate the plan never named, the verdict is a refusal
            {"contains": "# Verify the gate", "reply": {"ok": False, "ran": "nothing named", "evidence": "the plan named no gate"}},
        ]
    )
    env = dict(runs_env(project), FAKE_PI_SCRIPT=script, FAKE_PI_PROMPTS=os.path.join(os.path.dirname(project), "prompts.log"))
    result = cli(["fixit", "--review-file", review_file], cwd=project, env=env, timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "fixed"
    execution = result.manifest["execution"]
    assert len(execution["commits"]) == 1, "the finding commits despite having no gate and no verifier"
    with open(env["FAKE_PI_PROMPTS"], encoding="utf-8") as handle:
        prompts = [json.loads(line) for line in handle]
    assert not any("Verify the gate" in p["message"] for p in prompts), "no gate was named: the chair is never the verifier"


def test_the_blocking_gate_skips_findings_the_human_skips(cli, project):
    """The gate is the human's curation: one finding fixed, the one the human skipped left alone —
    and the run's only commit is the one that was fixed."""
    write_project_config(project, body(**{'human = "none"': 'human = "blocking"'}))
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
                "reply": review_phase0(
                    findings=[
                        {"claim": "the value is wrong", "severity": "blocker", "evidence": "src/mod.py:1"},
                        {"claim": "hello is not documented", "severity": "major", "evidence": "hello.py"},
                    ]
                ),
            },
            # both seats raise the same two findings: the chair's dedup folds each pair onto the first
            {
                "contains": "# Deduplicate",
                "reply": {"groups": [{"canonical": "F1", "duplicates": ["F3"]}, {"canonical": "F2", "duplicates": ["F4"]}]},
            },
            {
                "contains": "Round ",
                "reply": {
                    "positions": [{"id": "F1", "stance": "agree"}, {"id": "F2", "stance": "agree"}],
                    "signoff": True,
                },
            },
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
    result = cli(
        ["fixit"],
        cwd=project,
        env=dict(runs_env(project), FAKE_PI_SCRIPT=script),
        timeout=150,
        stdin="H1: 1\nH2: 2\n",
    )
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "fixed", "the one fixed finding lands, the skipped one stays"
    lines = git_log_lines(project)
    assert "fix F1" in " ".join(lines) and "fix F2" not in " ".join(lines), "only the gate-selected finding commits"
    execution = result.manifest["execution"]
    assert len(execution["findings"]) == 1, "the skipped finding never enters the fix loop"
    report = result.artifact("fix.md")
    gate_section = report.split("## The gate", 1)[1].split("\n## ", 1)[0]
    assert "selected" in gate_section and "skipped" in gate_section, "the gate's results stand in the report"


def test_the_gate_takes_an_answer_in_the_option_s_own_words(cli, project):
    """The gate's pick is the option: a bare number, or the option's own words — `skip` typed as a
    word skips the default-fixed finding, instead of the default taking it."""
    write_project_config(project, body(**{'human = "none"': 'human = "blocking"'}))
    commit_all(project)
    script = json.dumps(
        [
            {
                "contains": "Phase 0",
                "once": True,
                "reply": review_phase0(findings=[{"claim": "the value is wrong", "severity": "blocker", "evidence": "src/mod.py:1"}]),
            },
            {"contains": "# Deduplicate", "reply": {"groups": [{"canonical": "F1", "duplicates": ["F2"]}]}},
            {"contains": "Round ", "reply": {"positions": [{"id": "F1", "stance": "agree"}], "signoff": True}},
        ]
    )
    result = cli(
        ["fixit"],
        cwd=project,
        env=dict(runs_env(project), FAKE_PI_SCRIPT=script),
        timeout=150,
        stdin="H1: skip\n",
    )
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "nothing_to_fix", "the word skip takes the skip option"
    assert len(git_log_lines(project)) == 1, "the skipped finding is never fixed, so the run commits nothing"
