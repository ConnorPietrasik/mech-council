"""The two-speed interrupt: what the first press owes, what the next ones skip, and what bounds them."""

from __future__ import annotations

import io
import signal
from types import SimpleNamespace

from mech_council import interrupt
from mech_council.console import Console
from mech_council.interrupt import HARD_EXIT_S, Brake, signal_name
from mech_council.process.lifecycle import HURRY_STEP_S
from mech_council.render.context import Interrupted


def _brake(monkeypatch, *, killed: list[int] | None = None, armed: list[float] | None = None, stream=None, picture=None) -> Brake:
    def kill() -> None:
        if killed is not None:
            killed.append(1)

    def arm(seconds: float) -> None:
        if armed is not None:
            armed.append(seconds)

    brake = Brake(console=Console(stream=stream if stream is not None else io.StringIO()), kill=kill, picture=picture)
    # the real ones would rewire this process's handlers and end the suite with `os._exit`
    monkeypatch.setattr(brake, "_synchronous", lambda: None)
    monkeypatch.setattr(brake, "_arm", arm)
    return brake


def _a_beat_later(brake: Brake) -> None:
    """The last press is old enough for the next one to be a press of its own, not one keystroke delivered twice."""
    brake.last_at -= brake.coalesce_s + 1


def test_one_keystroke_counts_once_however_many_times_it_lands(monkeypatch):
    """The terminal signals the orchestrator's own group and the supervisor relays the same keystroke into it."""
    killed: list[int] = []
    brake = _brake(monkeypatch, killed=killed)

    brake.hit(signal.SIGINT)
    brake.hit(signal.SIGINT)
    assert brake.stop.is_set() and not brake.hard and killed == [], "the relay must not read as a request to hurry"


def test_a_second_press_arms_the_clock_the_kill_runs_on(monkeypatch):
    killed: list[int] = []
    armed: list[float] = []
    brake = _brake(monkeypatch, killed=killed, armed=armed)

    brake.hit(signal.SIGINT)
    assert brake.first == signal.SIGINT and brake.stop.is_set(), "the first press stops the deliberation and owes the report"
    assert not brake.hard and armed == []

    _a_beat_later(brake)
    brake.hit(signal.SIGINT)
    assert brake.hard and killed == [], "the members keep their closed stdins until the notice runs out"
    assert armed == [HURRY_STEP_S]


def test_a_press_once_the_clock_is_gone_bring_the_bound_back(monkeypatch):
    """Nothing but a press bounds the exit path, so no press may find the way out already out of time."""
    armed: list[float] = []
    said = io.StringIO()
    brake = _brake(monkeypatch, armed=armed, stream=said)

    brake.hit(signal.SIGINT)
    _a_beat_later(brake)
    brake.hit(signal.SIGINT)
    brake.disarm()  # an exit-path step deciding it had waited long enough
    _a_beat_later(brake)
    brake.hit(signal.SIGINT)
    assert armed == [HURRY_STEP_S, HURRY_STEP_S], "the press after the disarm arms the hurried exit again"
    assert said.getvalue().count("SIGINT again") == 2, "no press is answered with silence"


def test_a_press_after_the_kill_still_buys_the_whole_bound(monkeypatch):
    """A press means the same two seconds however far the hurried exit had got; leaving it the wreckage of
    the last bound would take away the very time the press was made to buy.
    """
    armed: list[float] = []
    killed: list[int] = []
    exits: list[int] = []
    brake = _brake(monkeypatch, armed=armed, killed=killed)
    monkeypatch.setattr(interrupt, "os", SimpleNamespace(_exit=exits.append))

    brake.hit(signal.SIGINT)
    _a_beat_later(brake)
    brake.hit(signal.SIGINT)
    brake._alarm()  # the notice is over, and the members go
    assert killed == [1] and brake.killed

    _a_beat_later(brake)
    brake.hit(signal.SIGINT)
    assert armed.count(HURRY_STEP_S) == 2, "the press arms the members' notice afresh, not the end of the run"
    brake._alarm()
    assert killed == [1, 1] and exits == [], "and the alarm after it kills again rather than ending the run"
    assert HARD_EXIT_S - 0.1 < armed[-1] <= HARD_EXIT_S, "on a bound counted from the press, not from what the last one left"


