"""The two lanes, restart-and-re-ask, and the chair's digest."""

from __future__ import annotations

import asyncio
import json
import time
from typing import cast

import pytest

from mech_council.asks import REPAIR_ASKS, AskLost, AskUnusable, Lanes, needs_repair
from mech_council.chairwork import Chair, ChairUnusable
from mech_council.config import Council, MemberSpec, provider_seat_pool
from mech_council.console import Console as RunConsole
from mech_council.deliberation import escalations
from mech_council.deliberation.state import Docket
from mech_council.humaninput import Checkpoint
from mech_council.members import Members
from mech_council.pi_rpc import Answer, ChildGone
from mech_council.prompts.brief import Brief
from mech_council.watchdog import Watchdog

pytestmark = pytest.mark.timeout(30)


class FakeRpc:
    """A child that answers whatever the script says, in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []
        self.timeouts: list[float | None] = []
        self.activity = None
        self.aborts = 0
        self.prompts = 0

    async def ask(self, message, *, timeout=None):
        self.calls.append(message)
        self.timeouts.append(timeout)
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
        self.compacting = False

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
        # mirrors the real one: a seat's global id, `chair` for the chair, the file_base where no spec holds it
        spec = SPEC.get(file_base)
        if spec is None:
            return file_base
        return "chair" if spec.id is None else spec.id

    def display_for(self, file_base):
        return SPEC[file_base].name

    def spec_for(self, file_base):
        return SPEC[self.members[file_base].file_base]

    async def restart(self, file_base, *, why=""):
        member = self.members[file_base]
        member.restarts += 1
        return member


SPEC = {
    "seat1-one": MemberSpec(name="one", id="Agent1", file_base="seat1-one", provider="anthropic", model="claude-opus-4"),
    "seat2-two": MemberSpec(name="two", id="Agent2", file_base="seat2-two"),
    "seat3-three": MemberSpec(name="three", id="Agent3", file_base="seat3-three", provider="beta", model="b1"),
    "seat4-four": MemberSpec(name="four", id="Agent4", file_base="seat4-four", provider="anthropic", model="claude-opus-4"),
    "chair": MemberSpec(name="gavel", file_base="chair"),
}


def council(n=2, *, providers=None, watchdog=0):
    agents = [
        MemberSpec(name=name, id=f"Agent{i}", file_base=fb, provider=SPEC[fb].provider, model=SPEC[fb].model)
        for i, (fb, name) in enumerate(
            [("seat1-one", "one"), ("seat2-two", "two"), ("seat3-three", "three"), ("seat4-four", "four")][:n], start=1
        )
    ]
    return Council(
        name="t",
        mode="review",
        goal="g",
        agents=agents,
        chair=SPEC["chair"],
        max_rounds=3,
        grace_rounds=0,
        seconding=True,
        watchdog_sec=watchdog,
        chair_timeout_sec=60,
        verbosity=0,
        sandbox_mib=64,
        pi_cli="pi",
        pi_version="1.4.0",
        config_path="/proj/council.toml",
        project="/proj",
        provider_concurrency=dict(providers or {}),
    )


class Console:
    def __init__(self):
        self.lines: list[str] = []
        self.events: list[dict] = []

    def say(self, text, *args):
        self.lines.append(text % args if args else text)

    def event(self, kind, **fields):
        self.events.append(dict(type=kind, **fields))


def lanes(*, n=2, providers=None, script=None, watchdog=0, script_defaults=False):
    """script: file_base → list of replies for FakeRpc."""
    script = dict(script or {})
    if script_defaults:  # a digest lane that has no scripted chair reply keeps everyone
        script.setdefault("chair", [{"text": '{"decisions": [{"agent": "Agent1", "action": "keep", "reason": "n"}]}'}])
    roster = {"chair": FakeMember("chair", None, FakeRpc(script.get("chair", [])))}
    for i, fb in enumerate(("seat1-one", "seat2-two", "seat3-three", "seat4-four")[:n], start=1):
        roster[fb] = FakeMember(fb, f"Agent{i}", FakeRpc(script.get(fb, [])))
    members = FakeMembers(roster)
    board = council(n=n, providers=providers, watchdog=watchdog)
    console = Console()
    # The fakes carry only what the lanes call on them.
    return Lanes(board, cast(Members, members), cast(RunConsole, console)), members, console, board


def verdict():
    return json.dumps({"positions": [], "new_items": [], "answers": [], "questions": [], "human_input": [], "signoff": True})


def prose_flag():
    """A sound reply whose `blocking` is worded as prose: one field the lane will not read."""
    return json.dumps(
        {
            "positions": [{"item": "F1", "stance": "agree"}],
            "human_input": [{"question": "which window?", "blocking": "urgent"}],
            "signoff": True,
        }
    )


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
    """With the provider capped at 1 and one wedged seat, the chair still answers."""
    order: list[str] = []

    async def main():
        board, _members, _console, _ = lanes(
            providers={"anthropic": 1},
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
            outcome = await board.ask_chair("digest?", round_=None, phase="digest", kind="watchdog", timeout=5)
            order.append("chair-end")
            return outcome

        seat_task, chair_task = asyncio.create_task(seat()), asyncio.create_task(chair())
        chair_outcome = await asyncio.wait_for(chair_task, 3)
        assert chair_outcome.text and not chair_outcome.failure
        assert order == ["seat-start", "chair-start", "chair-end"], order
        board.in_flight.clear()
        seat_task.cancel()

    run(main())


def test_the_seat_pool_lets_the_two_seats_answer_together():
    async def main():
        board, _members, _console, _ = lanes(
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
        assert len(outcomes) == 2 and elapsed < 0.55, "the two seats ran together, not one after the other"

    run(main())


def test_the_seat_pool_is_the_provider_limit_minus_the_chairs_share():
    board = council(providers={"anthropic": 3})
    assert provider_seat_pool(board, "anthropic") == 3, "the chair sits elsewhere"
    board.chair = MemberSpec(name="gavel", file_base="chair", provider="anthropic")
    assert provider_seat_pool(board, "anthropic") == 2, "the chair keeps one of the provider's slots"
    assert provider_seat_pool(council(providers={"anthropic": 1}), "anthropic") == 1, "one, with the chair: clamped, never zero"
    assert provider_seat_pool(council(), "anthropic") == 1, "uncapped: every seat on the provider"
    assert provider_seat_pool(council(), None) == 1, "and the default provider's"


def test_a_provider_cap_binds_only_its_own_seats():
    """anthropic capped at 1: while its first seat is wedged, the second anthropic seat waits and beta still answers."""
    script = {
        "seat1-one": [{"hang": 0.6, "text": verdict()}],
        "seat3-three": [{"text": verdict()}],
        "seat4-four": [{"text": verdict()}],
    }

    async def main():
        board, _members, _console, _ = lanes(n=4, providers={"anthropic": 1, "beta": 1}, script=script)

        async def seat(agent):
            await board.ask_agent(
                agent, "go", round_=1, kind="verdict", parse_kw={"mode": "review", "me": agent, "known_items": set()}, phase="round"
            )

        first = asyncio.create_task(seat("Agent1"))  # anthropic, wedged
        await asyncio.sleep(0.1)
        beta = asyncio.create_task(seat("Agent3"))
        second = asyncio.create_task(seat("Agent4"))  # anthropic, must wait
        await asyncio.wait_for(beta, 3)
        in_flight = set(board.in_flight.values())
        assert "seat1-one" in in_flight, "the wedged seat still holds its provider's slot"
        assert "seat4-four" not in in_flight, "the second anthropic seat waits for the pool; it was never prompted"
        await asyncio.wait_for(first, 5)
        await asyncio.wait_for(second, 5)

    run(main())


def test_the_digest_lists_the_members_in_flight_and_reads_their_live_activity():
    """The chair judges wedging from these rows: a seat that is not being asked is absent, and one that has moved reads as moved."""

    async def main():
        board, members, _console, _ = lanes(script={"seat1-one": [{"hang": 5, "text": verdict()}]})
        task = asyncio.create_task(
            board.ask_agent("Agent1", "go", round_=1, kind="verdict", parse_kw={"mode": "review", "me": "seat1-one", "known_items": set()})
        )
        await asyncio.sleep(0.1)
        # A peer idle with activity of its own; a wedge that has taken two more events since it began.
        members.members["seat2-two"].rpc.activity = _Activity(started=time.monotonic(), last_event=time.monotonic())
        members.members["seat1-one"].rpc.activity.events = 2
        members.members["seat1-one"].rpc.activity.compacting = True
        rows = board.digest()
        task.cancel()
        return rows

    rows = run(main())
    assert [row["agent"] for row in rows] == ["Agent1"], "only the seat being asked is in the digest"
    assert rows[0]["events"] == 2, "the row carries the activity as it stands, not as it was when the ask began"
    assert rows[0]["compacting"] is True, "a compaction in flight is the row's to carry"


def test_the_digest_line_shows_a_compacting_seat_without_the_idle_claim():
    """The console line is where a reader of the log spots it: compacting is work, not an idle stall."""

    async def main():
        board, _members, console, council_ = lanes(script={"chair": [{"text": '{"decisions": []}'}]})
        rows = [
            {
                "agent": "Agent1",
                "tool_name": "bash",
                "last_event": "compaction_start",
                "compacting": True,
                "idle": 436.0,
                "elapsed": 834.0,
                "events": 2191,
                "last_tool": "grep -r docker.sock compose/",
            },
        ]
        await Chair(council_, board, console, Brief(mode="review", project="/proj")).digest(rows)
        return console.lines

    lines = run(main())
    assert "watchdog: chair pass over one compacting 436s" in lines, "the line says compacting, not bash idle"


def test_the_chairs_watchdog_prompt_names_the_compaction_and_withholds_the_stale_args():
    """What the chair actually reads: the seat is marked compacting, and the last tool's args — the thing that
    reads as a hung grep — are not in the prompt at all."""

    rows = [
        {
            "agent": "Agent1",
            "tool_name": "bash",
            "last_event": "compaction_start",
            "compacting": True,
            "idle": 436.0,
            "elapsed": 834.0,
            "events": 2191,
            "last_tool": "grep -r docker.sock compose/",
        },
        {
            "agent": "Agent2",
            "tool_name": "read",
            "last_event": "tool_execution_end",
            "compacting": False,
            "idle": 12.0,
            "elapsed": 60.0,
            "events": 9,
            "last_tool": "path=src/app.py",
        },
    ]
    prompt = Brief(mode="review", project="/proj").watchdog(rows)
    assert "Agent1: compacting" in prompt
    assert "grep -r docker.sock" not in prompt, "the stale args are withheld while the seat is compacting"
    assert "path=src/app.py" in prompt, "an ordinary seat still gets its args"
    assert "tool_execution_end" in prompt, "an ordinary seat's last event type is shown"


def test_the_digest_line_names_the_seat_it_is_watching():
    """The digest is console: the rows carry the ids the chair answers to, the line carries the names."""

    async def main():
        board, _members, console, council_ = lanes(script={"chair": [{"text": '{"decisions": []}'}]})
        rows = [
            {"agent": "Agent1", "tool_name": "Read", "idle": 12.4, "elapsed": 1.0, "events": 2, "last_tool": "path=src/app.py"},
            {"agent": "chair", "tool_name": "", "idle": 2.0, "elapsed": 1.0, "events": 0, "last_tool": ""},
        ]
        await Chair(council_, board, console, Brief(mode="review", project="/proj")).digest(rows)
        return console.lines

    assert "watchdog: chair pass over one Read 12s idle; gavel no tool 2s idle" in run(main())


def test_the_crews_digest_labels_each_row_with_the_seats_global_id():
    """The run's chair reads one digest over every crew: each row wears the seat's global ID — how
    seats are addressed in this run, and what the chair echoes back — never a display name. A
    borrowed crew's chair is not the run's `chair`, so its row wears the crew's name."""
    import types

    from mech_council.crews import CrewLanes, Crews

    def crew_council(spec):
        return Council(
            name="t",
            mode="review",
            goal="g",
            agents=[spec],
            chair=SPEC["chair"],
            max_rounds=3,
            grace_rounds=0,
            seconding=True,
            watchdog_sec=0,
            chair_timeout_sec=60,
            verbosity=0,
            sandbox_mib=64,
            pi_cli="pi",
            pi_version="1.4.0",
            config_path="/proj/council.toml",
            project="/proj",
        )

    async def main():
        SPEC["bld-forge"] = MemberSpec(name="Forge", id="Agent3", file_base="bld-forge")
        SPEC["pln-metic"] = MemberSpec(name="Metic (planner)", id="Agent1", file_base="pln-metic")
        SPEC["pln-chair"] = MemberSpec(name="Plan Chair", file_base="pln-chair")
        member_a = FakeMember("bld-forge", "Agent3", FakeRpc([]))
        member_b = FakeMember("pln-metic", "Agent1", FakeRpc([]))
        member_c = FakeMember("pln-chair", None, FakeRpc([]))
        council_b = crew_council(SPEC["pln-metic"])
        council_b.chair = SPEC["pln-chair"]
        lanes_a = Lanes(crew_council(SPEC["bld-forge"]), cast(Members, FakeMembers({"bld-forge": member_a})), cast(RunConsole, Console()))
        lanes_b = Lanes(council_b, cast(Members, FakeMembers({"pln-metic": member_b, "pln-chair": member_c})), cast(RunConsole, Console()))
        for lanes, base, member in ((lanes_a, "bld-forge", member_a), (lanes_b, "pln-metic", member_b), (lanes_b, "pln-chair", member_c)):
            lanes.in_flight[lanes.members.label_for(base)] = base
            member.rpc.activity = _Activity(started=time.monotonic(), last_event=time.monotonic())
        crews = types.SimpleNamespace(
            live=[
                types.SimpleNamespace(label="builder", lanes=lanes_a, council=crew_council(SPEC["bld-forge"])),
                types.SimpleNamespace(label="planner", lanes=lanes_b, council=council_b),
            ]
        )
        return CrewLanes(cast(Crews, crews)).digest()

    rows = run(main())
    assert {row["agent"] for row in rows} == {"Agent3", "Agent1", "chair (planner)"}, (
        "each row wears the seat's global ID, never a name; the borrowed crew's chair is not the run's chair"
    )


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


