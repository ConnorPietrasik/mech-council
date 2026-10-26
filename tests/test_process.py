"""The dispose path and the namespace sweep, against real descendants."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from typing import cast

import pytest

from council.pi_rpc import PiRpc
from council.process import lifecycle
from council.process.lifecycle import SweepResult, send, survivor_line, sweep
from council.process.spawn import Member

from .test_sandbox import in_mapped_child

pytestmark = [pytest.mark.timeout(180)]

FAKE_PI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "support", "fake_pi.py")

#: A detached tool shell in its own session, ignoring TERM: the hardest survivor
#: this file can produce, and the reason a group kill is not sufficient.
STRAY = """\
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
os.setsid()
for _ in range(300):
    with open(sys.argv[1], "w") as handle:
        handle.write(str(os.getpid()))
    time.sleep(1)
"""


def spawn_stray(marker: str, *, keep_alive: bool = False) -> int:
    """Start a detached stray; return its pid once it has reported itself."""
    hold = "time.sleep(300)\n" if keep_alive else ""
    subprocess.Popen([sys.executable, "-c", STRAY + hold, marker], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if os.path.exists(marker):
            with open(marker, encoding="utf-8") as handle:
                text = handle.read().strip()
            if text:
                return int(text)
        time.sleep(0.02)
    raise AssertionError("the detached tool shell never reported its pid")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def live_member(tmp_path, *, env=None) -> Member:
    """One real member over real pipes: the fake pi, launched the way the launcher form launches it."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        FAKE_PI,
        "--name",
        "slot1-a",
        "--mode",
        "rpc",
        "--approve",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(tmp_path),
        env=dict(os.environ, **(env or {})),
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stdin is not None, "both halves were PIPEd just above"
    rpc = PiRpc(label="slot1-a", stdout=proc.stdout, stdin=proc.stdin)
    rpc.start()
    return Member(
        file_base="slot1-a",
        id="Agent1",
        pid=proc.pid,
        pgid=os.getpgid(proc.pid),
        namespace=0,
        ns_fd=os.open(f"/proc/{proc.pid}/ns/mnt", os.O_RDONLY),
        proc=proc,
        rpc=rpc,
        ns_records=[],
    )


# -------------------------------------------------------- The pid/pgid guards


@pytest.mark.parametrize("pgid", [0, 1])
def test_send_refuses_the_pids_that_would_hit_the_host(pgid):
    assert send(pgid, signal.SIGKILL, own_pgid=os.getpgid(0)) is False


def test_send_never_signals_our_own_group():
    own = os.getpgid(0)
    assert send(own, signal.SIGKILL, own_pgid=own) is False
    assert alive(os.getpid()), "a guard that fired would have killed the test process"


def test_send_reports_false_for_a_group_that_does_not_exist():
    assert send(4_194_300, signal.SIGKILL, own_pgid=os.getpgid(0)) is False


# ------------------------------------------------------- The dispose path


def test_closing_stdin_is_what_shuts_the_child_down_with_no_signal_at_all(tmp_path):
    """Stdin close first — SIGKILL-first is the bug that leaves tool shells running."""
    script = json.dumps([{"contains": "", "sleep": 30}])

    async def scenario():
        member = await live_member(tmp_path, env={"FAKE_PI_SCRIPT": script})
        started = time.monotonic()
        await lifecycle.dispose(member)
        return member, time.monotonic() - started

    member, elapsed = asyncio.run(scenario())
    assert member.proc.returncode is not None, "the child ignored its closed stdin"
    assert elapsed < lifecycle.STDIN_CLOSE_GRACE_S, f"a clean shutdown took {elapsed:.1f}s: it waited for TERM instead of stdin"
    os.close(member.ns_fd)


def test_a_member_ignoring_term_escalates_to_kill_and_still_goes(tmp_path):
    """The dispose escalation order, on a child that will not take TERM."""
    stubborn = subprocess.Popen(
        [sys.executable, "-c", "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(300)\n"],
        stdin=subprocess.PIPE,
        start_new_session=True,
    )
    assert stubborn.stdin is not None, "stdin was PIPEd above: it is the whole dispose path's first step"

    async def scenario():
        member = await live_member(tmp_path)
        # Hand the member's bookkeeping to the stubborn child: same dispose path,
        # and the only process that ends up signalled is the one this test started.
        member.rpc = cast(PiRpc, _QuietRpc(stubborn.stdin))
        member.pgid = os.getpgid(stubborn.pid)
        real_proc, member.proc = member.proc, cast(asyncio.subprocess.Process, _Pollable(stubborn))
        await lifecycle.dispose(member, own_pgid=os.getpgid(0))
        real_proc.kill()
        await real_proc.wait()
        os.close(member.ns_fd)

    asyncio.run(scenario())
    assert stubborn.stdin.closed, "dispose closes stdin first — that is what starts pi's own shutdown"
    deadline = time.monotonic() + lifecycle.TERM_GRACE_S + 15
    while stubborn.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert stubborn.poll() is not None, "a child ignoring TERM survived the dispose path"
    if alive(stubborn.pid):
        os.killpg(stubborn.pid, signal.SIGKILL)


