"""The CLI contract, driven as a subprocess against the fake pi."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys

import pytest

from .conftest import DEFAULT_CONFIG, FAKE_PI, canary_entries, write_project_config

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
    assert "run 'mech-council --help'" in result.console


def test_two_lines_not_a_usage_dump(cli, project):
    result = cli(["--nope"], cwd=project)
    assert result.returncode == 2
    assert len(result.console.strip().splitlines()) == 2, result.console


def test_unknown_flag_names_itself(cli, project):
    result = cli(["review", "--dry-run-ok"], cwd=project)
    assert result.returncode == 2
    assert "unknown flag '--dry-run-ok'" in result.console


def test_a_typoed_in_place_is_named_back(cli, project):
    result = cli(["review", "--inplace"])
    assert result.returncode == 2
    assert "did you mean --in-place?" in result.console


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


def test_a_note_is_printed_once_however_stdout_is_redirected(cli, project):
    """The orchestrator is a fork: the parent's unflushed buffer would print twice."""
    script = json.dumps([{"contains": "", "stop_reason": "error", "error_message": "429 provider quota exhausted"}])
    env = dict(runs_env(project), FAKE_PI_SCRIPT=script)
    result = cli(["review", "--in-place"], cwd=project, env=env, timeout=120)
    notes = [line for line in result.console.splitlines() if line.startswith("note: --in-place")]
    assert len(notes) == 1, f"one note, not one per process: {notes}"


# ---------------------------------------------------- The exit-code table


def test_a_stalled_run_exits_zero(cli, project):
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
    assert result.manifest.get("outcome") == "stalemate", "one seat refuting with evidence every round is no movement"
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


def test_sigterm_leaves_on_128_plus_signum(signalled_cli, project, child_env):
    """One keystroke is one press, delivered twice: the group signal reaches the orchestrator and the supervisor relays it."""
    env = dict(child_env, **runs_env(project), FAKE_PI_SCRIPT=_cut_short_script())
    result = signalled_cli(["review"], cwd=project, env=env, signum=signal.SIGTERM, wait_for="chair merged", settle=0.5)
    assert result.returncode == 128 + signal.SIGTERM, result.console
    assert "## Cut short by SIGTERM" in result.report(), result.console
    assert "exit_path" not in result.manifest, "the relay of a press must not hurry the run it is relaying"


def test_an_interrupted_run_reports_where_it_stood(signalled_cli, project):
    """The signal stops the deliberation, not the report: the state the seats built is what gets written."""
    env = dict(runs_env(project), FAKE_PI_SCRIPT=_cut_short_script())
    result = signalled_cli(["review"], cwd=project, env=env, wait_for="chair merged", settle=0.5)
    assert result.returncode == 130, result.console
    assert result.manifest["outcome"] == "aborted" and result.manifest["finished"], "an interrupted run finalizes like any other"
    assert result.manifest["rounds"] == 1
    assert result.manifest["interrupted"]["round"] == 1
    report = result.report()
    assert "# Why nothing ran" not in report, "the docket held state, so the report holds it"
    assert "the cache ignores tenants" in report, "what the seats raised before the signal survives into the report"
    head = report.split("## Roster")[0]
    assert "## Cut short by SIGINT" in head and "round 1" in head, "the cut short leads, before anything that reads as a verdict"
    assert "in flight" in head, "the seats that were mid-answer are named"


def test_a_second_signal_ends_the_run_where_it_stood(signalled_cli, project):
    """The second press buys time, not silence: the waits go, what the seats said stays."""
    env = dict(runs_env(project), FAKE_PI_SCRIPT=_cut_short_script(), FAKE_PI_SILENT_ABORT="1")
    result = signalled_cli(["review"], cwd=project, env=env, times=2, wait_for="chair merged", settle=0.5)
    assert result.returncode == 130, result.console
    assert result.manifest["exit_path"] == "fast", "a press after the coalescing window is a second press"
    assert result.manifest["outcome"] == "aborted" and result.manifest["finished"], "the fast way out still finalizes run.json"
    assert "the cache ignores tenants" in result.report(), "the report is what the second press must never cost"
    skipped = result.manifest["skipped_steps"]
    assert "the members' stop grace" in skipped and "the rest of the write journal walk" in skipped, skipped
    assert result.manifest["survivor_pids"] == [] and result.manifest["survivor_ns"] == [], "the fast way out leaves nothing alive"


