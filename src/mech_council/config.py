from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from typing import TypeIs

from . import suggest
from .cli import VERBOSITY_LEVELS, Args, ConfigError, UsageError, is_within

BUILTIN_DEFAULTS: dict[str, object] = {
    "mode": "review",
    "goal": None,
    "max_rounds": 10,
    "grace_rounds": 1,
    "seconding": True,
    "watchdog_sec": 600,
    "chair_timeout_sec": 300,
    "verbosity": "quiet",
    "report_format": "md",
    "pi_cli": None,
    "sandbox_mib": 32768,
    "in_place": False,
    "human": "blocking",
    "planning_rounds": 4,
    "fix_budget": 3,
    "replans": 2,
    "planner": None,
    "reviewer": None,
}

#: name → (type, `[defaults]`-allowed)
SCALAR_KEYS: dict[str, tuple[str, bool]] = {
    "human": ("human", True),
    "max_rounds": ("int>=1", True),
    "grace_rounds": ("int>=0", True),
    "seconding": ("bool", True),
    "watchdog_sec": ("int>=0", True),
    "chair_timeout_sec": ("int>=1", True),
    "sandbox_mib": ("int>=1", True),
    "in_place": ("bool", True),
    "verbosity": ("verbosity", True),
    "report_format": ("report_format", True),
    "pi_cli": ("str", True),
    "mode": ("mode", False),
    "goal": ("str", False),
    "planning_rounds": ("int>=1", True),
    "fix_budget": ("int>=1", True),
    "replans": ("int>=0", True),
    "planner": ("str", False),
    "reviewer": ("str", False),
}
#: What a seat table states; `drop` is a patch verb, so it is `PATCH_KEYS` only.
AGENT_KEYS = {"name": "str", "provider": "str", "model": "str", "persona": "str", "role": "role"}
PATCH_KEYS = AGENT_KEYS | {"drop": "bool"}

#: A builder seat's `role`: who it writes. `code` writes code, `prose` writes user-visible text, `both` writes either.
BUILDER_ROLES: tuple[str, ...] = ("code", "prose", "both")

#: The `max_concurrency` value that reads the limit from the provider itself at convene.
AUTO = "auto"

#: What a `[providers.<name>]` table states.
PROVIDER_KEYS: dict[str, str] = {"max_concurrency": "concurrency"}

#: The keys a council table (or a template, which is one too) may state.
COUNCIL_KEYS = set(SCALAR_KEYS) | {"agents", "chair", "extends"}

#: A council name starting with this is a template: `extends` reaches it, `mech-council <name>` does not.
TEMPLATE_PREFIX = "_"

#: council names the subcommands claim for themselves: a config may not name a council after one
RESERVED_NAMES: tuple[str, ...] = ("init", "list", "doctor")


@dataclass
class MemberSpec:
    name: str
    provider: str | None = None
    model: str | None = None
    persona: str | None = None
    id: str | None = None  # `AgentN`, assigned at convening
    file_base: str = ""
    model_warning: str | None = None
    role: str = "both"  # implement builders: code | prose | both


@dataclass
class Council:
    name: str
    mode: str
    goal: str | None
    agents: list[MemberSpec]
    chair: MemberSpec
    max_rounds: int
    grace_rounds: int
    seconding: bool
    watchdog_sec: int
    chair_timeout_sec: int
    verbosity: int  # index into cli.VERBOSITY_LEVELS: what the run directory keeps beyond the report
    sandbox_mib: int
    pi_cli: str
    pi_version: str
    human: str = "blocking"
    #: The report's format: `md` the markdown file, `html` one self-contained page, `both` the pair.
    report_format: str = "md"
    #: The councils whose tables merged into this one, nearest first.
    extends: list[str] = field(default_factory=list)
    config_path: str = ""
    #: Every file the config was read from, lowest precedence first.
    config_layers: list[str] = field(default_factory=list)
    untrusted: bool = False
    #: Members work in the real project: no namespaces, no overlays, no masks, no write journal.
    in_place: bool = False
    project: str = ""
    #: implement: the planning half's round budget; execution depth is the per-stage ladders.
    planning_rounds: int = 4
    #: implement: the per-stage repair ladder's fix passes.
    fix_budget: int = 3
    #: implement: the per-stage repair ladder's replan passes; 0 disables replanning.
    replans: int = 2
    #: implement: the council that writes the plan, and the one that reviews each stage.
    planner: str | None = None
    reviewer: str | None = None
    #: This roster's providers, by the total requests each takes at once, the chair's included; absent means uncapped.
    provider_concurrency: dict[str, int] = field(default_factory=dict)
    #: One line per `auto` provider the probe resolved or fell back on.
    provider_notes: list[str] = field(default_factory=list)

    @property
    def adapter(self):
        from .modes import adapter_for

        return adapter_for(self.mode)

    @property
    def member_file_bases(self) -> list[str]:
        return [agent.file_base for agent in self.agents] + [self.chair.file_base]

    @property
    def seat_ids(self) -> list[str]:
        ids: list[str] = []
        for agent in self.agents:
            if agent.id is None:
                raise ConfigError(f"seat {agent.file_base} was never given a roster id")
            ids.append(agent.id)
        return ids

    @property
    def seat_names(self) -> dict[str, str]:
        return dict(zip(self.seat_ids, [agent.name for agent in self.agents]))

    @property
    def roster_names(self) -> dict[str, str]:
        """Protocol label → the name to print for it, the chair's `chair` included."""
        return {**self.seat_names, self.chair.file_base: self.chair.name}

    @property
    def display_names(self) -> dict[str, str]:
        """`file_base` → the name to print for it: names are unique across the whole roster."""
        return {spec.file_base: spec.name for spec in (*self.agents, self.chair)}