class _Pollable:
    """`dispose` reads `returncode` and awaits `wait()`; this forwards both."""

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process

    @property
    def returncode(self):
        return self._process.poll()

    async def wait(self):
        while self._process.poll() is None:
            await asyncio.sleep(0.05)
        return self._process.returncode


class _QuietRpc:
    """The two RPC calls `dispose` makes, against a child that is not a pi."""

    def __init__(self, stdin) -> None:
        self._stdin = stdin

    async def close_stdin(self) -> None:
        # A `Popen` pipe is a BufferedWriter: it closes and it is done — there is no `wait_closed`
        # to await, and swallowing that mistake here would hide the very step this test is about.
        if self._stdin is not None and not self._stdin.closed:
            self._stdin.close()

    async def stop(self) -> None:
        return None


# --------------------------------------------------------- The namespace sweep


def test_the_sweep_kills_what_lives_in_the_namespace_it_holds(tmp_path):
    """A stray inside a namespace we hold dies; the report names that namespace."""
    marker = str(tmp_path / "stray")

    def probe():
        import json as _json
        import os as _os
        import subprocess as _sub
        import sys as _sys
        import time as _time

        child = _sub.Popen(
            [_sys.executable, "-c", STRAY + "import time; time.sleep(300)\n", marker], stdout=_sub.DEVNULL, stderr=_sub.DEVNULL
        )
        deadline = _time.monotonic() + 15
        pid = None
        while _time.monotonic() < deadline:
            if _os.path.exists(marker):
                with open(marker, encoding="utf-8") as handle:
                    text = handle.read().strip()
                if text:
                    pid = int(text)
                    break
            _time.sleep(0.02)
        if pid is None:
            raise AssertionError("the stray never reported its pid")
        fd = _os.open(f"/proc/{_os.getpid()}/ns/mnt", _os.O_RDONLY)
        inode = _os.fstat(fd).st_ino
        result = sweep({inode}, keep_fds=[fd])
        _os.close(fd)
        # SIGKILL is asynchronous: the sweep reports the kill it *sent*, and the proof it landed is that the pid stops
        # existing.  Reaping matters too — inside this namespace the stray is our own child, so it would linger as a zombie forever.
        deadline = _time.monotonic() + 10
        while _time.monotonic() < deadline:
            try:
                _os.waitpid(pid, _os.WNOHANG)
            except ChildProcessError:
                pass
            try:
                _os.kill(pid, 0)
            except ProcessLookupError:
                break
            _time.sleep(0.05)
        else:
            raise AssertionError(f"sweep reported {result} but {pid} is alive")
        try:
            child.wait(timeout=5)
        except Exception:
            pass
        return _json.loads(_json.dumps({"pids": result.pids, "ns": result.namespaces, "inode": inode, "killed": pid}))

    report = in_mapped_child(probe)
    assert report["killed"] in report["pids"], report
    assert report["ns"] == [report["inode"]], "the sweep credited a namespace it did not hold"


def test_holding_no_fd_and_naming_no_namespace_kills_nothing():
    """An empty key is not "kill everything"; the empty namespace pair means that."""
    result = sweep(set(), keep_fds=[])
    assert result.empty, "the sweep guessed a scope it was never given"


def test_a_pid_in_another_namespace_is_left_alone(tmp_path):
    """The "never a host pid" claim, and the direction that matters: not ours, not touched."""
    stray = spawn_stray(str(tmp_path / "pid"), keep_alive=True)
    held = os.open("/dev/null", os.O_RDONLY)  # an fd, but not a namespace
    try:
        ours = {os.stat("/proc/self/ns/mnt").st_ino + 1_000_003}
        result = sweep(ours, keep_fds=[held])
        assert result.empty and alive(stray)
    finally:
        os.close(held)
        if alive(stray):
            os.kill(stray, signal.SIGKILL)


# ------------------- The run dir's files / the one report the dispose path owes


def test_the_console_line_and_run_json_come_from_the_same_sweep_result():
    """The line is built from the two fields and nothing else."""
    loud = SweepResult(pids=[4242, 4243], namespaces=[900_001])
    line = survivor_line(loud)
    assert "4242" in line and "900001" in line
    assert "namespace" in line, "the line names the *scope* that died, not just a count"
    quiet = survivor_line(SweepResult())
    assert "no survivors" in quiet


def test_the_sweep_set_is_every_held_namespace_plus_the_orchestrators_own():
    """A restarted member's earlier namespace stays in scope; ours is in it too."""
    fd = os.open("/proc/self/ns/mnt", os.O_RDONLY)
    own = os.fstat(fd).st_ino
    try:
        # `namespace_set` reads the namespace record and nothing else, so this member is bookkeeping
        # only: no child and no session, which is what the two casts say rather than what it pretends.
        member = Member(
            file_base="chair",
            id=None,
            pid=1,
            pgid=1,
            namespace=own + 7,
            ns_fd=fd,
            proc=cast(asyncio.subprocess.Process, None),
            rpc=cast(PiRpc, None),
            ns_records=[own + 7],
        )
        names = lifecycle.namespace_set([member], own_namespace=own)
        assert own in names
        assert own + 7 in names, "the sweep's set covers every namespace fd ever recorded"
    finally:
        os.close(fd)
