"""The CLI surface: a table-driven flag set, one-page help, two-line errors.

Everything here is about the *invocation*; what the invocation means is `config.py`'s call.  A usage
error is exit 2, a config error is exit 1, and the two are different exceptions for exactly that reason.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from . import __version__, suggest


class UsageError(Exception):
    """Exit 2: the file is fine, the invocation is not."""


class ConfigError(Exception):
    """Exit 1: a config error, an unresolvable pi, a refused setup."""


@dataclass(frozen=True)
class Flag:
    long: str  # without the leading dashes
    short: str | None = None
    takes_value: bool = True
    choices: tuple[str, ...] | None = None
    note: str = ""  # the override note shown when a repeat changes a value


FLAGS: tuple[Flag, ...] = (
    Flag("project", "p", note="project path"),
    Flag("config", "c", note="config file"),
    Flag("rounds", "r", note="round limit"),
    Flag("goal", "g", note="goal"),
    Flag("transcripts", "t", choices=("off", "full"), note="transcripts level"),
    Flag("allow-nested", None, takes_value=False),
    Flag("untrusted", None, takes_value=False),
)

_LONG = {f"--{f.long}": f for f in FLAGS}
_SHORT = {f"-{f.short}": f for f in FLAGS if f.short}


@dataclass
class Args:
    """A parsed invocation.  Values stay ``None`` when the flag was never stated."""

    council: str | None = None
    project: str | None = None
    config: str | None = None
    rounds: int | None = None
    goal: str | None = None
    transcripts: str | None = None
    allow_nested: bool = False
    untrusted: bool = False
    notes: list[str] = field(default_factory=list)


HELP = """\
usage: council [<council>] [flags]

Run one named council from the project's council.toml.

positional arguments:
  <council>          a [councils.<name>] key; omit it when the file holds one council

flags:
  -p, --project <path>     project directory (default: the invoking cwd)
  -c, --config <file>      council.toml to convene from (default: <project>/council.toml)
  -r, --rounds <N>         round limit (whole number, no cap)
  -g, --goal <text>        the goal the seats are briefed with
  -t, --transcripts <lvl>  off (default) or full
      --allow-nested       let this invocation start inside a running council
      --untrusted          spawn members with --no-approve: ignore project-local files
  -h, --help               show this help and exit
  -V, --version            show the version and exit

exit codes: 0 a result was written, 1 no result, 2 usage error, 130/143 signalled
"""


def _whole_number(raw: str, flag: Flag) -> int:
    """Whole numbers only — `1e3`, `0x10`, `3.5` are refused, naming the long flag."""
    text = raw.strip()
    if text and text[0] in "+-":
        digits, sign = text[1:], text[0]
    else:
        digits, sign = text, ""
    if not digits or not digits.isdigit():
        raise UsageError(f"--{flag.long} wants a whole number, not {raw!r}")
    value = int(digits)
    return -value if sign == "-" else value


def parse_args(argv: list[str]) -> Args:
    """Table-driven parse: `--flag value`, `--flag=value`, bare `--`, last-wins."""
    args = Args()
    positional: list[str] = []
    seen_end = False
    i = 0
    while i < len(argv):
        token = argv[i]
        i += 1
        if seen_end or not token.startswith("-") or token == "-":
            positional.append(token)
            continue
        if token == "--":
            seen_end = True
            continue
        if token in ("-h", "--help"):
            sys.stdout.write(HELP)
            raise SystemExit(0)
        if token in ("-V", "--version"):
            sys.stdout.write(f"council {__version__}\n")
            raise SystemExit(0)
        inline: str | None = None
        name = token
        if token.startswith("--") and "=" in token:
            name, inline = token.split("=", 1)
        flag = _LONG.get(name) or _SHORT.get(name)
        if flag is None:
            hint = suggest.suggest(name, [f"--{f.long}" for f in FLAGS] + ["--help", "--version"])
            extra = f" (did you mean {hint}?)" if hint else ""
            raise UsageError(f"unknown flag {name!r}{extra}")
        if not flag.takes_value:
            if inline is not None:
                raise UsageError(f"--{flag.long} is a flag: it does not take a value")
            setattr(args, flag.long.replace("-", "_"), True)
            continue
        if inline is None:
            if i >= len(argv):
                raise UsageError(f"--{flag.long} wants a value")
            value = argv[i]
            i += 1
        else:
            value = inline
        if flag.choices and value not in flag.choices:
            raise UsageError(f"--{flag.long} is one of {'|'.join(flag.choices)}, not {value!r}")
        if flag.long == "rounds":
            value_num = _whole_number(value, flag)
            _record_override(args, "rounds", value_num, flag, args.notes)
        else:
            _record_override(args, flag.long, value, flag, args.notes)
    if len(positional) > 1:
        extra = " ".join(positional[1:])
        raise UsageError(f"one positional is accepted, not a second one ({extra!r})")
    if positional:
        args.council = positional[0]
    return args


def _record_override(obj: object, attr: str, value: object, flag: Flag, notes: list[str]) -> None:
    """A repeat keeps its last value; a repeat that *changes* it is noted."""
    old = getattr(obj, attr)
    if old is not None and old != value:
        notes.append(f"--{flag.long} {value} overrides {old} (the last one wins)")
    setattr(obj, attr, value)


def fail(message: str) -> int:
    """The CLI contract's two-line complaint, never a 30-line usage dump."""
    sys.stderr.write(f"{message}\n")
    sys.stderr.write("run 'council --help'\n")
    return 2


