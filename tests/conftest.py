"""§13's harness rules (PLAN §6), built once and used by every family.

The suite must pass from inside a council member, so nothing here assumes `/tmp` is
usable ground, nothing writes inside the project under test, and every CLI is spawned in
its own process group so a timed-out case cannot leave a supervisor holding mounts.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import sysconfig
import time
from dataclasses import dataclass, field

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_PI = os.path.join(HERE, "support", "fake_pi.py")

# PLAN §6: plain-fs ground, chosen at run time.  Inside a member, `$TMPDIR` is that
# ground; outside, a scratch dir beside the checkout, which is never the project under test.
# PLAN §6: plain ground outside the project under test.  Inside a member `$TMPDIR` *is*
# that ground (its own scratch); outside, a scratch dir beside the checkout.
# `COUNCIL_TEST_FS` names plain ground when the operator knows where it is; `$TMPDIR` is
# right inside a member, where it is that member's own scratch; the checkout's own
# filesystem is the last resort.  §13.9's skip then decides honestly whether the chosen
# ground can hold an overlay lower at all, rather than the family passing on `/tmp`
# because the overlay under it happens to tolerate one mount.
from tests.support.ground import FIXTURE_ROOT  # noqa: E402  (§13.9's rule)


def _plain_ground(name: str) -> str:
    path = os.path.join(FIXTURE_ROOT, f"council-{name}-{os.getpid()}-{time.time_ns()}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return os.path.realpath(path)


@pytest.fixture
def ground(tmp_path_factory):
    """A fresh directory outside any project, removed afterwards."""
    path = _plain_ground("case")
    yield path
    _rmtree(path)


def _rmtree(path: str) -> None:
    for root, dirs, files in os.walk(path, topdown=False):
        for name in dirs:
            _try(os.rmdir, os.path.join(root, name))
        for name in files:
            _try(os.unlink, os.path.join(root, name))
    _try(os.rmdir, path)


def _try(func, *args) -> None:
    try:
        func(*args)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# child environment (§13's harness rules, verbatim)
# ---------------------------------------------------------------------------

@pytest.fixture
def child_env(ground):
    """A scrubbed env: no inherited run marker, throwaway pi state, offline."""
    env = dict(os.environ)
    for key in ("COUNCIL_RUN_DIR", "COUNCIL_RUN_PATH", "COUNCIL_CONFIG"):
        env.pop(key, None)
    env["PI_CODING_AGENT_DIR"] = os.path.join(ground, "pi-state")
    env["PI_OFFLINE"] = "1"
    env["PI_SKIP_VERSION_CHECK"] = "1"
    env["COUNCIL_RUNS_DIR"] = os.path.join(ground, "canary")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    os.makedirs(env["PI_CODING_AGENT_DIR"], mode=0o700, exist_ok=True)
    return env


@pytest.fixture
def canary(child_env):
    """The runs root a family must see stay empty; `assert_canary_empty` checks it."""
    path = child_env["COUNCIL_RUNS_DIR"]
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def canary_entries(path: str) -> list[str]:
    out = []
    for root, _dirs, files in os.walk(path):
        for name in files:
            out.append(os.path.relpath(os.path.join(root, name), path))
    return sorted(out)


# ---------------------------------------------------------------------------
# projects and configs
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = """\
[defaults]
pi_cli = "{pi_cli}"
max_rounds = 2
watchdog_sec = 0

[councils.review]
goal = "Is this right?"
mode = "review"

[councils.review.chair]
name = "Chair"

[[councils.review.agents]]
name = "Metic"

[[councils.review.agents]]
name = "Senior"

[councils.plan]
goal = "How should we do it?"
mode = "plan"

[councils.plan.chair]
name = "Chair"

[[councils.plan.agents]]
name = "Metic"

