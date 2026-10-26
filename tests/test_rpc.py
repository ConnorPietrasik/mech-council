"""pi's dialect over real pipes, and what happens when a member dies."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from typing import cast

import pytest

from council.config import MemberSpec
from council.pi_rpc import ChildGone, PiRpc, ProtocolError
from council.process import lifecycle
from council.process.spawn import pi_argv
from council.sandbox import infer

from .conftest import FAKE_PI

pytestmark = pytest.mark.timeout(60)


async def start(script=None, *, extra_env=None, argv_extra=()):
    env = dict(os.environ, FAKE_PI_QUIET="1")
    if script is not None:
        env["FAKE_PI_SCRIPT"] = script if isinstance(script, str) else json.dumps(script)
    env.update(extra_env or {})
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        FAKE_PI,
        "--mode",
        "rpc",
        "--no-session",
        "--approve",
        *argv_extra,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    events: list[dict] = []
    assert child.stdout is not None and child.stdin is not None, "both halves were PIPEd above"
    rpc = PiRpc(label="Agent1", stdout=child.stdout, stdin=child.stdin, on_event=events.append)
    rpc.start()
    return child, rpc, events  # the list is the transcript sink the spawner would hand over


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------- The handshake


def test_get_state_comes_first_and_the_answer_is_correlated_by_id():
    async def main():
        child, rpc, _events = await start()
        try:
            state = await rpc.get_state()
            assert state.get("sessionId"), "pi answers get_state before anything else"
            answer = await rpc.ask("hello")
            return state, answer.text
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    _state, text = run(main())
    assert json.loads(text) == {"positions": [], "new_items": [], "answers": [], "questions": [], "human_input": [], "signoff": True}, (
        "the answer is this prompt's scripted reply, not just any text that came back"
    )


def test_an_error_stop_reason_and_its_message_survive_to_the_unusable_test():
    script = [{"contains": "", "stop_reason": "error", "error_message": "429 rate limited"}]

    async def main():
        child, rpc, _events = await start(script)
        try:
            return await rpc.ask("go")
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    answer = run(main())
    assert answer.errored and "429" in answer.error_message


def test_tool_events_are_counted_and_their_results_reach_the_sandbox_inference():
    """The sandbox inference hook is fed by pi's own tool events, not by parsing prose."""
    seen: list[dict] = []

    async def main():
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            FAKE_PI,
            "--mode",
            "rpc",
            "--no-session",
            "--approve",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(
                os.environ,
                FAKE_PI_QUIET="1",
                FAKE_PI_SCRIPT=json.dumps([{"contains": "", "reply": "done", "tools": [{"name": "bash", "args": {"path": "src/x.py"}}]}]),
            ),
        )
        assert child.stdout is not None and child.stdin is not None, "both halves were PIPEd above"
        rpc = PiRpc(label="Agent1", stdout=child.stdout, stdin=child.stdin, on_tool_result=seen.append)
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


def test_the_inference_pass_reads_erroring_tool_results_and_nothing_else():
    """`sandbox_full` / `sandbox_limit_hit` come from an *erroring* tool result, from its tail, and never from a successful one."""

    def record(text: str, *, error: bool = True) -> dict:
        return {"toolName": "bash", "result": text, "isError": error}

    assert infer.scan(record("cp: write failed: no space left on device")) == infer.FULL
    assert infer.scan(record("rename 'a': Invalid cross-device link")) == infer.LIMIT
    assert infer.scan(record("rm -rf src: Input/output error")) == infer.LIMIT
    # A *successful* result that merely mentions the errno proves nothing.
    assert infer.scan(record("no space left on device", error=False)) is None
    # Only the tail is scanned, so an old recitation is not a spent cap.
    assert infer.scan(record("no space left on device" + " " * 4096)) is None
    assert infer.scan({}) is None
    # `EROFS` — writing into a mask the seat was briefed about — is the one errno the report marks and the pump does not infer.
    assert infer.scan(record("rm: cannot remove 'x': Read-only file system")) is None
    assert infer.evidence_is_limit("Read-only file system")


def test_the_pump_hands_the_inference_a_record_it_can_act_on():
    """The end of the wiring: what the pump delivers is what the pass reads."""
    seen: list[dict] = []

    async def main():
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            FAKE_PI,
            "--mode",
            "rpc",
            "--no-session",
            "--approve",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(
                os.environ,
                FAKE_PI_QUIET="1",
                FAKE_PI_SCRIPT=json.dumps(
                    [
                        {
                            "contains": "",
                            "reply": "could not write",
                            "tools": [{"name": "bash", "result": "dd: error writing: No space left on device", "is_error": True}],
                        }
                    ]
                ),
            ),
        )
        assert child.stdout is not None and child.stdin is not None, "both halves were PIPEd above"
        rpc = PiRpc(label="Agent1", stdout=child.stdout, stdin=child.stdin, on_tool_result=seen.append)
        rpc.start()
        try:
            return await rpc.ask("go")
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    run(main())
    assert [infer.scan(record) for record in seen] == [infer.FULL], "the pump's record is the shape the pass is written against"