def test_a_press_inside_the_window_never_restarts_anything(monkeypatch):
    """The window runs from the last counted press: the relay of the second keystroke is not a third."""
    armed: list[float] = []
    brake = _brake(monkeypatch, armed=armed)

    brake.hit(signal.SIGINT)
    _a_beat_later(brake)
    brake.hit(signal.SIGINT)
    brake.hit(signal.SIGINT)  # the supervisor's relay of that same press
    assert armed == [HURRY_STEP_S] and brake.presses == 2


def test_a_press_that_cannot_picture_the_run_stops_it_anyway(monkeypatch):
    def picture(signum: int) -> Interrupted:
        raise RuntimeError("the docket had a surprise")

    brake = _brake(monkeypatch, picture=picture)
    brake.hit(signal.SIGINT)
    assert brake.stop.is_set(), "the picture is worth nothing if the run never stopped"
    assert brake.at is None, "and a failed one is a console line, not a crash"


def test_the_second_press_pictures_nothing_new(monkeypatch):
    seen: list[int] = []

    def picture(signum: int) -> Interrupted:
        seen.append(signum)
        return Interrupted(name=signal_name(signum), round=3, phase="the gap ladder")

    brake = _brake(monkeypatch, picture=picture)
    brake.hit(signal.SIGTERM)
    _a_beat_later(brake)
    brake.hit(signal.SIGTERM)
    assert seen == [signal.SIGTERM], "the picture is of the run as the first press found it"
    assert brake.at is not None and (brake.at.name, brake.at.round, brake.at.where) == ("SIGTERM", 3, "round 3 · the gap ladder")


def test_the_deadline_abandons_the_run_with_what_it_owes(monkeypatch):
    exits: list[int] = []
    written: list[str] = []
    said = io.StringIO()
    brake = Brake(console=Console(stream=said), abandon=lambda: written.append("run.json"))
    monkeypatch.setattr(interrupt, "os", SimpleNamespace(_exit=exits.append))

    brake.first = signal.SIGINT
    brake._abandon()
    assert written == ["run.json"], "the last-resort exit still writes the manifest"
    assert exits == [128 + signal.SIGINT]
    assert "out of time" in said.getvalue(), "and says so on the way, rather than stopping mid-line"


def test_the_notice_is_for_the_members_and_the_deadline_for_the_run(monkeypatch):
    """The hurried exit's two alarms: the members first, the run's own waits second."""
    killed: list[int] = []
    exits: list[int] = []
    brake = _brake(monkeypatch, killed=killed)
    monkeypatch.setattr(interrupt, "os", SimpleNamespace(_exit=exits.append))

    brake.hit(signal.SIGINT)
    _a_beat_later(brake)
    brake.hit(signal.SIGINT)
    brake._alarm()
    assert killed == [1] and exits == [], "the first alarm kills the members and leaves the run writing"
    brake._alarm()
    assert exits == [128 + signal.SIGINT], "and the second abandons the run where it stands"


def test_the_fast_way_out_leaves_no_deadline_behind():
    """Real timers here: the alarm is the only thing that bounds the exit path, and it must not outlive it."""
    handler, (remaining, interval) = signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL)
    brake = Brake(console=Console(stream=io.StringIO()))
    try:
        brake._deadline()
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0, "the fast way out is bounded"
        brake.disarm()
        assert signal.getitimer(signal.ITIMER_REAL)[0] == 0, "and the bound ends where the exit path does"
    finally:
        signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, remaining, interval)


def test_an_interval_below_the_timer_is_a_clock_and_not_a_crash():
    """`HARD_EXIT_S - HURRY_STEP_S` is two modules' constants in a subtraction: it goes negative eventually."""
    handler, (remaining, interval) = signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL)
    brake = Brake(console=Console(stream=io.StringIO()))
    try:
        brake._arm(-5.0)
        left = signal.getitimer(signal.ITIMER_REAL)[0]
        assert 0 < left < 1.0, "a clamped clock, never an exception thrown into whatever the signal interrupted"
    finally:
        brake.disarm()
        signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, remaining, interval)