def _check(value: object, kind: str, where: str, key: str) -> object:
    if kind == "int>=1" or kind == "int>=0":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"config key {where}.{key} wants an integer, got {type(value).__name__}: {value!r}")
        if value < (1 if kind == "int>=1" else 0):
            raise ConfigError(f"config key {where}.{key} must be ≥ {1 if kind == 'int>=1' else 0}, got {value}")
    elif kind == "str":
        if not isinstance(value, str):
            raise ConfigError(f"config key {where}.{key} wants a string, got {type(value).__name__}: {value!r}")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"config key {where}.{key} wants true or false, got {type(value).__name__}: {value!r}")
    elif kind == "verbosity":
        if value not in VERBOSITY_LEVELS:
            hint = suggest.suggest(str(value), VERBOSITY_LEVELS)
            extra = f" (did you mean {hint!r}?)" if hint else ""
            raise ConfigError(f"config key {where}.{key} is one of {'/'.join(VERBOSITY_LEVELS)}, got {value!r}{extra}")
    elif kind == "report_format":
        if value not in REPORT_FORMATS:
            hint = suggest.suggest(str(value), REPORT_FORMATS)
            extra = f" (did you mean {hint!r}?)" if hint else ""
            raise ConfigError(f"config key {where}.{key} is one of {'/'.join(REPORT_FORMATS)}, got {value!r}{extra}")
    elif kind == "role":
        if value not in BUILDER_ROLES:
            hint = suggest.suggest(str(value), BUILDER_ROLES)
            extra = f" (did you mean {hint!r}?)" if hint else ""
            raise ConfigError(f"config key {where}.{key} is 'code', 'prose' or 'both', got {value!r}{extra}")
    elif kind == "mode":
        hint = suggest.suggest(str(value), MODES)
        extra = f" (did you mean {hint!r}?)" if hint else ""
        raise ConfigError(f"config key {where}.mode is 'review', 'plan' or 'implement', got {value!r}{extra}")
    elif kind == "human":
        if value in HUMAN_MODES:
            return value
        hint = suggest.suggest(str(value), HUMAN_MODES)
        extra = f" (did you mean {hint!r}?)" if hint else ""
        raise ConfigError(f"config key {where}.human is 'blocking', 'non_blocking' or 'none', got {value!r}{extra}")
    elif kind == "concurrency":
        if value == AUTO:
            return value
        if isinstance(value, str):
            hint = suggest.suggest(value, [AUTO])
            extra = f" (did you mean {hint!r}?)" if hint else ""
            raise ConfigError(f"config key {where}.{key} is an integer ≥ 1 or 'auto', got {value!r}{extra}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"config key {where}.{key} is an integer ≥ 1 or 'auto', got {type(value).__name__}: {value!r}")
        if value < 1:
            raise ConfigError(f"config key {where}.{key} must be ≥ 1, got {value}")
        return value
    return value


#: The mode names config validation accepts.
MODES: tuple[str, ...] = ("review", "plan", "implement")

#: The report's format: the markdown file, one self-contained HTML page, or the pair.
REPORT_FORMATS: tuple[str, ...] = ("md", "html", "both")

#: The human's posture toward the council's questions: the run waits at the bar, never waits, or never asks.
HUMAN_MODES: tuple[str, ...] = ("blocking", "non_blocking", "none")


def _reject_unknown(table: dict, allowed: set[str] | dict, where: str) -> None:
    for key in table:
        if key not in allowed:
            hint = suggest.suggest(str(key), sorted(allowed))
            extra = f" (did you mean {hint}?)" if hint else ""
            raise ConfigError(f"unknown config key {key!r} in {where}{extra}")


def _members(raw: object, key: str, *, chair: bool) -> list[MemberSpec]:
    if chair:
        seats = [(_chair_table(raw, key), f"[{key}]")]
    else:
        seats = [(entry, f"[{key}][{index}]") for index, entry in enumerate(_seat_roster(raw, key), 1)]
    out: list[MemberSpec] = []
    for entry, where in seats:
        _seat_keys(entry, where, patch=False)
        name = _seat_name(entry, where)
        out.append(
            MemberSpec(
                name=name,
                provider=entry.get("provider"),
                model=entry.get("model"),
                persona=entry.get("persona"),
                role=str(entry.get("role") or "both"),
            )
        )
    return out


def _seat_list(value: object) -> TypeIs[list[dict]]:
    return isinstance(value, list) and all(isinstance(entry, dict) for entry in value)


def _seat_roster(raw: object, key: str) -> list[dict]:
    table = f"[{key}]"
    if not _seat_list(raw):
        shape = "a single table" if isinstance(raw, dict) else "not a list of seat tables"
        raise ConfigError(f"{table} is {shape}; agents are written [[{key}]]")
    return raw


def _chair_table(raw: object, key: str) -> dict:
    table = f"[{key}]"
    if isinstance(raw, list):
        raise ConfigError(f"{table} is written [[...]]; the chair is the single table, never a list")
    if not isinstance(raw, dict):
        raise ConfigError(f"{table} is {type(raw).__name__}, not a table; the chair is a single table")
    return raw


def _seat_keys(entry: dict, where: str, *, patch: bool) -> None:
    _reject_unknown(entry, PATCH_KEYS if patch else set(AGENT_KEYS), where)
    for key, kind in AGENT_KEYS.items():
        if key in entry:
            _check(entry[key], kind, where, key)
    if "drop" in entry:
        _check(entry["drop"], "bool", where, "drop")


def _seat_name(entry: dict, where: str) -> str:
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where} needs a non-empty 'name'")
    return name