def test_a_refused_field_never_costs_the_seat_its_whole_answer():
    """A field the lane will not read is told, refused by name, and the rest of the reply is applied."""

    async def main():
        board, _members, console, _ = lanes(script={"seat1-one": [{"text": prose_flag()}] * (REPAIR_ASKS + 1)})
        outcome = await board.ask_agent(
            "Agent1",
            "go",
            round_=2,
            kind="verdict",
            parse_kw={"mode": "review", "me": "seat1-one", "known_items": {"F1"}},
            phase="round",
        )
        return outcome, console

    outcome, console = run(main())
    assert outcome.repairs == REPAIR_ASKS and outcome.prompts == REPAIR_ASKS + 1, "the seat is asked to say it as a flag"
    assert not outcome.rejected, "one unreadable field is not an unreadable message"
    assert outcome.data.data["positions"], "the seat's stance stands"
    refused = [record for record in console.events if record["type"] == "verdict_field_refused"]
    assert refused and refused[-1]["field"] == "blocking" and refused[-1]["about"] == "which window?", refused
    assert not [record for record in console.events if record["type"] == "verdict_rejected" and record.get("final")], "no final rejection"


def test_needs_repair_distinguishes_the_repairable_from_themerely_empty():
    class Parsed:
        def __init__(self, invalid=(), errors=(), refused=()):
            self.invalid, self.errors, self.refused = list(invalid), list(errors), list(refused)
            self.problems = [*self.errors, *(f"`{r['field']}` {r['why']}" for r in self.refused)]

    assert needs_repair(Parsed(errors=["no JSON object"]))
    assert needs_repair(Parsed(invalid=["bad stance"]))
    assert needs_repair(Parsed(refused=[{"field": "signoff", "why": "must be true or false"}]))
    assert not needs_repair(Parsed())


