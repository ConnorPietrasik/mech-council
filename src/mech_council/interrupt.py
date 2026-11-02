"""Two-speed interrupt: the first signal stops the run and asks for its state; every later one re-arms the
clock that ends it."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .console import Console
from .process.lifecycle import HURRY_STEP_S
from .render.context import Interrupted

#: The hurried exit's whole bound: after this the process is abandoned.
HARD_EXIT_S = 2.0

#: A press inside this window of the last counted one is that same keystroke arriving again.
COALESCE_S = 1.0

#: The floor for `setitimer`: an interval of nil or less disarms the clock instead of firing.
MIN_ARM_S = 0.1


def signal_name(signum: int) -> str:
    with contextlib.suppress(ValueError):
        return signal.Signals(signum).name
    return f"signal {signum}"


def owed(console: Console, what: str, call: Callable[..., Any], *args, **kw) -> Any:
    """One owed exit-path step: its failure is a console line and an event, and the caller falls back on None."""
    try:
        return call(*args, **kw)
    except Exception as failure:
        frames = traceback.format_exc(limit=4).strip().splitlines()[-3:]
        console.say(f"{what} failed on the way out ({type(failure).__name__}: {failure})")
        console.event("exit_step_failed", step=what, kind=type(failure).__name__, reason=str(failure), frames=frames)
        return None


@dataclass
class Brake:
    """Signal bookkeeping for the exit path."""

    console: Console
    #: what the run looked like when the signal landed
    picture: Callable[[int], Interrupted] | None = None
    #: what the hurried exit does to the members once a closed stdin has not settled them
    kill: Callable[[], None] = lambda: None
    #: what the last-resort exit writes before the process exits
    abandon: Callable[[], None] = lambda: None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    at: Interrupted | None = None
    #: the signum this run is exiting on, 0 while nothing has been signalled
    first: int = 0
    #: when the last counted press landed
    last_at: float = 0.0
    #: counted presses
    presses: int = 0
    #: when the hurried exit's bound ends, counted from the press that armed it
    ends: float = 0.0
    coalesce_s: float = COALESCE_S
    hard: bool = False
    #: the members have been killed for the bound in hand
    killed: bool = False
    #: what the hurried exit gave up
    skipped: list[str] = field(default_factory=list)
    _loop: asyncio.AbstractEventLoop | None = None

    def arm(self) -> None:
        self._loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
                self._loop.add_signal_handler(sig, self.hit, sig)

    def hit(self, signum: int) -> None:
        """The first press stops the deliberation and asks for the report; every one after it means hurry."""
        now = time.monotonic()
        if not self.first:
            self.first = signum
            self.last_at = now
            self.presses = 1
            self.stop.set()
            self._synchronous()
            owed(self.console, "the picture of where the run stood", self._capture, signum)
            owed(self.console, "the first press", self._notice, signum, hard=False)
            return
        if now - self.last_at < self.coalesce_s:
            return
        self.hard = True
        self.last_at = now
        self.presses += 1
        owed(self.console, "the hurried exit's clock", self._deadline)
        owed(self.console, "a press after the first", self._notice, signum, hard=True)

    def skip(self, what: str) -> None:
        """Note something the hurried exit gave up."""
        if what not in self.skipped:
            self.skipped.append(what)

    def disarm(self) -> None:
        """Cancel the hurried exit's clock; only once the run has exited."""
        with contextlib.suppress(Exception):
            signal.setitimer(signal.ITIMER_REAL, 0.0)

    def _capture(self, signum: int) -> None:
        if self.picture is not None:
            self.at = self.picture(signum)

    def _notice(self, signum: int, *, hard: bool) -> None:
        name = signal_name(signum)
        if hard:
            line = f"{name} again: killing the members, skipping the waits; {HARD_EXIT_S:g} s, then the process goes"
        else:
            line = f"{name}: stopping, then reporting where the run stands"
        self.console.say(line)
        self.console.event("run_signalled", signal=name, hard=hard, presses=self.presses, elapsed=self.console.duration())

    def _synchronous(self) -> None:
        """After the first press the handler runs between bytecodes, not in the loop."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
                if self._loop is not None:
                    self._loop.remove_signal_handler(sig)
            with contextlib.suppress(ValueError, RuntimeError, OSError):
                signal.signal(sig, lambda number, _frame: self.hit(number))

    def _deadline(self) -> None:
        """The hurried exit's clock, counted afresh from every press after the first."""
        self.ends = time.monotonic() + HARD_EXIT_S
        self.killed = False
        self._arm(HURRY_STEP_S)

    def _arm(self, seconds: float) -> None:
        signal.signal(signal.SIGALRM, lambda *_: self._alarm())
        signal.setitimer(signal.ITIMER_REAL, max(seconds, MIN_ARM_S))

    def _alarm(self) -> None:
        """The notice is over: kill what a closed stdin did not settle, then wait out the rest of the bound."""
        if self.killed:
            self._abandon()
            return
        self.killed = True
        owed(self.console, "killing the members", self.kill)
        self._arm(self.ends - time.monotonic())

    def _abandon(self) -> None:
        """The last resort: the manifest first, then the notice."""
        with contextlib.suppress(Exception):
            self.abandon()
        owed(self.console, "the abandoning notice", self._notice_abandoning)
        os._exit(128 + self.first)

    def _notice_abandoning(self) -> None:
        self.console.say(f"out of time: leaving the run {self.console.duration():.1f} s in — `run.json` holds what it reached")
        self.console.event("run_abandoned", elapsed=self.console.duration(), presses=self.presses, skipped=self.skipped)