def _protocol_shaped(name: str) -> bool:
    """A name the human's line grammar would claim for its own: `H1`- or `N1`-shaped."""
    folded = name.strip().casefold()
    return len(folded) >= 2 and folded[0] in "hn" and folded[1:].isdigit()


def _check_written(table: dict, name: str) -> None:
    where = f"[councils.{name}]"
    _reject_unknown(table, COUNCIL_KEYS, where)
    if "extends" in table:
        _check(table["extends"], "str", where, "extends")
    agents_key = f"councils.{name}.agents"
    if table.get("agents") is not None:
        for index, entry in enumerate(_seat_roster(table["agents"], agents_key), 1):
            seat = f"[{agents_key}][{index}]"
            _seat_keys(entry, seat, patch=True)
            _seat_name(entry, seat)
            if entry.get("drop") and "extends" not in table:
                raise ConfigError(f"{seat} drops a seat, but {where} extends no council to inherit one from")
    chair_key = f"councils.{name}.chair"
    if table.get("chair") is not None:
        chair = _chair_table(table["chair"], chair_key)
        if "drop" in chair:
            raise ConfigError(f"[{chair_key}] drops the chair, and every council convenes with exactly one")
        _seat_keys(chair, f"[{chair_key}]", patch=False)


def _patch_seats(parent: list[dict] | None, child: list[dict], roster: str) -> list[dict]:
    """Patch `child`'s seats onto `parent`'s by name: a stated key replaces that key, a name the parent
    lacks joins the end, `drop = true` retires a seat, and order stays parent-first."""
    out: list[dict] = [] if parent is None else [dict(seat) for seat in parent]
    by_name: dict[str, int] = {}
    for written, seat in enumerate(out):
        name = seat.get("name")
        if isinstance(name, str):
            by_name[name] = written
    patched: set[str] = set()
    dropped: set[int] = set()
    for entry in child:
        seat = dict(entry)
        drop = bool(seat.pop("drop", False))
        name = seat.get("name")
        named = name if isinstance(name, str) else None
        if named is not None and named in patched and not drop:
            raise ConfigError(f"{roster} patches {named!r} twice: state a seat once, or drop it and state it again")
        slot = by_name.get(named) if named is not None else None
        if slot is None or slot in dropped:
            if drop:
                raise ConfigError(f"{roster} drops {name!r}, which inherits no seat of that name to drop")
            if named is not None:
                by_name[named] = len(out)
                patched.add(named)
            out.append(seat)
        elif drop:
            dropped.add(slot)
            if named is not None:
                by_name.pop(named, None)
                patched.discard(named)
        else:
            out[slot] = {**out[slot], **seat}
            if named is not None:
                patched.add(named)
    return [seat for written, seat in enumerate(out) if written not in dropped]


