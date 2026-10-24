"""Stdin never blocks the loop and is never joined on the way out.

One daemon thread reading lines into a thread-safe queue.  The checkpoint awaits the queue, so an `explain`
round-trip keeps the loop live and the run can leave while the reader is parked; nothing joins the
thread at teardown, which is why a Ctrl-C during a checkpoint still leaves on the signal's exit code.
"""

from __future__ import annotations

import queue
import sys
import threading

EOF = object()


class LineReader:
    """Daemon thread → `queue.Queue`.  Not a thread per agent, and not on the loop."""

    def __init__(self, stream=None) -> None:
        self.stream = stream if stream is not None else sys.stdin
        self.lines: queue.Queue = queue.Queue()
        self.eof = False
        self._thread: threading.Thread | None = None

    @property
    def interactive(self) -> bool:
        """The non-interactive stdin choice: EOF or not-a-tty means every checkpoint declines everything."""
        if self.eof:
            return False
        try:
            return bool(self.stream.isatty())
        except AttributeError, ValueError:
            return False

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
        try:
            return self.lines.get_nowait()
        except queue.Empty:
            return None

    async def next_line(self):
        """Await a line without blocking the loop (`add_reader` is the other shape; this one keeps the queue and the thread)."""
        import asyncio

        while True:
            item = self.read_nowait()
            if item is not None:
                return item
            await asyncio.sleep(0.05)