# --------------------------------------------------------------- The explain pass


class Reader:
    """Off-cycle stdin: scripted lines, then nothing."""

    tty = True

    def __init__(self, lines=("explain H1",)):
        self.lines = list(lines)

    def read_nowait(self):
        return self.lines.pop(0) if self.lines else None


def explain_lane(replies):
    """The `explain` lane, with Agent1 holding the one open question and `explain H1` typed; `replies` are the seat's."""
    from mech_council.run import _messenger

    board, members, console, _council = lanes(script={"seat1-one": replies})
    docket = Docket(mode="review", goal="g", roster=["Agent1", "Agent2"])
    escalations.raise_question(docket, text="which tenant?", asker="Agent1", item_id=None, blocking=False, round_=1)
    return members, console, Checkpoint(docket, Reader(), console=console, names={"Agent1": "one"}, ask_seat=_messenger(board))


def message_lane(replies, lines=("one: why?",)):
    """The message lane: `lines` typed off-cycle, `replies` the named seat's; Agent1 is named `one`, the chair `gavel`."""
    from mech_council.run import _messenger

    board, members, console, _council = lanes(script={"seat1-one": replies})
    docket = Docket(mode="review", goal="g", roster=["Agent1", "Agent2"])
    checkpoint = Checkpoint(docket, Reader(lines), console=console, names={"Agent1": "one", "chair": "gavel"}, ask_seat=_messenger(board))
    return board, members, console, checkpoint