def _resolve(config: ConfigFile, name: str, trail: tuple[str, ...] = ()) -> tuple[dict, list[str]]:
    table = config.councils[name]
    _check_written(table, name)
    roster = f"[councils.{name}.agents]"
    if "extends" not in table:
        own = dict(table)
        seats = own.get("agents")
        if _seat_list(seats):
            own["agents"] = [{key: value for key, value in seat.items() if key != "drop"} for seat in seats]
        return own, []
    parent = str(table["extends"])
    path = (*trail, name)
    if parent not in config.councils:
        hint = suggest.suggest(parent, list(config.councils))
        extra = f" (did you mean {hint!r}?)" if hint else ""
        raise ConfigError(f"council {name!r} extends {parent!r}, which is not a council in {config.where}{extra}")
    if parent in path:
        raise ConfigError(f"councils cannot extend in a cycle: {' → '.join([*path, parent])}")
    inherited, chain = _resolve(config, parent, path)
    merged: dict = {**inherited, **table}
    merged.pop("extends")
    if isinstance(merged.get("chair"), dict) and isinstance(inherited.get("chair"), dict):
        merged["chair"] = {**inherited["chair"], **merged["chair"]}
    seats = inherited.get("agents")
    child = table.get("agents")
    if _seat_list(child):
        merged["agents"] = _patch_seats(seats if _seat_list(seats) else None, child, roster)
    return merged, [parent, *chain]


def _flatten(source: dict, defaults: dict, where: str) -> dict:
    _reject_unknown(source, set(SCALAR_KEYS) | {"agents", "chair"}, where)
    out: dict[str, object] = {}
    for key, (kind, _in_defaults) in SCALAR_KEYS.items():
        if key == "mode":
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
            raise ConfigError(f"config key {where}.mode is 'review', 'plan' or 'implement', got {mode!r}{extra}")
        out["mode"] = mode
    return out


@dataclass
class ConfigFile:
    path: str
    defaults: dict = field(default_factory=dict)
    councils: dict[str, dict] = field(default_factory=dict)
    providers: dict[str, dict] = field(default_factory=dict)
    #: Every file read, lowest precedence first.
    paths: list[str] = field(default_factory=list)
    project_layer: str | None = None
    raw: dict[str, dict] = field(default_factory=dict)

    def names(self) -> list[str]:
        """The convenable councils: a `_` name is a template."""
        return [name for name in self.councils if not str(name).startswith(TEMPLATE_PREFIX)]

    @property
    def where(self) -> str:
        """The files councils were read from."""
        return " and ".join(self.paths or [self.path])

    def supplied_by(self, name: str) -> str:
        """The highest-precedence file that states this council's own table."""
        for path in reversed(self.paths or [self.path]):
            if name in (self.raw.get(path, {}).get("councils") or {}):
                return path
        return self.path


def load_config(path: str) -> ConfigFile:
    """Load a single file; it must state councils."""
    return _read(path, require_councils=True)


def load_layers(paths: list[str], *, project_layer: str | None = None) -> ConfigFile:
    """The files merged, lowest precedence first: a later file replaces a same-named council outright."""
    read = [path for path in paths if os.path.isfile(path)]
    if not read:
        if len(paths) == 1:
            load_config(paths[0])  # raises: the one-file refusal, verbatim
        raise ConfigError(f"no council.toml anywhere: no {' and no '.join(paths)}")
    merged = ConfigFile(path=read[-1], paths=read, project_layer=project_layer if project_layer in read else None)
    for path in read:
        layer = _read(path, require_councils=False)
        merged.defaults = {**merged.defaults, **layer.defaults}
        merged.councils = {**merged.councils, **layer.councils}
        merged.providers = {**merged.providers, **layer.providers}
        merged.raw.update(layer.raw)
    if not merged.councils:
        raise ConfigError(f"{merged.where} declare no councils: add a [councils.<name>] table")
    return merged