[[councils.plan.agents]]
name = "Senior"
"""


@pytest.fixture
def project(ground):
    """A tiny project with a `council.toml`; `write_config` rewrites it."""
    path = os.path.join(ground, "proj")
    os.makedirs(os.path.join(path, "src"), exist_ok=True)
    with open(os.path.join(path, "hello.py"), "w", encoding="utf-8") as handle:
        handle.write("print('hello')\n")
    with open(os.path.join(path, "src", "mod.py"), "w", encoding="utf-8") as handle:
        handle.write("VALUE = 1\n")
    write_project_config(path)
    return path


def write_project_config(path: str, text: str | None = None, *,
                         pi_cli: str = FAKE_PI) -> str:
    config = os.path.join(path, "council.toml")
    with open(config, "w", encoding="utf-8") as handle:
        handle.write(text if text is not None else DEFAULT_CONFIG.format(pi_cli=pi_cli))
    return config


# ---------------------------------------------------------------------------
# the CLI, as a subprocess in its own process group
# ---------------------------------------------------------------------------

@dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str
    run_dir: str | None = None
    events: list[dict] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)

    @property
    def console(self) -> str:
        return self.stdout + self.stderr

    def kinds(self) -> list[str]:
        return [str(record.get("event")) for record in self.events]

    def artifact(self, name: str) -> str:
        assert self.run_dir, "the run created no directory to read from"
        with open(os.path.join(self.run_dir, name), encoding="utf-8") as handle:
            return handle.read()

    def report(self) -> str:
        for name in ("report.md", "plan.md"):
            if self.run_dir and os.path.exists(os.path.join(self.run_dir, name)):
                with open(os.path.join(self.run_dir, name), encoding="utf-8") as handle:
                    return handle.read()
        return ""


@pytest.fixture
def cli(child_env):
    """`run_cli(args, cwd=...)` — the real console script, in its own process group."""
    spawned: list[int] = []

    def runner(args, *, cwd=None, env=None, stdin=None, timeout=90, extra_scrub=(),
               scrub=True):
        """Run the CLI.  `scrub=False` is how §13.5's nested-guard case sets the marker."""
        full = dict(child_env)
        full.update(env or {})
        for key in extra_scrub:
            full.pop(key, None)
        if scrub:
            # §13's harness rule: an inherited run marker would trip §3.5's guard.
            full.pop("COUNCIL_RUN_DIR", None)
            full.pop("COUNCIL_RUN_PATH", None)
        process = subprocess.Popen(
            [sys.executable, "-m", "council", *args], cwd=cwd, env=full,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        spawned.append(process.pid)
        try:
            out, err = process.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            out, err = process.communicate(timeout=20)
            raise AssertionError(f"council {' '.join(args)} timed out\n{out}\n{err}")
        record = Run(process.returncode, out, err)
        record.run_dir = _run_dir_from(out)
        if record.run_dir:
            record.events = _read_events(record.run_dir)
            record.manifest = _read_json(os.path.join(record.run_dir, "run.json"))
        return record

    yield runner
    for pid in spawned:
        _kill_group_pid(pid)


def _kill_group(process) -> None:
    _kill_group_pid(process.pid)


def _kill_group_pid(pid: int) -> None:
    with_open_errors = (ProcessLookupError, PermissionError)
    try:
        os.killpg(pid, signal.SIGKILL)
    except with_open_errors:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except with_open_errors:
            return
        time.sleep(0.05)


def _run_dir_from(text: str) -> str | None:
    marker = "run directory "
    for line in text.splitlines():
        if marker in line:
            return line.split(marker, 1)[1].strip()
    return None


def _read_events(run_dir: str) -> list[dict]:
    path = os.path.join(run_dir, "events.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError:
            return {}


# ---------------------------------------------------------------------------
# an offline council, convened in-process (no CLI, no namespace)
# ---------------------------------------------------------------------------

@pytest.fixture
def council(project):
    """The `review` council from the fixture config, convened for real (§3.3)."""
    from council import config as config_mod
    from council.cli import Args

    loaded = config_mod.load_config(os.path.join(project, "council.toml"))
    name = config_mod.select_council(loaded, "review")
    return config_mod.convene(loaded, name, Args(), project)


@pytest.fixture(scope="session")
def console_script() -> str:
    """The installed entry point — §13.9 asserts a member can exec it by absolute path."""
    scripts = sysconfig.get_path("scripts")
    path = os.path.join(scripts, "council")
    return path if os.path.exists(path) else sys.executable