def test_an_explain_ask_costs_one_prompt_and_answers_in_the_seat_s_own_words():
    """`explain H1` asks for prose, so the lane must not parse a verdict out of it: one prompt, no repair ladder."""
    said = "keep the tenant key:\n\nbecause the cache is indexed by it"
    members, console, cp = explain_lane([{"text": said}])
    result = run(cp.drain())

    assert len(members.members["seat1-one"].rpc.calls) == 1, "the seat is asked once"
    assert result.recommendations == {"H1": "keep the tenant key: because the cache is indexed by it"}, "the reply, in one line"
    assert not [line for line in console.lines if "refused" in line or "rejected" in line], "prose is not a refused answer"


def test_an_explain_asked_of_a_dead_seat_costs_the_answer_and_not_the_run():
    """The one ask a human asks for themselves cannot end the run: the dead seat is reported, the question stays open."""
    members, console, cp = explain_lane([GONE, GONE])
    result = run(cp.drain())

    assert members.members["seat1-one"].restarts == 1, "the lane took its one restart and one re-ask before giving up"
    assert not result.recommendations
    assert any("still yours to answer or skip" in line for line in console.lines)


# --------------------------------------------------------------- The message lane


def test_a_message_costs_one_prompt_and_the_seat_answers_in_its_own_words():
    """A direct message asks for prose, so the lane must not parse a verdict out of it: one prompt, no repair ladder."""
    said = "it is bounded:\n\n10k entries, oldest drops"
    _board, members, console, cp = message_lane([{"text": said}])
    run(cp.drain())

    assert len(members.members["seat1-one"].rpc.calls) == 1, "the seat is asked once"
    record = cp.docket.human_messages[0]
    assert record.to == "Agent1" and record.reply == said, "the reply, whole, on the record"
    assert not [line for line in console.lines if "refused" in line or "rejected" in line], "prose is not a refused answer"
    assert any("replies:" in line for line in console.lines)


