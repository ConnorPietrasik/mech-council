"""§3.3 — the config loader: strict where §5.3 is lenient, and the two never meet.

`tomllib` polices neither keys nor types, so the loader owns the key set and the value
types at every level; what it must not soften is `TOMLDecodeError`'s line/column, which
belongs in the user-fixable line and never in a traceback.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field

from . import suggest
from .cli import Args, ConfigError

BUILTIN_DEFAULTS: dict[str, object] = {
    "mode": "review",
    "goal": None,
    "max_rounds": 10,
    "watchdog_sec": 600,
    "chair_timeout_sec": 300,
    "max_concurrency": 4,
    "transcripts": "off",
    "pi_cli": None,
    "sandbox_mib": 32768,
}

#: §3.3's knob table: name → (type, `[defaults]`-allowed).
#: A new knob is one row here plus one consumer (plan §7).
SCALAR_KEYS: dict[str, tuple[str, bool]] = {
    "max_rounds": ("int>=1", True),
    "watchdog_sec": ("int>=0", True),
    "chair_timeout_sec": ("int>=1", True),
    "max_concurrency": ("int>=1", True),
    "sandbox_mib": ("int>=1", True),
    "transcripts": ("transcripts", True),
    "pi_cli": ("str", True),
    "mode": ("mode", False),
    "goal": ("str", False),
}
AGENT_KEYS = {"name": "str", "provider": "str", "model": "str", "persona": "str"}


@dataclass
class MemberSpec:
    """An agent or the chair: exactly the same key set (§3.3)."""

    name: str
    provider: str | None = None
    model: str | None = None
    persona: str | None = None
    slot: int = 0
    id: str | None = None          # `AgentN`, assigned at convening (§1.2)
    file_base: str = ""
    model_warning: str | None = None

    @property
    def is_chair(self) -> bool:
        return self.slot == 0


@dataclass
class Council:
    name: str
    mode: str
    goal: str | None
    agents: list[MemberSpec]
    chair: MemberSpec
    max_rounds: int
    watchdog_sec: int
    chair_timeout_sec: int
    max_concurrency: int
    transcripts: str
    sandbox_mib: int
    pi_cli: str
    pi_version: str
    config_path: str = ""
    untrusted: bool = False
    project: str = ""

    @property
    def adapter(self):
        """§12.1.4: the mode is a registry entry, reached through the council that named it."""
        from .modes import adapter_for
        return adapter_for(self.mode)

    @property
    def member_file_bases(self) -> list[str]:
        """Every member, seat and chair alike: §10's per-member pairs are keyed on these."""
        return [agent.file_base for agent in self.agents] + [self.chair.file_base]


def _check(value: object, kind: str, where: str, key: str) -> object:
    if kind == "int>=1" or kind == "int>=0":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"config key {where}.{key} wants an integer, got "
                              f"{type(value).__name__}: {value!r}")
        if value < (1 if kind == "int>=1" else 0):
            raise ConfigError(f"config key {where}.{key} must be ≥ "
                              f"{1 if kind == 'int>=1' else 0}, got {value}")
    elif kind == "str":
        if not isinstance(value, str):
            raise ConfigError(f"config key {where}.{key} wants a string, got "
                              f"{type(value).__name__}: {value!r}")
    elif kind == "transcripts":
        if value not in ("off", "full"):
            raise ConfigError(f"config key {where}.{key} is 'off' or 'full', got {value!r}")
    elif kind == "mode":
        hint = suggest.suggest(str(value), MODES)
        extra = f" (did you mean {hint!r}?)" if hint else ""
        raise ConfigError(f"config key {where}.mode is 'review' or 'plan', got {value!r}{extra}")
    return value


#: The mode registry: config validation reads its literals from here (plan §7), so a new
#: mode is a registry entry plus an adapter, never a scattered literal.
MODES: tuple[str, ...] = ("review", "plan")


def _reject_unknown(table: dict, allowed: set[str] | dict, where: str) -> None:
    for key in table:
        if key not in allowed:
            hint = suggest.suggest(str(key), sorted(allowed))
            extra = f" (did you mean {hint}?)" if hint else ""
            raise ConfigError(f"unknown config key {key!r} in {where}{extra}")


