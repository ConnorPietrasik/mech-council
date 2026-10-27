from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from typing import TypeIs

from . import suggest
from .cli import Args, ConfigError, UsageError, is_within

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
    "in_place": False,
}

#: The knob table: name → (type, `[defaults]`-allowed).  A new knob is one row here plus one consumer.
SCALAR_KEYS: dict[str, tuple[str, bool]] = {
    "max_rounds": ("int>=1", True),
    "watchdog_sec": ("int>=0", True),
    "chair_timeout_sec": ("int>=1", True),
    "max_concurrency": ("int>=1", True),
    "sandbox_mib": ("int>=1", True),
    "in_place": ("bool", True),
    "transcripts": ("transcripts", True),
    "pi_cli": ("str", True),
    "mode": ("mode", False),
    "goal": ("str", False),
}
#: What a seat table states.  `drop` is a patch verb rather than a seat field, so it is `PATCH_KEYS` only.
AGENT_KEYS = {"name": "str", "provider": "str", "model": "str", "persona": "str"}
PATCH_KEYS = AGENT_KEYS | {"drop": "bool"}

#: The keys a council table (or a template, which is one too) may state.
COUNCIL_KEYS = set(SCALAR_KEYS) | {"agents", "chair", "extends"}

#: A council name starting with this is a template: `extends` reaches it, `mech-council <name>` does not.
TEMPLATE_PREFIX = "_"


@dataclass
class MemberSpec:
    name: str
    provider: str | None = None
    model: str | None = None
    persona: str | None = None
    slot: int = 0
    id: str | None = None  # `AgentN`, assigned at convening
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
    #: The councils whose tables merged into this one, nearest first.
    extends: list[str] = field(default_factory=list)
    config_path: str = ""
    #: Every file the config was read from, lowest precedence first.
    config_layers: list[str] = field(default_factory=list)
    untrusted: bool = False
    #: Members run in the real project: no namespaces, no overlays, no masks, no write journal.
    in_place: bool = False
    project: str = ""

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
    elif kind == "transcripts":
        if value not in ("off", "full"):
            raise ConfigError(f"config key {where}.{key} is 'off' or 'full', got {value!r}")
    elif kind == "mode":
        hint = suggest.suggest(str(value), MODES)
        extra = f" (did you mean {hint!r}?)" if hint else ""
        raise ConfigError(f"config key {where}.mode is 'review' or 'plan', got {value!r}{extra}")
    return value


#: The mode names config validation accepts; `modes.REGISTRY` is what the machinery dispatches on.
MODES: tuple[str, ...] = ("review", "plan")


def _reject_unknown(table: dict, allowed: set[str] | dict, where: str) -> None:
    for key in table:
        if key not in allowed:
            hint = suggest.suggest(str(key), sorted(allowed))
            extra = f" (did you mean {hint}?)" if hint else ""
            raise ConfigError(f"unknown config key {key!r} in {where}{extra}")


def _members(raw: object, key: str, *, chair: bool) -> list[MemberSpec]:
    """The merged roster or chair, read into specs.  Rules about a *written* table are `_check_written`'s,
    so an inherited typo names the council that wrote it; rules about the *merged* seat are `convene`'s."""
    if chair:
        seats = [(_chair_table(raw, key), f"[{key}]")]
    else:
        seats = [(entry, f"[{key}][{index}]") for index, entry in enumerate(_seat_roster(raw, key), 1)]
    out: list[MemberSpec] = []
    for entry, where in seats:
        _seat_keys(entry, where, patch=False)
        name = _seat_name(entry, where)
        out.append(MemberSpec(name=name, provider=entry.get("provider"), model=entry.get("model"), persona=entry.get("persona")))
    return out


def _seat_list(value: object) -> TypeIs[list[dict]]:
    return isinstance(value, list) and all(isinstance(entry, dict) for entry in value)


def _seat_roster(raw: object, key: str) -> list[dict]:
    """The seat tables of `councils.<name>.agents`.  Shape is settled once, so a roster that cannot be
    read is not patched over in silence by the council doing the patching."""
    table = f"[{key}]"
    if not _seat_list(raw):
        shape = "a single table" if isinstance(raw, dict) else "not a list of seat tables"
        raise ConfigError(f"{table} is {shape}; agents are written [[{key}]]")
    return raw