def test_a_message_to_the_chair_goes_through_the_chair_lane_not_the_pool():
    board, members, _console, cp = message_lane([], lines=("gavel: why did you merge those?",))
    members.members["chair"].rpc = FakeRpc([{"text": "they were the same defect"}])
    run(cp.drain())

    assert len(members.members["chair"].rpc.calls) == 1, "the chair takes its own slot"
    assert not members.members["seat1-one"].rpc.calls and not members.members["seat2-two"].rpc.calls
    assert cp.docket.human_messages[0].to == "chair"
    assert members.members["chair"].rpc.timeouts == [board.council.chair_timeout_sec], "the chair's message takes the chair's budget"


def test_a_message_asked_of_a_dead_seat_costs_the_answer_and_not_the_run():
    _board, members, console, cp = message_lane([GONE, GONE])
    run(cp.drain())

    assert members.members["seat1-one"].restarts == 1, "the lane took its one restart and one re-ask before giving up"
    assert cp.docket.human_messages[0].failure and not cp.docket.human_messages[0].reply
    assert any("did not reach" in line for line in console.lines)


def test_a_message_to_a_seat_that_is_mid_ask_is_refused_not_restarted():
    """A seat mid-ask cannot take a second prompt; restarting it to deliver a message would lose the round ask in flight."""
    board, members, console, cp = message_lane([{"text": "never asked"}])
    board.in_flight["one"] = "seat1-one"
    run(cp.drain())

    assert not members.members["seat1-one"].rpc.calls and members.members["seat1-one"].restarts == 0
    assert cp.docket.human_messages[0].failure
    assert any("mid-ask" in line for line in console.lines)


