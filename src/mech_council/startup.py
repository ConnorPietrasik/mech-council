from __future__ import annotations

import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field

from . import cli, naming
from . import config as config_mod
from .cli import ConfigError, UsageError


@dataclass
class Launch:
    council: config_mod.Council
    runs_dir: str
    run_name: str
    run_dir: str
    sandbox_dir: str
    notes: list[str]
    #: Where `--output` sends the report; empty leaves it in `run_dir`.
    output_path: str = ""
    #: `--overwrite`: replace an existing `output_path` without asking.
    output_overwrite: bool = False
    #: implement runs: the run's crews in crew order — `(label, council)`, the implement council itself under `builder`
    crews: list[tuple[str, config_mod.Council]] = field(default_factory=list)
    #: implement: `--plan <file>` — a plan document the run's chair structures instead of convening the planners
    plan_file: str = ""
    #: fix: `--review-file <file>` — a previous run's report.json whose findings the run fixes
    review_file: str = ""


def stdin_is_terminal() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except AttributeError, OSError, ValueError:
        return False


def convene(argv: list[str], *, environ: dict[str, str] | None = None) -> tuple[cli.Args, Launch]:
    environ = dict(os.environ if environ is None else environ)
    args = cli.parse_args(argv)
    if refusal := cli.no_fork_refusal():
        raise ConfigError(refusal)
    plat = sys.platform
    namespaces_available = cli.sandbox_supported()
    if message := cli.check_nested_guard(args.allow_nested, environ):
        raise ConfigError(message)
    project = cli.resolve_project(args.project)
    if not os.path.isdir(project):
        raise UsageError(f"--project is not a directory: {args.project!r}")
    if args.config and not os.path.isfile(args.config):
        raise UsageError(f"--config is not a file: {args.config!r}")
    paths, project_layer = cli.config_candidates(args.config, project, environ)
    loaded = config_mod.load_layers(paths, project_layer=project_layer)
    name = config_mod.select_council(loaded, args.council)
    council = config_mod.convene(loaded, name, args, project, untrusted=args.untrusted, namespaces_available=namespaces_available)
    # a run with no one at the keyboard takes the never-asks posture, unless a posture was stated
    human_fallback = (
        not stdin_is_terminal() and args.human is None and config_mod.stated_by(loaded, council.name, council.extends, "human") is None
    )
    if human_fallback:
        council.human = "none"
    output_path = _output_target(args, council)
    runs_dir = cli.runs_root_from_env(environ)
    _check_runs_root(runs_dir, project)
    run_name = _run_name(project)
    if cli.is_within(os.path.join(runs_dir, run_name), project):
        raise ConfigError(f"the run directory would land inside the project: {os.path.join(runs_dir, run_name)} is under {project}")
    notes = list(args.notes)
    notes += _layer_notes(loaded)
    notes += _trust_notes(council, args)
    notes += _in_place_notes(loaded, council, args, plat=plat)
    if human_fallback:
        notes.append(
            'stdin is not a terminal: the run takes human = "none" — the council is never asked, and its open questions '
            "are recorded in the report (state a posture to ask anyway)"
        )
    if output_path:
        word = "the reports" if os.path.isdir(output_path) else "the report"
        notes.append(f"--output writes {word} to {output_path} instead of into the run directory; every other artifact stays there")
        if cli.is_within(output_path, project):
            notes.append(f"the report lands inside {project} as an ordinary write by the orchestrator, so it is in no write journal")
    elif args.overwrite:
        notes.append("--overwrite asks about an existing --output target, and there is none")
    if note := cli.proc_clearing_note(council.in_place):
        notes.append(note)
    launch = Launch(
        council=council,
        runs_dir=runs_dir,
        run_name=run_name,
        run_dir=os.path.join(runs_dir, run_name),
        sandbox_dir=_sandbox_path(runs_dir, run_name),
        notes=notes,
        output_path=output_path,
        output_overwrite=args.overwrite,
    )
    if council.mode in ("implement", "fix"):
        launch.crews = config_mod.resolve_crews(loaded, council, args)
    if council.mode == "implement" and args.plan:
        if not os.path.isfile(args.plan):
            raise UsageError(f"--plan is not a file: {args.plan!r}")
        # absolute: the orchestrator's cwd moves to the runs dir before the document is read
        launch.plan_file = os.path.abspath(args.plan)
    if council.mode == "fix" and args.review_file:
        if not os.path.isfile(args.review_file):
            raise UsageError(f"--review-file is not a file: {args.review_file!r}")
        launch.review_file = os.path.abspath(args.review_file)
    # after the crews: a crew's auto lines have joined the run's in resolve_crews
    notes += council.provider_notes
    if council.mode == "implement":
        notes += _implement_notes(council, args)
    if council.mode == "fix":
        notes += _fix_notes(council, args)
    return args, launch