def _chair_table(raw: object, key: str) -> dict:
    """`councils.<name>.chair` read as the one table the chair is, never the list `[[...]]` writes."""
    table = f"[{key}]"
    if isinstance(raw, list):
        raise ConfigError(f"{table} is written [[...]]; the chair is the single table, never a list")
    if not isinstance(raw, dict):
        raise ConfigError(f"{table} is {type(raw).__name__}, not a table; the chair is a single table")
    return raw


def _seat_keys(entry: dict, where: str, *, patch: bool) -> None:
    """Keys and types of one seat table.  `patch` allows `drop`; a merged seat never carries it, so a
    `drop` the merge failed to strip is named here rather than reaching a member."""
    _reject_unknown(entry, PATCH_KEYS if patch else set(AGENT_KEYS), where)
    for key, kind in AGENT_KEYS.items():
        if key in entry:
            _check(entry[key], kind, where, key)
    if "drop" in entry:
        _check(entry["drop"], "bool", where, "drop")


def _seat_name(entry: dict, where: str) -> str:
    """A seat's name.  Patches match seats by name, so every seat needs one — including the merged
    chair, whose patch states only the keys it changes."""
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where} needs a non-empty 'name'")
    return name


def _check_written(table: dict, name: str) -> None:
    """What one written table states, checked in that table: a typo inside a council another one extends
    names the council that wrote it, not the one that inherited it."""
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
            # Only an `extends` can hand this table a seat to retire.
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
    lacks joins the end, `drop = true` retires a seat, and order stays parent-first.  `drop` is
    stripped here, so it never reaches a `MemberSpec`.  Both lists are validated seat tables."""
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
            # The second would quietly take the keys the first stated; to rebuild a seat, drop it first.
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
    """The table a council convenes with — its own over the one it `extends` — and the chain, nearest
    first.  Raw tables merge rather than resolved values, so a key neither council states still falls
    through to `[defaults]`.  Only this chain is validated."""
    table = config.councils[name]
    _check_written(table, name)
    roster = f"[councils.{name}.agents]"
    if "extends" not in table:
        own = dict(table)
        seats = own.get("agents")
        # `_check_written` keeps `drop = true` out of a table with nothing to patch, so only the verb
        # is stripped here; duplicate names and the rest are for `convene`, which reads the roster.
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
    # No child roster of its own means `{**inherited, **table}` already left the inherited one in place.
    if _seat_list(child):
        merged["agents"] = _patch_seats(seats if _seat_list(seats) else None, child, roster)
    return merged, [parent, *chain]


def _flatten(source: dict, defaults: dict, where: str) -> dict:
    # The merged table, so this is where an `extends` `_resolve` forgot to pop would surface: a
    # council's own keys are `_check_written`'s.
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
            raise ConfigError(f"config key {where}.mode is 'review' or 'plan', got {mode!r}{extra}")
        out["mode"] = mode
    return out


@dataclass
class ConfigFile:
    path: str
    defaults: dict = field(default_factory=dict)
    councils: dict[str, dict] = field(default_factory=dict)
    #: Every file read, lowest precedence first, with the raw table of each: a merged council's own keys
    #: come from one of them, and the messages and `run.json` have to be able to say which.
    paths: list[str] = field(default_factory=list)
    project_layer: str | None = None
    raw: dict[str, dict] = field(default_factory=dict)

    def names(self) -> list[str]:
        """The convenable councils: a `_` name is a template, which `extends` reaches and selection
        never does, and which may state less than a whole council."""
        return [name for name in self.councils if not str(name).startswith(TEMPLATE_PREFIX)]

    @property
    def where(self) -> str:
        """The files councils were read from, for the messages raised before one is chosen."""
        return " and ".join(self.paths or [self.path])

    def supplied_by(self, name: str) -> str:
        """The highest-precedence file that states this council's own table."""
        for path in reversed(self.paths or [self.path]):
            if name in (self.raw.get(path, {}).get("councils") or {}):
                return path
        return self.path


def load_config(path: str) -> ConfigFile:
    """One file, whole: it must state councils and nothing shadows it."""
    return _read(path, require_councils=True)


