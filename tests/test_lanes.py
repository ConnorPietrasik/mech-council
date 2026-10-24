"""The two lanes, restart-and-re-ask, and the chair's digest.

Fakes stand in for the *child process* only: the lanes, the repairs, the restart accounting and
the watchdog's enforcement are the real ones, because those are what the chair and the two-lane
rule promise.  The process behaviour around them is `test_rpc.py` and `test_sandbox.py`.
"""

from __future__ import annotations

import asyncio
import time
from typing import cast

import pytest

from council.asks import REPAIR_ASKS, AskLost, AskUnusable, Lanes, needs_repair
from council.chairwork import Chair
from council.config import Council, MemberSpec
from council.console import Console as RunConsole
from council.members import Members
from council.pi_rpc import Answer, ChildGone
from council.prompts.brief import Brief
from council.watchdog import Watchdog

pytestmark = pytest.mark.timeout(30)


class FakeRpc:
    """A child that answers whatever the script says, in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []
        self.activity = None
        self.aborts = 0
        self.prompts = 0

    async def ask(self, message, *, timeout=None):
        self.calls.append(message)
        self.prompts += 1
        item = self.script.pop(0) if self.script else {"text": ""}
        if isinstance(item, Exception):
            raise item
        self.inflight = asyncio.get_running_loop().create_future()
        if item.get("hang"):
            self.activity = _Activity(started=time.monotonic(), last_event=time.monotonic())
            try:
                await asyncio.wait_for(self.inflight, float(item["hang"]))
            except TimeoutError:
                pass
        return Answer(text=item.get("text", ""), stop_reason=item.get("stop", "stop"), error_message=item.get("error", ""), settled=True)

    async def abort(self):
        """The abort *settles* the ask in flight — that is how a restart ends one."""
        self.aborts += 1
        inflight = getattr(self, "inflight", None)
        if inflight is not None and not inflight.done():
            inflight.set_result(None)
        return True

    async def close_stdin(self):
        pass

    async def stop(self):
        pass


class _Activity:
    def __init__(self, started, last_event):
        self.started, self.last_event = started, last_event
        self.events, self.last_tool, self.last_event_name = 0, "", ""
        self.last_tool_args = {}

    def age(self, now=None):
        return (now or time.monotonic()) - self.last_event


class FakeMember:
    def __init__(self, file_base, agent_id, rpc):
        self.file_base, self.id, self.rpc = file_base, agent_id, rpc
        self.restarts = 0

    async def restart(self, *, why=""):
        self.restarts += 1


class FakeMembers:
    def __init__(self, members: dict):
        self.members = members  # keyed by file_base, exactly as the real one is

    def file_base_for(self, agent_id):
        # The real registry answers in either vocabulary; so does this one.
        if agent_id in self.members:
            return agent_id
        for member in self.members.values():
            if agent_id == member.id:
                return member.file_base
        raise KeyError(agent_id)

    def label_for(self, file_base):
        return file_base

    def spec_for(self, file_base):
        return SPEC[self.members[file_base].file_base]

    async def restart(self, file_base, *, why=""):
        member = self.members[file_base]
        member.restarts += 1
        return member


SPEC = {
    "seat1-one": MemberSpec(name="one", slot=1, id="Agent1", file_base="seat1-one", provider="anthropic", model="claude-opus-4"),
    "seat2-two": MemberSpec(name="two", slot=2, id="Agent2", file_base="seat2-two"),
    "chair": MemberSpec(name="gavel", slot=0, file_base="chair"),
}


def council(n=2, *, concurrency=2, watchdog=0):
    agents = [
        MemberSpec(name=name, slot=i, id=f"Agent{i}", file_base=fb, provider=SPEC[fb].provider, model=SPEC[fb].model)
        for i, (fb, name) in enumerate([("seat1-one", "one"), ("seat2-two", "two")][:n], start=1)
    ]
    return Council(
        name="t",
        mode="review",
        goal="g",
        agents=agents,
        chair=SPEC["chair"],
        max_rounds=3,
        watchdog_sec=watchdog,
        chair_timeout_sec=60,
        max_concurrency=concurrency,
        transcripts="errors",
        sandbox_mib=64,
        pi_cli="pi",
        pi_version="1.4.0",
        config_path="/proj/council.toml",
        project="/proj",
    )


class Console:
    def __init__(self):
        self.lines: list[str] = []
        self.events: list[dict] = []

    def say(self, text, *args):
        self.lines.append(text % args if args else text)

    def event(self, kind, **fields):
        self.events.append(dict(type=kind, **fields))


def lanes(*, n=2, concurrency=2, script=None, watchdog=0, script_defaults=False):
    """script: file_base → list of replies for FakeRpc."""
    script = dict(script or {})
    if script_defaults:  # a digest lane that has no scripted chair reply keeps everyone
        script.setdefault("chair", [{"text": '{"decisions": [{"agent": "Agent1", "action": "keep", "reason": "n"}]}'}])
    roster = {"chair": FakeMember("chair", None, FakeRpc(script.get("chair", [])))}
    for i, fb in enumerate(("seat1-one", "seat2-two")[:n], start=1):
        roster[fb] = FakeMember(fb, f"Agent{i}", FakeRpc(script.get(fb, [])))
    members = FakeMembers(roster)
    board = council(n=n, concurrency=concurrency, watchdog=watchdog)
    console = Console()
    # The fakes stand in for the registry and the console: the lanes themselves, which are the
    # subject here, hold only what they call on them.
    return Lanes(board, cast(Members, members), cast(RunConsole, console)), members, console, board


def verdict(text="ok"):
    import json

    return json.dumps({"positions": [], "new_items": [], "answers": [], "questions": [], "human_input": [], "signoff": True})


GONE = ChildGone("child stdout closed")
"""Restart-and-re-ask is *did a message arrive*: a child that says nothing takes the
restart path, and a message that arrives and is unreadable is a rejection.  So the
restart path is scripted with a dead child, not with an empty reply —
`test_a_repairable_answer_gets_exactly_two_re_ask_and_then_is_rejected` covers the
other half."""


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ The two-lane rule's lanes


def test_the_chair_slot_is_outside_the_pool_a_wedged_agent_cannot_hold_it():
    """With max_concurrency = 1 and one wedged seat, the chair still answers."""
    order: list[str] = []

    async def main():
        board, _members, _console, _ = lanes(
            concurrency=1,
            script={
                "seat1-one": [{"hang": 5, "text": verdict()}],
                "chair": [{"text": '{"decisions": [{"agent": "Agent1", "action": "keep", "reason": "reading"}]}'}],
            },
        )

        async def seat():
            order.append("seat-start")
            outcome = await board.ask_agent(
                "Agent1", "go", round_=1, kind="verdict", parse_kw={"known_items": [], "mode": "review"}, phase="round"
            )
            order.append("seat-end")
            return outcome

        async def chair():
            await asyncio.sleep(0.1)  # let the seat take the only slot
            order.append("chair-start")
            outcome = await board.ask_chair("digest?", round_=1, phase="digest", kind="watchdog", timeout=5)
            order.append("chair-end")
            return outcome

        seat_task, chair_task = asyncio.create_task(seat()), asyncio.create_task(chair())
        chair_outcome = await asyncio.wait_for(chair_task, 3)
        assert chair_outcome.text and not chair_outcome.failure
        assert order == ["seat-start", "chair-start", "chair-end"], order
        board.in_flight.clear()
        seat_task.cancel()

    run(main())


def test_the_agent_pool_caps_concurrency_but_never_serialises_the_chair():
    async def main():
        board, _members, _console, _ = lanes(
            concurrency=2,
            script={
                "seat1-one": [{"hang": 0.3, "text": verdict()}],
                "seat2-two": [{"hang": 0.3, "text": verdict()}],
            },
        )
        started = time.monotonic()
        outcomes = await board.ask_seats(
            [("Agent1", "go"), ("Agent2", "go")],
            round_=1,
            kind="verdict",
            phase="round",
            parse_for=lambda a: {"mode": "review", "me": a, "known_items": set()},
        )
        elapsed = time.monotonic() - started
        assert len(outcomes) == 2 and elapsed < 0.55, "the two seats ran together"
        serial = await board.ask_seats(
            [("Agent1", "go"), ("Agent2", "go")],
            round_=2,
            kind="verdict",
            phase="round",
            parse_for=lambda a: {"mode": "review", "me": a, "known_items": set()},
        )
        assert len(serial) == 2

    run(main())


def test_the_digest_rows_read_the_live_activity_not_a_snapshot():
    """A frozen digest would report every member at zero events: all wedged, none so."""

    async def main():
        board, _members, _console, _ = lanes(concurrency=2, script={"seat1-one": [{"hang": 5, "text": verdict()}]})
        task = asyncio.create_task(
            board.ask_agent("Agent1", "go", round_=1, kind="verdict", parse_kw={"mode": "review", "me": "seat1-one", "known_items": set()})
        )
        await asyncio.sleep(0.1)
        rows = board.digest()
        task.cancel()
        return rows

    rows = run(main())
    assert len(rows) == 1 and rows[0]["agent"] == "seat1-one"
    assert "elapsed" in rows[0] and rows[0]["events"] == 0


# --------------------------------------------------------- Lenient extraction's repairs


def test_a_repairable_answer_gets_exactly_two_re_ask_and_then_is_rejected():
    async def main():
        board, _members, _console, _ = lanes(
            script={"seat1-one": [{"text": "prose, no JSON at all"}, {"text": "still prose"}, {"text": "and still prose"}]}
        )
        return await board.ask_agent(
            "Agent1", "go", round_=2, kind="verdict", parse_kw={"mode": "review", "me": "seat1-one", "known_items": set()}, phase="round"
        )

    outcome = run(main())
    assert outcome.repairs == REPAIR_ASKS and outcome.rejected
    assert outcome.prompts == REPAIR_ASKS + 1
    assert outcome.failure is None, "a refused message becomes a gap, never an abort"


def test_a_repair_that_lands_is_not_a_rejection():
    async def main():
        board, _members, _console, _ = lanes(script={"seat1-one": [{"text": "prose"}, {"text": verdict()}]})
        return await board.ask_agent(
            "Agent1", "go", round_=1, kind="verdict", parse_kw={"mode": "review", "me": "seat1-one", "known_items": set()}
        )

    outcome = run(main())
    assert outcome.repairs == 1 and not outcome.rejected and outcome.data is not None


def test_needs_repair_distinguishes_the_repairable_from_themerely_empty():
    class Parsed:
        def __init__(self, invalid=(), errors=()):
            self.invalid, self.errors = list(invalid), list(errors)

    assert needs_repair(Parsed(errors=["no JSON object"]))
    assert needs_repair(Parsed(invalid=["bad stance"]))
    assert not needs_repair(Parsed())


# -------------------------------------------------- The restart-and-re-ask path


def ask_once(board):
    """A first ask that came back with nothing — restart-and-re-ask's territory."""
    return {
        "round_": 3,
        "kind": "verdict",
        "phase": "round",
        "parse_kw": {"mode": "review", "me": "seat1-one", "known_items": set()},
        "message": "go",
    }


