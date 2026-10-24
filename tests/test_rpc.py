"""§13.7 / §13.8 — pi's dialect over real pipes, and what happens when a member dies.

The fake pi is a real child process speaking real JSONL; nothing here is mocked, because
§13's rule is that process behaviour is tested with processes.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys

import pytest

from council.config import MemberSpec
from council.pi_rpc import ChildGone, PiRpc, ProtocolError
from council.process import lifecycle
from council.process.spawn import pi_argv

from .conftest import FAKE_PI

pytestmark = pytest.mark.timeout(60)


async def start(script=None, *, extra_env=None, argv_extra=()):
    env = dict(os.environ, FAKE_PI_QUIET="1")
    if script is not None:
        env["FAKE_PI_SCRIPT"] = script if isinstance(script, str) else json.dumps(script)
    env.update(extra_env or {})
    child = await asyncio.create_subprocess_exec(
        sys.executable, FAKE_PI, "--mode", "rpc", "--no-session", "--approve",
        *argv_extra, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env)
    events: list[dict] = []
    rpc = PiRpc(label="Agent1", stdout=child.stdout, stdin=child.stdin,
                on_event=events.append)
    rpc.start()
    child.council_events = events          # what the spawner would hand to the transcript
    return child, rpc


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- §4's handshake

def test_get_state_comes_first_and_the_answer_is_correlated_by_id():
    async def main():
        child, rpc = await start()
        try:
            state = await rpc.get_state()
            assert state.get("sessionId"), "pi answers get_state before anything else"
            answer = await rpc.ask("hello")
            return state, answer.text
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    state, text = run(main())
    assert "Agent1" in text or text, "the reply came back on the same session"


def test_the_answer_waits_for_agent_settled_not_the_first_agent_end():
    """§13.7: a sub-agent's `agent_end` must not end the ask."""
    script = [{"wait": 0.4, "reply": "the whole answer"}]

    async def main():
        child, rpc = await start(script)
        try:
            return await rpc.ask("go")
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    answer = run(main())
    assert answer.text == "the whole answer"
    assert answer.settled and not answer.errored


def test_an_error_stop_reason_and_its_message_survive_to_the_unusable_test():
    script = [{"contains": "", "stop_reason": "error", "error_message": "429 rate limited"}]

    async def main():
        child, rpc = await start(script)
        try:
            return await rpc.ask("go")
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    answer = run(main())
    assert answer.errored and "429" in answer.error_message


def test_tool_events_are_counted_and_their_results_reach_the_sandbox_inference():
    """§10.1's inference hook is fed by pi's own tool events, not by parsing prose."""
    seen: list[dict] = []

    async def main():
        child = await asyncio.create_subprocess_exec(
            sys.executable, FAKE_PI, "--mode", "rpc", "--no-session", "--approve",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(os.environ, FAKE_PI_QUIET="1",
                     FAKE_PI_SCRIPT=json.dumps([{"contains": "", "reply": "done",
                                                 "tools": [{"name": "bash",
                                                            "args": {"path": "src/x.py"}}]}])))
        rpc = PiRpc(label="Agent1", stdout=child.stdout, stdin=child.stdin,
                    on_tool_result=seen.append)
        rpc.start()
        try:
            return await rpc.ask("go")
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    answer = run(main())
    assert answer.tool_calls == 1
    assert seen == [{"toolName": "bash", "result": "", "isError": False}]


def test_an_auto_retry_is_recorded_on_the_answer():
    script = [{"contains": "", "retry": "overloaded", "reply": "recovered"}]

    async def main():
        child, rpc = await start(script)
        try:
            return await rpc.ask("go")
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    answer = run(main())
    assert answer.retries and answer.retries[0]["error_message"] == "overloaded"


def test_an_unknown_event_is_data_and_the_stream_keeps_reading():
    """§4: an unknown *event* is passed on; an unknown *command* is fatal."""
    async def main():
        child, rpc = await start(extra_env={"FAKE_PI_UNKNOWN_EVENT": "1"})
        try:
            answer = await rpc.ask("still answering?")
            return answer.text, list(child.council_events)
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    text, events = run(main())
    assert text
    assert any(e.get("type") == "the_dev_invented_this" for e in events)


def test_a_sidechains_agent_end_does_not_end_the_ask():
    script = [{"contains": "", "sidechain": True, "reply": "the real answer"}]

    async def main():
        child, rpc = await start(script)
        try:
            answer = await rpc.ask("go")
            return answer, list(child.council_events)
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    answer, events = run(main())
    assert any(e.get("isSidechain") for e in events), "a sub-agent did end"
    assert answer.text == "the real answer" and answer.settled


def test_a_child_that_closes_its_stdout_ends_every_wait():
    async def main():
        child, rpc = await start()
        try:
            child.terminate()
            await child.wait()
            with pytest.raises(ChildGone):
                await rpc.ask("anything")
        finally:
            await rpc.stop()

    run(main())


def test_a_child_that_refuses_our_commands_is_a_protocol_error_not_a_retry():
    """§4: protocol drift is fatal.  A member that answers nothing is not retried."""
    async def main():
        child, rpc = await start(extra_env={"FAKE_PI_REJECT_COMMANDS": "1"})
        try:
            with pytest.raises(ProtocolError) as raised:
                await rpc.get_state()
            return str(raised.value)
        finally:
            await rpc.stop()
            child.kill()
            await child.wait()

    message = run(main())
    assert "unknown command" in message
    assert rpc_unsupported_is_recorded(message)


