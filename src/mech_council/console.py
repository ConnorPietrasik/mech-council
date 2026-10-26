from __future__ import annotations

import json
import os
import sys
import time
from typing import BinaryIO


class Console:
    def __init__(self, *, run_dir: str | None = None, stream=None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.events_path = os.path.join(run_dir, "events.jsonl") if run_dir else ""
        self._events: list[dict] = []
        self.lines: list[str] = []
        self.started = time.monotonic()
        self._written = 0

    def say(self, text: str) -> None:
        self.lines.append(text)
        print(text, file=self.stream, flush=True)

    def note(self, text: str) -> None:
        self.say(f"note: {text}")

    def event(self, name: str, **fields) -> None:
        record = {"event": name, "at": round(time.monotonic() - self.started, 3), **fields}
        self._events.append(record)
        self.lines.append(f"[event] {name}")
        if self.events_path:
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def events(self) -> list[dict]:
        return list(self._events)

    def duration(self) -> float:
        return round(time.monotonic() - self.started, 1)

    def transcript_note(self) -> str:
        if self._written:
            return f"{self._written} bytes of raw protocol"
        return "stderr only, under `agents/`"

    def note_bytes(self, count: int) -> None:
        self._written += count


class Transcripts:
    """The two sinks per member, closed only once nothing can still write into them.  Append mode, so a restart continues the
    file rather than truncating the run's own record of it.
    """

    def __init__(self, run_dir: str, level: str) -> None:
        self.run_dir = run_dir
        self.level = level
        self.agents_dir = os.path.join(run_dir, "agents")
        self._raw: dict[str, BinaryIO] = {}
        self._err: dict[str, BinaryIO] = {}
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
        handle = self.manager._err.get(self.file_base)
        if handle is None:
            os.makedirs(self.manager.agents_dir, exist_ok=True)
            handle = open(self.manager.stderr_path(self.file_base), "ab")
            self.manager._err[self.file_base] = handle
        handle.write(chunk)