def test_a_failed_ask_restarts_once_and_re_asks_once():
    async def main():
        board, members, _console, _ = lanes(script={"seat1-one": [GONE, {"text": verdict()}]})
        outcome = await board.ask_agent(
            "Agent1", "go", round_=3, kind="verdict", parse_kw={"mode": "review", "me": "seat1-one", "known_items": set()}
        )
        return outcome, members.members["seat1-one"].restarts

    outcome, restarts = run(main())
    assert restarts == 1, "one restart, one re-ask, no more"
    assert outcome.data is not None and outcome.prompts == 2


def test_the_second_failure_ends_the_run_naming_the_member_and_the_round():
    async def main():
        board, members, _console, _ = lanes(script={"seat1-one": [GONE, GONE]})
        with pytest.raises(AskLost) as raised:
            await board.ask_agent(
                "Agent1", "go", round_=4, kind="verdict", parse_kw={"mode": "review", "me": "seat1-one", "known_items": set()}
            )
        return raised.value, members.members["seat1-one"].restarts

    failure, restarts = run(main())
    assert restarts == 1
    assert "seat1-one" in str(failure) and "round 4" in str(failure)
    assert "restart" in str(failure)


def test_an_errored_first_prompt_is_an_unusable_seat_naming_the_provider():
    """The unusable-seat rule, and *only* the first prompt: the same error later is restart-and-re-ask's path."""

    async def main():
        board, members, _console, _ = lanes(script={"seat1-one": [{"stop": "error", "error": "401 invalid api key"}]})
        with pytest.raises(AskUnusable) as raised:
            await board.ask_agent(
                "Agent1",
                "go",
                round_=0,
                kind="phase0",
                parse_kw={"mode": "review", "me": "seat1-one", "known_items": set()},
                phase="phase0",
                classify_unusable=True,
            )
        return raised.value, members.members["seat1-one"].restarts

    failure, restarts = run(main())
    assert restarts == 0, "an unusable seat is not restarted: one prompt and it is over"
    assert "anthropic" in str(failure) and "claude-opus-4" in str(failure)
    assert "401" in str(failure)