def _cut_short_script() -> str:
    """Phase 0 and the merge answer at once; round 1 never ends, so the signal lands on a docket with state."""
    return json.dumps(
        [
            {
                "contains": "Phase 0",
                "reply": {
                    "findings": [{"claim": "the cache ignores tenants", "severity": "major", "evidence": "src/app.py:1"}],
                    "summary": "one finding",
                },
            },
            {"contains": "# Deduplicate", "reply": {"groups": [], "human_input": []}},
            {"contains": "Round deliberation", "sleep": 90},
        ]
    )


# ------------------------------------------------------- The nested-council guard


def test_council_run_dir_refuses_and_prints_the_parent_verbatim(cli, project, canary):
    parent = "/nowhere/this-suite-never-made-this-run"
    env = {"COUNCIL_RUN_DIR": "/also/nowhere", "COUNCIL_RUN_PATH": parent}
    result = cli(["review"], cwd=project, env=env, scrub=False)
    assert result.returncode == 1, "a well-formed refusal is a setup failure, not a usage error"
    assert "nested council" in result.console
    assert parent in result.console, "verbatim, never re-resolved from COUNCIL_RUNS_DIR"
    assert "--allow-nested" in result.console, "the refusal names the way out"
    assert canary_entries(canary) == [], "a refusal creates nothing"


def test_allow_nested_overrides_the_guard(cli, project):
    env = dict(runs_env(project), COUNCIL_RUN_DIR="/also/nowhere")
    result = cli(["review", "--allow-nested"], cwd=project, env=env, timeout=120)
    assert "nested council" not in result.console
    assert result.run_dir


# ------------------------------------------------------- A host with no namespaces


MACOS = "import sys\nsys.platform = 'darwin'\nfrom mech_council.cli import main\nsys.exit(main(sys.argv[1:]))\n"