def test_an_auto_retry_is_recorded_on_the_answer():
    script = [{"contains": "", "retry": "overloaded", "reply": "recovered"}]

    async def main():
        child, rpc, _events = await start(script)
        try:
            return await rpc.ask("go")
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    answer = run(main())
    assert answer.retries and answer.retries[0]["error_message"] == "overloaded"


def test_an_unknown_event_is_data_and_the_stream_keeps_reading():
    """An unknown *event* is passed on; an unknown *command* is fatal."""

    async def main():
        child, rpc, events = await start(extra_env={"FAKE_PI_UNKNOWN_EVENT": "1"})
        try:
            answer = await rpc.ask("still answering?")
            return answer.text, list(events)
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
        child, rpc, events = await start(script)
        try:
            answer = await rpc.ask("go")
            return answer, list(events)
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    answer, events = run(main())
    assert any(e.get("isSidechain") for e in events), "a sub-agent did end"
    assert answer.text == "the real answer" and answer.settled and not answer.errored


def test_a_child_that_closes_its_stdout_ends_every_wait():
    async def main():
        child, rpc, _events = await start()
        try:
            child.terminate()
            await child.wait()
            with pytest.raises(ChildGone):
                await rpc.ask("anything")
        finally:
            await rpc.stop()

    run(main())


def test_a_child_that_refuses_our_commands_is_a_protocol_error_not_a_retry():
    """Protocol drift is fatal.  A member that answers nothing is not retried."""

    async def main():
        child, rpc, _events = await start(extra_env={"FAKE_PI_REJECT_COMMANDS": "1"})
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


# --------------------------------------------------------- The abort


def test_abort_replies_and_reports_what_was_cleared():
    script = [{"contains": "", "sleep": 30}]

    async def main():
        child, rpc, _events = await start(script)
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
    """The constants-not-knobs choice: 30 s bounds the abort response, and a hang is a false, not a hang."""

    async def main():
        child, rpc, _events = await start(extra_env={"FAKE_PI_SILENT_ABORT": "1"})
        try:
            from council import pi_rpc

            pi_rpc.ABORT_TIMEOUT_S = 1.0  # the bound itself, not a 30 s wait
            return await rpc.abort()
        finally:
            await rpc.stop()
            child.terminate()
            await child.wait()

    assert run(main()) is False


# ---------------------------------------------------------- pi's argv


def test_the_spawn_argv_is_pi_plus_exactly_the_flags_we_add():
    spec = MemberSpec(name="Metic", persona="be picky", provider="anthropic", model="claude-opus-4")
    argv = pi_argv(spec, "/usr/sbin/pi", file_base="seat1-metic", untrusted=False)
    # pi's spawn configuration, in the order `pi_argv` builds it: nothing else is added.
    assert argv == [
        "/usr/sbin/pi",
        "--mode",
        "rpc",
        "--no-session",
        "--approve",
        "--name",
        "council-seat1-metic",
        "--provider",
        "anthropic",
        "--model",
        "claude-opus-4",
        "--append-system-prompt",
        "be picky",
    ]
    untrusted = pi_argv(spec, "/usr/sbin/pi", file_base="seat1-metic", untrusted=True)
    assert "--no-approve" in untrusted and "--approve" not in untrusted


def test_a_chair_carries_no_id_in_its_name():
    argv = pi_argv(MemberSpec(name="Chair"), "/usr/sbin/pi", file_base="chair", untrusted=False)
    assert "council-chair" in argv


# ------------------------------- The dispose path on teardown


def test_dispose_escalates_from_sigterm_to_sigkill_on_the_group():
    """The *group*, because pi detaches its tool shells; KILL if TERM is ignored."""
    sent: list[tuple[int, int]] = []
    settled: list[int | None] = []
    member = _member(stubborn=True, settled=settled)
    real_killpg = os.killpg

    def fake_killpg(pgid, sig):
        sent.append((pgid, sig))
        if sig == signal.SIGKILL:
            member.proc.returncode = -9  # the KILL lands: the child reports its exit

    os.killpg = fake_killpg
    lifecycle.STDIN_CLOSE_GRACE_S = 0.05
    lifecycle.TERM_GRACE_S = 0.05
    try:
        asyncio.run(lifecycle.dispose(member, own_pgid=os.getpgid(0)))
    finally:
        os.killpg = real_killpg
        lifecycle.STDIN_CLOSE_GRACE_S, lifecycle.TERM_GRACE_S = 5.0, 5.0
    assert sent == [(member.pgid, signal.SIGTERM), (member.pgid, signal.SIGKILL)], sent
    assert settled == [-9], "the exit the KILL caused is awaited, not just the KILL sent"