def _read(path: str, *, require_councils: bool) -> ConfigFile:
    if path.endswith(".json"):
        raise ConfigError(f"refusing {path!r}: the config is TOML (council.toml), never JSON")
    try:
        with open(path, "rb") as fh:  # tomllib takes a binary-mode file only
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}") from None
    except IsADirectoryError:
        raise ConfigError(f"{path} is a directory, not a config file") from None
    except PermissionError:
        raise ConfigError(f"cannot read the config file {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid TOML: {exc}") from None
    _reject_unknown(raw, {"defaults", "councils", "providers"}, path)
    defaults = dict(raw.get("defaults") or {})
    _reject_unknown(defaults, {k for k, (_t, ok) in SCALAR_KEYS.items() if ok}, "[defaults]")
    for key, value in defaults.items():
        kind = SCALAR_KEYS[key][0]
        _check(value, kind, "[defaults]", key)
    councils_raw = raw.get("councils") or {}
    if not isinstance(councils_raw, dict) or (not councils_raw and require_councils):
        raise ConfigError(f"{path} declares no councils: add a [councils.<name>] table")
    for key, table in councils_raw.items():
        if not str(key).strip():
            raise ConfigError(f"{path}: a council name (its table key) must not be empty")
        if str(key) in RESERVED_NAMES:
            raise ConfigError(f"{path}: the council name {key!r} is reserved for the mech-council {key} subcommand")
        if not isinstance(table, dict):
            raise ConfigError(f"council {key!r} must be a table")
    providers = raw.get("providers") or {}
    if not isinstance(providers, dict):
        raise ConfigError(f"{path} states [providers] as {type(providers).__name__}, not a table")
    for name, table in providers.items():
        if not str(name).strip():
            raise ConfigError(f"{path}: a provider name (a [providers.<name>] table key) must not be empty")
        if not isinstance(table, dict):
            raise ConfigError(f"{path} states [providers.{name}] as {type(table).__name__}, not a table")
        _reject_unknown(table, set(PROVIDER_KEYS), f"[providers.{name}]")
        if "max_concurrency" not in table:
            raise ConfigError(f"{path}: [providers.{name}] states no max_concurrency: state the limit or drop the table")
        for key, kind in PROVIDER_KEYS.items():
            if key in table:
                _check(table[key], kind, f"[providers.{name}]", key)
    return ConfigFile(path=path, defaults=defaults, councils=councils_raw, providers=providers, paths=[path], raw={path: raw})


def select_council(config: ConfigFile, name: str | None) -> str:
    names = config.names()
    if not names:
        raise UsageError(f"{config.where} states no council to convene: every [councils.<name>] there starts with '_' and is a template")
    if name is None:
        if len(names) == 1:
            return names[0]
        raise UsageError(f"{'/'.join(names)} are all in {config.where}: name the council to convene")
    if name in names:
        return name
    if name in config.councils:
        raise UsageError(f"council {name!r} in {config.where} is a template: it is there to be extended, not convened")
    hint = suggest.suggest(name, names)
    extra = f" (did you mean {hint}?)" if hint else ""
    raise UsageError(f"no council {name!r} in {config.where}{extra}: it offers {', '.join(names)}")


def stated_by(config: ConfigFile, name: str, chain: list[str], key: str) -> str | None:
    """The highest-precedence council file that states `key` for this council, or None."""
    for path in reversed(config.paths or [config.path]):
        raw = config.raw.get(path, {})
        if key in (raw.get("defaults") or {}):
            return path
        councils = raw.get("councils") or {}
        if any(key in (councils.get(owned) or {}) for owned in [name, *chain]):
            return path
    return None


def _pi_cli_stated_by_project(config: ConfigFile, chain: list[str]) -> bool:
    """Whether the project file is what asked for this `pi_cli`."""
    return stated_by(config, chain[0], chain[1:], "pi_cli") == config.project_layer


def resolve_pi(explicit: str | None, project: str, *, from_project: bool = False) -> tuple[str, str]:
    if explicit:
        if "/" in explicit:
            candidate = explicit if os.path.isabs(explicit) else os.path.join(os.getcwd(), explicit)
            if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
                raise ConfigError(f"pi_cli {explicit!r} is not an executable file")
            resolved = os.path.realpath(candidate)
            if from_project and is_within(resolved, project):
                raise ConfigError(
                    f"pi_cli {explicit!r} is inside the project that stated it, which cannot name the executable every "
                    "member runs: put it in the user council.toml, or convene your own file with --config"
                )
        else:
            found = shutil.which(explicit)
            if not found:
                raise ConfigError(f"pi_cli {explicit!r} is not on PATH")
            resolved = os.path.realpath(found)
    else:
        found = shutil.which("pi")
        if not found:
            raise ConfigError("no pi CLI found: 'pi' is not on PATH and no pi_cli is configured")
        resolved = os.path.realpath(found)
    try:
        proc = subprocess.run([resolved, "--version"], capture_output=True, text=True, timeout=30, check=False)
        version = (proc.stdout or proc.stderr).strip().splitlines()[0] if ((proc.stdout or proc.stderr).strip()) else "unknown"
    except OSError:
        version = "unknown"
    return resolved, version