def _output_target(args: cli.Args, council: config_mod.Council) -> str:
    """Where `--output` sends the report, resolved against the invoking cwd.

    A directory keeps the mode's report name (one file, or both for `both`); a file is the report
    itself and must wear the format's extension, which `both` cannot wear: it writes two.
    """
    raw = args.output
    if raw is None:
        return ""
    expanded = os.path.expanduser(raw)
    if not expanded:
        raise UsageError("--output wants a path")
    path = os.path.realpath(expanded if os.path.isabs(expanded) else os.path.join(os.getcwd(), expanded))
    fmt = council.report_format
    names = council.adapter.report_files(fmt)
    if raw.endswith((os.sep, "/")) or os.path.isdir(path):
        if not os.path.isdir(path):
            raise UsageError(f"--output is not a directory: {raw!r}")
        if not os.access(path, os.W_OK | os.X_OK):
            raise UsageError(f"--output wants a directory you can write: {path}")
        return os.path.join(path, names[0]) if len(names) == 1 else path
    if len(names) == 2:
        both_names = ", ".join(names)
        raise UsageError(f"--output names a file, but {fmt!r} writes two reports ({both_names}): name a directory instead")
    ext = names[0].rsplit(".", 1)[1]
    if not path.lower().endswith("." + ext):
        raise UsageError(f"--output writes a {ext} report: the file's extension must match, not {os.path.basename(path)!r}")
    holder = os.path.dirname(path)
    if not os.path.isdir(holder):
        raise UsageError(f"--output wants a directory, or a file in a directory that exists: {raw!r}")
    if not os.access(holder, os.W_OK | os.X_OK):
        raise UsageError(f"--output wants a directory you can write: {holder}")
    return path


def report_paths(launch: Launch) -> list[str]:
    """The report files this run writes, in write order: the run directory, or where `--output` sends them."""
    names = launch.council.adapter.report_files(launch.council.report_format)
    if not launch.output_path:
        return [os.path.join(launch.run_dir, name) for name in names]
    if os.path.isdir(launch.output_path):
        return [os.path.join(launch.output_path, name) for name in names]
    return [launch.output_path]


def confirm_overwrite(launch: Launch) -> list[str]:
    """Ask before `--output` replaces a file that is already there, on stderr."""
    if not launch.output_path:
        return []
    existing = [target for target in report_paths(launch) if os.path.exists(target)]
    if not existing:
        return []
    if len(existing) > 1:
        note = f"--output replaces the {' and '.join(existing)} that were already there"
    else:
        note = f"--output replaces the {existing[0]} that was already there"
    target = existing[0]
    if not launch.output_overwrite:
        sys.stderr.write(f"--output {target} already exists: overwrite it? [y/N] ")
        sys.stderr.flush()
        terminal = stdin_is_terminal()
        try:
            # fd 0 closed before the run started leaves `sys.stdin` None
            answer = sys.stdin.readline().strip().lower()
        except AttributeError, OSError, ValueError:
            answer = ""
        if answer not in ("y", "yes"):
            if not terminal:
                sys.stderr.write("\n")  # no typed answer echoed onto the prompt's line, so the refusal prints one
            unseen = "" if terminal else " (stdin is not a terminal, so the question above went unseen)"
            raise ConfigError(f"{target} was left alone and nothing ran{unseen}: pass --overwrite to replace it without being asked")
    return [note]


