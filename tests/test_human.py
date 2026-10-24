"""The checkpoint: what prints, what waits, and what a line means.

The human checkpoint's are load-bearing and easy to get subtly wrong: the listing always
prints, only the ask set waits, a deferred question never holds the run, `explain` is advice
and never authority, and EOF declines everything rather than parking the run forever.
"""

from __future__ import annotations

import asyncio

import pytest

from council.deliberation import escalations, state
from council.humaninput import Checkpoint, parse_line, render_ask_set
from council.humanio import EOF

NAMES = {"Agent1": "Metic", "Agent2": "Senior", "chair": "Gavel"}


def build(n=3):
    return state.Docket(mode="review", goal="g", roster=[f"Agent{i}" for i in range(1, n + 1)])


def item(docket, id_, claim="a call", severity="major"):
    docket.add_item(state.Item(id=id_, claim=claim, severity=severity, raised_by=["Agent1"], opened_round=0))


# --------------------------------------------------------------------------- the line grammar


@pytest.mark.parametrize(
    "text, kind, qid, body",
    [
        ("H1: the tenant is fixed", "answer", "H1", "the tenant is fixed"),
        ("h2 = later", "answer", "H2", "later"),
        ("explain H1", "explain", "H1", "Why are you asking this?"),
        ("ask H2: what breaks if we don't?", "explain", "H2", "what breaks if we don't?"),
        ("skip", "skip", "", "skip"),
        ("skip all", "skip", "", "skip all"),
        ("none", "skip", "", "none"),
        ("", "end", "", ""),
        ("   ", "end", "", ""),
    ],
)
def test_the_line_grammar(text, kind, qid, body):
    line = parse_line(text)
    assert (line.kind, line.id, line.text) == (kind, qid, body)


def test_a_bare_line_answers_the_first_open_question_rather_than_being_dropped():
    line = parse_line("the tenant is fixed by contract")
    assert line.kind == "answer" and line.id == "" and line.text


def test_an_answer_containing_a_colon_is_still_one_answer():
    assert parse_line("H1: use X: because Y").text == "use X: because Y"


# --------------------------------------------------------------------------- what prints


def test_the_listing_prints_even_when_nothing_waits():
    docket = build()
    escalations.raise_question(docket, text="what is the retention policy?", asker="Agent2", item_id=None, blocking=False, round_=1)
    text = render_ask_set(docket, list(docket.questions.values()), names=NAMES, waiting=False)
    assert "keeps working" in text and "H1" in text and "Senior (Agent2)" in text
    assert "retention policy" in text


def test_an_empty_checkpoint_says_so_and_waits_for_nothing():
    docket = build()
    assert "no question for you" in render_ask_set(docket, [], names=NAMES, waiting=False)


# --------------------------------------------------------------------------- the wait rule


class Reader:
    """A scripted stdin: lines, then EOF; `interactive` is the tty question."""

    def __init__(self, lines=(), interactive=True):
        self.lines = list(lines)
        self.interactive = interactive
        self.eof = False
        self.asked: list[str] = []

    async def next_line(self):
        self.asked.append("line")
        if not self.lines:
            return EOF
        return self.lines.pop(0)


class Console:
    def __init__(self):
        self.lines: list[str] = []

    def say(self, text, *args):
        self.lines.append(text % args if args else text)


def make_checkpoint(docket, lines=(), interactive=True, ask_seat=None):
    console = Console()
    reader = Reader(lines, interactive)
    return Checkpoint(docket, reader, console=console, names=NAMES, ask_seat=ask_seat), console, reader


def questions(docket, *specs):
    """specs: (text, asker, item, blocking)"""
    return [
        escalations.raise_question(docket, text=text, asker=asker, item_id=item_id, blocking=blocking, round_=1)[0]
        for text, asker, item_id, blocking in specs
    ]


def test_a_deferred_question_prints_and_the_run_never_waits():
    docket = build()
    listing = questions(docket, ("what is the retention policy?", "Agent2", None, False))
    cp, console, reader = make_checkpoint(docket)
    outcome = asyncio.run(cp.run(listing, ask_set=[], round_=1, terminal=False, waiting=False))
    assert outcome.waited is False
    assert any("keeps working" in line for line in console.lines)
    assert reader.asked == [], "not one line was read"