def convene(
    config: ConfigFile,
    name: str,
    args: Args,
    project: str,
    *,
    untrusted: bool = False,
    namespaces_available: bool = True,
) -> Council:
    council = convene_without_pi(config, name, args, project, untrusted=untrusted, namespaces_available=namespaces_available)
    stated = _pi_cli_stated_by_project(config, [name, *council.extends])
    pi_cli, pi_version = resolve_pi(council.pi_cli or None, project, from_project=stated)
    council.pi_cli, council.pi_version = pi_cli, pi_version
    _auto_providers(config, council)
    return council


def _auto_providers(config: ConfigFile, council: Council) -> None:
    """`auto`: ask each used provider its limit; a provider that answers none runs uncapped, with a note."""
    from . import probe

    used = {spec.provider for spec in (*council.agents, council.chair) if spec.provider}
    for name, table in config.providers.items():
        if table.get("max_concurrency") != AUTO or name not in used or name in council.provider_concurrency:
            continue
        limit, reason = probe.probe(name)
        if limit is None:
            council.provider_notes.append(
                f"provider {name!r} is max_concurrency = 'auto' but reports no limit ({reason}); it runs uncapped"
            )
            continue
        council.provider_concurrency[name] = limit
        council.provider_notes.append(f"provider {name!r} takes {limit} request(s) at once (read from the provider, 'auto')")


