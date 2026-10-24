"""§11.1 — the `[council]` line and §3.4's `events.jsonl`, from one emitter.

Every event name is snake_case (§3.3's naming rule) and the file is append-only, so a
consumer never breaks when a new event appears.  Console line and event record are
emitted together: a fact never reaches the run directory without being said, or the
reverse.
"""
from __future__ import annotations

import json
import os
import sys
import time


class Console:
    """The run's voice.  Pure output: no state the deliberation could depend on."""

    def __init__(self, *, run_dir: str | None = None, stream=None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.events_path = os.path.join(run_dir, "events.jsonl") if run_dir else ""
        self._events: list[dict] = []
        self.lines: list[str] = []
        self.started = time.monotonic()
        self._written = 0

    def say(self, text: str) -> None:
        line = f"[council] {text}"
        self.lines.append(line)
        print(line, file=self.stream, flush=True)

    def note(self, text: str) -> None:
        """A plan §3.1 override note or any other non-event chatter, still `[council]`-tagged."""
        self.say(f"note: {text}")

    def event(self, name: str, **fields) -> None:
        record = {"event": name, "at": round(time.monotonic() - self.started, 3), **fields}
        self._events.append(record)
        self.lines.append(f"[event] {name}")
        if self.events_path:
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def events(self) -> list[dict]:
        """The records this console emitted, in order (§11.1's log, read back in tests)."""
        return list(self._events)

    def duration(self) -> float:
        return round(time.monotonic() - self.started, 1)

    def transcript_note(self) -> str:
        """§11.2's honest note: with `off` there is nothing to point at but stderr."""
        if self._written:
            return f"{self._written} bytes of raw protocol"
        return "stderr only, under `agents/`"

    def note_bytes(self, count: int) -> None:
        self._written += count


class Transcripts:
    """§11.2's two levels and §3.4's sink rule, including the lazy `agents/` directory."""

    def __init__(self, run_dir: str, level: str) -> None:
        self.run_dir = run_dir
        self.level = level
        self.agents_dir = os.path.join(run_dir, "agents")
        self._raw: dict[str, object] = {}
        self._err: dict[str, object] = {}
        self.bytes_written = 0

    def transcript_path(self, file_base: str) -> str:
        return os.path.join(self.agents_dir, f"{file_base}.transcripts.jsonl")

    def stderr_path(self, file_base: str) -> str:
        return os.path.join(self.agents_dir, f"{file_base}.stderr.txt")

    def append_raw(self, chunk: bytes, *, file_base: str | None = None) -> None:
        raise NotImplementedError

    def tally(self) -> str:
        if self.level != "full":
            return "none written" if not self.bytes_written else f"{self.bytes_written} bytes"
        return f"{self.bytes_written} bytes"

    def close(self) -> None:
        for handle in list(self._raw.values()) + list(self._err.values()):
            try:
                handle.close()
            except OSError:
                pass
        self._raw.clear()
        self._err.clear()


class MemberSinks:
    """One member's two sinks: the verbatim stdout stream and its stderr file (§3.4/D7)."""

    def __init__(self, manager: Transcripts, file_base: str) -> None:
        self.manager = manager
        self.file_base = file_base

    def append_raw(self, chunk: bytes) -> None:
        if self.manager.level != "full":
            return
        handle = self.manager._raw.get(self.file_base)
        if handle is None:
            os.makedirs(self.manager.agents_dir, exist_ok=True)
            handle = open(self.manager.transcript_path(self.file_base), "ab")
            self.manager._raw[self.file_base] = handle
        handle.write(chunk)
        self.manager.bytes_written += len(chunk)

    def append_stderr(self, chunk: bytes) -> None:
        """`agents/` is created **lazily**: the default configuration has a defined sink."""
        handle = self.manager._err.get(self.file_base)
        if handle is None:
            os.makedirs(self.manager.agents_dir, exist_ok=True)
            handle = open(self.manager.stderr_path(self.file_base), "ab")
            self.manager._err[self.file_base] = handle
        handle.write(chunk)