def _members(raw: object, where: str, *, chair: bool) -> list[MemberSpec]:
    if chair and isinstance(raw, list):
        # §3.3: `[[councils.<name>.chair]]` is a config error — the chair is one table.
        raise ConfigError(f"{where} is written [[...]]; the chair is the single table "
                          f"[{where.rsplit('.', 1)[-1]}]")
    if isinstance(raw, dict):
        # §3.3's shape: the chair is one table, `[councils.x.chair]`; agents are a list.
        if not chair:
            raise ConfigError(f"{where} is a single table; agents are written "
                              f"[[{where.rsplit('.', 1)[-1]}]]")
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(m, dict) for m in raw):
        raise ConfigError(f"{where} must be a list of tables")
    out: list[MemberSpec] = []
    for index, entry in enumerate(raw, 1):
        table = f"{where}[{index}]"
        _reject_unknown(entry, set(AGENT_KEYS), table)
        for key, kind in AGENT_KEYS.items():
            if key in entry:
                _check(entry[key], kind, table, key)
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{table} needs a non-empty 'name'")
        if "provider" in entry and "model" not in entry:
            raise ConfigError(f"{table} states 'provider' without 'model' — pi refuses "
                              "to start a seat configured that way")
        out.append(MemberSpec(name=name, provider=entry.get("provider"),
                              model=entry.get("model"), persona=entry.get("persona")))
    if chair and len(out) != 1:
        raise ConfigError(f"{where} is exactly one member")
    return out


def _flatten(source: dict, defaults: dict, where: str) -> dict:
    _reject_unknown(source, set(SCALAR_KEYS) | {"agents", "chair"}, where)
    out: dict[str, object] = {}
    for key, (kind, _in_defaults) in SCALAR_KEYS.items():
        if key == "mode":
            # The mode is validated against the registry below, never as a type, and a
            # council that states none is `review` — the built-in default, not `[defaults]`.
            out[key] = BUILTIN_DEFAULTS[key]
            continue
        if key in source:
            out[key] = _check(source[key], kind, where, key)
        elif key in defaults:
            out[key] = defaults[key]
        else:
            out[key] = BUILTIN_DEFAULTS[key]
    if "mode" in source:
        mode = source["mode"]
        if mode not in MODES:
            hint = suggest.suggest(str(mode), list(MODES))
            extra = f" (did you mean {hint!r}?)" if hint else ""
            raise ConfigError(f"config key {where}.mode is 'review' or 'plan', "
                              f"got {mode!r}{extra}")
        out["mode"] = mode
    return out


@dataclass
class ConfigFile:
    """A parsed `council.toml`: the councils it offers plus the shared `[defaults]`."""

    path: str
    defaults: dict = field(default_factory=dict)
    councils: dict[str, dict] = field(default_factory=dict)

    def names(self) -> list[str]:
        return list(self.councils)


def load_config(path: str) -> ConfigFile:
    """Parse and file-level validate.  Nothing per-council is checked here (§3.3)."""
    if path.endswith(".json"):
        raise ConfigError(f"refusing {path!r}: the config is TOML (council.toml), "
                          "never JSON")
    try:
        with open(path, "rb") as fh:          # tomllib takes a binary-mode file only
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}") from None
    except IsADirectoryError:
        raise ConfigError(f"{path} is a directory, not a config file") from None
    except PermissionError:
        raise ConfigError(f"cannot read the config file {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid TOML: {exc}") from None
    _reject_unknown(raw, {"defaults", "councils"}, path)
    defaults = dict(raw.get("defaults") or {})
    _reject_unknown(defaults, {k for k, (_t, ok) in SCALAR_KEYS.items() if ok}, "[defaults]")
    for key in defaults:
        kind = SCALAR_KEYS[key][0]
        _check(defaults[key], kind, "[defaults]", key)
    councils_raw = raw.get("councils")
    if not isinstance(councils_raw, dict) or not councils_raw:
        raise ConfigError(f"{path} declares no councils: add a [councils.<name>] table")
    for key, table in councils_raw.items():
        if not str(key).strip():
            raise ConfigError(f"{path}: a council name (its table key) must not be empty")
        if not isinstance(table, dict):
            raise ConfigError(f"council {key!r} must be a table")
    return ConfigFile(path=path, defaults=defaults, councils=councils_raw)