def test_the_ask_set_waits_and_an_answer_is_applied():
    docket = build()
    listing = questions(docket, ("which tenant?", "Agent1", "F1", True))
    cp, _console, _reader = make_checkpoint(docket, lines=["H1: the fixed one"])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.waited and outcome.answers == {"H1": "the fixed one"}


def test_a_line_without_an_id_answers_the_first_still_open_question():
    docket = build()
    listing = questions(docket, ("first?", "Agent1", "F1", True), ("second?", "Agent2", "F1", True))
    cp, _console, _reader = make_checkpoint(docket, lines=["the answer"])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.answers == {"H1": "the answer"}


def test_a_skip_declines_everything_still_open_and_the_run_moves_on():
    docket = build()
    listing = questions(docket, ("which tenant?", "Agent1", "F1", True))
    cp, _console, _reader = make_checkpoint(docket, lines=["skip"])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.skipped == ["H1"] and not outcome.answers


def test_a_blank_line_ends_the_checkpoint_without_answering_or_declining():
    docket = build()
    listing = questions(docket, ("which tenant?", "Agent1", "F1", True))
    cp, _console, _reader = make_checkpoint(docket, lines=[""])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert not outcome.answers and not outcome.skipped


def test_eof_declines_the_whole_ask_set_instead_of_parking_the_run():
    """The non-interactive stdin choice: a run with no stdin must not stop for a person who is not there."""
    docket = build()
    listing = questions(docket, ("a?", "Agent1", "F1", True), ("b?", "Agent2", "F1", True))
    cp, _console, _reader = make_checkpoint(docket, lines=[])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.skipped == ["H1", "H2"]


def test_a_non_interactive_run_declines_the_ask_set_and_says_so_once():
    docket = build()
    listing = questions(docket, ("a?", "Agent1", "F1", True))
    cp, console, reader = make_checkpoint(docket, interactive=False)
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=2, terminal=True, waiting=True))
    assert outcome.skipped == ["H1"]
    assert any("not interactive" in line for line in console.lines)
    assert reader.asked == []


def test_an_unknown_id_is_corrected_not_swallowed():
    docket = build()
    listing = questions(docket, ("which tenant?", "Agent1", "F1", True))
    cp, console, _reader = make_checkpoint(docket, lines=["H9: nope", "H1: yes"])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.answers == {"H1": "yes"}
    assert any("no question H9" in line for line in console.lines)


# --------------------------------------------------------------------------- explain


def test_explain_asks_the_asker_and_records_a_recommendation_not_an_answer():
    """The recommendation is advice; only the human's own line is authoritative."""
    docket = build()
    listing = questions(docket, ("which tenant?", "Agent1", "F1", True))
    calls: list[tuple[str, str]] = []

    class Reply:
        def __init__(self, text):
            self.text = text

    async def ask_seat(agent, text, *, round_, phase):
        calls.append((agent, text))
        return Reply("because the tenant key is what the cache is indexed by")

    cp, console, _reader = make_checkpoint(docket, lines=["explain H1", ""], ask_seat=ask_seat)
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert calls and calls[0][0] == "Agent1"
    assert "H1" in outcome.recommendations and not outcome.answers
    assert any("recommendation" in line.lower() for line in console.lines)


def test_explain_of_a_chair_raised_question_asks_no_one():
    """The chair holds no roster ID and never receives a deliberation ask."""
    docket = build()
    listing = questions(docket, ("what should we optimise for?", "chair", None, True))
    calls = []

    async def ask_seat(agent, text, *, round_, phase):
        calls.append(agent)

    cp, console, _reader = make_checkpoint(docket, lines=["explain H1", ""], ask_seat=ask_seat)
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert calls == [] and not outcome.recommendations
    assert any("never deliberates" in line for line in console.lines)


def test_explain_without_a_channel_says_so_and_keeps_waiting():
    docket = build()
    listing = questions(docket, ("which tenant?", "Agent1", "F1", True))
    cp, _console, _reader = make_checkpoint(docket, lines=["explain H1", ""])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert not outcome.recommendations and not outcome.answers