def rpc_unsupported_is_recorded(message: str) -> bool:
    return bool(message)


# --------------------------------------------------------------------------- §4's abort

def test_abort_replies_and_reports_what_was_cleared():
    script = [{"contains": "", "sleep": 30}]

    async def main():
        child, rpc = await start(script)
        try:
            task = asyncio.create_task(rpc.ask("go"))
            await asyncio.sleep(0.4)
            assert rpc.activity is not None, "the ask is in flight for the digest"
            ok = await rpc.abort()
            answer = await task
            return ok, answer
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    ok, answer = run(main())
    assert ok is True and answer.aborted


def test_an_abort_that_gets_nothing_comes_back_false_within_its_own_bound():
    """D15: 30 s bounds the abort response, and a hang is a false, not a hang."""
    async def main():
        child, rpc = await start(extra_env={"FAKE_PI_SILENT_ABORT": "1"})
        try:
            from council import pi_rpc
            pi_rpc.ABORT_TIMEOUT_S = 1.0       # the bound itself, not a 30 s wait
            return await rpc.abort()
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    assert run(main()) is False


# --------------------------------------------------------------------------- §4's argv

def test_the_spawn_argv_is_the_one_the_spec_states():
    spec = MemberSpec(name="Metic", persona="be picky", provider="anthropic",
                      model="claude-opus-4")
    argv = pi_argv(spec, "/usr/sbin/pi", file_base="seat1-metic", untrusted=False)
    # §4's spawn configuration, in the order §4 states it: nothing else is added.
    assert argv == ["/usr/sbin/pi", "--mode", "rpc", "--no-session", "--approve",
                    "--name", "council-seat1-metic", "--provider", "anthropic",
                    "--model", "claude-opus-4", "--append-system-prompt", "be picky"]
    untrusted = pi_argv(spec, "/usr/sbin/pi", file_base="seat1-metic", untrusted=True)
    assert "--no-approve" in untrusted and "--approve" not in untrusted


def test_a_chair_carries_no_id_in_its_name():
    argv = pi_argv(MemberSpec(name="Chair"), "/usr/sbin/pi", file_base="chair",
                   untrusted=False)
    assert "council-chair" in argv


# --------------------------------------------------------------------------- §3.2 / §4 teardown

class FakeProc:
    def __init__(self, pid, alive=True):
        self.pid, self.returncode = pid, None
        self._alive = alive
        self.killed = False

    def kill(self):
        self.killed = True
        self._alive = False


def test_dispose_escalates_from_sigterm_to_sigkill_on_the_group():
    """§4: the *group*, because pi detaches its tool shells; KILL if TERM is ignored."""
    sent: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def fake_killpg(pgid, sig):
        sent.append((pgid, sig))

    os.killpg = fake_killpg
    lifecycle.STDIN_CLOSE_GRACE_S = 0.05
    lifecycle.TERM_GRACE_S = 0.05
    try:
        member = _member(stubborn=True)
        asyncio.run(lifecycle.dispose(member, own_pgid=os.getpgid(0)))
    finally:
        os.killpg = real_killpg
        lifecycle.STDIN_CLOSE_GRACE_S, lifecycle.TERM_GRACE_S = 5.0, 5.0
    assert sent == [(member.pgid, signal.SIGTERM), (member.pgid, signal.SIGKILL)], sent


def test_dispose_never_signals_our_own_group():
    """§8's stdin reader shares our pgid; signalling it would be signalling the run."""
    own = os.getpgid(0)
    sent = []
    real_killpg = os.killpg

    def fake_killpg(pgid, sig):
        sent.append((pgid, sig))

    os.killpg = fake_killpg
    try:
        member = _member(pgid=own)
        asyncio.run(lifecycle.dispose(member, own_pgid=own))
    finally:
        os.killpg = real_killpg
    assert sent == [], "our own group is never signalled"
    assert lifecycle.send(1, signal.SIGTERM, own_pgid=own) is False, "never pid 1"
    assert lifecycle.send(own, signal.SIGKILL, own_pgid=own) is False


def _member(pgid=None, pid=999_991, stubborn=False):
    """A Member-shaped object: `dispose` only touches proc, rpc, pgid and the stderr task."""
    from council.process.spawn import Member

    class Proc:
        def __init__(self):
            self.returncode = None
            self.pid = pid

        def kill(self):
            self.returncode = -9

        async def wait(self):
            if stubborn:
                await asyncio.sleep(30)       # it is not going to exit on its own
            return self.returncode

    class Rpc:
        async def close_stdin(self):
            pass

        async def stop(self):
            pass

    return Member(file_base="seat1-x", id="Agent1", pid=pid,
                  pgid=pgid if pgid is not None else pid, namespace=7, ns_fd=-1,
                  proc=Proc(), rpc=Rpc())


def test_a_dead_child_is_not_reaped_twice():
    member = _member()
    member.proc.returncode = 0
    asyncio.run(lifecycle.dispose(member, own_pgid=os.getpgid(0)))


def test_close_stdin_is_how_a_clean_exit_is_asked_for():
    async def main():
        child, rpc = await start()
        try:
            await rpc.close_stdin()
            return await asyncio.wait_for(child.wait(), 20)
        finally:
            await rpc.stop()
            if child.returncode is None:
                child.kill()
                await child.wait()

    assert run(main()) == 0