def resolve_project(raw: str | None) -> str:
    """The project directory, canonicalized — both sides of every containment test are resolved the same way."""
    path = os.path.abspath(raw) if raw else os.getcwd()
    return os.path.realpath(path)


def _runs_root_from_env(environ: dict[str, str]) -> str:
    """The three `COUNCIL_RUNS_DIR` resolution rules; pure — nothing is created here."""
    raw = environ.get("COUNCIL_RUNS_DIR") or ""
    if raw:
        # Rule 2: a relative COUNCIL_RUNS_DIR is made absolute against the invoking cwd.
        return os.path.realpath(raw if os.path.isabs(raw) else os.path.join(environ.get("CWD", os.getcwd()), raw))
    cache = environ.get("XDG_CACHE_HOME") or ""
    if cache:
        # Rule 2: a relative XDG_CACHE_HOME is invalid and ignored — XDG's own rule.
        if os.path.isabs(cache):
            return os.path.realpath(os.path.join(cache, "council", "runs"))
    home = environ.get("HOME") or ""
    if not home:
        raise ConfigError("no runs root: set COUNCIL_RUNS_DIR or HOME (XDG_CACHE_HOME is unset or empty and there is no $HOME)")
    return os.path.realpath(os.path.join(home, ".cache", "council", "runs"))


def is_within(path: str, parent: str) -> bool:
    """Pure containment test, both sides already resolved."""
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def check_nested_guard(allow_nested: bool, environ: dict[str, str]) -> str | None:
    """The nested-council guard.  `COUNCIL_RUN_DIR` is the only marker."""
    marker = environ.get("COUNCIL_RUN_DIR") or ""
    if not marker or allow_nested:
        return None
    parent = environ.get("COUNCIL_RUN_PATH") or ""
    where = f"; parent run directory: {parent}" if parent else ""
    return (
        f"refusing to start a nested council: COUNCIL_RUN_DIR={marker} is set{where}. "
        "Pass --allow-nested to convene inside a running council."
    )


# --------------------------------------------------------------------------- CLI contract, exit codes `main()`, and the only place
# exit codes are chosen.  `python -m council` reaches it the same way the console script does, so there is one entry and one
# contract. The internal `--setup-seat` dispatch comes first: the launcher form is this same entry point, resolved by the spawner
# as `sys.executable -m council` and never as `sys.argv[0]`, so a member never runs whatever `sys.argv[0]` happened to be called.


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from . import launcher

    if argv[:1] == ["--setup-seat"]:
        return launcher.setup_seat_main(argv[1:])
    from . import startup

    try:
        _args, launch = startup.convene(argv)
    except UsageError as failure:
        return fail(str(failure))
    except ConfigError as failure:
        sys.stderr.write(f"{failure}\n")
        return 1
    try:
        startup.prepare_runs_root(launch.runs_dir)
    except ConfigError as failure:
        sys.stderr.write(f"{failure}\n")
        return 1
    for note in launch.notes:  # the override notes, each saying what it costs
        print(f"note: {note}")
    from . import supervisor

    return supervisor.spawn_orchestrator(lambda: _inside_namespace(launch), name="orchestrator")


def _inside_namespace(launch) -> int:
    """The sandbox steps — private remount, dumpability, the tmpfs cap, the per-member overlays, the
    lower-layer fd — then the run, then nothing left behind, in the order `sandbox/setup.py` performs them.

    A sandbox step that fails is fatal: one line naming the syscall and its errno, no fallback, no probe,
    and — because the run directory is created only after this returns cleanly — nothing created anywhere.
    """
    import asyncio

    from . import startup
    from .sandbox import setup as sandbox_setup

    members = launch.council.member_file_bases
    try:
        sandbox = sandbox_setup.establish(
            project=launch.council.project,
            runs_dir=launch.runs_dir,
            sandbox_mib=launch.council.sandbox_mib,
            members=members,
            scratch_cwd=launch.runs_dir,
            sandbox_dir=launch.sandbox_dir,
        )
    except OSError as failure:
        where = f"{failure.filename} " if failure.filename else ""
        sys.stderr.write(f"sandbox setup failed: {where}{os.strerror(failure.errno or 0)} (errno {failure.errno})\n")
        return 1
    except Exception as failure:
        sys.stderr.write(f"sandbox setup failed: {failure}\n")
        return 1
    try:
        launch.run_dir = startup.create_run_dir(launch)
    except ConfigError as failure:
        sys.stderr.write(f"{failure}\n")
        return 1
    from .run import orchestrate

    try:
        return asyncio.run(orchestrate(launch, sandbox))
    finally:
        sandbox.close()