def select_council(config: ConfigFile, name: str | None) -> str:
    """§3.3's selection: exact match, or the only council in the file. Never a silent pick."""
    names = config.names()
    if name is None:
        if len(names) == 1:
            return names[0]
        from .cli import UsageError
        raise UsageError(f"{'/'.join(names)} are all in {config.path}: name the council "
                         "to convene")
    if name in names:
        return name
    hint = suggest.suggest(name, names)
    extra = f" (did you mean {hint}?)" if hint else ""
    from .cli import UsageError
    raise UsageError(f"no council {name!r} in {config.path}{extra}: it offers "
                     f"{', '.join(names)}")


def resolve_pi(explicit: str | None, project: str) -> tuple[str, str]:
    """§3.3's pi discovery: council → `[defaults]` → PATH, no env-var step (plan D20)."""
    if explicit:
        if "/" in explicit:
            candidate = explicit if os.path.isabs(explicit) else os.path.join(os.getcwd(), explicit)
            if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
                raise ConfigError(f"pi_cli {explicit!r} is not an executable file")
            resolved = os.path.realpath(candidate)
        else:
            found = shutil.which(explicit)
            if not found:
                raise ConfigError(f"pi_cli {explicit!r} is not on PATH")
            resolved = os.path.realpath(found)
    else:
        found = shutil.which("pi")
        if not found:
            raise ConfigError("no pi CLI found: 'pi' is not on PATH and no pi_cli is "
                              "configured")
        resolved = os.path.realpath(found)
    try:
        proc = subprocess.run([resolved, "--version"], capture_output=True, text=True,
                              timeout=30, check=False)
        version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (
            (proc.stdout or proc.stderr).strip()) else "unknown"
    except OSError:
        version = "unknown"
    return resolved, version


def convene(config: ConfigFile, name: str, args: Args, project: str, *,
            untrusted: bool = False) -> Council:
    """Flatten precedence (flag → council → `[defaults]` → built-in) and validate the
    convened council — the point where §3.3's per-council rules are all load-time checks."""
    table = config.councils[name]
    flat = _flatten(table, config.defaults, f"[councils.{name}]")
    agents_raw = table.get("agents")
    if agents_raw is None:
        raise ConfigError(f"[councils.{name}] declares no [[councils.{name}.agents]]")
    agents = _members(agents_raw, f"[councils.{name}.agents]", chair=False)
    if len(agents) < 2:
        raise ConfigError(f"[councils.{name}] convenes {len(agents)} agent(s); a council "
                          "needs ≥ 2")
    seen = set()
    for agent in agents:
        if agent.name in seen:
            raise ConfigError(f"[councils.{name}] lists the agent name {agent.name!r} twice")
        seen.add(agent.name)
    if "chair" not in table:
        raise ConfigError(f"[councils.{name}] has no [councils.{name}.chair]; every "
                          "council needs one (§9)")
    chair = _members(table["chair"], f"[councils.{name}.chair]", chair=True)[0]
    if chair.name in seen:
        raise ConfigError(f"[councils.{name}] lists the name {chair.name!r} for both a "
                          "seat and the chair")

    goal = args.goal if args.goal is not None else flat["goal"]
    if flat["mode"] == "plan" and not (goal or "").strip():
        raise ConfigError(f"[councils.{name}] is a plan council with no goal stated "
                          "anywhere (§7.1)")

    from .naming import chair_file_base, seat_file_base
    for index, agent in enumerate(agents, 1):
        agent.slot = index
        agent.id = f"Agent{index}"
        agent.file_base = seat_file_base(index, agent.name)
    chair.slot = 0
    chair.id = None
    chair.file_base = chair_file_base()

    pi_cli, pi_version = resolve_pi(flat["pi_cli"], project)
    council = Council(
        name=name,
        mode=str(flat["mode"]),
        goal=goal,
        agents=agents,
        chair=chair,
        max_rounds=args.rounds if args.rounds is not None else int(flat["max_rounds"]),
        watchdog_sec=int(flat["watchdog_sec"]),
        chair_timeout_sec=int(flat["chair_timeout_sec"]),
        max_concurrency=int(flat["max_concurrency"]),
        transcripts=args.transcripts or str(flat["transcripts"]),
        sandbox_mib=int(flat["sandbox_mib"]),
        pi_cli=pi_cli,
        pi_version=pi_version,
        config_path=config.path,
        untrusted=untrusted,
        project=project,
    )
    if council.max_rounds < 1:
        raise ConfigError(f"--rounds must be ≥ 1, got {council.max_rounds}")
    return council