# -------------------------------------------------- The restart-and-re-ask path


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
    assert str(failure).startswith("one failed (round 4)"), "the member by name, the round it was lost in"
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
    from mech_council.pi_rpc import ChildGone

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
    assert restarts == 1, "one restart, not a retry loop"
    assert not outcome.data.invalid and not outcome.data.errors, "the restarted attempt's reply parsed"
    assert outcome.data.data["signoff"] is True, "and it carries the verdict the seat sent, not an empty outcome"


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
    assert not outcomes["Agent2"].data.invalid and outcomes["Agent2"].data.data["signoff"] is True, (
        "the surviving seat got its own answer back whole"
    )


def test_a_restart_mid_repair_re_asks_the_full_prompt_not_the_fragment():
    """A restarted seat has no session: a re-asked repair fragment would reach a process that never saw the
    round, so the re-ask carries the round prompt instead."""
    original = "the round prompt, docket included"

    async def main():
        board, members, _console, _ = lanes(script={"seat1-one": [{"text": "prose, no JSON"}, GONE, {"text": verdict()}]})
        parse_kw = {"mode": "review", "me": "seat1-one", "known_items": set()}
        outcome = await board.ask_agent("Agent1", original, round_=3, kind="verdict", parse_kw=parse_kw, phase="round")
        calls = members.members["seat1-one"].rpc.calls
        return outcome, calls, members.members["seat1-one"].restarts

    outcome, calls, restarts = run(main())
    assert restarts == 1
    assert calls[0] == original
    assert calls[1] != calls[0], "the live seat's repair stays a fragment"
    assert calls[2] == calls[0], "the re-ask after the restart re-delivers the full prompt, not the fragment"
    assert outcome.data is not None and outcome.repairs == 1


def test_a_fragment_ask_restarted_gets_its_context_not_the_fragment():
    """A gap or defense ask continues the round prompt: a seat restarted mid-fragment is re-asked the round,
    so a fresh process cannot answer a pass it was never shown."""
    fragment = "you were shown these open items: F1, F2"
    context = "the round prompt, docket included"

    async def main():
        board, members, _console, _ = lanes(script={"seat1-one": [GONE, {"text": verdict()}]})
        parse_kw = {"mode": "review", "me": "seat1-one", "known_items": set()}
        outcome = await board.ask_agent("Agent1", fragment, round_=3, kind="verdict", parse_kw=parse_kw, phase="gap", context=context)
        calls = members.members["seat1-one"].rpc.calls
        return outcome, calls, members.members["seat1-one"].restarts

    outcome, calls, restarts = run(main())
    assert restarts == 1
    assert calls == [fragment, context], "the re-ask after the restart is the context, not the fragment"
    assert outcome.data is not None