def test_a_transport_failure_takes_the_restart_path_not_the_unusable_exit():
    from council.pi_rpc import ChildGone

    async def main():
        board, members, _console, _ = lanes(script={"seat1-one": [ChildGone("child stdout closed"), {"text": verdict()}]})
        outcome = await board.ask_agent(
            "Agent1",
            "go",
            round_=1,
            kind="verdict",
            parse_kw={"mode": "review", "me": "seat1-one", "known_items": set()},
            classify_unusable=True,
        )
        return outcome, members.members["seat1-one"].restarts

    outcome, restarts = run(main())
    assert restarts == 1 and outcome.data is not None


def test_one_seat_dying_does_not_abandon_its_siblings():
    async def main():
        board, _members, _console, _ = lanes(script={"seat1-one": [GONE, GONE], "seat2-two": [{"text": verdict()}]})
        outcomes = await board.ask_seats(
            [("Agent1", "go"), ("Agent2", "go")],
            round_=1,
            kind="verdict",
            phase="round",
            parse_for=lambda a: {"mode": "review", "me": a, "known_items": set()},
        )
        return outcomes

    outcomes = run(main())
    assert outcomes["Agent1"].failure and not outcomes["Agent2"].failure
    assert outcomes["Agent2"].data is not None


# ----------------------------------------------------------------- The chair's watchdog


