"""Everything that must succeed *before* anything exists.

The startup order is the whole module: load, validate, select and flatten the council, resolve the runs root,
discover pi, establish the sandbox, and **only then** create the run directory and write `run.json`.  A
bad config, an unresolvable pi, or a host where `unshare`/`mount` fails therefore leaves nothing behind
anywhere — which is what the suite asserts against, and why this file creates no directory until its last line.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from dataclasses import dataclass

from . import cli, naming
from . import config as config_mod
from .cli import ConfigError, UsageError


@dataclass
class Launch:
    """Everything the orchestrator needs, and the run directory it has not made yet."""

    council: config_mod.Council
    runs_dir: str
    run_name: str
    run_dir: str
    sandbox_dir: str
    notes: list[str]


def convene(argv: list[str], *, environ: dict[str, str] | None = None) -> tuple[cli.Args, Launch]:
    """Parse and validate everything.  Raises UsageError (exit 2) or ConfigError (exit 1)."""
    environ = dict(os.environ if environ is None else environ)
    args = cli.parse_args(argv)
    if message := cli.check_nested_guard(args.allow_nested, environ):
        raise ConfigError(message)
    project = cli.resolve_project(args.project)
    if not os.path.isdir(project):
        raise UsageError(f"--project is not a directory: {args.project!r}")
    config_path = args.config or os.path.join(project, "council.toml")
    if args.config and not os.path.isfile(args.config):
        raise UsageError(f"--config is not a file: {args.config!r}")
    loaded = config_mod.load_config(config_path)
    name = config_mod.select_council(loaded, args.council)
    council = config_mod.convene(loaded, name, args, project, untrusted=args.untrusted)
    runs_dir = cli._runs_root_from_env(environ)
    _check_runs_root(runs_dir, project)
    run_name = _run_name(project)
    if cli.is_within(os.path.join(runs_dir, run_name), project):
        raise ConfigError(f"the run directory would land inside the project: {os.path.join(runs_dir, run_name)} is under {project}")
    notes = list(args.notes)
    notes += _trust_notes(council, args)
    return args, Launch(
        council=council,
        runs_dir=runs_dir,
        run_name=run_name,
        run_dir=os.path.join(runs_dir, run_name),
        sandbox_dir=os.path.join(runs_dir, "sandbox", run_name),
        notes=notes,
    )


def _check_runs_root(runs_dir: str, project: str) -> None:
    """A runs root inside the project is exit 1 naming **both** paths."""
    if cli.is_within(runs_dir, project):
        raise ConfigError(
            f"COUNCIL_RUNS_DIR is inside the project: {runs_dir} is under {project}. Point it somewhere else, or pass --project."
        )


def _run_name(project: str) -> str:
    """The name is `<timestamp>-<project>`, the slug `naming`'s, as `file_base` uses."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{naming.slugify(os.path.basename(project.rstrip('/'))) or 'project'}"


def _trust_notes(council: config_mod.Council, args: cli.Args) -> list[str]:
    """The override is noted, and it states what it costs."""
    if not args.untrusted:
        return []
    return ["--untrusted: every member spawns with --no-approve, so project-local .pi resources are not provided to them"]


def prepare_runs_root(runs_dir: str) -> str:
    """The runs root is load's **last** action, with 0700 and no silent fallback.

    A path that exists but is not a directory is not replaced: it is a
    user-fixable line naming the path, exit 1, and no sandbox step runs.
    """
    try:
        os.makedirs(runs_dir, mode=0o700, exist_ok=True)
    except FileExistsError as failure:
        raise ConfigError(f"the runs root is not a directory: {runs_dir} ({failure})") from failure
    except OSError as failure:
        raise ConfigError(f"could not create the runs root {runs_dir}: {os.strerror(failure.errno or 0)}") from failure
    if not stat.S_ISDIR(os.stat(runs_dir).st_mode):
        raise ConfigError(f"the runs root is not a directory: {runs_dir}")
    return runs_dir


def create_run_dir(launch: Launch) -> str:
    """The last step of the startup order: the run directory, created only once everything above held."""
    try:
        os.makedirs(launch.run_dir, mode=0o700, exist_ok=False)
    except FileExistsError:  # the same second, twice: never reuse a run dir
        launch.run_name += f"-{os.getpid()}"
        launch.run_dir = os.path.join(launch.runs_dir, launch.run_name)
        os.makedirs(launch.run_dir, mode=0o700, exist_ok=False)
    except OSError as failure:
        raise ConfigError(f"could not create the run directory {launch.run_dir}: {os.strerror(failure.errno or 0)}") from failure
    return launch.run_dir


def remove_run_dir(run_dir: str) -> None:
    """A setup failure leaves nothing behind, so what the startup order made is undone."""
    shutil.rmtree(run_dir, ignore_errors=True)
