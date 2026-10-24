"""§4 / §12.1.7 — the one boundary where pi's dialect is spoken.

Framing, id correlation and the command set live here and nowhere else, so a protocol
change is a one-file fix.  Nothing in this module matches sandbox text (§10's inference
is handed to an injected callback) and nothing renames pi's names into ours: this is the
only rename site in the design.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Callable

#: §4's command set, and only these.
COMMANDS = ("prompt", "abort", "clear_queue", "get_state")

ABORT_TIMEOUT_S = 30          # plan D15: the abort response's own bound (§4)


class ProtocolError(Exception):
    """A child that does not speak the protocol we send.  Hard error, per §3.2."""


class ChildGone(ProtocolError):
    """The child's stdout closed: nothing more will be said."""


@dataclass
class Activity:
    """What the watchdog needs about an in-flight ask (§9's digest inputs)."""

    started: float
    last_event: float
    events: int = 0
    last_tool: str = ""
    last_event_name: str = ""
    last_tool_args: dict = field(default_factory=dict)

    def age(self, now: float | None = None) -> float:
        return (now or time.monotonic()) - self.last_event


@dataclass
class Answer:
    """One ask's outcome: text, and the two fields §3.3's unusable-seat test reads."""

    text: str = ""
    stop_reason: str = ""
    error_message: str = ""
    aborted: bool = False
    settled: bool = False
    disposition: str = ""
    retries: list[dict] = field(default_factory=list)
    tool_calls: int = 0

    @property
    def errored(self) -> bool:
        return self.stop_reason == "error"