def chair_board(script, *, watchdog=1):
    board, members, console, council_ = lanes(watchdog=watchdog, script=script, script_defaults=True)
    brief = Brief(mode="review", project="/proj")
    chair = Chair(council_, board, console, brief)
    return board, members, console, Watchdog(council_, board, chair, console)


def test_a_pass_in_flight_suppresses_the_next_tick_instead_of_queueing_one():
    async def main():
        board, _members, console, watchdog = chair_board(
            {"chair": [{"hang": 5, "text": '{"decisions": [{"agent": "Agent1", "action": "keep", "reason": "x"}]}'}]}
        )
        board.chair_busy = True
        assert await watchdog.pass_once() is None
        assert watchdog.deferred == 1
        assert any(e["type"] == "watchdog_deferred" for e in console.events)

    run(main())


def test_nothing_in_flight_means_no_digest_at_all():
    """A parked checkpoint is not in-flight work, so no pass is asked for."""

    async def main():
        _board, _members, console, watchdog = chair_board({"chair": []})
        return await watchdog.pass_once(), console.events

    parsed, events = run(main())
    assert parsed is None
    assert not any(e["type"] == "watchdog_pass" for e in events)


def test_an_unreadable_digest_keeps_everyone():
    async def main():
        board, members, _console, watchdog = chair_board({"chair": [{"text": "the chair wrote prose instead of JSON"}]})
        board.in_flight["seat1-one"] = "seat1-one"
        members.members["seat1-one"].rpc.activity = _Activity(started=time.monotonic(), last_event=time.monotonic())
        return await watchdog.pass_once(), members.members["seat1-one"].restarts

    parsed, restarts = run(main())
    assert parsed is None and restarts == 0


def test_a_restart_decision_aborts_then_restarts_that_one_member():
    async def main():
        decisions = '{"decisions": [{"agent": "Agent1", "action": "restart", '
        decisions += '"reason": "no event for 400s"}]}'
        board, members, console, watchdog = chair_board({"chair": [{"text": decisions}]})
        board.in_flight["seat1-one"] = "seat1-one"
        members.members["seat1-one"].rpc.activity = _Activity(started=time.monotonic(), last_event=time.monotonic())
        parsed = await watchdog.pass_once()
        return parsed, members.members["seat1-one"], console

    parsed, member, console = run(main())
    assert parsed and member.restarts == 1 and member.rpc.aborts == 1
    assert any(e["type"] == "watchdog_restart" for e in console.events)


def test_a_pass_naming_an_id_that_is_not_on_the_roster_restarts_nobody():
    async def main():
        decisions = '{"decisions": [{"agent": "Agent9", "action": "restart", "reason": "?"}]}'
        board, members, console, watchdog = chair_board({"chair": [{"text": decisions}]})
        board.in_flight["seat1-one"] = "seat1-one"
        members.members["seat1-one"].rpc.activity = _Activity(started=time.monotonic(), last_event=time.monotonic())
        await watchdog.pass_once()
        return members.members["seat1-one"].restarts, console

    restarts, console = run(main())
    assert restarts == 0
    assert any(e["type"] == "watchdog_unknown_agent" for e in console.events)


def test_watchdog_sec_zero_disables_the_tick_and_says_so():
    async def main():
        _board, _members, console, watchdog = chair_board({}, watchdog=0)
        watchdog.start()
        assert watchdog.task is None
        await watchdog.stop()
        return console.lines

    lines = run(main())
    assert any("watchdog disabled" in line for line in lines)


def test_stop_cancels_a_pass_in_flight_rather_than_awaiting_it():
    """Awaiting the tick would hang the exit behind a chair that never answers."""

    async def main():
        board, _members, _console, watchdog = chair_board({"chair": [{"hang": 30, "text": ""}]})
        board.in_flight["seat1-one"] = "seat1-one"
        started = time.monotonic()
        watchdog.start()
        await asyncio.sleep(0.05)
        await watchdog.stop()
        return time.monotonic() - started, watchdog.task

    elapsed, task = run(main())
    assert elapsed < 2, f"stopping waited for the chair ({elapsed:.1f}s)"
    assert task is None or task.cancelled() or task.done()