# ----------------------------------------------------------------- The chair's watchdog


def chair_board(script, *, watchdog=1):
    board, members, console, council_ = lanes(watchdog=watchdog, script=script, script_defaults=True)
    brief = Brief(mode="review", project="/proj")
    chair = Chair(council_, board, console, brief)
    return board, members, console, Watchdog(council_, board, chair, console)


def test_the_chair_is_told_the_goal_its_pass_serves():
    """Both chair passes get the council's goal, which is what they judge against."""

    async def main():
        board, members, console, council_ = lanes(script={"chair": [{"text": '{"groups": [], "human_input": []}'}]})
        council_.goal = "ship the migration"
        chair = Chair(council_, board, console, Brief(mode="review", project="/proj"))
        await chair.merge("### F1 the claim", round_=0)
        council_.mode = "plan"
        members.members["chair"].rpc.script.append({"text": '{"agreed": [], "conflicts": [], "uncovered": [], "human_input": []}'})
        return await chair.compare("Agent1: land it in two steps", round_=0), members.members["chair"].rpc.calls

    _data, calls = run(main())
    assert len(calls) == 2, f"one ask per pass, no repair re-asks: {len(calls)}"
    for ask in calls:
        assert "Goal: ship the migration" in ask, f"the chair is told what the council was convened for: {ask[:200]}"


def test_an_unreadable_pass_restarts_the_chair_under_its_own_file_base():
    """A borrowed crew's chair sits under a crew-suffixed base: the unreadable-pass repair restarts
    that base, not the run's `chair`, which belongs to a different crew."""

    async def main():
        SPEC["planner-chair"] = MemberSpec(name="plan gavel", file_base="planner-chair")
        garble = {"text": "the chair wrote prose instead of JSON"}
        good = {"text": '{"question": "which way?", "context": "a split held two rounds"}'}
        members = FakeMembers({"planner-chair": FakeMember("planner-chair", None, FakeRpc([garble, garble, garble, good]))})
        council_ = council(n=0)
        council_.chair = SPEC["planner-chair"]
        console = Console()
        board = Lanes(council_, cast(Members, members), cast(RunConsole, console))
        chair = Chair(council_, board, cast(RunConsole, console), Brief(mode="plan", project="/proj"))
        data = await chair.direction("### Agent1\n- Agent1, backing `x`:\nthe plan text", round_=1)
        return data, members, console

    data, members, console = run(main())
    assert data == {"question": "which way?", "context": "a split held two rounds"}
    assert members.members["planner-chair"].restarts == 1, "the crew's own chair is the one restarted"
    assert any(e["type"] == "chair_unreadable" for e in console.events)


def test_a_lost_chair_fails_the_pass_like_an_unreadable_one():
    """A lost chair raises `ChairUnusable`, not `AskLost`: the port turns only the former into its `Abort`."""

    async def main():
        board, _members, _console, council_ = lanes(script={"chair": [GONE, GONE]})
        chair = Chair(council_, board, _console, Brief(mode="review", project="/proj"))
        return await chair.merge("### F1 the claim", round_=0)

    with pytest.raises(ChairUnusable) as raised:
        run(main())
    assert raised.value.phase == "merge" and raised.value.round == 0
    assert "restart and one re-ask" in str(raised.value), "the loss is still reported as such"


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


def test_an_action_the_chair_invented_keeps_the_seat_not_restarting_it():
    """A restart wipes the seat's session, so only a spelled-out `restart` may spend it."""

    async def main():
        _board, members, console, watchdog = chair_board({})
        await watchdog.enforce({"decisions": [{"agent": "Agent1", "action": "reboot", "reason": "?"}]})
        return members.members["seat1-one"].restarts, console

    restarts, console = run(main())
    assert restarts == 0, "the invented action is not a restart"
    assert any(e["type"] == "watchdog_unknown_action" for e in console.events)


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
    assert task is not None and task.done(), "the tick loop is finished, not left running behind the exit"
