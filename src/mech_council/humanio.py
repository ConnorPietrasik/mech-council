"""Stdin never blocks the loop and is never joined on the way out."""

from __future__ import annotations

import queue
import sys
import threading

EOF = object()


class LineReader:
    def __init__(self, stream=None) -> None:
        self.stream = stream if stream is not None else sys.stdin
        self.lines: queue.Queue = queue.Queue()
        self.eof = False
        self._thread: threading.Thread | None = None
        try:
            self.tty = bool(self.stream.isatty())
        except AttributeError, ValueError:
            self.tty = False

    @property
    def interactive(self) -> bool:
        """Whether anyone is at the keyboard: `tty` is what the stream was when it opened, so closing it ends the listening."""
        return self.tty and not self.eof

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._pump, name="stdin-reader", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        while True:
            try:
                line = self.stream.readline()
            except Exception:
                line = ""
            if not line:
                self.eof = True
                self.lines.put(EOF)
                return
            self.lines.put(line.rstrip("\n"))

    def read_nowait(self):
        """A line if one arrived, None if nothing has, EOF once stdin closed — queued lines outlive the close."""
        try:
            return self.lines.get_nowait()
        except queue.Empty:
            return None

    async def next_line(self):
        import asyncio

        while True:
            item = self.read_nowait()
            if item is not None:
                return item
            await asyncio.sleep(0.05)