def _member(pgid=None, pid=999_991, stubborn=False, settled=None):
    """A doubled child and session: `dispose` is the subject, and neither fake carries more than it inspects."""
    from council.process.spawn import Member

    class Proc:
        def __init__(self):
            self.returncode = None
            self.pid = pid

        async def wait(self):
            if not stubborn:
                return self.returncode
            # Stubborn: nothing is reported until the caller's group KILL sets `returncode`.  The bound outlasts
            # `dispose`'s TERM grace, so a `dispose` that never escalates fails an assertion instead of the timeout.
            loop = asyncio.get_running_loop()
            bound = loop.time() + lifecycle.TERM_GRACE_S * 4
            while self.returncode is None and loop.time() < bound:
                await asyncio.sleep(0.01)
            if self.returncode is not None and settled is not None:
                settled.append(self.returncode)
            return self.returncode

    class Rpc:
        async def close_stdin(self):
            pass

        async def stop(self):
            pass

    return Member(
        file_base="seat1-x",
        id="Agent1",
        pid=pid,
        pgid=pgid if pgid is not None else pid,
        namespace=7,
        ns_fd=-1,
        proc=cast(asyncio.subprocess.Process, Proc()),
        rpc=cast(PiRpc, Rpc()),
    )


def test_a_child_that_already_exited_is_never_signalled_or_awaited():
    member = _member()
    member.proc.returncode = 0
    waited: list[int] = []

    async def wait():
        waited.append(1)
        return member.proc.returncode

    member.proc.wait = wait
    sent: list[tuple[int, int]] = []
    real_killpg = os.killpg
    os.killpg = lambda pgid, sig: sent.append((pgid, sig))
    try:
        asyncio.run(lifecycle.dispose(member, own_pgid=os.getpgid(0)))
    finally:
        os.killpg = real_killpg
    assert sent == [], "a process that reported its exit code is not signalled"
    assert waited == [], "nothing waits on it a second time"


def test_close_stdin_is_how_a_clean_exit_is_asked_for():
    async def main():
        child, rpc, _events = await start()
        try:
            await rpc.close_stdin()
            return await asyncio.wait_for(child.wait(), 20)
        finally:
            await rpc.stop()
            if child.returncode is None:
                child.kill()
                await child.wait()

    assert run(main()) == 0


# ------------------------------------------------- The reader loop's framing: bytes, not lines


class _NullStdin:
    """PiRpc writes commands here.  Nothing in the framing tests writes one."""

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


def _framed(payload: bytes, *, chunk: int) -> list[dict]:
    """One PiRpc over a hand-fed StreamReader, with `payload` arriving in `chunk`-sized pieces."""
    events: list[dict] = []

    async def main():
        reader = asyncio.StreamReader()
        rpc = PiRpc(label="Agent1", stdout=reader, stdin=cast(asyncio.StreamWriter, _NullStdin()), on_event=events.append)
        rpc.start()
        for start in range(0, len(payload), chunk):
            reader.feed_data(payload[start : start + chunk])
        reader.feed_eof()
        for _ in range(50):
            await asyncio.sleep(0)  # let the reader task drain everything that was fed
        await rpc.stop()

    run(main())
    return events


def test_the_reader_splits_on_a_newline_byte_never_on_what_splitlines_calls_a_line():
    """U+0085/U+2028/U+2029 are legal inside a JSON string: `str.splitlines()` would cut one record in two."""
    text = "a\u0085b\u2028c\u2029d and a literal \\n too"
    # ensure_ascii=False, so the separators reach the pipe as raw bytes rather than `\u2028` escapes.
    payload = json.dumps({"type": "council_probe", "text": text}, ensure_ascii=False).encode("utf-8") + b"\n"
    events = _framed(payload, chunk=7)
    assert events == [{"type": "council_probe", "text": text}], "one record, whole, every exotic separator intact"


def test_a_record_larger_than_asyncios_own_line_limit_still_arrives_whole():
    """`StreamReader.readline` raises at 64 KiB and clears the buffer; raw chunk reads carry no cap."""
    text = "x" * (300 * 1024)
    payload = json.dumps({"type": "council_probe", "text": text}, ensure_ascii=False).encode("utf-8") + b"\n"
    events = _framed(payload, chunk=64 * 1024)
    assert len(events) == 1 and events[0]["text"] == text, "a multi-megabyte answer must not be dropped"
