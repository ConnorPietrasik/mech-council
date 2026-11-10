from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from . import __version__, suggest


class UsageError(Exception):
    pass


class ConfigError(Exception):
    pass


#: What a run's directory keeps beyond the report, lowest level first: summary adds `run.json`,
#: detail adds `human_input.json`, debug adds `events.jsonl`, full adds the verbatim transcripts.
VERBOSITY_LEVELS: tuple[str, ...] = ("quiet", "summary", "detail", "debug", "full")

#: each level's index in VERBOSITY_LEVELS, so comparisons read as `verbosity >= SUMMARY`
QUIET, SUMMARY, DETAIL, DEBUG, FULL = (VERBOSITY_LEVELS.index(name) for name in VERBOSITY_LEVELS)


@dataclass(frozen=True)
class Flag:
    long: str  # without the leading dashes
    short: str | None = None
    takes_value: bool = True
    choices: tuple[str, ...] | None = None
    note: str = ""


FLAGS: tuple[Flag, ...] = (
    Flag("project", "p", note="project path"),
    Flag("config", "c", note="config file"),
    Flag("rounds", "r", note="round limit"),
    Flag("goal", "g", note="goal"),
    Flag("plan", None, note="a plan document to structure into stages (implement runs only)"),
    Flag("review-file", None, note="a previous run's report.json: fix its findings instead of convening the review crew (fix runs only)"),
    Flag("output", "o", note="where the report is written"),
    Flag("verbosity", None, choices=VERBOSITY_LEVELS, note="artifact level"),
    Flag("format", None, choices=("md", "html", "json", "both"), note="report format"),
    Flag("human", None, choices=("blocking", "non_blocking", "none"), note="the human's posture toward the council's questions"),
    Flag("allow-nested", None, takes_value=False),
    Flag("untrusted", None, takes_value=False),
    Flag("in-place", None, takes_value=False),
    Flag("dirty", None, takes_value=False, note="implement and fix runs: start on a dirty git tree anyway"),
    Flag("overwrite", None, takes_value=False),
)

_LONG = {f"--{f.long}": f for f in FLAGS}
_SHORT = {f"-{f.short}": f for f in FLAGS if f.short}


@dataclass
class Args:
    council: str | None = None
    project: str | None = None
    config: str | None = None
    rounds: int | None = None
    goal: str | None = None
    plan: str | None = None
    review_file: str | None = None
    verbosity: str | None = None
    format: str | None = None
    human: str | None = None
    output: str | None = None
    allow_nested: bool = False
    untrusted: bool = False
    in_place: bool = False
    dirty: bool = False
    overwrite: bool = False
    notes: list[str] = field(default_factory=list)


HELP = """\
usage: mech-council [<council>] [flags]

Run one named council from the project's council.toml, over the user's.  A council may `extends = "<name>"`
another for its roster, chair and knobs; a `_` name is what one inherits from, never
something you convene.

positional arguments:
  <council>          a [councils.<name>] to convene; omit it when the file holds exactly one
  init               write the starter user council.toml — it convenes no council; `mech-council init --help`
  list               list the councils the config offers, in config order; `mech-council list --help`
  doctor             check what a run needs — config, pi, namespaces, the runs root; `mech-council doctor --help`

flags:
  -p, --project <path>     project directory (default: the invoking cwd)
  -c, --config <file>      the only council.toml to read (default: <project>/council.toml over $XDG_CONFIG_HOME/council/council.toml)
  -r, --rounds <N>         round limit (whole number, no cap)
  -g, --goal <text>        the goal the seats are briefed with
      --plan <file>        implement runs only: skip the plan phase and structure this plan document
                           (a previous run's plan.md) into the stages and gates the run builds
      --review-file <file> fix runs only: skip the review crew and fix the findings of this file —
                           a review run's report.json (run it with --format json to get one)
      --dirty              implement and fix runs: start even on a dirty git tree — the stage diffs and
                           the report's diffstat are measured against it
      --verbosity <lvl>    quiet (default) < summary < detail < debug < full: what the run directory keeps beyond
                           the report — summary adds run.json, detail adds human_input.json, debug adds events.jsonl,
                           full adds the verbatim transcripts under agents/
      --format <fmt>       md (default), html, json or both: the report's format. html is one self-contained
                           page — inline styles, no scripts, nothing to fetch; json is the docket as one
                           machine-readable document; both writes the .md and the .html side by side.
                           The report_format key in council.toml sets it; this flag overrides it
  --human <posture>        blocking (default), non_blocking or none: whether the run waits on the
                           council's questions, and whether the human answers them at all. Unstated with
                           no terminal on stdin, the run takes none
  -o, --output <path>      write the report here instead of into the run directory: an existing
                           directory keeps the mode's name (report.md / plan.md / implementation.md /
                           fix.md, or the .html and .json equivalents), anything else is the file itself.
                           Nothing else follows it there; run.json names it
                           when the verbosity keeps run.json (summary or higher)
      --overwrite          replace an existing --output target without being asked first
      --allow-nested       let this invocation start inside a running council
      --untrusted          spawn members with --no-approve: ignore project-local files
      --in-place           run members in the real project: no namespaces, no overlays, no masks
  -h, --help               show this help and exit
  -V, --version            show the version and exit

exit codes: 0 a result was written, 1 no result, 2 usage error, 130/143 signalled — the first
              signal reports where the run stood; a second, a beat later, stops waiting for the
              members, kills what is left, and ends faster; one more after that starts the two
              seconds it takes to end over from there
"""