def convene_without_pi(
    config: ConfigFile,
    name: str,
    args: Args,
    project: str,
    *,
    untrusted: bool = False,
    namespaces_available: bool = True,
) -> Council:
    """Convene without probing pi: `pi_cli` is what the config wrote, `pi_version` empty; `convene` runs the probe on the result."""
    table, chain = _resolve(config, name)
    via = f" [the roster merges {' → '.join([*reversed(chain), name])}: it can be stated in any of them]" if chain else ""
    flat = _flatten(table, config.defaults, f"[councils.{name}]")
    agents_raw = table.get("agents")
    if agents_raw is None:
        raise ConfigError(f"[councils.{name}] declares no [[councils.{name}.agents]]{via}")
    agents = _members(agents_raw, f"councils.{name}.agents", chair=False)
    if flat["mode"] == "implement":
        # the run's plan comes from a planner crew or a --plan document; the builders never plan themselves
        if not agents:
            raise ConfigError(f"[councils.{name}] declares no builders: an implement run needs at least one{via}")
        if not (flat["planner"] or args.plan):
            raise ConfigError(
                f"[councils.{name}] names no planner council and --plan was not passed: the run's plan comes from one of the two{via}"
            )
    elif len(agents) < 2:
        raise ConfigError(f"[councils.{name}] convenes {len(agents)} agent(s); a council needs ≥ 2{via}")
    #: casefolded name → as written: the human's `<name>: <text>` line matches case-insensitively
    seen: dict[str, str] = {}
    for agent in agents:
        folded = agent.name.casefold()
        if folded in seen:
            if seen[folded] == agent.name:
                raise ConfigError(f"[councils.{name}] lists the agent name {agent.name!r} twice")
            raise ConfigError(f"[councils.{name}] lists the agent names {seen[folded]!r} and {agent.name!r}, which differ only in case")
        seen[folded] = agent.name
    if "chair" not in table:
        raise ConfigError(f"[councils.{name}] has no [councils.{name}.chair]; every council needs one{via}")
    chair = _members(table["chair"], f"councils.{name}.chair", chair=True)[0]
    if "role" in table["chair"]:
        raise ConfigError(f"config key [councils.{name}.chair].role is a builder-seat knob: the chair takes no role{via}")
    folded = chair.name.casefold()
    if folded in seen:
        if seen[folded] == chair.name:
            raise ConfigError(f"[councils.{name}] lists the name {chair.name!r} for both a seat and the chair")
        raise ConfigError(f"[councils.{name}] lists {seen[folded]!r} for a seat and {chair.name!r} for the chair, differing only in case")
    for index, agent in enumerate(agents, 1):
        if _protocol_shaped(agent.name):
            raise ConfigError(
                f"[councils.{name}.agents][{index}] the seat name {agent.name!r} reads like a question or note id (H1, N1), "
                "which the human's line grammar claims for its own"
            )
    if _protocol_shaped(chair.name):
        raise ConfigError(
            f"[councils.{name}.chair] the chair name {chair.name!r} reads like a question or note id (H1, N1), "
            "which the human's line grammar claims for its own"
        )
    incomplete = "states 'provider' without 'model' — pi refuses to start a seat configured that way" + via
    for index, agent in enumerate(agents, 1):
        if agent.provider and not agent.model:
            raise ConfigError(f"[councils.{name}.agents][{index}] {incomplete}")
    if chair.provider and not chair.model:
        raise ConfigError(f"[councils.{name}.chair] {incomplete}")

    goal = args.goal if args.goal is not None else flat["goal"]
    if flat["mode"] == "plan" and not (goal or "").strip():
        raise ConfigError(f"[councils.{name}] is a plan council with no goal stated anywhere")
    if flat["mode"] == "implement":
        if not (goal or "").strip():
            raise ConfigError(f"[councils.{name}] is an implement council with no goal stated anywhere: state a goal or pass --goal")
        if flat["planner"] or flat["reviewer"]:
            _crew_reference(config, name, flat)
    else:
        for key, what in (("planner", "plan"), ("reviewer", "review")):
            if flat[key]:
                raise ConfigError(
                    f"config key [councils.{name}].{key} names a {what} council for an implement run, "
                    f"but {name!r} is a {flat['mode']} council"
                )
        for index, entry in enumerate(agents_raw, 1):
            if isinstance(entry, dict) and "role" in entry:
                raise ConfigError(
                    f"config key [councils.{name}.agents][{index}].role is an implement builder knob, "
                    f"but {name!r} is a {flat['mode']} council"
                )
    if args.plan is not None and flat["mode"] != "implement":
        raise UsageError(f"--plan is an implement flag: {name!r} is a {flat['mode']} council, it runs no plan phase")
    if args.dirty and flat["mode"] != "implement":
        raise UsageError("--dirty overrides a dirty-tree refusal, and only implement runs refuse a dirty tree")

    from .naming import chair_file_base, seat_file_base

    for index, agent in enumerate(agents, 1):
        agent.id = f"Agent{index}"
        agent.file_base = seat_file_base(index, agent.name)
    chair.id = None
    chair.file_base = chair_file_base()

    council = Council(
        name=name,
        mode=str(flat["mode"]),
        goal=goal,
        agents=agents,
        chair=chair,
        max_rounds=args.rounds if args.rounds is not None else int(flat["max_rounds"]),
        grace_rounds=int(flat["grace_rounds"]),
        seconding=bool(flat["seconding"]),
        watchdog_sec=int(flat["watchdog_sec"]),
        chair_timeout_sec=int(flat["chair_timeout_sec"]),
        verbosity=VERBOSITY_LEVELS.index(args.verbosity or str(flat["verbosity"])),
        sandbox_mib=int(flat["sandbox_mib"]),
        pi_cli=str(flat["pi_cli"] or ""),
        pi_version="",
        human=args.human or str(flat["human"]),
        report_format=args.format or str(flat["report_format"]),
        extends=chain,
        config_path=config.supplied_by(name),
        config_layers=list(config.paths),
        untrusted=untrusted,
        in_place=flat["mode"] == "implement" or bool(args.in_place or not namespaces_available or flat["in_place"]),
        project=project,
        planning_rounds=int(flat["planning_rounds"]),
        fix_budget=int(flat["fix_budget"]),
        replans=int(flat["replans"]),
        planner=flat["planner"],
        reviewer=flat["reviewer"],
        provider_concurrency=_provider_limits(config, agents, chair),
    )
    if council.max_rounds < 1:
        raise ConfigError(f"--rounds must be ≥ 1, got {council.max_rounds}")
    return council


def _provider_limits(config: ConfigFile, agents: list[MemberSpec], chair: MemberSpec) -> dict[str, int]:
    """The stated limits for the providers this roster uses; `auto` awaits the probe, so it is absent here."""
    used = {spec.provider for spec in (*agents, chair) if spec.provider}
    return {
        name: int(table["max_concurrency"])
        for name, table in config.providers.items()
        if name in used and isinstance(table.get("max_concurrency"), int)
    }


