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

#: The hurried exit's whole bound: this much time, then the process is abandoned where it stands.
HARD_EXIT_S = 2.0

#: A press inside this window of the last counted one is that same keystroke arriving again: it reaches the
#: orchestrator twice, once from the terminal's own group and once relayed by the supervisor.
COALESCE_S = 1.0

#: The floor for `setitimer`: an interval of nil or less disarms the clock instead of firing, and the alarm is
#: the only thing between a wedged exit path and a terminal that never comes back.
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
    """Signal bookkeeping for the exit path: what to owe, what to skip, and when to stop owing anything."""

    console: Console
    #: what the run looked like when the signal landed, captured before the teardown clears it
    picture: Callable[[int], Interrupted] | None = None
    #: what the hurried exit does to the members once a closed stdin has not settled them
    kill: Callable[[], None] = lambda: None
    #: what the last-resort exit writes before the process goes
    abandon: Callable[[], None] = lambda: None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    at: Interrupted | None = None
    #: the signum this run is exiting on, 0 while nothing has been signalled
    first: int = 0
    #: when the last counted press landed, for the window that makes one keystroke one press
    last_at: float = 0.0
    #: counted presses, for the event log
    presses: int = 0
    #: when the hurried exit's bound ends, counted from the press that armed it
    ends: float = 0.0
    coalesce_s: float = COALESCE_S
    hard: bool = False
    #: the members have been killed for the bound in hand; the alarm after this is its end
    killed: bool = False
    #: what the hurried exit gave up, for the console tally and `run.json`
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
            # the stop first: nothing that can raise may precede the one thing only this handler can do
            self.stop.set()
            self._synchronous()
            owed(self.console, "the picture of where the run stood", self._capture, signum)
            owed(self.console, "the first press", self._notice, signum, hard=False)
            return
        # a press inside the window is the same keystroke arriving again, not a request to hurry
        if now - self.last_at < self.coalesce_s:
            return
        # every press after that means hurry, and starts the bound over: the way out may not run out
        self.hard = True
        self.last_at = now
        self.presses += 1
        owed(self.console, "the hurried exit's clock", self._deadline)
        owed(self.console, "a press after the first", self._notice, signum, hard=True)

    def skip(self, what: str) -> None:
        """Note something the hurried exit gave up; the tally prints at the end of the exit path."""
        if what not in self.skipped:
            self.skipped.append(what)

    def disarm(self) -> None:
        """Cancel the hurried exit's clock — only once the run has finished leaving."""
        with contextlib.suppress(Exception):
            signal.setitimer(signal.ITIMER_REAL, 0.0)

    def _capture(self, signum: int) -> None:
        if self.picture is not None:
            self.at = self.picture(signum)

    def _notice(self, signum: int, *, hard: bool) -> None:
        """No press is answered with silence, and none with a bound it will not get."""
        name = signal_name(signum)
        if hard:
            line = f"{name} again: killing the members, skipping the waits; {HARD_EXIT_S:g} s, then the process goes"
        else:
            line = f"{name}: stopping, then reporting where the run stands"
        self.console.say(line)
        self.console.event("run_signalled", signal=name, hard=hard, presses=self.presses, elapsed=self.console.duration())

    def _synchronous(self) -> None:
        """After the first press the handler has to run between bytecodes: the exit path spends long stretches
        outside the loop, where a signal handler of the loop's own is never reached.
        """
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
                if self._loop is not None:
                    self._loop.remove_signal_handler(sig)
            # the removal restores the default, so the handler goes on afterwards
            with contextlib.suppress(ValueError, RuntimeError, OSError):
                signal.signal(sig, lambda number, _frame: self.hit(number))

    def _deadline(self) -> None:
        """The hurried exit's clock, counted afresh from every press after the first: `kill_all`'s notice of a
        closed stdin, then the kill, then the end of the bound. A press that left only the wreckage of the
        last bound would cost the run the very time the press was meant to buy.
        """
        self.ends = time.monotonic() + HARD_EXIT_S
        self.killed = False
        self._arm(HURRY_STEP_S)

    def _arm(self, seconds: float) -> None:
        signal.signal(signal.SIGALRM, lambda *_: self._alarm())
        signal.setitimer(signal.ITIMER_REAL, max(seconds, MIN_ARM_S))

    def _alarm(self) -> None:
        """The notice is over: kill what a closed stdin did not settle, then run out the rest of the bound.
        Past the kill the clock has nothing left to wait for, so its next alarm is the end.
        """
        if self.killed:
            self._abandon()
            return
        self.killed = True
        owed(self.console, "killing the members", self.kill)
        self._arm(self.ends - time.monotonic())

    def _abandon(self) -> None:
        """The last resort, in the order of what is worth saving: the manifest first, because the console it
        would print to may be the very thing the run is wedged on.
        """
        with contextlib.suppress(Exception):
            self.abandon()
        owed(self.console, "the abandoning notice", self._notice_abandoning)
        os._exit(128 + self.first)

    def _notice_abandoning(self) -> None:
        """Silence is the last thing a run being abandoned may cause."""
        self.console.say(f"out of time: leaving the run {self.console.duration():.1f} s in — `run.json` holds what it reached")
        self.console.event("run_abandoned", elapsed=self.console.duration(), presses=self.presses, skipped=self.skipped)