def _whole_number(raw: str, flag: Flag) -> int:
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
            sys.stdout.write(f"mech-council {__version__}\n")
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
            _record_override(args, flag.long.replace("-", "_"), value, flag, args.notes)
    if len(positional) > 1:
        extra = " ".join(positional[1:])
        raise UsageError(f"one positional is accepted, not a second one ({extra!r})")
    if positional:
        args.council = positional[0]
    return args


def _record_override(obj: object, attr: str, value: object, flag: Flag, notes: list[str]) -> None:
    old = getattr(obj, attr)
    if old is not None and old != value:
        notes.append(f"--{flag.long} {value} overrides {old} (the last one wins)")
    setattr(obj, attr, value)


@dataclass(frozen=True)
class InspectArgs:
    """What `-p`/`-c` give the inspect subcommands; `help` is the caller's to print as its own text."""

    project: str | None
    config: str | None
    help: bool = False


def parse_inspect_flags(argv: list[str], what: str) -> InspectArgs:
    """The flags `list` and `doctor` share: `-p/--project` and `-c/--config`, the last one winning."""
    project: str | None = None
    config: str | None = None
    i = 0
    while i < len(argv):
        token = argv[i]
        i += 1
        if token in ("-h", "--help"):
            return InspectArgs(project, config, help=True)
        name, inline = token, None
        if token.startswith("--") and "=" in token:
            name, inline = token.split("=", 1)
        if name not in ("-p", "--project", "-c", "--config"):
            if not token.startswith("-"):
                raise UsageError(f"{what} accepts no positionals: {token!r} (it takes only -p/--project and -c/--config)")
            hint = suggest.suggest(name.lstrip("-"), ["project", "config", "help"])
            extra = f" (did you mean {hint}?)" if hint else ""
            raise UsageError(f"unknown flag {name!r} for {what}: it takes only -p/--project and -c/--config{extra}")
        if inline is None:
            if i >= len(argv):
                raise UsageError(f"{name} wants a value")
            inline = argv[i]
            i += 1
        if name in ("-p", "--project"):
            project = inline
        else:
            config = inline
    return InspectArgs(project, config)


def fail(message: str) -> int:
    sys.stderr.write(f"{message}\n")
    sys.stderr.write("run 'mech-council --help'\n")
    return 2


def resolve_project(raw: str | None) -> str:
    path = os.path.abspath(raw) if raw else os.getcwd()
    return os.path.realpath(path)


def runs_root_from_env(environ: dict[str, str]) -> str:
    raw = environ.get("COUNCIL_RUNS_DIR") or ""
    if raw:
        return os.path.realpath(raw if os.path.isabs(raw) else os.path.join(environ.get("CWD", os.getcwd()), raw))
    cache = environ.get("XDG_CACHE_HOME") or ""
    if cache:
        if os.path.isabs(cache):
            return os.path.realpath(os.path.join(cache, "council", "runs"))
    home = environ.get("HOME") or ""
    if not home:
        raise ConfigError("no runs root: set COUNCIL_RUNS_DIR or HOME (XDG_CACHE_HOME is unset or empty and there is no $HOME)")
    return os.path.realpath(os.path.join(home, ".cache", "council", "runs"))


def _user_config_path(environ: dict[str, str]) -> str | None:
    raw = environ.get("COUNCIL_CONFIG") or ""
    if raw:
        return os.path.realpath(raw if os.path.isabs(raw) else os.path.join(environ.get("CWD", os.getcwd()), raw))
    config_home = environ.get("XDG_CONFIG_HOME") or ""
    if config_home and os.path.isabs(config_home):
        return os.path.realpath(os.path.join(config_home, "council", "council.toml"))
    home = environ.get("HOME") or ""
    return os.path.realpath(os.path.join(home, ".config", "council", "council.toml")) if home else None