def elsewhere(child_env, cwd, args, *, timeout=60, env=None, harness=MACOS):
    """The real entry point under a replaced world: `harness` patches what it patches before `main` runs, so the
    whole child lives inside it.  The members it spawns are separate processes, which never look."""
    return subprocess.run(
        [sys.executable, "-c", harness, *args],
        check=False,
        cwd=cwd,
        env=dict(child_env, **(env or {})),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def manifest_of(console: str) -> dict:
    line = next(line for line in console.splitlines() if "run directory " in line)
    with open(os.path.join(line.split("run directory ", 1)[1].strip(), "run.json"), encoding="utf-8") as handle:
        return json.load(handle)


def test_a_host_with_no_namespaces_convenes_in_place(child_env, project):
    """Off Linux the council runs instead of refusing, in a sentence that names the cost — and nothing said
    afterwards claims an overlay, a cap or a namespace that was never set up."""
    result = elsewhere(child_env, project, ["review"], timeout=150)
    console = result.stdout + result.stderr
    assert result.returncode == 0, console
    assert "darwin" in console and "in place" in console, console
    for claim in ("tmpfs ", "· ns ", "sandbox_mib"):
        assert claim not in console, f"the console still advertises {claim!r}, which it never built"
    finished = manifest_of(console)
    assert finished["settings"]["in_place"] is True
    assert finished["namespace_sweep"] == "none (in place)", "the manifest says which sweep did not run"
    assert [record["namespace"] for record in finished["members"].values()] == [None] * len(finished["members"])
    assert finished["write_journal"] == [], "in place there is no upper layer to journal"


REFUSE_UNSHARE = (
    "import sys\n"
    "from mech_council.sandbox import mount\n"
    "def refuse(_flags):\n    raise OSError(1, 'Operation not permitted')\n"
    "mount.unshare = refuse\n"
    "from mech_council.cli import main\n"
    "sys.exit(main(sys.argv[1:]))\n"
)


def test_a_linux_kernel_that_refuses_unshare_names_the_way_out(child_env, project):
    """Linux never drops the sandbox on its own: a kernel that will not cooperate is a refusal that says what to
    pass to run without one — the one place where `--in-place` must not be chosen for the operator."""
    result = elsewhere(child_env, project, ["review"], env=runs_env(project), harness=REFUSE_UNSHARE)
    console = result.stdout + result.stderr
    assert result.returncode == 1, console
    assert "unshare(CLONE_NEWUSER|CLONE_NEWNS)" in console and "--in-place" in console, console


def test_version_and_help_answer_on_any_platform(child_env, tmp_path):
    for flag in ("-V", "--help"):
        result = elsewhere(child_env, str(tmp_path), [flag])
        assert result.returncode == 0, f"{flag}: {result.stderr}"
        assert "mech-council" in result.stdout


# ------------------------------------------------------- The config layers


ONE_COUNCIL = """\
[defaults]
{knob}max_rounds = 1
watchdog_sec = 0

[councils.review]
goal = "{goal}"

[councils.review.chair]
name = "Chair"

[[councils.review.agents]]
name = "Metic"

[[councils.review.agents]]
name = "Senior"
"""


def one_council(goal: str, pi: str | None = FAKE_PI) -> str:
    """A convenable `review` council that reports its own goal, in either layer's voice."""
    return ONE_COUNCIL.format(goal=goal, knob=f'pi_cli = "{pi}"\n' if pi else "")


def user_at(ground: str) -> dict:
    """An env whose user config is `<ground>/council.toml`, which is where `write_project_config` writes."""
    return {"COUNCIL_CONFIG": os.path.join(ground, "council.toml")}


def bare(ground: str) -> str:
    """A project with source in it and no council.toml of its own."""
    path = os.path.join(ground, "bare")
    os.makedirs(os.path.join(path, "src"), exist_ok=True)
    with open(os.path.join(path, "hello.py"), "w", encoding="utf-8") as handle:
        handle.write("print('hello')\n")
    return path


def test_a_council_kept_in_the_user_config_convenes_a_project_that_has_none(cli, ground):
    """The point of the user layer: state the council once, convene it in every project."""
    write_project_config(ground)
    result = cli(["review"], cwd=bare(ground), env=user_at(ground), timeout=150)
    assert result.returncode == 0, result.console
    assert os.path.join(ground, "council.toml") in result.console, "the narration names the file it convened from"
    assert result.manifest["config_layers"] == [os.path.join(ground, "council.toml")]


def test_the_project_layer_replaces_a_same_named_council_and_says_so(cli, project, ground):
    write_project_config(ground, one_council("the goal nobody should see"))
    write_project_config(project, one_council("the goal of this project"))
    result = cli(["review"], cwd=project, env=user_at(ground), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["goal"] == "the goal of this project"
    assert "the goal nobody should see" not in result.console, "the whole table is replaced, not merged"
    assert "replaces any council of the same name" in result.console, "the shadowing is stated, not assumed"


def test_the_project_layer_extends_a_roster_kept_in_the_user_config(cli, project, ground):
    write_project_config(ground)  # the reference project config: a roster and a chair, stated once
    write_project_config(project, '[councils.review-staged]\nextends = "review"\ngoal = "Did the staged work land?"\n')
    result = cli(["review-staged"], cwd=project, env=user_at(ground), timeout=150)
    assert result.returncode == 0, result.console
    assert [row["name"] for row in result.manifest["roster"]] == ["Metic", "Senior"], "the roster came from the user layer"
    assert result.manifest["extends"] == ["review"] and result.manifest["goal"] == "Did the staged work land?"


def test_no_config_anywhere_names_both_places_it_looked(cli, ground):
    env = {"XDG_CONFIG_HOME": os.path.join(ground, "no-user-config")}
    result = cli(["review"], cwd=bare(ground), env=env)
    assert result.returncode == 1
    for path in (os.path.join(ground, "no-user-config", "council", "council.toml"), os.path.join(bare(ground), "council.toml")):
        assert path in result.console, f"the refusal should name {path}"


def test_config_names_the_only_file_read(cli, project, ground):
    write_project_config(ground)  # this one offers a `plan` council, and --config must not see it
    write_project_config(project, one_council("g"))
    result = cli(["plan", "-c", os.path.join(project, "council.toml")], cwd=project, env=user_at(ground))
    assert result.returncode == 2
    assert "no council 'plan'" in result.console
    assert os.path.join(ground, "council.toml") not in result.console


def test_a_project_council_may_not_name_a_pi_that_arrived_with_the_project(cli, project):
    planted = os.path.join(project, "pi-harness.py")
    shutil.copyfile(FAKE_PI, planted)
    os.chmod(planted, 0o755)
    write_project_config(project, one_council("g", pi=planted))
    result = cli(["review"], cwd=project)
    assert result.returncode == 1
    assert "inside the project" in result.console and "user council.toml" in result.console, result.console


def test_the_same_executable_is_fine_once_your_own_config_names_it(cli, project, ground):
    planted = os.path.join(project, "pi-harness.py")
    shutil.copyfile(FAKE_PI, planted)
    os.chmod(planted, 0o755)
    write_project_config(ground, one_council("g", pi=planted))
    write_project_config(project, one_council("g", pi=None))
    result = cli(["review"], cwd=project, env=user_at(ground), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["pi_cli"] == os.path.realpath(planted)


def home_layer(ground: str) -> str:
    """A `$HOME` holding `~/.config/council/council.toml`: where the councils of someone who set nothing at all live."""
    home = os.path.join(ground, "home")
    directory = os.path.join(home, ".config", "council")
    os.makedirs(directory, exist_ok=True)
    write_project_config(directory, one_council("convened from the home default"))
    return home


def test_the_home_default_convenes_a_project_that_has_none(cli, ground):
    """The README's promise, with nothing set but `$HOME` — the case no `COUNCIL_CONFIG` or `XDG_CONFIG_HOME` test reaches."""
    home = home_layer(ground)
    result = cli(["review"], cwd=bare(ground), env={"HOME": home}, extra_scrub=("XDG_CONFIG_HOME", "COUNCIL_CONFIG"), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["config_layers"] == [os.path.join(home, ".config", "council", "council.toml")]
    assert result.manifest["goal"] == "convened from the home default"


def test_an_invalid_xdg_config_home_is_ignored_and_the_home_default_still_convenes(cli, ground):
    """`XDG_CONFIG_HOME=.config` is invalid, so it is ignored rather than fatal: the home default stays the user layer
    instead of the run refusing over a project file that was never the one at fault."""
    home = home_layer(ground)
    result = cli(["review"], cwd=bare(ground), env={"HOME": home, "XDG_CONFIG_HOME": ".config"}, timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["goal"] == "convened from the home default"


@pytest.mark.parametrize(
    "council_config, xdg, home, expected",
    [
        # $COUNCIL_CONFIG wins when it is set at all, relative or not …
        ("cfg.toml", "xdg", "home", "cfg.toml"),
        # … unset → $XDG_CONFIG_HOME/council/council.toml …
        (None, "xdg", "home", os.path.join("xdg", "council", "council.toml")),
        # … unset and empty alike …
        (None, "", "home", os.path.join("home", ".config", "council", "council.toml")),
        # … unset → the home default, asserted so a change is a visible edit …
        (None, None, "home", os.path.join("home", ".config", "council", "council.toml")),
        # … and a relative $XDG_CONFIG_HOME is invalid, so the default is used anyway, as for the runs root …
        (None, "relative-xdg", "home", os.path.join("home", ".config", "council", "council.toml")),
        # … with a relative one and no home there is nothing that could hold a council
        (None, "relative-xdg", None, None),
    ],
)
def test_user_config_resolution(ground, council_config, xdg, home, expected):
    from mech_council.cli import _user_config_path

    env = {"CWD": ground}
    for key, name in (("COUNCIL_CONFIG", council_config), ("XDG_CONFIG_HOME", xdg), ("HOME", home)):
        if name is None:
            continue
        # A relative `$XDG_CONFIG_HOME` stays relative on purpose, and an empty one stays empty.
        env[key] = name if name in ("", "relative-xdg") else os.path.join(ground, name)
    assert _user_config_path(env) == (None if expected is None else os.path.realpath(os.path.join(ground, expected)))


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
    from mech_council.cli import _runs_root_from_env

    env = {"CWD": ground}
    for key, name in (("COUNCIL_RUNS_DIR", runs), ("XDG_CACHE_HOME", xdg), ("HOME", home)):
        if name is None:
            continue
        # `relative-xdg` stays relative on purpose: the resolution rules say it is ignored.
        env[key] = name if name == "relative-xdg" else os.path.join(ground, name)
    assert _runs_root_from_env(env) == os.path.realpath(os.path.join(ground, expected))


def test_a_relative_runs_root_is_absolute_against_the_invoking_cwd(ground):
    from mech_council.cli import _runs_root_from_env

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
    # The offline plan fixture never converges, so the section must say so rather than print a heading nobody chose.
    direction = report.split("## Chosen direction", 1)[1].split("\n## ", 1)[0]
    assert "did not settle a direction" in direction, f"the report chose a direction this run never agreed:\n{direction[:400]}"
    assert "backed by" not in direction.split("The candidates")[0], "no `backed by` line stands where no direction was chosen"
    # Nothing here invents a chair-authored rule list.
    assert "prescriptive" in report and "descriptive" in report
    assert "wrap the call in a retry helper" in report, "the stage gates reach the plan"
    assert "bounded backoff" in report
    assert result.manifest["outcome"] in ("consensus", "stalemate", "round_limit_reached")


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
    """The human checkpoint's wait rule with `grace_rounds = 0`: `round_limit_reached` is the one way a question ends unasked."""
    write_project_config(project, DEFAULT_CONFIG.format(pi_cli=FAKE_PI).replace("max_rounds = 2", "max_rounds = 2\ngrace_rounds = 0"))
    # Round 2 raises something new, so the run ends on the round limit and not on termination's stalemate.
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


# ------------------------------------------------------ Where the report lands


def output_dir(project: str) -> str:
    """A directory that is neither the project nor the runs root."""
    path = os.path.join(os.path.dirname(project), "reported")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _one_finding_script() -> str:
    """The shortest path to a whole report."""
    return json.dumps(
        [
            {
                "contains": "Phase 0",
                "reply": {"findings": [{"claim": "the cache ignores tenants", "severity": "major", "evidence": "src/app.py:1"}]},
            },
            {"contains": "Round ", "reply": {"positions": [{"id": "F1", "stance": "agree", "note": "read the key"}], "sign_off": True}},
            {"contains": "# Deduplicate", "reply": {"groups": [], "human_input": []}},
        ]
    )


def test_output_writes_the_report_there_and_leaves_the_rest_behind(cli, project):
    out = output_dir(project)
    env = dict(runs_env(project), FAKE_PI_SCRIPT=_one_finding_script())
    result = cli(["review", "-o", out], cwd=project, env=env, timeout=150)
    target = os.path.join(out, "report.md")
    assert result.returncode == 0, result.console
    assert os.path.exists(target)
    assert result.manifest["report"] == target, "run.json names where the report actually stands"
    written = sorted(name for name in os.listdir(result.run_dir) if os.path.isfile(os.path.join(result.run_dir, name)))
    assert "report.md" not in written and "plan.md" not in written, "-o replaces the run directory's copy rather than adding one"
    assert "run.json" in written and "events.jsonl" in written, "everything else still lands in the run directory"
    assert "the cache ignores tenants" in result.report()
    assert f"report {target}" in result.console
    assert f"note: --output writes the report to {target}" in result.console


def test_an_interrupted_run_still_writes_the_report_where_you_asked(signalled_cli, project):
    out = output_dir(project)
    env = dict(runs_env(project), FAKE_PI_SCRIPT=_cut_short_script())
    result = signalled_cli(["review", "-o", out], cwd=project, env=env, wait_for="chair merged", settle=0.5)
    assert result.returncode == 130, result.console
    assert "the cache ignores tenants" in result.report(), "the interrupted run's report is at --output, not in the run directory"
    assert not os.path.exists(os.path.join(result.run_dir, "report.md"))


def test_a_directory_keeps_the_name_the_mode_gives_it(cli, project):
    out = output_dir(project)
    env = dict(runs_env(project), FAKE_PI_SCRIPT=PLAN_SCRIPT)
    result = cli(["plan", "-o", out], cwd=project, env=env, timeout=150)
    assert result.returncode == 0, result.console
    assert os.path.exists(os.path.join(out, "plan.md")), "plan mode keeps plan.md wherever it is asked to write"
    assert not os.path.exists(os.path.join(out, "report.md"))


def test_the_report_is_named_where_it_lands_and_says_so(cli, project):
    out = os.path.join(output_dir(project), "PLAN.md")
    env = dict(runs_env(project), FAKE_PI_SCRIPT=_one_finding_script())
    result = cli(["review", "-o", out], cwd=project, env=env, timeout=150)
    assert result.returncode == 0, result.console
    assert os.path.exists(out), "a file target is the file itself, whatever the mode's name"
    assert f"- `{out}` — this report (outside the run directory)" in result.report(), "the report says where it is, not where it usually is"


@pytest.mark.parametrize("way", ["answered yes", "--overwrite"])
def test_output_replaces_a_file_once_you_said_so(cli, project, way):
    out = output_dir(project)
    target = os.path.join(out, "PLAN.md")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("handwritten\n")
    argv = ["review", "-o", target] if way == "answered yes" else ["review", "-o", target, "--overwrite"]
    env = dict(runs_env(project), FAKE_PI_SCRIPT=_one_finding_script())
    result = cli(argv, cwd=project, stdin="y\n" if way == "answered yes" else None, env=env, timeout=150)
    assert result.returncode == 0, result.console
    report = result.report()
    assert "the cache ignores tenants" in report and "handwritten" not in report, "the report replaced the file you okayed"
    assert ("overwrite it?" in result.stderr) == (way == "answered yes"), "--overwrite skips the asking, nothing else"
    assert "overwrite it?" not in result.stdout, "the guard is not narration, so a redirected stdout does not swallow it"
    assert "that was already there" in result.console


def test_a_declined_overwrite_ends_before_anything_starts(cli, project, canary):
    target = os.path.join(output_dir(project), "PLAN.md")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("handwritten\n")
    result = cli(["review", "-o", target], cwd=project, stdin="\n", timeout=150)
    assert result.returncode == 1, result.console
    assert "--overwrite" in result.console, "the refusal says how to get past it"
    assert "stdin is not a terminal" in result.console, "a question nobody could have seen is said out loud"
    with open(target, encoding="utf-8") as handle:
        assert handle.read() == "handwritten\n", "a no keeps the file as it was"
    assert result.run_dir is None and canary_entries(canary) == [], "a declined overwrite creates nothing anywhere"


def test_output_naming_a_place_that_cannot_hold_a_file_exits_two(cli, project, canary):
    missing = os.path.join(os.path.dirname(project), "no-such-directory", "PLAN.md")
    result = cli(["review", "-o", missing], cwd=project)
    assert result.returncode == 2
    assert "--output" in result.console
    assert canary_entries(canary) == [], "a usage error creates nothing anywhere"


@pytest.mark.skipif(os.getuid() == 0, reason="uid 0 writes anywhere, so only another uid can be refused")
def test_output_naming_a_directory_you_cannot_write_exits_two(cli, project, canary):
    out = output_dir(project)
    os.chmod(out, 0o500)
    result = cli(["review", "-o", out], cwd=project)
    assert result.returncode == 2
    assert "--output" in result.console
    assert canary_entries(canary) == [], "a usage error creates nothing anywhere"


def test_an_overwrite_with_nothing_to_replace_says_so(cli, project):
    script = json.dumps([{"contains": "", "stop_reason": "error", "error_message": "429 provider quota exhausted"}])
    result = cli(["review", "--overwrite"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=120)
    assert any(line.startswith("note: --overwrite") for line in result.console.splitlines()), result.console


def test_a_relative_output_lands_in_the_project_you_invoked_from(cli, project):
    env = dict(runs_env(project), FAKE_PI_SCRIPT=PLAN_SCRIPT)
    result = cli(["plan", "-o", "PLAN.md"], cwd=project, env=env, timeout=150)
    target = os.path.join(project, "PLAN.md")
    assert result.returncode == 0, result.console
    assert os.path.exists(target) and result.manifest["report"] == target
    assert "note: the report lands inside" in result.console, "an ordinary write into the project is said out loud"
