"""Implement mode: the config surface, crew resolution, and a full run off a plan document."""

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

[councils.planners]
goal = "Plan the build"
mode = "plan"

[councils.planners.chair]
name = "Plan Chair"

[[councils.planners.agents]]
name = "Metic"

[[councils.planners.agents]]
name = "Senior"

[councils.inspect]
goal = "Review the stage"
mode = "review"

[councils.inspect.chair]
name = "Review Chair"

[[councils.inspect.agents]]
name = "Metic"

[[councils.inspect.agents]]
name = "Senior"

[councils.build]
goal = "Build the hello tool"
mode = "implement"
planner = "planners"
reviewer = "inspect"

[councils.build.chair]
name = "Chair"

[[councils.build.agents]]
name = "Forge"
role = "both"
"""


def convene(tmp_path, text: str, name: str = "build", **kw):
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


# ------------------------------------------------------------- the config surface


def test_an_implement_council_convenes_with_its_crew_keys(tmp_path):
    council = convene(tmp_path, body())
    assert council.mode == "implement"
    assert council.planning_rounds == 4 and council.fix_budget == 3 and council.replans == 2
    assert council.planner == "planners" and council.reviewer == "inspect"
    assert council.in_place is True, "an implement run is in place by default"
    assert council.agents[0].role == "both"


def test_an_implement_council_needs_a_goal(tmp_path):
    with pytest.raises(ConfigError):
        convene(tmp_path, body().replace('goal = "Build the hello tool"\n', ""))


def test_a_planner_must_name_a_plan_council(tmp_path):
    with pytest.raises(ConfigError):
        convene(tmp_path, body().replace('planner = "planners"', 'planner = "inspect"'))


def test_a_reviewer_must_name_a_review_council(tmp_path):
    with pytest.raises(ConfigError):
        convene(tmp_path, body().replace('reviewer = "inspect"', 'reviewer = "planners"'))


def test_a_reviewer_that_states_no_mode_takes_the_builtin_default(tmp_path):
    """A reviewer council that omits `mode` is a review council by the built-in default: the
    reference holds, and the error never names a None council."""
    text = body().replace('goal = "Review the stage"\nmode = "review"', 'goal = "Review the stage"')
    council = convene(tmp_path, text)
    assert council.reviewer == "inspect"
    loaded = config_mod.load_config(os.path.join(str(tmp_path), "council.toml"))
    crews = config_mod.resolve_crews(loaded, council, Args())
    assert [label for label, _ in crews] == ["planner", "builder", "reviewer"]


def test_a_planner_on_a_non_implement_council_is_refused(tmp_path):
    with pytest.raises(ConfigError):
        convene(
            tmp_path,
            body().replace(
                '[councils.inspect]\ngoal = "Review the stage"',
                '[councils.inspect]\ngoal = "Review the stage"\nplanner = "planners"',
            ),
            name="inspect",
        )


def test_the_plan_flag_is_an_implement_flag(tmp_path):
    with pytest.raises(UsageError):
        convene(tmp_path, body(), name="inspect", plan=os.path.join(str(tmp_path), "plan.md"))


def test_the_dirty_flag_is_an_implement_flag(tmp_path):
    with pytest.raises(UsageError):
        convene(tmp_path, body(), name="inspect", dirty=True)


def test_a_seat_role_must_be_a_builder_role(tmp_path):
    with pytest.raises(ConfigError):
        convene(tmp_path, body().replace('role = "both"', 'role = "cook"'))


def test_a_builder_council_needs_a_planner_or_a_plan_file(tmp_path):
    """The builders never plan a run of their own: with neither a planner council nor --plan the
    convene is refused, however many builders it declares; a plan file stands in for the planner."""
    solo = body().replace('planner = "planners"\nreviewer = "inspect"', 'reviewer = "inspect"')
    with pytest.raises(ConfigError):
        convene(tmp_path, solo)
    pair = solo.replace(
        '[[councils.build.agents]]\nname = "Forge"\nrole = "both"',
        '[[councils.build.agents]]\nname = "Forge"\nrole = "both"\n\n[[councils.build.agents]]\nname = "Hammer"\nrole = "both"',
    )
    with pytest.raises(ConfigError):
        convene(tmp_path, pair)
    council = convene(tmp_path, solo, plan=os.path.join(str(tmp_path), "plan.md"))
    assert len(council.agents) == 1


def test_a_seat_role_is_an_implement_knob(tmp_path):
    text = body().replace(
        '[[councils.inspect.agents]]\nname = "Metic"',
        '[[councils.inspect.agents]]\nname = "Metic"\nrole = "code"',
    )
    with pytest.raises(ConfigError):
        convene(tmp_path, text, name="inspect")


def test_a_chair_role_is_refused_in_every_mode(tmp_path):
    """`role` is a builder-seat knob and the chair never builds: a `role` in the chair table is
    refused in an implement council and in the borrowed ones alike."""
    with pytest.raises(ConfigError):
        convene(tmp_path, body().replace('[councils.build.chair]\nname = "Chair"', '[councils.build.chair]\nname = "Chair"\nrole = "both"'))
    with pytest.raises(ConfigError):
        convene(
            tmp_path,
            body().replace(
                '[councils.inspect.chair]\nname = "Review Chair"',
                '[councils.inspect.chair]\nname = "Review Chair"\nrole = "prose"',
            ),
            name="inspect",
        )


def test_the_stage_diff_is_a_snapshot_since_the_last_review(tmp_path):
    """A modified tracked file shows its hunks — never a whole-content 'new file' dump — and the
    second review shows only what changed since the first. The run directory sits outside the
    project, the way a run's root always does, so its private object store is never snapshotted."""
    import asyncio
    import types
    from typing import cast

    from mech_council.deliberation.implement import Implement

    project = os.path.join(str(tmp_path), "proj")
    os.makedirs(project)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    with open(os.path.join(project, "app.py"), "w", encoding="utf-8") as handle:
        handle.write("v = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=project, check=True)

    host = types.SimpleNamespace(project=project, run_dir=os.path.join(str(tmp_path), "runs"))
    host._git = types.MethodType(Implement._git, host)
    host._snapshot_tree = types.MethodType(Implement._snapshot_tree, host)
    host.last_tree = Implement._snapshot_tree(cast("Implement", host))
    assert host.last_tree, "the starting tree snapshots"

    with open(os.path.join(project, "app.py"), "w", encoding="utf-8") as handle:
        handle.write("v = 2\n")
    diff = asyncio.run(Implement._stage_diff(cast("Implement", host), changed=["app.py"]))
    assert "-v = 1" in diff and "+v = 2" in diff
    assert "new file" not in diff, "a modified tracked file is not a new file"

    with open(os.path.join(project, "docs.md"), "w", encoding="utf-8") as handle:
        handle.write("# docs\n")
    diff = asyncio.run(Implement._stage_diff(cast("Implement", host), changed=["docs.md"]))
    assert "+++ b/docs.md" in diff, "a genuinely new file shows as a real new-file diff"

    with open(os.path.join(project, "app.py"), "w", encoding="utf-8") as handle:
        handle.write("v = 3\n")
    diff = asyncio.run(Implement._stage_diff(cast("Implement", host), changed=["app.py"]))
    assert "-v = 2" in diff and "+v = 3" in diff
    assert "+v = 2" not in diff and "-v = 1" not in diff, "only what changed since the last review"


def test_a_failed_diff_does_not_advance_the_last_tree(tmp_path):
    """A review whose git diff fails falls back to the changed files, but the failed pass is not
    the next review's baseline: the next diff still shows what the failed pass changed, or it
    would be the only record of it."""
    import asyncio
    import types
    from typing import cast

    from mech_council.deliberation.implement import Implement

    project = os.path.join(str(tmp_path), "proj")
    os.makedirs(project)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    with open(os.path.join(project, "app.py"), "w", encoding="utf-8") as handle:
        handle.write("v = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=project, check=True)

    host = types.SimpleNamespace(project=project, run_dir=os.path.join(str(tmp_path), "runs"))
    host._git = types.MethodType(Implement._git, host)
    host._snapshot_tree = types.MethodType(Implement._snapshot_tree, host)
    host.last_tree = Implement._snapshot_tree(cast("Implement", host))
    baseline_tree = host.last_tree
    assert baseline_tree

    real_git = host._git

    def failing_diff(*args, **kwargs):
        if args and args[0] == "diff":
            return False, ""
        return real_git(*args, **kwargs)

    host._git = failing_diff
    with open(os.path.join(project, "app.py"), "w", encoding="utf-8") as handle:
        handle.write("v = 2\n")
    diff = asyncio.run(Implement._stage_diff(cast("Implement", host), changed=["app.py"]))
    assert "### app.py" in diff and "v = 2" in diff, "a failed diff falls back to the changed files"
    assert host.last_tree == baseline_tree, "the failed diff is not the next review's baseline"

    host._git = real_git
    with open(os.path.join(project, "app.py"), "w", encoding="utf-8") as handle:
        handle.write("v = 3\n")
    diff = asyncio.run(Implement._stage_diff(cast("Implement", host), changed=["app.py"]))
    assert "-v = 1" in diff and "+v = 3" in diff, "the next review still sees what the failed pass changed"


def test_the_snapshot_is_scoped_to_the_project_when_it_sits_in_a_larger_repo(tmp_path):
    """A project that is a subdirectory of a larger repo snapshots only the project's files: a change
    outside the project never enters a stage diff, and neither does the run's private object store."""
    import asyncio
    import types
    from typing import cast

    from mech_council.deliberation.implement import Implement

    repo = str(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    with open(os.path.join(repo, "outside.py"), "w", encoding="utf-8") as handle:
        handle.write("outside = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=repo, check=True, capture_output=True)
    project = os.path.join(repo, "proj")
    os.makedirs(project)
    with open(os.path.join(project, "app.py"), "w", encoding="utf-8") as handle:
        handle.write("v = 1\n")

    host = types.SimpleNamespace(project=project, run_dir=os.path.join(repo, "runs"))
    host._git = types.MethodType(Implement._git, host)
    host._snapshot_tree = types.MethodType(Implement._snapshot_tree, host)
    host.last_tree = Implement._snapshot_tree(cast("Implement", host))
    assert host.last_tree, "the starting tree snapshots"

    with open(os.path.join(repo, "outside.py"), "w", encoding="utf-8") as handle:
        handle.write("outside = 2\n")
    with open(os.path.join(project, "app.py"), "w", encoding="utf-8") as handle:
        handle.write("v = 2\n")
    diff = asyncio.run(Implement._stage_diff(cast("Implement", host), changed=["app.py"]))
    assert "-v = 1" in diff and "+v = 2" in diff
    assert "outside" not in diff, "a change outside the project is not in the stage diff"


def test_the_snapshots_keep_the_repo_object_store_untouched(tmp_path):
    """The run's trees land in the run directory's private object store: a run that never commits
    leaves the repo's object store as it found it, and the store's trees still diff."""
    import asyncio
    import types
    from typing import cast

    from mech_council.deliberation.implement import Implement

    project = os.path.join(str(tmp_path), "proj")
    run_dir = os.path.join(str(tmp_path), "runs")
    os.makedirs(project)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    # git >= 2.55 detaches an auto-maintenance run from the commit below; its transient
    # .git/objects/maintenance.lock would race the store listing under load
    subprocess.run(["git", "config", "maintenance.auto", "false"], cwd=project, check=True)
    with open(os.path.join(project, "app.py"), "w", encoding="utf-8") as handle:
        handle.write("v = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=project, check=True)

    def objects() -> list[str]:
        found: list[str] = []
        for root, _dirs, files in os.walk(os.path.join(project, ".git", "objects")):
            found += [os.path.join(root, name) for name in files]
        return sorted(found)

    before = objects()

    host = types.SimpleNamespace(project=project, run_dir=run_dir)
    host._git = types.MethodType(Implement._git, host)
    host._snapshot_tree = types.MethodType(Implement._snapshot_tree, host)
    host._stage_diff = types.MethodType(Implement._stage_diff, host)
    host.last_tree = Implement._snapshot_tree(cast("Implement", host))
    assert host.last_tree, "the starting tree snapshots"

    with open(os.path.join(project, "app.py"), "w", encoding="utf-8") as handle:
        handle.write("v = 2\n")
    diff = asyncio.run(Implement._stage_diff(cast("Implement", host), changed=["app.py"]))
    assert "+v = 2" in diff, "the trees in the private store still diff"

    assert objects() == before, "the repo's object store is untouched by the run's snapshots"
    assert os.path.isdir(os.path.join(run_dir, "review_objects")), "the run's trees live in the run directory"


def test_the_dirty_check_measures_only_the_project_tree(tmp_path):
    """A project that is a subdirectory of a larger repo: dirt outside the project does not refuse
    the run, dirt inside it does — the check measures the same tree the snapshots measure."""
    import types
    from typing import cast

    from mech_council import startup

    repo = str(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    with open(os.path.join(repo, "outside.txt"), "w", encoding="utf-8") as handle:
        handle.write("outside = 1\n")
    project = os.path.join(repo, "proj")
    os.makedirs(project)
    with open(os.path.join(project, "app.py"), "w", encoding="utf-8") as handle:
        handle.write("v = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=repo, check=True, capture_output=True)

    council = cast(config_mod.Council, types.SimpleNamespace(project=project))
    with open(os.path.join(repo, "outside.txt"), "a", encoding="utf-8") as handle:
        handle.write("outside = 2\n")
    notes = startup._implement_notes(council, Args())
    assert not any("dirty" in note for note in notes), "dirt outside the project does not refuse the run"

    with open(os.path.join(project, "app.py"), "a", encoding="utf-8") as handle:
        handle.write("v = 2\n")
    with pytest.raises(UsageError):
        startup._implement_notes(council, Args())


# ------------------------------------------------------------ crew resolution


def test_crews_line_up_in_crew_order_with_global_seat_ids(tmp_path):
    council = convene(tmp_path, body())
    loaded = config_mod.load_config(os.path.join(str(tmp_path), "council.toml"))
    crews = config_mod.resolve_crews(loaded, council, Args())
    assert [label for label, _ in crews] == ["planner", "builder", "reviewer"]
    planner, builder, reviewer = (crew for _label, crew in crews)
    assert [a.id for a in planner.agents] == ["Agent1", "Agent2"]
    assert [a.id for a in builder.agents] == ["Agent3"]
    assert [a.id for a in reviewer.agents] == ["Agent4", "Agent5"]
    # the borrowed seats are the borrowed council's roster, suffixed so a human can tell the crews apart
    assert planner.agents[0].name == "Metic (planner)"
    assert reviewer.agents[0].file_base.startswith("reviewer-")
    assert builder.agents[0].name == "Forge", "the run's own roster is not a borrowed one"


def test_a_plan_file_skips_the_planner_crew(tmp_path):
    council = convene(tmp_path, body())
    loaded = config_mod.load_config(os.path.join(str(tmp_path), "council.toml"))
    crews = config_mod.resolve_crews(loaded, council, Args(plan=os.path.join(str(tmp_path), "plan.md")))
    assert [label for label, _ in crews] == ["builder", "reviewer"]
    _label, builder = crews[0]
    _label, reviewer = crews[1]
    assert [a.id for a in builder.agents] == ["Agent1"]
    assert [a.id for a in reviewer.agents] == ["Agent2", "Agent3"]


def test_a_goalless_planner_council_takes_the_runs_goal(tmp_path):
    """A plan council that states no goal of its own is still borrowable: the run's goal is what it
    plans, and it is what lets it convene at all."""
    text = body().replace('goal = "Plan the build"\n', "")
    council = convene(tmp_path, text)
    loaded = config_mod.load_config(os.path.join(str(tmp_path), "council.toml"))
    crews = config_mod.resolve_crews(loaded, council, Args())
    planner = next(crew for label, crew in crews if label == "planner")
    assert planner.goal == "Build the hello tool"


def test_disposed_planner_seats_leave_the_bar_the_build_stops_are_measured_against():
    """The planner crew disposes at the phase boundary: its seats leave the shared docket, or the bar
    a declared stop must reach can exceed the live crew and the run never waits on its own blocking asks."""
    import types
    from typing import cast

    from mech_council.deliberation.implement import Implement
    from mech_council.deliberation.state import Docket

    planner_ids = [f"Agent{i}" for i in range(1, 5)]  # four planners; two builders and two reviewers stay live
    roster = [f"Agent{i}" for i in range(1, 9)]
    planner_crew = types.SimpleNamespace(council=types.SimpleNamespace(agents=[types.SimpleNamespace(id=one) for one in planner_ids]))
    host = types.SimpleNamespace(
        docket=Docket(mode="implement", goal="g", roster=roster, chair_name="Chair", seconding=True, human="blocking"),
        crews=types.SimpleNamespace(planner=planner_crew),
    )
    live = len(host.docket.roster) - len(planner_ids)
    assert host.docket.bar() > live, "before: the bar can exceed the live crew"
    Implement._planners_leave(cast("Implement", host))
    assert host.docket.roster == [f"Agent{i}" for i in range(5, 9)], "the live crew keeps its seats"
    assert host.docket.bar() <= live, "after: the bar is measured against the live crew"


# ------------------------------------------------------------- the builder's reply shapes


def test_a_work_reply_keeps_only_lines_that_name_real_seats_and_items():
    from mech_council.extraction import parse_work

    parsed = parse_work(
        json.dumps(
            {
                "summary": "built the stage",
                "changed": ["a.py"],
                "say_to": [{"to": "Agent2", "text": "heads up"}, {"to": "chair", "text": "no seat by that name"}],
                "addressed": [{"id": "C1", "how": "fixed"}, {"id": "x1", "how": "not an item id"}],
            }
        )
    )
    assert not parsed.invalid
    assert parsed.data["say_to"] == [{"to": "Agent2", "text": "heads up"}], "say_to may only name a seat id"
    assert parsed.data["addressed"] == [{"id": "C1", "how": "fixed"}], "addressed may only name a docket item"


def test_user_text_accumulates_across_passes():
    """A fix pass that names no user-visible surfaces does not erase the ones the work pass named:
    the stage's `user_text` is the union of its passes, the way `changed` is."""
    import types
    from typing import cast

    from mech_council.deliberation.implement import Implement
    from mech_council.deliberation.state import Docket, StageRecord
    from mech_council.extraction import parse_work

    host = types.SimpleNamespace(
        docket=Docket(mode="implement", goal="g", roster=["Agent1"], chair_name="Chair", seconding=True, human="none")
    )
    rec = StageRecord(index=1, state="write it", gate="test -f app.py")
    Implement._apply_work(
        cast("Implement", host), rec, parse_work(json.dumps({"summary": "built it", "changed": ["app.py"], "user_text": ["docs.md"]}))
    )
    Implement._apply_work(
        cast("Implement", host),
        rec,
        parse_work(json.dumps({"summary": "fixed it", "addressed": [{"id": "C1", "how": "done"}]})),
        fix_pass=1,
    )
    assert rec.user_text == ["docs.md"], "a later pass that names no surfaces keeps the earlier ones"
    assert rec.changed == ["app.py"]


def test_a_replan_and_a_structure_reply_carry_stages():
    from mech_council.extraction import parse_replan, parse_structure

    parsed = parse_replan(json.dumps({"note": "smaller", "stages": [{"state": "s1", "gate": "g"}]}))
    assert not parsed.invalid and parsed.data["stages"] and parsed.data["note"] == "smaller"
    parsed = parse_replan(json.dumps({"note": "no stages here"}))
    assert parsed.errors, "a replan with no stages is refused and re-asked"
    data, errors = parse_structure(json.dumps({"stages": [{"state": "s", "gate": "g", "kind": "prose"}]}))
    assert data is not None and not errors and data["stages"][0]["kind"] == "prose"
    data, _errors = parse_structure(json.dumps({"stages": [{"state": "s", "kind": "both"}]}))
    assert data is not None and data["stages"][0]["kind"] == "code", "a kind the chair mangled counts as code"


def test_the_chair_can_pick_only_a_direction_the_plans_named():
    from mech_council.extraction import parse_pick

    data, _errors = parse_pick(json.dumps({"backing": "Agent1"}))
    assert data == {"backing": "Agent1"}
    assert parse_pick(json.dumps({"backing": "no_preference"}))[0] is None


# ------------------------------------------------------------ the adoption rule


def test_a_majority_backed_direction_is_adopted_and_a_split_goes_to_the_run_chair():
    """D1: on budget exhaustion a strict majority is adopted; with no majority the run's chair picks,
    and only from the directions the plans named."""
    import asyncio
    import types
    from typing import cast

    from mech_council.deliberation.engine import Abort, Engine
    from mech_council.deliberation.implement import Implement
    from mech_council.deliberation.state import Docket, Execution

    class PickPort:
        def __init__(self, token):
            self.token = token
            self.asked = False

        async def chair_pick(self, message):
            self.asked = True
            return {"backing": self.token}

    def resolve(n, backings, token):
        docket = Docket(
            mode="plan", goal="g", roster=[f"Agent{i}" for i in range(1, n + 1)], chair_name="Chair", seconding=True, human="blocking"
        )
        docket.backing = backings
        port = PickPort(token)
        requested: list = []

        def pick_port_for(crew):
            requested.append(crew)
            return port

        host = types.SimpleNamespace(
            docket=docket,
            execution=Execution(),
            goal="g",
            names={a: a for a in docket.roster},
            console=types.SimpleNamespace(say=lambda line: None),
            crews={"builder": "builder-crew"},
        )
        host._port = pick_port_for
        host._adopted = types.MethodType(Implement._adopted, host)
        engine = types.SimpleNamespace(direction_option=lambda t: t)
        try:
            chosen = asyncio.run(Implement._resolve_direction(cast("Implement", host), docket, cast("Engine", engine)))
            return chosen, host.execution.adopted_how, port.asked, requested
        except Abort:
            return "aborted", None, port.asked, requested

    # a strict majority (2 of 3) is adopted: the chair is never asked
    chosen, how, asked, _requested = resolve(3, {"Agent1": "Agent1", "Agent2": "Agent1", "Agent3": "Agent2"}, "ZZZ")
    assert (chosen, how, asked) == ("Agent1", "majority", False)
    # no majority: the run's chair picks, and the pass goes to the run's crew, not the planner crew's
    chosen, how, asked, requested = resolve(3, {"Agent1": "Agent1", "Agent2": "Agent2", "Agent3": "Agent3"}, "Agent2")
    assert (chosen, how, asked, requested) == ("Agent2", "chair", True, ["builder-crew"])
    # the chair's invention is refused
    chosen, _how, _asked, _requested = resolve(3, {"Agent1": "Agent1", "Agent2": "Agent2", "Agent3": "Agent3"}, "ZZZ")
    assert chosen == "aborted"


def test_declined_backings_are_not_a_majority_camp():
    """A planner that declined to name a backing is not a camp: a decline that meets the bar never
    wins the adoption — the run's chair picks from the directions the plans named instead."""
    import asyncio
    import types
    from typing import cast

    from mech_council.deliberation.engine import Abort, Engine
    from mech_council.deliberation.implement import Implement
    from mech_council.deliberation.state import DECLINED, Docket, Execution

    class PickPort:
        def __init__(self, token):
            self.token = token
            self.asked = False

        async def chair_pick(self, message):
            self.asked = True
            return {"backing": self.token}

    def resolve(backings, token):
        docket = Docket(mode="plan", goal="g", roster=list(backings), chair_name="Chair", seconding=True, human="blocking")
        docket.backing = dict(backings)
        port = PickPort(token)
        host = types.SimpleNamespace(
            docket=docket,
            execution=Execution(),
            goal="g",
            names={a: a for a in docket.roster},
            console=types.SimpleNamespace(say=lambda line: None),
            crews={"builder": "builder-crew"},
        )
        host._port = lambda crew: port
        host._adopted = types.MethodType(Implement._adopted, host)
        engine = types.SimpleNamespace(direction_option=lambda t: t)
        try:
            chosen = asyncio.run(Implement._resolve_direction(cast("Implement", host), docket, cast("Engine", engine)))
            return chosen, host.execution.adopted_how, port.asked
        except Abort:
            return "aborted", None, port.asked

    # three of four planners declined to name a backing: the decline is not a camp, so the one real
    # camp that misses the bar goes to the run's chair, and the chair is asked
    chosen, how, asked = resolve({"Agent1": "Agent1", "Agent2": DECLINED, "Agent3": DECLINED, "Agent4": DECLINED}, "Agent1")
    assert (chosen, how, asked) == ("Agent1", "chair", True)
    # the real camp that meets the bar still wins outright, the declines simply not counting
    chosen, how, asked = resolve({"Agent1": "Agent1", "Agent2": "Agent1", "Agent3": DECLINED}, "Agent1")
    assert (chosen, how, asked) == ("Agent1", "majority", False)


def test_settled_planning_decisions_stay_out_of_the_builder_prompt():
    """An answered decision is already baked into the plan the builders execute: the report keeps
    every planning decision point, but only the ones no answer settled ride into the work prompt."""
    import types
    from typing import cast

    from mech_council.deliberation import escalations
    from mech_council.deliberation.implement import Implement
    from mech_council.deliberation.state import ANSWERED, SKIPPED, Docket, Execution, StageRecord

    plan_docket = Docket(mode="plan", goal="g", roster=["Agent1", "Agent2"], chair_name="Chair", seconding=True, human="blocking")
    escalations.raise_question(plan_docket, text="which storage engine?", asker="Agent1", item_id=None, round_=1)
    escalations.raise_question(plan_docket, text="which data directory?", asker="Agent1", item_id=None, round_=1)
    plan_docket.questions["H1"].state, plan_docket.questions["H1"].answer = ANSWERED, "sql"
    plan_docket.questions["H2"].state = SKIPPED

    execution = Execution()
    host = types.SimpleNamespace(execution=execution)
    Implement._record_assumptions(cast("Implement", host), plan_docket)
    assert len(execution.assumptions) == 2, "the report keeps every decision point"
    assert [line.split(":")[0] for line in execution.open_assumptions] == ["H2"], "only the unsettled one rides into the build"

    class Crews:
        order = ["builder"]

        def __init__(self, crew) -> None:
            self.crew = crew

        def __getitem__(self, label: str):
            return self.crew

    stage = {"state": "write it", "gate": "g", "scope": "", "exceptions": [], "kind": "code"}
    crew = types.SimpleNamespace(live=True, council=types.SimpleNamespace(agents=[types.SimpleNamespace(id="Agent1", role="both")]))
    host2 = types.SimpleNamespace(docket=plan_docket, execution=execution, stages=[stage], goal="g", human="blocking", crews=Crews(crew))
    host2._drain = types.MethodType(Implement._drain, host2)
    host2._seats = types.MethodType(Implement._seats, host2)
    prompt = Implement._work_task(cast("Implement", host2), StageRecord(index=1, state="write it", gate="g"), stage)
    assert "which data directory?" in prompt, "the open decision rides into the build"
    assert "which storage engine?" not in prompt, "the answered decision is in the plan, not the prompt"


# ------------------------------------------------------------- the run, end to end


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


def solo_config() -> str:
    return """\
[defaults]
pi_cli = "{pi}"
watchdog_sec = 0
verbosity = "debug"
human = "none"

[councils.build]
goal = "Build the hello tool"
mode = "implement"

[councils.build.chair]
name = "Chair"

[[councils.build.agents]]
name = "Forge"
role = "both"
""".format(pi=PI)


def test_a_plan_file_drives_a_full_implement_run(cli, project):
    """The chair structures the document, the builder lands each stage, and the run reports what landed."""
    write_project_config(project, solo_config())
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the hello module. 2. Document it.\n")
    script = json.dumps(
        [
            {
                "contains": "# Structure the plan",
                "reply": {
                    "stages": [
                        {"state": "write the hello module", "gate": "test -f hello.py"},
                        {"state": "document the tool", "gate": "test -f docs.md", "kind": "prose"},
                    ]
                },
            },
            {
                "contains": "# Your work",
                "reply": {
                    "summary": "built the stage",
                    "changed": ["hello.py", "docs.md"],
                    "user_text": ["docs.md"],
                    "open_issues": [],
                },
                "write": [{"path": "docs.md", "text": "# Docs\n"}],
            },
        ]
    )
    result = cli(["build", "--plan", plan], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    # the run's crews are recorded in the manifest, and the builder's stage work is real: the file is in the tree
    assert [crew["label"] for crew in result.manifest["crews"]] == ["builder"]
    with open(os.path.join(project, "docs.md"), encoding="utf-8") as handle:
        assert handle.read() == "# Docs\n", "the builder's stage write is a real file in the tree"
    report = result.artifact("implementation.md")
    assert "## Crews" in report and "## Adopted plan" in report and "## Stages" in report and "## What landed" in report
    crews_section = report.split("## Crews", 1)[1].split("\n## ", 1)[0]
    assert "Chair" in crews_section and "'name'" not in crews_section, "the crews table prints the chair's name, not the record"
    assert "docs.md" in report, "the changed path lands in the report's what-landed section"
    assert "user-visible text" in report and "# Docs" in report, "the stage's user-visible text is printed in the report"
    assert "never asked" in report and "never settled" not in report, "a recorded approval is reported as never asked, not standing"


def test_the_prose_seat_rewrites_the_code_builders_user_text(cli, project):
    """D4: a code stage that touched user-visible text hands the baton to the prose seat; its rewrite is what ships."""
    text = solo_config().replace(
        '[[councils.build.agents]]\nname = "Forge"\nrole = "both"',
        '[[councils.build.agents]]\nname = "Forge"\nrole = "both"\n\n[[councils.build.agents]]\nname = "Wordsmith"\nrole = "prose"',
    )
    write_project_config(project, text)
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the app and its docs.\n")
    script = json.dumps(
        [
            {"contains": "# Structure the plan", "reply": {"stages": [{"state": "write the app and its docs", "gate": "test -f app.py"}]}},
            {
                "contains": "# The prose pass",
                "reply": {"summary": "rewrote the docs", "changed": ["docs.md"], "open_issues": []},
                "write": [{"path": "docs.md", "text": "# docs, by the prose seat\n"}],
            },
            {
                "contains": "# Your work",
                "reply": {"summary": "built the app", "changed": ["app.py", "docs.md"], "user_text": ["docs.md"], "open_issues": []},
                "write": [{"path": "app.py", "text": "print('app')\n"}, {"path": "docs.md", "text": "# raw docs by the coder\n"}],
            },
        ]
    )
    result = cli(["build", "--plan", plan], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    with open(os.path.join(project, "docs.md"), encoding="utf-8") as handle:
        assert handle.read() == "# docs, by the prose seat\n", "the prose seat's rewrite is what ships"
    report = result.artifact("implementation.md")
    stages = report.split("## Stages", 1)[1].split("\n## ", 1)[0]
    assert "prose pass" in stages, "the stage ledger says the prose seat rewrote the stage's user-visible text"


def test_a_prose_stage_goes_to_the_dedicated_prose_seat(cli, project):
    """A prose stage is not handed to a `both` seat listed first in the roster: the dedicated
    prose seat writes the stage, and the prose pass does not run on the prose seat's own work."""
    text = solo_config().replace(
        '[[councils.build.agents]]\nname = "Forge"\nrole = "both"',
        '[[councils.build.agents]]\nname = "Forge"\nrole = "both"\n\n[[councils.build.agents]]\nname = "Wordsmith"\nrole = "prose"',
    )
    write_project_config(project, text)
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the release notes.\n")
    script = json.dumps(
        [
            {
                "contains": "# Structure the plan",
                "reply": {"stages": [{"state": "write the release notes", "gate": "test -f notes.md", "kind": "prose"}]},
            },
            {
                "contains": "# Your work",
                "reply": {"summary": "wrote the notes", "changed": ["notes.md"], "user_text": ["notes.md"], "open_issues": []},
                "write": [{"path": "notes.md", "text": "# Release notes\n"}],
            },
        ]
    )
    result = cli(["build", "--plan", plan], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    assert "the baton goes to Wordsmith" in result.console, (
        "the prose stage goes to the dedicated prose seat, not the first-listed `both` seat"
    )
    assert "implement_prose_pass" not in result.kinds(), "the builder was the prose seat: no second prose pass"
    with open(os.path.join(project, "notes.md"), encoding="utf-8") as handle:
        assert handle.read() == "# Release notes\n"


def test_a_fix_passs_user_text_gets_the_prose_rewrite(cli, project):
    """A fix pass that touches user-visible text gets the prose pass too: the work pass names no
    text, the docs land on the fix pass, and the prose seat's rewrite of them is what ships."""
    text = solo_config().replace(
        '[[councils.build.agents]]\nname = "Forge"\nrole = "both"',
        '[[councils.build.agents]]\nname = "Forge"\nrole = "code"\n\n[[councils.build.agents]]\nname = "Wordsmith"\nrole = "prose"',
    )
    write_project_config(project, text)
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the app and its docs.\n")
    script = json.dumps(
        [
            {
                "contains": "# Structure the plan",
                "reply": {"stages": [{"state": "write the app and its docs", "gate": "test -f fixed.txt"}]},
            },
            {
                "contains": "# The prose pass",
                "reply": {"summary": "rewrote the docs", "changed": ["docs.md"], "open_issues": []},
                "write": [{"path": "docs.md", "text": "# docs, by the prose seat\n"}],
            },
            # the fix entry must precede the work entry: the fake matches by substring, and the fix
            # prompt's workspace section contains "# Your work" inside "# Your workspace"
            {
                "contains": "# Fix pass",
                "reply": {
                    "summary": "fixed the gate and wrote the docs",
                    "changed": ["fixed.txt", "docs.md"],
                    "user_text": ["docs.md"],
                    "open_issues": [],
                },
                "write": [{"path": "fixed.txt", "text": "fixed\n"}, {"path": "docs.md", "text": "# raw docs by the coder\n"}],
            },
            # the work pass names no user text: the chair re-verifies the gate each rung, failing once and
            # passing after the fix lands — the docs land on the fix pass
            {
                "contains": "# Verify the stage's gate",
                "once": True,
                "reply": {"ok": False, "ran": "test -f fixed.txt", "evidence": "no such file"},
            },
            {"contains": "# Verify the stage's gate", "reply": {"ok": True, "ran": "test -f fixed.txt", "evidence": "ok"}},
            {
                "contains": "# Your work",
                "reply": {"summary": "built the app", "changed": ["app.py"], "open_issues": []},
                "write": [{"path": "app.py", "text": "print('app')\n"}],
            },
        ]
    )
    prompts = os.path.join(os.path.dirname(project), "prompts.jsonl")
    result = cli(
        ["build", "--plan", plan],
        cwd=project,
        env=dict(runs_env(project), FAKE_PI_SCRIPT=script, FAKE_PI_PROMPTS=prompts),
        timeout=150,
    )
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    with open(os.path.join(project, "docs.md"), encoding="utf-8") as handle:
        assert handle.read() == "# docs, by the prose seat\n", "the prose seat rewrites the fix pass's text, not the builder's raw text"
    with open(prompts, encoding="utf-8") as handle:
        prose = [record for record in map(json.loads, handle) if "# The prose pass" in record["message"]]
    assert len(prose) == 1, "the prose pass runs once, after the fix pass"
    assert "# raw docs by the coder" in prose[0]["message"], "the prose seat rewrites what the fix pass left on disk"


def test_a_say_to_line_lands_at_the_targets_next_prompt_and_unsent_lines_are_reported(cli, project):
    """A say_to line queues for its target's next prompt, printed under `Messages for you`; a line
    whose target never gets a prompt is said out loud and kept in the report, not dropped."""
    text = solo_config().replace(
        '[[councils.build.agents]]\nname = "Forge"\nrole = "both"',
        '[[councils.build.agents]]\nname = "Forge"\nrole = "code"\n\n[[councils.build.agents]]\nname = "Wordsmith"\nrole = "prose"',
    )
    write_project_config(project, text)
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the app. 2. Write the docs.\n")
    script = json.dumps(
        [
            {
                "contains": "# Structure the plan",
                "reply": {
                    "stages": [
                        {"state": "write the app", "gate": "test -f app.py"},
                        {"state": "write the docs", "gate": "test -f docs.md", "kind": "prose"},
                    ]
                },
            },
            # only the prose builder's prompt carries the queued line: a prompt without it takes the
            # index-fallback, writes no docs, and the stage blocks
            {
                "contains": "## Messages for you",
                "reply": {"summary": "wrote the docs", "changed": ["docs.md"], "open_issues": []},
                "write": [{"path": "docs.md", "text": "# docs, with the flag\n"}],
            },
            {
                "contains": "# Your work",
                "reply": {
                    "summary": "built the app",
                    "changed": ["app.py"],
                    "open_issues": [],
                    "say_to": [
                        {"to": "Agent2", "text": "the docs should mention the flag"},
                        {"to": "Agent3", "text": "heads up: nothing to build"},
                    ],
                },
                "write": [{"path": "app.py", "text": "print('app')\n"}],
            },
        ]
    )
    prompts = os.path.join(os.path.dirname(project), "prompts.jsonl")
    result = cli(
        ["build", "--plan", plan],
        cwd=project,
        env=dict(runs_env(project), FAKE_PI_SCRIPT=script, FAKE_PI_PROMPTS=prompts),
        timeout=150,
    )
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented", "the delivered line reached the prose builder's prompt"
    with open(os.path.join(project, "docs.md"), encoding="utf-8") as handle:
        assert handle.read() == "# docs, with the flag\n"
    # Agent3 is no seat of this run: its line never lands, and the run says so instead of dropping it
    assert "say_to never landed: Forge to Agent3" in result.console
    assert "implement_say_to_unsent" in result.kinds()
    # the delivered line is attributed by the sender's ID: no display name reaches a seat
    with open(prompts, encoding="utf-8") as handle:
        prose = [record for record in map(json.loads, handle) if "## Messages for you" in record["message"]]
    assert len(prose) == 1, "the line reached exactly one prompt"
    assert "- from Agent1: the docs should mention the flag" in prose[0]["message"]
    assert "Forge" not in prose[0]["message"] and "Wordsmith" not in prose[0]["message"]
    report = result.artifact("implementation.md")
    assert "Unsent messages" in report and "nothing to build" in report
    assert "the docs should mention the flag" not in report, "a delivered line is not an unsent one"


def test_a_builders_prompt_lists_the_live_seats_by_id_not_names(cli, project):
    """The roster is a seat's only address book: the builder's prompt lists the live seats by ID —
    how `say_to` is addressed — and carries no display name, which would bias trust between seats."""
    text = solo_config().replace(
        '[[councils.build.agents]]\nname = "Forge"\nrole = "both"',
        '[[councils.build.agents]]\nname = "Forge"\nrole = "code"\n\n[[councils.build.agents]]\nname = "Wordsmith"\nrole = "prose"',
    )
    write_project_config(project, text)
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the app.\n")
    prompts = os.path.join(os.path.dirname(project), "prompts.jsonl")
    script = json.dumps(
        [
            {"contains": "# Structure the plan", "reply": {"stages": [{"state": "write the app", "gate": "test -f app.py"}]}},
            {
                "contains": "# Your work",
                "reply": {"summary": "built it", "changed": ["app.py"], "open_issues": []},
                "write": [{"path": "app.py", "text": "print('app')\n"}],
            },
        ]
    )
    result = cli(
        ["build", "--plan", plan],
        cwd=project,
        env=dict(runs_env(project), FAKE_PI_SCRIPT=script, FAKE_PI_PROMPTS=prompts),
        timeout=150,
    )
    assert result.returncode == 0, result.console
    with open(prompts, encoding="utf-8") as handle:
        work = [record for record in map(json.loads, handle) if "# Your work" in record["message"]]
    assert len(work) == 1, "the builder got one work prompt"
    text = work[0]["message"]
    assert "- Agent1 (you)" in text and "- Agent2 (prose)" in text, "the roster lists the live seats by ID"
    assert "Forge" not in text and "Wordsmith" not in text, "no display name reaches a seat"


def test_a_gate_that_never_passes_blocks_the_stage_and_the_run_ends_partial(cli, project):
    write_project_config(project, solo_config().replace("[councils.build]", "[councils.build]\nfix_budget = 1\nreplans = 0"))
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the never file.\n")
    script = json.dumps(
        [
            {
                "contains": "# Structure the plan",
                "reply": {"stages": [{"state": "write the never file", "gate": "test -f never.md"}]},
            },
            {"contains": "# Verify the stage's gate", "reply": {"ok": False, "ran": "test -f never.md", "evidence": "no such file"}},
            {
                "contains": "# Your work",
                "reply": {"summary": "tried", "changed": ["never.md"], "open_issues": []},
                "write": [{"path": "almost.md", "text": "close\n"}],
            },
        ]
    )
    result = cli(["build", "--plan", plan], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "partial"
    report = result.artifact("implementation.md")
    assert "blocked" in report, "the report says which stage is blocked and why"
    assert "C1" in report, "the failing gate is a standing item on the shared docket"
    blocked = report.split("Stage 1: blocked")[1].split("\n## ")[0]
    assert "the stage's gate fails" not in blocked, "the gate failure is its own line, not also an item line"


def test_a_blocked_stage_names_its_standing_findings_not_just_the_last_pass(cli, project):
    """A finding that surfaced early in the stage's review and is not re-reported on the blocking
    pass is still standing on the stage: the block record names it alongside the last pass's —
    the last pass's findings alone would keep it out of the stage's own record."""
    text = CONFIG.format(pi=PI).replace("[councils.build]", "[councils.build]\nfix_budget = 1\nreplans = 0")
    write_project_config(project, text)
    commit_all(project)
    script = json.dumps(
        [
            # the re-review's prompt carries the standing-findings section: report only the late finding —
            # the early one is not re-reported, and it stays standing on the stage
            {
                "contains": "## The findings still standing",
                "reply": {
                    "findings": [{"claim": "the late finding", "severity": "major", "evidence": "hello.py:1"}],
                    "summary": "one late finding",
                    "open_questions": [],
                },
            },
            {
                "contains": "## The stage",
                "index": 0,
                "reply": {
                    "findings": [{"claim": "the early finding", "severity": "major", "evidence": "hello.py:1"}],
                    "summary": "one early finding",
                    "open_questions": [],
                },
            },
            {
                "contains": "Phase 0",
                "reply": {
                    "approach": {"claim": "the simple plan", "stages": [{"state": "write the hello module", "gate": "test -f hello.py"}]}
                },
            },
            {"contains": "Round ", "reply": {"positions": [], "signoff": True, "backing": "Agent1"}},
            {"contains": "# Classify the stages", "reply": {"stages": [{"kind": "code"}]}},
            {
                "contains": "# Your work",
                "reply": {"summary": "done", "changed": ["hello.py"], "open_issues": []},
                "write": [{"path": "hello.py", "text": "print('hello')\n"}],
            },
            {"contains": "# Fix pass", "reply": {"summary": "tried", "changed": [], "addressed": [{"id": "C1", "how": "noted"}]}},
        ]
    )
    result = cli(["build"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "partial"
    report = result.artifact("implementation.md")
    blocked = report.split("Stage 1: blocked")[1].split("\n## ")[0]
    assert "the early finding" in blocked, "a finding not re-reported on the blocking pass is still the stage's"
    assert "the late finding" in blocked


def test_a_planner_crew_convenes_a_plan_the_reviewer_crew_watches_the_stage(cli, project):
    """All three crews: the planners converge, the run's chair classifies, the builders build, the reviewers pass."""
    write_project_config(project, CONFIG.format(pi=PI))
    commit_all(project)
    script = json.dumps(
        [
            {"contains": "## The stage", "reply": {"findings": [], "summary": "clean", "open_questions": []}},
            {
                "contains": "Phase 0",
                "reply": {
                    "approach": {"claim": "the simple plan", "stages": [{"state": "write the hello module", "gate": "test -f hello.py"}]}
                },
            },
            {"contains": "Round ", "reply": {"positions": [], "sign_off": True, "backing": "Agent1"}},
            {"contains": "# Classify the stages", "reply": {"stages": [{"kind": "code"}]}},
            {"contains": "# Your work", "reply": {"summary": "done", "changed": ["hello.py"], "open_issues": []}},
        ]
    )
    result = cli(["build"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    assert [crew["label"] for crew in result.manifest["crews"]] == ["planner", "builder", "reviewer"]
    report = result.artifact("implementation.md")
    assert "Metic (planner)" in report and "Metic (reviewer)" in report, "borrowed seats keep their crew suffix"
    assert "every planner backed it" in report, "the report says the direction was adopted by consensus"
    # the planner crew disposes at the phase boundary: its teardown lands before the first stage builds
    lines = result.console.splitlines()
    first_dispose = next(i for i, line in enumerate(lines) if line.startswith("disposing "))
    first_stage = next(i for i, line in enumerate(lines) if line.startswith("stage 1/"))
    assert first_dispose < first_stage, "the planner crew must dispose before the build begins"


def test_a_stop_in_the_build_is_measured_against_the_live_crew_not_the_disposed_planners(cli, project):
    """The planner crew disposes before the build: a declared stop's bar counts the live crew — with
    two planners, one builder and two reviewers the console says (2/2 at the bar), not the (2/3) the
    disposed roster would give. Both reviewers raise the same ask: the merge gives it its two askers,
    so it clears the seconding bar, and it carries two stop-holders."""
    write_project_config(project, CONFIG.format(pi=PI).replace('human = "none"', 'human = "blocking"'))
    commit_all(project)
    script = json.dumps(
        [
            {
                "contains": "## The stage",
                "reply": {
                    "summary": "clean",
                    "open_questions": [],
                    "human_input": [{"question": "which data directory?", "blocking": True, "context": "two viable locations"}],
                },
            },
            {
                "contains": "Phase 0",
                "reply": {
                    "approach": {"claim": "the simple plan", "stages": [{"state": "write the hello module", "gate": "test -f hello.py"}]}
                },
            },
            {"contains": "Round ", "reply": {"positions": [], "signoff": True, "backing": "Agent1"}},
            {"contains": "# Classify the stages", "reply": {"stages": [{"kind": "code"}]}},
            {"contains": "# Your work", "reply": {"summary": "done", "changed": ["hello.py"], "open_issues": []}},
        ]
    )
    env = dict(runs_env(project), FAKE_PI_SCRIPT=script)
    result = cli(["build"], cwd=project, env=env, timeout=150, stdin="H1: 1\nH2: the data directory\n")
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    assert "(2/2 at the bar)" in result.console, "the stop's bar counts the live crew, not the disposed planners"


def test_a_dirty_git_tree_is_refused_unless_dirty_is_passed(cli, project):
    write_project_config(project, solo_config())
    commit_all(project)
    with open(os.path.join(project, "loose.txt"), "w", encoding="utf-8") as handle:
        handle.write("uncommitted\n")
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the hello module.\n")
    script = json.dumps(
        [
            {
                "contains": "# Structure the plan",
                "reply": {"stages": [{"state": "write the hello module", "gate": "test -f hello.py"}]},
            },
            {"contains": "# Your work", "reply": {"summary": "done", "changed": ["hello.py"], "open_issues": []}},
        ]
    )
    env = dict(runs_env(project), FAKE_PI_SCRIPT=script)
    refused = cli(["build", "--plan", plan], cwd=project, env=env, timeout=60)
    assert refused.returncode != 0
    assert "dirty" in refused.console.lower()
    assert "run directory" not in refused.console, "the refusal lands before a run starts"
    gone = cli(["build", "--plan", plan, "--dirty"], cwd=project, env=env, timeout=150)
    assert gone.returncode == 0, gone.console
    assert gone.manifest["outcome"] == "implemented"


def test_a_repo_without_commits_is_not_mislabelled_a_non_repo(tmp_path):
    """An unborn HEAD is a git repo with no commits yet, not a non-repo: its uncommitted tree is
    refused like any dirty tree, and the note says what the tree actually is."""
    import subprocess
    import types
    from typing import cast

    from mech_council import startup

    project = str(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
    with open(os.path.join(project, "a.txt"), "w", encoding="utf-8") as handle:
        handle.write("x\n")
    council = cast(config_mod.Council, types.SimpleNamespace(project=project))
    with pytest.raises(UsageError):
        startup._implement_notes(council, Args())
    notes = startup._implement_notes(council, Args(dirty=True))
    assert any("no commits yet" in note for note in notes), notes


def test_a_blocking_run_asks_the_human_to_proceed(cli, project):
    """The approval gate is the run's own question, raised by the chair: the seconding bar is for the
    seats' asks, so a blocking run with the default bar shown asks the human before a line is written,
    and a relative --plan path survives the orchestrator's move to the runs dir."""
    write_project_config(project, solo_config().replace('human = "none"', 'human = "blocking"'))
    plan = os.path.join(project, "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the hello module.\n")
    commit_all(project)
    script = json.dumps(
        [
            {"contains": "# Structure the plan", "reply": {"stages": [{"state": "write the hello module", "gate": "test -f hello.py"}]}},
            {"contains": "# Your work", "reply": {"summary": "done", "changed": ["hello.py"], "open_issues": []}},
        ]
    )
    result = cli(
        ["build", "--plan", "plan.md"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150, stdin="H1: 1\n"
    )
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    assert "implement it in the real tree" in result.console, "the approval question is shown to the human"
    before_answer = result.console.split("H1: answered")[0]
    assert "write the hello module" in before_answer and "test -f hello.py" in before_answer, (
        "the human sees the plan's stages and gates with the approval, before answering"
    )
    assert "H1: 1" not in result.console and "option 1" in result.console
    report = result.artifact("implementation.md")
    assert "The human approved the build" in report


def test_a_message_to_a_seat_not_yet_up_is_refused_not_fatal(cli, project):
    """The planning checkpoint is shown every crew's names, so the human may message a builder or the
    run's chair before they are up: the message is refused and the run goes on — it is not a KeyError."""
    write_project_config(project, CONFIG.format(pi=PI).replace('human = "none"', 'human = "blocking"'))
    commit_all(project)
    script = json.dumps(
        [
            {"contains": "## The stage", "reply": {"findings": [], "summary": "clean", "open_questions": []}},
            {
                "contains": "Phase 0",
                "reply": {
                    "approach": {"claim": "the simple plan", "stages": [{"state": "write the hello module", "gate": "test -f hello.py"}]},
                    "human_input": [{"question": "which storage engine?", "blocking": True, "context": "sql or kv"}],
                },
            },
            {"contains": "Round ", "reply": {"positions": [], "backing": "Agent1", "signoff": True}},
            {"contains": "# Classify the stages", "reply": {"stages": [{"kind": "code"}]}},
            {"contains": "# Your work", "reply": {"summary": "done", "changed": ["hello.py"], "open_issues": []}},
        ]
    )
    stdin = (
        "Forge: heads up, the gate needs the file committed first\n"
        "Chair: is the plan on track?\n"
        "Review Chair (reviewer): when is my part?\n"
        "H1: sql\n"
        "H1: 1\n"
    )
    result = cli(["build"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150, stdin=stdin)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    assert "the message did not reach Forge" in result.console, "the early message is refused, not dropped"
    assert "KeyError" not in result.console and "internal error" not in result.console
    assert "did not reach Chair" not in result.console, "the run's chair is up and hears the message"
    assert "the message did not reach Review Chair (reviewer)" in result.console, "a not-yet-up crew's chair is refused, not dropped"
    assert "Review Chair (reviewer) is not in this run right now" not in result.console, "the crew convenes later, so not absent"


# ------------------------------------------------------------- the run's own gates


def test_the_approval_carries_the_plan_the_human_agrees_to():
    """The human's only look at the plan before the first edit: the approval's context lists
    every stage's final state and gate, so `proceed` agrees to a plan, not a bare yes/no."""
    import asyncio
    import types
    from typing import cast

    from mech_council.deliberation.implement import Implement
    from mech_council.deliberation.state import Docket, Execution

    host = types.SimpleNamespace(
        docket=Docket(mode="implement", goal="g", roster=["Agent1"], chair_name="Chair", seconding=True, human="none"),
        execution=Execution(adopted="Agent1"),
        stages=[
            {"state": "write the hello module", "gate": "test -f hello.py", "scope": "hello.py", "kind": "code"},
            {"state": "document the tool", "gate": "", "scope": "docs.md", "kind": "prose"},
        ],
        names={"Agent1": "Agent1"},
        human="none",
    )
    granted = asyncio.run(Implement._approval(cast("Implement", host)))
    assert granted is True, "a no-human run records the approval and keeps going"
    context = host.docket.questions["H1"].context
    assert "write the hello module" in context and "test -f hello.py" in context, "stage one with its gate"
    assert "document the tool" in context, "a stage without a gate is listed too"


def test_a_qualified_no_is_not_an_approval(cli, project):
    """The approval reads one bit: `don't proceed` does not pick `proceed`, it refuses it — the run
    holds and writes no code, and the report carries the refusal."""
    write_project_config(project, solo_config().replace('human = "none"', 'human = "blocking"'))
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the hello module.\n")
    script = json.dumps(
        [
            {
                "contains": "# Structure the plan",
                "reply": {"stages": [{"state": "write the staged module", "gate": "test -f staged.py"}]},
            },
            {
                "contains": "# Your work",
                "reply": {"summary": "done", "changed": ["staged.py"], "open_issues": []},
                "write": [{"path": "staged.py", "text": "staged = True\n"}],
            },
        ]
    )
    result = cli(
        ["build", "--plan", plan],
        cwd=project,
        env=dict(runs_env(project), FAKE_PI_SCRIPT=script),
        timeout=150,
        stdin="H1: don't proceed\n",
    )
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "needs_human", "a declined approval is a result with the refusal in the report"
    assert not os.path.exists(os.path.join(project, "staged.py")), "no code was written"
    report = result.artifact("implementation.md")
    assert "declined" in report, "the report says the human declined to proceed"


def test_the_options_own_words_are_the_selection(cli, project):
    """`H1: proceed` — the option's words, not its number — is the explicit pick: the build proceeds."""
    write_project_config(project, solo_config().replace('human = "none"', 'human = "blocking"'))
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the hello module.\n")
    script = json.dumps(
        [
            {"contains": "# Structure the plan", "reply": {"stages": [{"state": "write the hello module", "gate": "test -f hello.py"}]}},
            {"contains": "# Your work", "reply": {"summary": "done", "changed": ["hello.py"], "open_issues": []}},
        ]
    )
    result = cli(
        ["build", "--plan", plan],
        cwd=project,
        env=dict(runs_env(project), FAKE_PI_SCRIPT=script),
        timeout=150,
        stdin="H1: proceed\n",
    )
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    report = result.artifact("implementation.md")
    assert "The human approved the build" in report


def test_a_non_blocking_approval_is_reported_recorded_not_standing(cli, project):
    """A run that never waits on its own approval gate: the report says it was recorded and the run
    kept going — not `standing`, which would say the run is waiting on a gate it already passed."""
    write_project_config(project, solo_config().replace('human = "none"', 'human = "non_blocking"'))
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the hello module.\n")
    script = json.dumps(
        [
            {
                "contains": "# Structure the plan",
                "reply": {"stages": [{"state": "write the hello module", "gate": "test -f hello.py"}]},
            },
            {"contains": "# Your work", "reply": {"summary": "done", "changed": ["hello.py"], "open_issues": []}},
        ]
    )
    result = cli(["build", "--plan", plan], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    report = result.artifact("implementation.md")
    assert "H1 (not waited on)" in report
    assert "the run recorded it and kept going" in report
    assert "never settled" not in report


def test_a_blocked_stage_holds_when_the_human_says_hold(cli, project):
    """The blocked-stage call reads one bit too: `hold` — the option's words — holds the run, and the
    rest of the plan stays unbuilt."""
    text = (
        solo_config()
        .replace('human = "none"', 'human = "blocking"')
        .replace("[councils.build]", "[councils.build]\nfix_budget = 1\nreplans = 0")
    )
    write_project_config(project, text)
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the never file. 2. Write the hello file.\n")
    script = json.dumps(
        [
            # the builder's third prompt is stage 2's work pass: it lands only if the run carried on past the hold
            {
                "contains": "# Your work",
                "index": 2,
                "reply": {"summary": "built the second file", "changed": ["second.py"], "open_issues": []},
                "write": [{"path": "second.py", "text": "second = True\n"}],
            },
            {
                "contains": "# Structure the plan",
                "reply": {
                    "stages": [
                        {"state": "write the never file", "gate": "test -f never.md"},
                        {"state": "write the second file", "gate": "test -f second.py"},
                    ]
                },
            },
            {"contains": "# Verify the stage's gate", "reply": {"ok": False, "ran": "test -f never.md", "evidence": "no such file"}},
            {"contains": "# Your work", "reply": {"summary": "tried", "changed": [], "open_issues": []}},
        ]
    )
    result = cli(
        ["build", "--plan", plan],
        cwd=project,
        env=dict(runs_env(project), FAKE_PI_SCRIPT=script),
        timeout=150,
        stdin="H1: 1\nH2: hold\n",
    )
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "partial"
    assert not os.path.exists(os.path.join(project, "second.py")), "the stage after the hold was never built"
    report = result.artifact("implementation.md")
    assert "the human held the run" in report, "the report says the hold, not a carry-on"


# ------------------------------------------------------------- the edge cases a future edit will reintroduce


def test_a_stage_with_no_gate_passes_on_the_review_alone(cli, project):
    """A stage that names no gate has nothing to re-run: with a clean review it passes, and the
    run is not a partial for a gate that was never there."""
    write_project_config(project, solo_config())
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the hello module.\n")
    script = json.dumps(
        [
            {"contains": "# Structure the plan", "reply": {"stages": [{"state": "write the hello module"}]}},
            {
                "contains": "# Your work",
                "reply": {"summary": "built it", "changed": ["hello.py"], "open_issues": []},
                "write": [{"path": "hello.py", "text": "print('hello v2')\n"}],
            },
        ]
    )
    result = cli(["build", "--plan", plan], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented", "no gate and a clean review is a pass, not a block"
    report = result.artifact("implementation.md")
    assert "(no gate)" in report and "blocked" not in report


def test_a_prose_gate_is_verified_by_the_review_crew_not_a_shell(cli, project):
    """A gate that is prose — no shell could run it — still gates: the review crew verifies the
    condition itself, and its verdict is what the run reads, not a shell's exit code."""
    write_project_config(project, CONFIG.format(pi=PI))
    commit_all(project)
    script = json.dumps(
        [
            {
                "contains": "## The stage",
                "reply": {
                    "new_items": [],
                    "summary": "clean",
                    "gate": {"ok": True, "ran": "the docs and the suite, checked by hand", "evidence": "all green"},
                },
            },
            {
                "contains": "Phase 0",
                "reply": {
                    "approach": {
                        "claim": "the simple plan",
                        "stages": [
                            {
                                "state": "write the hello module",
                                "gate": "check that the docs mention the flag and the suite still passes",
                            }
                        ],
                    }
                },
            },
            {"contains": "Round ", "reply": {"positions": [], "signoff": True, "backing": "Agent1"}},
            {"contains": "# Classify the stages", "reply": {"stages": [{"kind": "code"}]}},
            {
                "contains": "# Your work",
                "reply": {"summary": "done", "changed": ["hello.py"], "open_issues": []},
                "write": [{"path": "hello.py", "text": "print('hello')\n"}],
            },
        ]
    )
    result = cli(["build"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    report = result.artifact("implementation.md")
    assert "check that the docs mention the flag" in report, "the prose gate is the stage's gate in the report"
    passed = [record for record in result.events if record.get("event") == "implement_stage_passed"]
    assert passed and "all green" in passed[0]["gate"], "the crew's verdict is what the run recorded, not a shell output"


def test_gate_verdicts_aggregate_across_the_review_crew_and_the_chair_steps_in_when_none_read():
    """One dissenting seat holds the stage open, and a crew with no readable verdict at all leaves
    the gate to the run's chair — the orchestrator itself never runs the gate."""
    import asyncio
    import types
    from typing import cast

    from mech_council.deliberation.engine import RawAsk
    from mech_council.deliberation.implement import Implement
    from mech_council.deliberation.state import StageRecord
    from mech_council.extraction import Extraction

    def host_for():
        host = types.SimpleNamespace(names={"Agent1": "Metic", "Agent2": "Senior"})
        host._close_gate_item = lambda rec: None
        host._ensure_gate_item = lambda rec: None
        return host

    def settled(gate_data):
        return RawAsk(text="", failure=None, rejected=False, data=Extraction(data={"gate": gate_data}))

    stage = {"state": "s", "gate": "pytest"}

    rec = StageRecord(index=1, state="s", gate="pytest")
    asyncio.run(
        Implement._settle_gate(
            cast("Implement", host_for()),
            rec,
            stage,
            {
                "Agent1": settled({"ok": True, "ran": "pytest", "evidence": "42 passed"}),
                "Agent2": settled({"ok": False, "ran": "pytest", "evidence": "2 failed"}),
            },
        )
    )
    assert rec.gate_ok is False, "a dissenting seat vetoes the gate"
    assert "Metic" in rec.gate_rerun and "Senior" in rec.gate_rerun, "the evidence carries both seats' accounts"

    calls: list[dict] = []

    async def chair_check(the_stage: dict):
        calls.append(the_stage)
        return False, "the chair ran pytest: 2 failed"

    rec = StageRecord(index=2, state="s", gate="pytest")
    host = host_for()
    host._chair_gate_check = chair_check
    asyncio.run(
        Implement._settle_gate(
            cast("Implement", host),
            rec,
            stage,
            {
                "Agent1": settled(None),  # the reply carried no gate slot: it is no verdict, not a pass
                "Agent2": RawAsk(text="prose only", failure=None, rejected=True, data=None),
            },
        )
    )
    assert calls == [stage], "the chair checked the gate when the crew's verdicts did not read"
    assert rec.gate_ok is False and rec.gate_rerun == "the chair ran pytest: 2 failed"


def test_an_unreadable_builder_spends_the_ladder_instead_of_aborting_the_run(cli, project):
    """A builder reply that survives its repair re-asks unreadable does not abort the run: it spends
    a rung of the stage's ladders, and the stage blocks when they are spent."""
    write_project_config(project, solo_config().replace("[councils.build]", "[councils.build]\nfix_budget = 1\nreplans = 1"))
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the never file.\n")
    garble = "the builder wrote prose, no shape at all"
    script = json.dumps(
        [
            {"contains": "# Structure the plan", "reply": {"stages": [{"state": "write the never file", "gate": "test -f never.md"}]}},
            {"contains": "# Verify the stage's gate", "reply": {"ok": False, "ran": "test -f never.md", "evidence": "no such file"}},
            {"contains": "# Fix pass", "reply": garble},
            {"contains": "# Replan", "reply": garble},
            {"contains": "# Your work", "reply": garble},
        ]
    )
    result = cli(["build", "--plan", plan], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "partial", "the unreadable replies spent the ladders; the stage blocked, the run did not abort"
    console = result.console
    assert "work reply was unreadable" in console
    assert "fix pass 1 was unreadable" in console
    assert "replan 1 was unreadable" in console
    report = result.artifact("implementation.md")
    assert "blocked" in report and "the gate still fails" in report


def test_a_standing_finding_is_re_reported_not_duplicated(cli, project):
    """A finding the review crew still finds after a pass is not re-raised as a new item: the
    re-report joins the standing one, and it still holds the stage open until the ladders are spent."""
    text = CONFIG.format(pi=PI).replace("[councils.build]", "[councils.build]\nfix_budget = 2\nreplans = 0")
    write_project_config(project, text)
    commit_all(project)
    script = json.dumps(
        [
            # the re-review is handed the stage's standing findings: the re-report joins them, so a prompt without the
            # section takes the clean fallback, closes the stage, and the run ends implemented, not partial
            {
                "contains": "## The findings still standing",
                "reply": {
                    "findings": [{"claim": "the code is smelly", "severity": "major", "evidence": "hello.py:1"}],
                    "summary": "still standing",
                    "open_questions": [],
                },
            },
            {
                "contains": "## The stage",
                "index": 0,
                "reply": {
                    "findings": [{"claim": "the code is smelly", "severity": "major", "evidence": "hello.py:1"}],
                    "summary": "one finding",
                    "open_questions": [],
                },
            },
            {
                "contains": "## The stage",
                "reply": {"findings": [], "summary": "clean", "open_questions": []},
            },
            {
                "contains": "Phase 0",
                "reply": {
                    "approach": {
                        "claim": "the simple plan",
                        "stages": [{"state": "write the hello module", "gate": "test -f hello.py"}],
                    }
                },
            },
            {"contains": "Round ", "reply": {"positions": [], "sign_off": True, "backing": "Agent1"}},
            {"contains": "# Classify the stages", "reply": {"stages": [{"kind": "code"}]}},
            {"contains": "# Your work", "reply": {"summary": "done", "changed": [], "open_issues": []}},
            {"contains": "# Fix pass", "reply": {"summary": "left it", "changed": [], "addressed": [{"id": "C1", "how": "noted"}]}},
        ]
    )
    result = cli(["build"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "partial", "the standing finding spent the fix ladder and blocked the stage"
    assert "still finds C1" in result.console, "the re-report is spoken as standing, not as found anew"
    report = result.artifact("implementation.md")
    assert "C1" in report and "the code is smelly" in report
    assert "C2" not in report, "the re-reports joined C1 instead of raising new items"
    assert "re-confirmed by" in report


def test_a_trivial_finding_stops_holding_the_stage_after_two_passes(cli, project):
    """A `trivial` finding holds the stage open through the work pass and the first fix pass; from the
    third builder pass on, only `minor` or higher keeps the stage open — so a stage that is down to a
    nit passes instead of spending the ladder and blocking on it. The nit stays on the docket."""
    text = CONFIG.format(pi=PI).replace("[councils.build]", "[councils.build]\nfix_budget = 3\nreplans = 0")
    write_project_config(project, text)
    commit_all(project)
    plan = os.path.join(os.path.dirname(project), "plan.md")
    with open(plan, "w", encoding="utf-8") as handle:
        handle.write("# Plan\n\n1. Write the hello module.\n")
    script = json.dumps(
        [
            # every re-review re-reports the nit: held through pass 2, released at pass 3
            {
                "contains": "## The findings still standing",
                "reply": {
                    "findings": [{"claim": "the code is smelly", "severity": "trivial", "evidence": "hello.py:1"}],
                    "summary": "still there",
                    "open_questions": [],
                },
            },
            {
                "contains": "## The stage",
                "reply": {
                    "findings": [{"claim": "the code is smelly", "severity": "trivial", "evidence": "hello.py:1"}],
                    "summary": "one nit",
                    "open_questions": [],
                },
            },
            {"contains": "# Structure the plan", "reply": {"stages": [{"state": "write the hello module", "gate": "test -f hello.py"}]}},
            {
                "contains": "# Your work",
                "reply": {"summary": "done", "changed": ["hello.py"], "open_issues": []},
                "write": [{"path": "hello.py", "text": "print('hello')\n"}],
            },
            {"contains": "# Fix pass", "reply": {"summary": "left it", "changed": [], "addressed": [{"id": "C1", "how": "noted"}]}},
        ]
    )
    result = cli(["build", "--plan", plan], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented", "the nit held the stage through pass two, then let it go — the ladder never spent"
    assert "still finds C1" in result.console, "through the second builder pass the nit still held the stage open"
    assert "does not hold the stage open" in result.console
    assert "implement_finding_not_holding" in result.kinds()
    report = result.artifact("implementation.md")
    assert "C1 [trivial] the code is smelly — still standing" in report, "the nit stays on the docket, unclosed, in the report"


def test_a_stage_passes_on_its_gate_when_the_review_crew_is_unreadable(cli, project):
    """Every reviewer unreadable after its repair re-asks: the chair steps in to verify the gate, and
    the stage still passes when the chair says it does — the run says so, the event, the console
    and the report naming who checked."""
    write_project_config(project, CONFIG.format(pi=PI))
    commit_all(project)
    script = json.dumps(
        [
            {"contains": "could not be read as structured output", "reply": "still prose, no shape at all"},
            {"contains": "## The stage", "reply": "the reviewer wrote prose, no shape at all"},
            {
                "contains": "Phase 0",
                "reply": {
                    "approach": {"claim": "the simple plan", "stages": [{"state": "write the hello module", "gate": "test -f hello.py"}]}
                },
            },
            {"contains": "Round ", "reply": {"positions": [], "signoff": True, "backing": "Agent1"}},
            {"contains": "# Classify the stages", "reply": {"stages": [{"kind": "code"}]}},
            {
                "contains": "# Your work",
                "reply": {"summary": "done", "changed": ["hello.py"], "open_issues": []},
                "write": [{"path": "hello.py", "text": "print('hello')\n"}],
            },
        ]
    )
    result = cli(["build"], cwd=project, env=dict(runs_env(project), FAKE_PI_SCRIPT=script), timeout=150)
    assert result.returncode == 0, result.console
    assert result.manifest["outcome"] == "implemented"
    assert "implement_stage_review_unreadable" in result.kinds()
    assert "the final review was unreadable" in result.console
    report = result.artifact("implementation.md")
    assert "the final review was unreadable" in report and "the chair verified the gate, and it passed" in report


def test_a_held_back_ask_ends_marked_unseconded_not_unasked():
    """An ask no second seat backs is held back for the whole run: it ends carrying the held-back
    reason — not `unasked`, which would say a showable ask slipped past a checkpoint, and the
    report never says it stands unsettled."""
    import types
    from typing import cast

    from mech_council.deliberation import escalations
    from mech_council.deliberation.implement import Implement
    from mech_council.deliberation.state import Docket

    docket = Docket(mode="implement", goal="g", roster=["Agent1", "Agent2", "Agent3"], chair_name="Chair", seconding=True, human="blocking")
    escalations.raise_question(docket, text="which data directory?", asker="Agent3", item_id=None, round_=1)
    host = types.SimpleNamespace(docket=docket, _asked_of_human=set())
    Implement._mark_unasked(cast("Implement", host))
    assert "unseconded" in docket.questions["H1"].markers, "the held-back reason is recorded"
    assert "unasked" not in docket.questions["H1"].markers

    # a shown ask that got no answer is still marked unanswered
    escalations.raise_question(docket, text="which tenant?", asker="Agent1", item_id=None, round_=1)
    escalations.second(docket, qid="H2", agent="Agent2", round_=1)
    host._asked_of_human.add("H2")
    Implement._mark_unasked(cast("Implement", host))
    assert docket.questions["H2"].markers == ["unanswered"]


def test_the_build_checkpoint_drains_off_cycle_answers():
    """What the human typed while the build worked is applied at the stage boundary, the way the
    engine applies it at a round boundary: a non-blocking build is not one that cannot be answered."""
    import asyncio
    import types
    from typing import cast

    from mech_council.console import Console
    from mech_council.deliberation import escalations
    from mech_council.deliberation.implement import Implement
    from mech_council.deliberation.state import ANSWERED, Docket
    from mech_council.humaninput import Checkpoint

    class TtyReader:
        tty = True
        eof = False

        def __init__(self, lines: list[str]) -> None:
            self._lines = list(lines)

        def read_nowait(self):
            return self._lines.pop(0) if self._lines else None

    docket = Docket(
        mode="implement", goal="g", roster=["Agent1", "Agent2", "Agent3"], chair_name="Chair", seconding=True, human="non_blocking"
    )
    # two seats, one wording: the ask clears the seconding bar, so the off-cycle input may answer it
    escalations.raise_question(docket, text="which data directory?", asker="Agent1", item_id=None, round_=1)
    escalations.raise_question(docket, text="which data directory?", asker="Agent2", item_id=None, round_=1)
    question = docket.questions["H1"]
    checkpoint = Checkpoint(docket, TtyReader(["H1: the data directory"]), console=Console(), names={a: a for a in docket.roster})
    host = types.SimpleNamespace(
        docket=docket,
        checkpoint=checkpoint,
        emit=lambda name, **fields: None,
        _asked_of_human=set(),
    )
    host._apply_human = types.MethodType(Implement._apply_human, host)
    asyncio.run(Implement._checkpoint(cast("Implement", host), terminal=False))
    assert question.state == ANSWERED and question.answer == "the data directory", "the typed answer landed at the boundary"