class PiRpc:
    """One long-lived `pi --mode rpc` child, read continuously (§4's backpressure rule)."""

    def __init__(self, *, label: str, stdout: asyncio.StreamReader,
                 stdin: asyncio.StreamWriter,
                 on_event: Callable[[dict], None] | None = None,
                 on_tool_result: Callable[[dict], None] | None = None,
                 transcript=None) -> None:
        self.label = label
        self._out = stdout
        self._in = stdin
        self._on_event = on_event
        self._on_tool_result = on_tool_result
        self._transcript = transcript
        self._seq = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._ask: asyncio.Future | None = None
        self._answer: Answer | None = None
        self.activity: Activity | None = None
        self.alive = True
        self.unknown_command: str | None = None
        self._last_assistant: dict | None = None

    # -- plumbing ------------------------------------------------------------

    def start(self) -> None:
        self._reader_task = asyncio.create_task(self._pump(), name=f"rpc-{self.label}")

    async def stop(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task

    async def _pump(self) -> None:
        """Raw chunk reads split on `b"\\n"` only (§4, plan D22).

        `str.splitlines()` would break on U+0085/U+2028/U+2029 — all legal inside a JSON
        string — and asyncio's line limit would raise a plain `ValueError` and clear the
        buffer on a multi-megabyte record.  There is no line-size cap here at all.
        """
        buffer = b""
        try:
            while True:
                chunk = await self._out.read(65536)
                if not chunk:
                    break
                if self._transcript is not None:
                    self._transcript.append_raw(chunk)
                buffer += chunk
                while True:
                    index = buffer.find(b"\n")
                    if index < 0:
                        break
                    line, buffer = buffer[:index], buffer[index + 1:]
                    self._dispatch(line)
        except (asyncio.CancelledError, ChildGone):
            raise
        except Exception:                      # a broken pipe is a dead child, not a crash
            pass
        finally:
            self.alive = False
            self._fail_pending(ChildGone(f"{self.label}: child stdout closed"))

    def _dispatch(self, line: bytes) -> None:
        line = line.rstrip(b"\r")
        if not line.strip():
            return
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return                             # stderr-shaped noise never reaches us anyway
        if not isinstance(record, dict):
            return
        self._note_activity(record)
        if record.get("type") == "response":
            self._resolve(record)
            return
        if record.get("type") == "extension_ui_request":
            self._cancel_dialog(record)
            return
        self._on_session_event(record)
        if self._on_event is not None:
            self._on_event(record)

    def _note_activity(self, record: dict) -> None:
        if self.activity is None:
            return
        self.activity.events += 1
        self.activity.last_event = time.monotonic()
        self.activity.last_event_name = str(record.get("type") or "")
        if record.get("type") == "tool_execution_start":
            self.activity.last_tool = str(record.get("toolName") or "")
            args = record.get("args")
            self.activity.last_tool_args = args if isinstance(args, dict) else {}

    def _on_session_event(self, record: dict) -> None:
        kind = record.get("type")
        if kind == "message_end":
            message = record.get("message") or {}
            if message.get("role") == "assistant":
                self._last_assistant = message
                if self._answer is not None:
                    self._answer.stop_reason = str(message.get("stopReason") or "")
                    self._answer.error_message = str(message.get("errorMessage") or "")
                    self._answer.text = _text_of(message)
        elif kind == "tool_execution_end":
            if self._answer is not None:
                self._answer.tool_calls += 1
            if self._on_tool_result is not None:
                self._on_tool_result({"toolName": record.get("toolName"),
                                      "result": record.get("result"),
                                      "isError": bool(record.get("isError"))})
        elif kind == "auto_retry_start":
            if self._answer is not None:
                self._answer.retries.append({"attempt": record.get("attempt"),
                                             "max_attempts": record.get("maxAttempts"),
                                             "error_message": record.get("errorMessage")})
        elif kind == "agent_settled":
            future, self._ask = self._ask, None
            answer, self._answer = self._answer, None
            if answer is None:
                return
            answer.settled = True
            answer.aborted = bool(record.get("aborted"))
            if not answer.stop_reason and self._last_assistant is not None:
                answer.stop_reason = str(self._last_assistant.get("stopReason") or "")
                answer.error_message = str(self._last_assistant.get("errorMessage") or "")
            if future is not None and not future.done():
                future.set_result(answer)

    def _resolve(self, record: dict) -> None:
        request_id = record.get("id")
        future = self._pending.pop(request_id, None) if request_id else None
        if future is None:
            return
        if not record.get("success", False):
            error = str(record.get("error") or "")
            if _names_unknown_command(error):
                self.unknown_command = error
                future.set_exception(ProtocolError(f"{self.label}: pi refused a command "
                                                   f"this tool sends: {error}"))
                return
            future.set_exception(ProtocolError(error or "command failed"))
            return
        future.set_result(record.get("data") or {})

    def _fail_pending(self, error: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
                # A caller that already timed out or was aborted has no awaiter left; an
                # unretrieved exception on such a future is printed by asyncio when the
                # loop closes, which is noise that reads like a hidden bug.  Marking it
                # seen changes nothing for a future someone *is* awaiting: it still raises.
                future.exception()
        self._pending.clear()
        if self._ask is not None and not self._ask.done():
            self._ask.set_exception(error)
            # The waiting `ask()` learns of the death from its own command future, so this
            # one is left unawaited by design.  An exception nobody retrieves is reported
            # by asyncio when the loop closes — noise that reads like a swallowed bug, so
            # it is marked seen here while still carrying the error for any other awaiter.
            self._ask.exception()
        self._ask = None

    def _cancel_dialog(self, record: dict) -> None:
        """A project extension's dialog must never wedge the run: cancel it and move on."""
        method = record.get("method")
        if method in ("notify", "setStatus", "setWidget", "setTitle", "set_editor_text"):
            return
        self._send({"type": "extension_ui_response", "id": record.get("id"),
                    "cancelled": True})

    def _send(self, payload: dict) -> None:
        try:
            self._in.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        except Exception as exc:                        # a dead child, said plainly
            raise ChildGone(f"{self.label}: {exc}") from exc

    async def _request(self, command: dict, timeout: float | None = None) -> dict:
        if not self.alive:
            raise ChildGone(f"{self.label}: child is gone")
        self._seq += 1
        request_id = f"{self.label}-{self._seq}"
        payload = {"id": request_id, **command}
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future
        self._send(payload)
        try:
            if timeout is None:
                return await future
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise TimeoutError(f"{self.label}: {command.get('type')} timed out "
                               f"after {timeout}s") from exc
        finally:
            self._pending.pop(request_id, None)

    # -- the commands (§4) -----------------------------------------------------

    async def get_state(self) -> dict:
        """Every child's first RPC call (§3.3 point 2): it proves the child speaks JSONL."""
        return await self._request({"type": "get_state"}, timeout=60)

    async def ask(self, message: str, *, timeout: float | None = None) -> Answer:
        """Prompt and wait for `agent_settled` — never merely `agent_end` (§4).

        The read pump is already subscribed from `start()`, so a fast completion cannot be
        missed: that is how "subscribe before prompt" holds here.  It also means a quick
        member can settle *while this coroutine is still parked on the prompt's own "ok*"
        response — one event-loop turn serves both — so the answer is held in a local: the
        settle handler owns `self._answer`, and this one must not reach for it afterwards.
        """
        if self._ask is not None:
            raise ProtocolError(f"{self.label}: asked while an ask is in flight")
        loop = asyncio.get_running_loop()
        answer = Answer()
        settled: asyncio.Future = loop.create_future()
        self._answer = answer
        self._ask = settled
        self.activity = Activity(started=time.monotonic(), last_event=time.monotonic())
        data = await self._request({"type": "prompt", "message": message}, timeout=60)
        disposition = str(data.get("disposition") or "")
        answer.disposition = disposition
        if disposition == "handled":
            # An extension consumed the prompt: no run starts, so nothing to wait for.
            self._release(answer, settled)
            raise ProtocolError(f"{self.label}: pi handled the prompt itself "
                                "(disposition: handled) — no run started")
        if disposition == "queued":
            self._release(answer, settled)
            raise ProtocolError(f"{self.label}: pi queued the prompt behind an in-flight "
                                "run (disposition: queued) — the settled event would "
                                "belong to the previous run")
        try:
            if timeout is None:
                return await settled
            async with asyncio.timeout(timeout):
                return await settled
        except TimeoutError:
            self._release(answer, settled)
            raise

    def _release(self, answer: Answer, settled: asyncio.Future) -> None:
        """Drop the in-flight ask, but only if it is still this one."""
        if self._answer is answer:
            self._answer = None
        if self._ask is settled:
            self._ask = None

    async def abort(self) -> bool:
        """`clear_queue` first, then `abort`; the response *is* the synchronization point.

        Waiting additionally for `agent_settled` risks burning the grace period and then
        tree-killing a child that had stopped cleanly (§4) — and the abort request needs
        **its own** bounded timeout, because an abort to a wedged child never returns.
        """
        ok = True
        try:
            async with asyncio.timeout(ABORT_TIMEOUT_S):
                with contextlib.suppress(Exception):
                    await self._request({"type": "clear_queue"})
                await self._request({"type": "abort"})
        except (TimeoutError, Exception):
            ok = False
        return ok

    async def close_stdin(self) -> None:
        """§4: an orderly shutdown starts here — pi disposes its runtime, and that dispose
        aborts every in-flight tool call."""
        with contextlib.suppress(Exception):
            if self._in.can_write_eof():
                self._in.write_eof()
            self._in.close()


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "thinking"):
                parts.append(str(block.get("text") or block.get("thinking") or ""))
        return "\n".join(p for p in parts if p)
    return ""


def _names_unknown_command(error: str) -> bool:
    lowered = error.casefold()
    return "unknown command" in lowered or "unknown request" in lowered \
        or "unsupported command" in lowered
