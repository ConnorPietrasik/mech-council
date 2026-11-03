"""`mech-council doctor`: what a run needs on this host. Pi is probed with `--version`; no council is convened and no prompt is spent."""

from __future__ import annotations

import os
import sys

from . import cli
from . import config as config_mod

OK = "ok:   "
NOTE = "note: "
FAIL = "fail: "


HELP = """\
usage: mech-council doctor [flags]

Check what a run needs on this host: fork, the config, pi, the namespaces the sandbox
stands on, and the runs root. No council is convened and no prompt is spent: pi is probed with
`--version`, the namespaces in a throwaway child.

flags:
  -p, --project <path>   project directory (default: the invoking cwd)
  -c, --config <file>    the only council.toml to read
  -h, --help             show this help and exit

exit: 0 when nothing failed, 1 when a check failed
"""


def main(argv: list[str]) -> int:
    try:
        parsed = cli.parse_inspect_flags(argv, "doctor")
    except cli.UsageError as failure:
        return cli.fail(str(failure))
    if parsed.help:
        sys.stdout.write(HELP)
        return 0
    try:
        project = cli.resolve_project(parsed.project)
        if not os.path.isdir(project):
            raise cli.UsageError(f"--project is not a directory: {parsed.project}")
        if parsed.config and not os.path.isfile(parsed.config):
            raise cli.UsageError(f"--config is not a file: {parsed.config}")
    except cli.UsageError as failure:
        return cli.fail(str(failure))
    lines = _checks(project, parsed)
    print("\n".join(lines))
    if any(line.startswith(FAIL) for line in lines):
        print("doctor: a run would refuse here (see the fail lines)")
        return 1
    print("doctor: everything a run needs is in place")
    return 0


def _checks(project: str, parsed: cli.InspectArgs) -> list[str]:
    environ = dict(os.environ)
    lines: list[str] = []
    if refusal := cli.no_fork_refusal():
        lines.append(FAIL + refusal)
    else:
        lines.append(OK + "fork")
    loaded = None
    shape: list[tuple[str, config_mod.Council | None, str | None]] = []
    paths, project_layer = cli.config_candidates(parsed.config, project, environ)
    if not any(os.path.isfile(path) for path in paths):
        lines.append(NOTE + "config: no council.toml to read — `mech-council init` writes the user starter")
    else:
        try:
            loaded = config_mod.load_layers(paths, project_layer=project_layer)
        except cli.ConfigError as failure:
            lines.append(FAIL + f"config: {failure}")
        else:
            shape = _convene_all(loaded, project)
            lines.extend(_config(loaded, shape))
    lines.extend(_pi(loaded, shape, project))
    lines.extend(_namespaces())
    lines.extend(_runs_root(project))
    if nested := cli.check_nested_guard(False, environ):
        lines.append(NOTE + nested)
    return lines


def _convene_all(loaded: config_mod.ConfigFile, project: str) -> list[tuple[str, config_mod.Council | None, str | None]]:
    """Each named council, as-is: its shape when it convenes, or the refusal when it does not."""
    shape: list[tuple[str, config_mod.Council | None, str | None]] = []
    for name in loaded.names():
        try:
            council = config_mod.convene_without_pi(loaded, name, cli.Args(), project, namespaces_available=cli.sandbox_supported())
        except cli.ConfigError as failure:
            shape.append((name, None, str(failure)))
        else:
            shape.append((name, council, None))
    return shape


def _config(loaded: config_mod.ConfigFile, shape: list[tuple[str, config_mod.Council | None, str | None]]) -> list[str]:
    names = loaded.names()
    templates = [name for name in loaded.councils if name.startswith(config_mod.TEMPLATE_PREFIX)]
    good = [name for name, council, _refusal in shape if council is not None]
    out: list[str] = []
    if good:
        line = f"config: {loaded.where} — {len(good)} convenable council(s): {', '.join(good)}"
        if templates:
            line += f" + {len(templates)} template(s)"
        out.append(OK + line)
    else:
        reason = "every name is a template (extended, never convened)" if not names else "see the notes"
        out.append(FAIL + f"config: {loaded.where} — no council convenes: {reason}")
    out.extend(NOTE + refusal for _name, _council, refusal in shape if refusal is not None)
    if len(loaded.paths) > 1 and loaded.project_layer in loaded.paths:
        out.append(NOTE + f"{loaded.project_layer} replaces any council of the same name in the other layer")
    return out


