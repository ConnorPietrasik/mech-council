"""`mech-council list`: what the resolved config offers, read the way a run would read it."""

from __future__ import annotations

import os
import sys

from . import cli
from . import config as config_mod

#: how much of a goal's first line reaches the listing
GOAL_WIDTH = 100


HELP = """\
usage: mech-council list [flags]

List the councils the config offers, in the order the config writes them: the user
council.toml with <project>/council.toml layered over it. A `_` name is a template:
extended via `extends`, never convened. A council that does not convene as-is (no
flags) is listed with the reason.

flags:
  -p, --project <path>   project directory (default: the invoking cwd)
  -c, --config <file>    the only council.toml to read (nothing layers under it)
  -h, --help             show this help and exit

exit: 0 when nothing failed, 1 when the config could not be read
"""


def main(argv: list[str]) -> int:
    try:
        parsed = cli.parse_inspect_flags(argv, "list")
    except cli.UsageError as failure:
        return cli.fail(str(failure))
    if parsed.help:
        sys.stdout.write(HELP)
        return 0
    try:
        print(_render(parsed))
        return 0
    except cli.UsageError as failure:
        return cli.fail(str(failure))
    except cli.ConfigError as failure:
        sys.stderr.write(f"{failure}\n")
        return 1


def _render(parsed: cli.InspectArgs) -> str:
    project = cli.resolve_project(parsed.project)
    if not os.path.isdir(project):
        raise cli.UsageError(f"--project is not a directory: {parsed.project}")
    if parsed.config and not os.path.isfile(parsed.config):
        raise cli.UsageError(f"--config is not a file: {parsed.config}")
    paths, project_layer = cli.config_candidates(parsed.config, project, dict(os.environ))
    loaded = config_mod.load_layers(paths, project_layer=project_layer)
    out = [f"councils from {loaded.where}:"]
    if len(loaded.paths) > 1 and loaded.project_layer in loaded.paths:
        others = " and ".join(path for path in loaded.paths if path != loaded.project_layer)
        out.append(f"note: {loaded.project_layer} replaces any council of the same name in {others}")
    out.append("")
    for name in loaded.councils:
        out.extend(_block(name, loaded, project))
        out.append("")
    return "\n".join(out).rstrip()


def _block(name: str, loaded: config_mod.ConfigFile, project: str) -> list[str]:
    template = name.startswith(config_mod.TEMPLATE_PREFIX)
    try:
        council = config_mod.convene_without_pi(loaded, name, cli.Args(), project, namespaces_available=cli.sandbox_supported())
    except cli.ConfigError as failure:
        if template:
            return [f"{name} · template", f"  not convenable as-is: {failure}"]
        return [f"{name} · not convenable", f"  as-is: {failure}"]
    lines = [f"{name} · {council.mode}" + (" · template (extended, never convened)" if template else "")]
    lines.append(f"  goal: {_goal(council.goal)}")
    lines.append(f"  roster: {_roster(council)}")
    if knobs := _knobs(council):
        lines.append(f"  knobs: {knobs}")
    if council.extends:
        lines.append(f"  extends: {' → '.join(council.extends)}")
    lines.append(f"  source: {loaded.supplied_by(name)}")
    return lines


def _goal(goal: str | None) -> str:
    if not goal:
        return "(none)"
    line = next((line.strip() for line in goal.splitlines() if line.strip()), "")
    return line if len(line) <= GOAL_WIDTH else line[: GOAL_WIDTH - 1] + "…"


def _seat(spec: config_mod.MemberSpec) -> str:
    return f"{spec.name} ({spec.provider}/{spec.model})" if spec.model else spec.name


def _roster(council: config_mod.Council) -> str:
    return ", ".join(_seat(spec) for spec in council.agents) + f" · chair: {_seat(council.chair)}"


def _knobs(council: config_mod.Council) -> str:
    """The effective knobs that differ from the built-in defaults, as a config would state them."""
    effective = {
        "max_rounds": council.max_rounds,
        "grace_rounds": council.grace_rounds,
        "seconding": council.seconding,
        "watchdog_sec": council.watchdog_sec,
        "chair_timeout_sec": council.chair_timeout_sec,
        "max_concurrency": council.max_concurrency,
        "verbosity": cli.VERBOSITY_LEVELS[council.verbosity],
        "pi_cli": council.pi_cli or None,
        "sandbox_mib": council.sandbox_mib,
        "in_place": council.in_place,
        "human": council.human,
    }
    return " · ".join(f"{key} = {_toml(value)}" for key, value in effective.items() if value != config_mod.BUILTIN_DEFAULTS[key])


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)