def provider_seat_pool(council: Council, provider: str | None) -> int:
    """The seats of one provider that ask at once: the provider's limit, the chair's share reserved, or every seat when uncapped."""
    if provider is not None and provider in council.provider_concurrency:
        share = 1 if council.chair.provider == provider else 0
        return max(1, council.provider_concurrency[provider] - share)
    return sum(1 for agent in council.agents if agent.provider == provider)


def _crew_reference(config: ConfigFile, name: str, flat: dict) -> None:
    """`planner` must name a plan council and `reviewer` a review council: no self, no template, no other mode."""
    for key, mode in (("planner", "plan"), ("reviewer", "review")):
        target = flat[key]
        if not target:
            continue
        if target == name:
            raise ConfigError(f"config key [councils.{name}].{key} names the implement council itself, which cannot be its own {key}")
        if str(target).startswith(TEMPLATE_PREFIX):
            raise ConfigError(f"config key [councils.{name}].{key} names {target!r}, a template: templates are extended, not convened")
        if target not in config.councils:
            hint = suggest.suggest(str(target), list(config.councils))
            extra = f" (did you mean {hint}?)" if hint else ""
            raise ConfigError(f"config key [councils.{name}].{key} names {target!r}, which is not a council in {config.where}{extra}")
        try:
            resolved, _chain = _resolve(config, target)
        except ConfigError as failure:
            raise ConfigError(f"config key [councils.{name}].{key} names {target!r}, which does not convene: {failure}") from None
        # the mode as _flatten would read it: stated in the table, else the built-in default
        target_mode = resolved.get("mode") or "review"
        if target_mode != mode:
            raise ConfigError(
                f"config key [councils.{name}].{key} names {target!r}, which is a {target_mode} council: the {key} must be a {mode} council"
            )


def resolve_crews(config: ConfigFile, council: Council, args: Args) -> list[tuple[str, Council]]:
    """The implement run's crews in crew order: `(label, council)` — planner, builder, reviewer.

    A borrowed council contributes its roster, chairs and personas — the run's knobs and posture apply to it.
    Seats take global `AgentN` ids in crew order; borrowed seats are shown with a crew suffix.
    """
    crews: list[tuple[str, Council]] = []
    # `--plan` skips the planning phase, so the planner crew is not convened at all
    if council.planner and not args.plan:
        crews.append(("planner", _borrow(config, council, council.planner)))
    crews.append(("builder", council))
    if council.reviewer:
        crews.append(("reviewer", _borrow(config, council, council.reviewer)))
    for _label, crew in crews:
        if crew is not council:
            # a crew's auto lines are the run's: its notes and run.json carry them; a line the run
            # already has (its own failed probe of the same provider) is not repeated
            council.provider_notes += [note for note in crew.provider_notes if note not in council.provider_notes]
    counter = 0
    for label, crew in crews:
        borrowed = crew is not council
        for agent in crew.agents:
            counter += 1
            agent.id = f"Agent{counter}"
            if borrowed:
                agent.file_base = f"{label}-{agent.file_base}"
                agent.name = f"{agent.name} ({label})"
        if borrowed:
            crew.chair.file_base = f"{label}-{crew.chair.file_base}"
            crew.chair.name = f"{crew.chair.name} ({label})"
    return crews


def _borrow(config: ConfigFile, run: Council, name: str) -> Council:
    """The referenced council as a crew: its roster, chair and personas under the run's knobs.

    The run's goal rides in on the convene, not a patch after it: a plan council that states no goal
    of its own is refused at convening before one could be set.
    """
    crew = convene_without_pi(config, name, Args(goal=run.goal), run.project, untrusted=run.untrusted)
    crew.max_rounds = run.max_rounds
    crew.grace_rounds = run.grace_rounds
    crew.seconding = run.seconding
    crew.watchdog_sec = run.watchdog_sec
    crew.chair_timeout_sec = run.chair_timeout_sec
    # the run's chair stays live through the crew's phase too: it keeps its share of every limit, the
    # crew's re-probed limits included — a failed run probe stores nothing, so the share is applied last
    crew.provider_concurrency = {**crew.provider_concurrency, **run.provider_concurrency}
    _auto_providers(config, crew)  # the run's resolutions are already there: only what the run never probed is asked
    crew.provider_concurrency = {
        name: max(0, limit - (1 if run.chair.provider == name else 0)) for name, limit in crew.provider_concurrency.items()
    }
    crew.verbosity = run.verbosity
    crew.human = run.human
    crew.in_place = True
    crew.project = run.project
    crew.untrusted = run.untrusted
    crew.pi_cli = run.pi_cli
    return crew