def load_layers(paths: list[str], *, project_layer: str | None = None) -> ConfigFile:
    """The files merged, lowest precedence first.  A later file replaces a same-named council outright and
    patches `[defaults]` key by key, so `extends` reaches a parent stated in any one of them."""
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
    _reject_unknown(raw, {"defaults", "councils"}, path)
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
        if not isinstance(table, dict):
            raise ConfigError(f"council {key!r} must be a table")
    return ConfigFile(path=path, defaults=defaults, councils=councils_raw, paths=[path], raw={path: raw})


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
    """The highest-precedence council file that states `key` for this council — in its `[defaults]`, in the
    council's own table, or in a table it extends — or None when no file states it."""
    for path in reversed(config.paths or [config.path]):
        raw = config.raw.get(path, {})
        if key in (raw.get("defaults") or {}):
            return path
        councils = raw.get("councils") or {}
        if any(key in (councils.get(owned) or {}) for owned in [name, *chain]):
            return path
    return None


def _pi_cli_stated_by_project(config: ConfigFile, chain: list[str]) -> bool:
    """Whether the auto-discovered project file is what asked for this `pi_cli`, in its own table, in a
    template it extends, or in `[defaults]`."""
    return stated_by(config, chain[0], chain[1:], "pi_cli") == config.project_layer


def resolve_pi(explicit: str | None, project: str, *, from_project: bool = False) -> tuple[str, str]:
    if explicit:
        if "/" in explicit:
            candidate = explicit if os.path.isabs(explicit) else os.path.join(os.getcwd(), explicit)
            if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
                raise ConfigError(f"pi_cli {explicit!r} is not an executable file")
            resolved = os.path.realpath(candidate)
            if from_project and is_within(resolved, project):
                # A council.toml that arrives with a clone must not hand over an executable that arrived with it.
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
    table, chain = _resolve(config, name)
    # A merged seat may be stated in any council of the chain, so roster errors say which ones.
    via = f" [the roster merges {' → '.join([*reversed(chain), name])}: it can be stated in any of them]" if chain else ""
    flat = _flatten(table, config.defaults, f"[councils.{name}]")
    agents_raw = table.get("agents")
    if agents_raw is None:
        raise ConfigError(f"[councils.{name}] declares no [[councils.{name}.agents]]{via}")
    agents = _members(agents_raw, f"councils.{name}.agents", chair=False)
    if len(agents) < 2:
        raise ConfigError(f"[councils.{name}] convenes {len(agents)} agent(s); a council needs ≥ 2{via}")
    seen: set[str] = set()
    for agent in agents:
        if agent.name in seen:
            raise ConfigError(f"[councils.{name}] lists the agent name {agent.name!r} twice")
        seen.add(agent.name)
    if "chair" not in table:
        raise ConfigError(f"[councils.{name}] has no [councils.{name}.chair]; every council needs one{via}")
    chair = _members(table["chair"], f"councils.{name}.chair", chair=True)[0]
    if chair.name in seen:
        raise ConfigError(f"[councils.{name}] lists the name {chair.name!r} for both a seat and the chair")
    # A patch may complete an inherited seat, so this is only knowable about the merged seat.
    incomplete = "states 'provider' without 'model' — pi refuses to start a seat configured that way" + via
    for index, agent in enumerate(agents, 1):
        if agent.provider and not agent.model:
            raise ConfigError(f"[councils.{name}.agents][{index}] {incomplete}")
    if chair.provider and not chair.model:
        raise ConfigError(f"[councils.{name}.chair] {incomplete}")

    goal = args.goal if args.goal is not None else flat["goal"]
    if flat["mode"] == "plan" and not (goal or "").strip():
        raise ConfigError(f"[councils.{name}] is a plan council with no goal stated anywhere")

    from .naming import chair_file_base, seat_file_base

    for index, agent in enumerate(agents, 1):
        agent.slot = index
        agent.id = f"Agent{index}"
        agent.file_base = seat_file_base(index, agent.name)
    chair.slot = 0
    chair.id = None
    chair.file_base = chair_file_base()

    pi_cli, pi_version = resolve_pi(flat["pi_cli"], project, from_project=_pi_cli_stated_by_project(config, [name, *chain]))
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
        extends=chain,
        config_path=config.supplied_by(name),
        config_layers=list(config.paths),
        untrusted=untrusted,
        # A flag, a config layer, or a host with nothing to build the sandbox from: any one of them runs in place.
        in_place=bool(args.in_place or not namespaces_available or flat["in_place"]),
        project=project,
    )
    if council.max_rounds < 1:
        raise ConfigError(f"--rounds must be ≥ 1, got {council.max_rounds}")
    return council