def config_candidates(explicit: str | None, project: str, environ: dict[str, str]) -> tuple[list[str], str | None]:
    if explicit:
        return [os.path.realpath(explicit)], None
    paths = list(dict.fromkeys([path for path in (_user_config_path(environ), os.path.join(project, "council.toml")) if path]))
    return paths, os.path.join(project, "council.toml")


def is_within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def sandbox_supported() -> bool:
    from .sandbox import mount

    return mount.supported()


def no_fork_refusal(plat: str | None = None) -> str | None:
    if hasattr(os, "fork"):
        return None
    plat = sys.platform if plat is None else plat
    return f"this host ({plat!r}) cannot fork: the council's orchestrator and every member are forked children, so nothing can run here."


def downgrade_hint() -> str:
    return " Pass --in-place to run the members in the project itself instead."


def proc_clearing_note(in_place: bool) -> str:
    """Which side of the uid line the run is on."""
    if in_place or os.getuid() != 0:
        return ""
    return (
        "running as uid 0: the sandbox's /proc clearing is best-effort at this uid — a seat can list this run's open "
        "descriptors (their numbers only; its cwd, environ and exe stay unreadable)"
    )


def check_nested_guard(allow_nested: bool, environ: dict[str, str]) -> str | None:
    marker = environ.get("COUNCIL_RUN_DIR") or ""
    if not marker or allow_nested:
        return None
    parent = environ.get("COUNCIL_RUN_PATH") or ""
    where = f"; parent run directory: {parent}" if parent else ""
    return (
        f"refusing to start a nested council: COUNCIL_RUN_DIR={marker} is set{where}. "
        "Pass --allow-nested to convene inside a running council."
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from . import launcher

    if argv[:1] == ["--setup-seat"]:
        return launcher.setup_seat_main(argv[1:])
    if argv[:1] == ["init"]:
        from . import init_config

        return init_config.main(argv[1:])
    if argv[:1] == ["list"]:
        from . import list_councils

        return list_councils.main(argv[1:])
    if argv[:1] == ["doctor"]:
        from . import doctor

        return doctor.main(argv[1:])
    from . import startup

    try:
        _args, launch = startup.convene(argv)
    except UsageError as failure:
        return fail(str(failure))
    except ConfigError as failure:
        sys.stderr.write(f"{failure}\n")
        return 1
    try:
        launch.notes += startup.confirm_overwrite(launch)
    except ConfigError as failure:
        sys.stderr.write(f"{failure}\n")
        return 1
    try:
        startup.prepare_runs_root(launch.runs_dir)
    except ConfigError as failure:
        sys.stderr.write(f"{failure}\n")
        return 1
    for note in launch.notes:
        print(f"note: {note}")
    from . import supervisor

    return supervisor.spawn_orchestrator(lambda: _run_council(launch), name="orchestrator", in_place=launch.council.in_place)


def _run_council(launch) -> int:
    import asyncio

    from . import startup
    from .sandbox import setup as sandbox_setup

    members = launch.council.member_file_bases
    in_place = launch.council.in_place
    what = "in-place setup" if in_place else "sandbox setup"
    hint = "" if in_place else downgrade_hint()
    # the run directory is claimed before the sandbox is established: on a same-second collision the
    # rename must reach `sandbox_dir` before the scratch paths are made at the old name
    try:
        launch.run_dir = startup.create_run_dir(launch)
    except ConfigError as failure:
        sys.stderr.write(f"{failure}\n")
        return 1
    try:
        sandbox = sandbox_setup.establish(
            project=launch.council.project,
            runs_dir=launch.runs_dir,
            sandbox_mib=launch.council.sandbox_mib,
            members=members,
            scratch_cwd=launch.runs_dir,
            sandbox_dir=launch.sandbox_dir,
            in_place=in_place,
        )
    except OSError as failure:
        where = f"{failure.filename} " if failure.filename else ""
        sys.stderr.write(f"{what} failed: {where}{os.strerror(failure.errno or 0)} (errno {failure.errno}).{hint}\n")
        startup.discard_run_dir(launch)
        return 1
    except Exception as failure:
        sys.stderr.write(f"{what} failed: {failure}.{hint}\n")
        startup.discard_run_dir(launch)
        return 1
    except BaseException:
        # a signal during setup: clear the still-empty run directory, and let the signal through
        startup.discard_run_dir(launch)
        raise
    from .run import orchestrate

    try:
        return asyncio.run(orchestrate(launch, sandbox))
    finally:
        sandbox.close()