def _pi(loaded: config_mod.ConfigFile | None, shape: list[tuple[str, config_mod.Council | None, str | None]], project: str) -> list[str]:
    """The pis a run could probe: each distinct `pi_cli` the councils resolve to, plus `pi` on
    PATH when none is stated. A council that fails to convene is skipped — its run refuses first."""
    checks: set[tuple[str | None, bool]] = set()
    if loaded is not None:
        for name, council, _refusal in shape:
            if council is None:
                continue
            stated = config_mod.stated_by(loaded, name, council.extends, "pi_cli") == loaded.project_layer
            checks.add((council.pi_cli or None, stated))
    if not checks:
        checks = {(None, False)}
    lines: list[str] = []
    for value, stated in sorted(checks, key=lambda pair: (pair[0] is not None, pair[0] or "")):
        try:
            pi_cli, version = config_mod.resolve_pi(value, project, from_project=stated)
        except cli.ConfigError as failure:
            lines.append(FAIL + f"pi: {failure}")
        else:
            shown = f" — pi_cli = {value!r}" if value else ""
            lines.append(OK + f"pi {version} ({pi_cli}){shown}")
    return lines


def _namespaces() -> list[str]:
    if sys.platform != "linux":
        return [NOTE + f"{sys.platform!r}: no namespaces here — a run works in place (the members share the project)"]
    worked, detail = _unshare_probe()
    if worked:
        lines = [OK + "namespaces: unshare(CLONE_NEWUSER|CLONE_NEWNS) works"]
    else:
        lines = [
            FAIL
            + (
                f"namespaces: {detail} — a run would refuse here; pass --in-place (on Debian/Ubuntu set "
                "kernel.unprivileged_userns_clone=1; in Docker allow unshare in the seccomp profile)"
            )
        ]
    if note := cli.proc_clearing_note(in_place=False):
        lines.append(NOTE + note)
    return lines


def _unshare_probe() -> tuple[bool, str]:
    """The run's own first step, in a throwaway child: unshare(CLONE_NEWUSER|CLONE_NEWNS)."""
    from .sandbox import mount

    try:
        pid = os.fork()
    except OSError as failure:
        return False, f"fork: {os.strerror(failure.errno or 0)}"
    if pid == 0:
        try:
            mount.unshare(mount.CLONE_NEWUSER | mount.CLONE_NEWNS)
        except OSError as failure:
            os._exit(128 + (failure.errno or 1))
        os._exit(0)
    _pid, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
        return True, ""
    if os.WIFEXITED(status):
        errno = os.WEXITSTATUS(status) - 128
        detail = os.strerror(errno) if 0 < errno < 256 else f"exit {os.WEXITSTATUS(status)}"
        return False, f"unshare(2) failed: {detail}"
    return False, "the probe child was signalled"


def _runs_root(project: str) -> list[str]:
    try:
        runs_dir = cli.runs_root_from_env(dict(os.environ))
    except cli.ConfigError as failure:
        return [FAIL + f"runs root: {failure}"]
    if cli.is_within(runs_dir, project):
        return [FAIL + f"runs root: {runs_dir} is inside the project — a run would refuse (point COUNCIL_RUNS_DIR elsewhere)"]
    if os.path.isdir(runs_dir):
        return [OK + f"runs root: {runs_dir}"]
    holder = runs_dir
    while holder and not os.path.isdir(holder):
        holder = os.path.dirname(holder)
    if not holder or not os.access(holder, os.W_OK | os.X_OK):
        return [FAIL + f"runs root: {runs_dir} cannot be created (the nearest existing directory {holder or '/'} is not writable)"]
    return [OK + f"runs root: {runs_dir} (created on first run)"]