def _git_state(project: str) -> dict | None:
    """The project's git tree for a build run's preflight: None when git is not reachable,
    `inside` false when the project is no repo, else the branch and the dirt."""
    import subprocess

    def run(*args: str):
        return subprocess.run(["git", "-C", project, *args], check=False, capture_output=True, text=True, timeout=30)

    try:
        inside = run("rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return {"inside": False}
        rev = run("rev-parse", "HEAD")
        if rev.returncode != 0:  # an unborn HEAD: the repo exists and has no commits
            branch = "(no commits yet)"
        else:
            head = run("rev-parse", "--abbrev-ref", "HEAD")
            branch = head.stdout.strip() or "HEAD"
        # scoped to the project directory: a project that is a subdirectory of a larger repo runs on
        # the project's dirt, the way the snapshots are scoped to it
        dirty = run("status", "--porcelain", "--", ".")
    except OSError, subprocess.TimeoutExpired:
        return None
    dirty_ok = None if dirty.returncode != 0 else bool(dirty.stdout.strip())
    return {"inside": True, "branch": branch, "dirty": dirty_ok}


def _tree_state_notes(council: config_mod.Council, args: cli.Args, state: dict, *, what: str) -> list[str]:
    """The branch and dirt lines a build run's preflight owes, shared by implement and fix."""
    branch = state["branch"]
    diffs = "stage diffs" if what == "implement" else "finding diffs"
    if state["dirty"] is None:
        return ["git status failed: the tree's dirt could not be checked — the run proceeds against it as found"]
    if state["dirty"]:
        if args.dirty:
            tail = f"the {diffs} and the report's diffstat are measured against it"
            if what == "fix" and council.commit:
                tail += ", and the first fix commit sweeps in the pre-existing dirt"
            return [f"the git tree is dirty on branch {branch!r} and --dirty was passed: {tail}"]
        raise UsageError(
            f"the project's git tree is dirty on branch {branch!r}: a {what} run measures its {diffs} "
            "against a clean baseline — commit or stash first, or pass --dirty to proceed anyway"
        )
    return [f"on git branch {branch!r}: the {diffs} and the report's diffstat are measured against it"]


def _implement_notes(council: config_mod.Council, args: cli.Args) -> list[str]:
    """What an implement run owes the human before the first edit: the tree's state and the no-commit line."""
    notes = ["an implement run never commits: when it is done, review the diff before you do"]
    state = _git_state(council.project)
    if state is None:
        notes.append("git is not reachable: the stage diffs and the report's diffstat read the tree directly")
        return notes
    if not state["inside"]:
        notes.append(
            "the project is not a git repo: the stage diffs and the report's diffstat read the tree directly, "
            "and the run's diff is weaker for it"
        )
        return notes
    notes += _tree_state_notes(council, args, state, what="implement")
    return notes


def _fix_notes(council: config_mod.Council, args: cli.Args) -> list[str]:
    """What a fix run owes before the first edit: the commit line and the tree's state. A fix run
    needs git — the commit is the safety model — so no repo is a refusal, not a note."""
    notes = [
        "a fix run commits each verified finding as its own commit; a blocked finding is rolled back to its pre-tree"
        if council.commit
        else "a fix run leaves its fixes uncommitted: a blocked finding is rolled back to its pre-tree"
    ]
    state = _git_state(council.project)
    if state is None:
        notes.append("git is not reachable: the run cannot take its pre-snapshots and will refuse before the first finding")
        return notes
    if not state["inside"]:
        raise UsageError(f"{council.project} is not a git repo: a fix run commits each verified finding, so it needs one")
    notes += _tree_state_notes(council, args, state, what="fix")
    return notes


def _check_runs_root(runs_dir: str, project: str) -> None:
    if cli.is_within(runs_dir, project):
        raise ConfigError(
            f"COUNCIL_RUNS_DIR is inside the project: {runs_dir} is under {project}. Point it somewhere else, or pass --project."
        )


def _run_name(project: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{naming.slugify(os.path.basename(project.rstrip('/'))) or 'project'}"


def _layer_notes(loaded: config_mod.ConfigFile) -> list[str]:
    if len(loaded.paths) < 2 or loaded.project_layer not in loaded.paths:
        return []
    others = " and ".join(path for path in loaded.paths if path != loaded.project_layer)
    return [f"{loaded.project_layer} replaces any council of the same name in {others}"]


def _trust_notes(council: config_mod.Council, args: cli.Args) -> list[str]:
    if not args.untrusted:
        return []
    return ["--untrusted: every member spawns with --no-approve, so project-local .pi resources are not provided to them"]


def _in_place_notes(loaded: config_mod.ConfigFile, council: config_mod.Council, args: cli.Args, *, plat: str) -> list[str]:
    """Why the run works in place: the flag, a config line, the mode, or no namespaces."""
    if not council.in_place:
        return []
    costs = "members share one project and nothing they write is journaled or discarded"
    if args.in_place:
        note = f"--in-place: no namespaces, no overlays and no masks — {costs}"
    elif (stated := config_mod.stated_value(loaded, council.name, council.extends, "in_place")) and stated[0]:
        note = f"in place, asked for by {stated[1]} — {costs}"
    elif council.mode in ("implement", "fix"):
        article = "an" if council.mode == "implement" else "a"
        note = f"{article} {council.mode} run works in place — {costs}"
    else:
        note = f"no namespaces on {plat!r}: running in place — {costs}"
    if config_mod.stated_by(loaded, council.name, council.extends, "sandbox_mib"):
        note += f" · sandbox_mib = {council.sandbox_mib} is ignored"
    return [note]


def prepare_runs_root(runs_dir: str) -> str:
    try:
        os.makedirs(runs_dir, mode=0o700, exist_ok=True)
    except FileExistsError as failure:
        raise ConfigError(f"the runs root is not a directory: {runs_dir} ({failure})") from failure
    except OSError as failure:
        raise ConfigError(f"could not create the runs root {runs_dir}: {os.strerror(failure.errno or 0)}") from failure
    if not stat.S_ISDIR(os.stat(runs_dir).st_mode):
        raise ConfigError(f"the runs root is not a directory: {runs_dir}")
    return runs_dir


def _sandbox_path(runs_dir: str, run_name: str) -> str:
    return os.path.join(runs_dir, "sandbox", run_name)


def create_run_dir(launch: Launch) -> str:
    try:
        os.makedirs(launch.run_dir, mode=0o700, exist_ok=False)
    except FileExistsError:  # a second run in the same second
        launch.run_name += f"-{os.getpid()}"
        launch.run_dir = os.path.join(launch.runs_dir, launch.run_name)
        launch.sandbox_dir = _sandbox_path(launch.runs_dir, launch.run_name)
        try:
            os.makedirs(launch.run_dir, mode=0o700, exist_ok=False)
        except OSError as failure:
            raise ConfigError(f"could not create the run directory {launch.run_dir}: {os.strerror(failure.errno or 0)}") from failure
    except OSError as failure:
        raise ConfigError(f"could not create the run directory {launch.run_dir}: {os.strerror(failure.errno or 0)}") from failure
    return launch.run_dir


def discard_run_dir(launch: Launch) -> None:
    """Remove a run directory the run never filled."""
    shutil.rmtree(launch.run_dir, ignore_errors=True)
