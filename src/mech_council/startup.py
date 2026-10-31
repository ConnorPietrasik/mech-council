from __future__ import annotations

import os
import stat
import sys
import time
from dataclasses import dataclass

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
    runs_dir = cli._runs_root_from_env(environ)
    _check_runs_root(runs_dir, project)
    run_name = _run_name(project)
    if cli.is_within(os.path.join(runs_dir, run_name), project):
        raise ConfigError(f"the run directory would land inside the project: {os.path.join(runs_dir, run_name)} is under {project}")
    notes = list(args.notes)
    notes += _layer_notes(loaded)
    notes += _trust_notes(council, args)
    notes += _in_place_notes(loaded, council, args, plat=plat)
    if note := cli.proc_clearing_note(council.in_place):
        notes.append(note)
    return args, Launch(
        council=council,
        runs_dir=runs_dir,
        run_name=run_name,
        run_dir=os.path.join(runs_dir, run_name),
        sandbox_dir=os.path.join(runs_dir, "sandbox", run_name),
        notes=notes,
    )


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
    """Who asked to run unsandboxed, in the order the operator should read it."""
    if not council.in_place:
        return []
    costs = "members share one project and nothing they write is journaled or discarded"
    if args.in_place:
        note = f"--in-place: no namespaces, no overlays and no masks — {costs}"
    else:
        asked_by = config_mod.stated_by(loaded, council.name, council.extends, "in_place")
        note = f"in place, asked for by {asked_by} — {costs}" if asked_by else f"no namespaces on {plat!r}: running in place — {costs}"
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


def create_run_dir(launch: Launch) -> str:
    try:
        os.makedirs(launch.run_dir, mode=0o700, exist_ok=False)
    except FileExistsError:  # a second run in the same second
        launch.run_name += f"-{os.getpid()}"
        launch.run_dir = os.path.join(launch.runs_dir, launch.run_name)
        os.makedirs(launch.run_dir, mode=0o700, exist_ok=False)
    except OSError as failure:
        raise ConfigError(f"could not create the run directory {launch.run_dir}: {os.strerror(failure.errno or 0)}") from failure
    return launch.run_dir
