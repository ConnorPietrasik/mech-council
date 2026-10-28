"""The checkpoint: what prints, what waits, and what a line means."""

from __future__ import annotations

import asyncio

import pytest

from mech_council.deliberation import escalations, state
from mech_council.humaninput import Checkpoint, parse_line, render_ask_set
from mech_council.humanio import EOF

NAMES = {"Agent1": "Metic", "Agent2": "Senior", "chair": "Gavel"}


def build(n=3):
    return state.Docket(mode="review", goal="g", roster=[f"Agent{i}" for i in range(1, n + 1)])


# --------------------------------------------------------------------------- the line grammar


@pytest.mark.parametrize(
    "text, kind, qid, body, ids",
    [
        ("H1: the tenant is fixed", "answer", "H1", "the tenant is fixed", ()),
        ("h2 = later", "answer", "H2", "later", ()),
        ("explain H1", "explain", "H1", "Why are you asking this?", ()),
        ("ask H2: what breaks if we don't?", "explain", "H2", "what breaks if we don't?", ()),
        ("skip", "skip", "", "skip", ()),
        ("skip all", "skip", "", "skip all", ()),
        ("none", "skip", "", "none", ()),
        ("skip H2", "skip", "", "skip H2", ("H2",)),
        ("skip h2", "skip", "", "skip h2", ("H2",)),
        ("skip H1, h2", "skip", "", "skip H1, h2", ("H1", "H2")),
        ("skip H1 H2", "skip", "", "skip H1 H2", ("H1", "H2")),
        ("", "end", "", "", ()),
        ("   ", "end", "", "", ()),
    ],
)
def test_the_line_grammar(text, kind, qid, body, ids):
    line = parse_line(text)
    assert (line.kind, line.id, line.text, line.ids) == (kind, qid, body, ids)


def test_a_bare_line_answers_the_first_open_question_rather_than_being_dropped():
    line = parse_line("the tenant is fixed by contract")
    assert line.kind == "answer" and line.id == "" and line.text


@pytest.mark.parametrize("text", ["skip the cache one", "skip H1 and H2", "skip all H1", "none of the above", "explain H1 why?"])
def test_a_line_that_opened_like_a_command_gets_the_grammar_and_is_never_an_answer(text):
    """A near miss must not be recorded as an authoritative answer to whichever question is open first."""
    line = parse_line(text)
    assert line.kind == "error" and "`skip H1` declines one" in line.text


def test_an_answer_containing_a_colon_is_still_one_answer():
    assert parse_line("H1: use X: because Y").text == "use X: because Y"


# --------------------------------------------------------------------------- the menu


def offered(docket, *options, text="which tenant?", **kw):
    question = escalations.raise_question(docket, text=text, asker="Agent1", item_id="F1", blocking=True, round_=1, options=list(options))[
        0
    ]
    return question


def test_the_menu_prints_numbered_and_the_help_line_says_a_number_takes_one():
    docket = build()
    question = offered(docket, "key it on the tenant", "drop the cache")
    question.context = "no caller passes a tenant id"
    text = render_ask_set(docket, [question], names=NAMES, waiting=True)
    assert "context: no caller passes a tenant id" in text
    assert "1) key it on the tenant" in text and "2) drop the cache" in text
    assert "and nothing else" in text and "qualifier, answer in your own words" in text and "`skip H1` just one" in text


def test_a_number_takes_the_option_verbatim_and_the_selection_is_recorded():
    docket = build()
    question = offered(docket, "key it on the tenant", "drop the cache")
    cp, console, _reader = make_checkpoint(docket, lines=["H1: 2"])
    outcome = asyncio.run(cp.run([question], ask_set=[question], round_=1, terminal=True, waiting=True))
    assert outcome.answers == {"H1": "drop the cache"}, "the answer is the asker's own line, not the digit"
    assert outcome.answer_options == {"H1": 2}
    assert any('"drop the cache" (option 2)' in line for line in console.lines), "the echo proves what the number meant"


def test_a_number_off_the_end_of_the_menu_is_an_answer_of_its_own():
    """`H1: 30` where four options were offered is the human answering `30` — never a selection of nothing."""
    docket = build()
    question = offered(docket, "one", "two")
    cp, _console, _reader = make_checkpoint(docket, lines=["H1: 30"])
    outcome = asyncio.run(cp.run([question], ask_set=[question], round_=1, terminal=True, waiting=True))
    assert outcome.answers == {"H1": "30"} and outcome.answer_options == {}


def test_a_number_with_any_word_after_it_stays_the_human_s_own_words():
    """The number **alone** is the only selection there is."""
    docket = build()
    question = offered(docket, "key it on the tenant", "drop the cache")
    cp, _console, _reader = make_checkpoint(docket, lines=["H1: 1, only for new installs"])
    outcome = asyncio.run(cp.run([question], ask_set=[question], round_=1, terminal=True, waiting=True))
    assert outcome.answers == {"H1": "1, only for new installs"} and outcome.answer_options == {}
    assert outcome.answers["H1"] != question.options[0], "the option was not taken on the human's behalf"


def test_a_bare_number_selects_on_the_first_open_question():
    docket = build()
    question = offered(docket, "one", "two")
    cp, _console, _reader = make_checkpoint(docket, lines=["2"])
    outcome = asyncio.run(cp.run([question], ask_set=[question], round_=1, terminal=True, waiting=True))
    assert outcome.answers == {"H1": "two"} and outcome.answer_options == {"H1": 2}


# --------------------------------------------------------------------------- what prints


def test_the_listing_prints_even_when_nothing_waits():
    docket = build()
    escalations.raise_question(docket, text="what is the retention policy?", asker="Agent2", item_id=None, blocking=False, round_=1)
    text = render_ask_set(docket, list(docket.questions.values()), names=NAMES, waiting=False)
    assert "keeps working" in text and "H1" in text and "asked by Senior" in text
    assert "Agent2" not in text, "the checkpoint is console: it names the asker, the id stays in `human_input.json`"
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


def test_skip_can_name_one_question_and_leave_the_rest_for_an_answer():
    """With a menu on the table you usually can answer two and decline one; `skip` was all-or-nothing."""
    docket = build()
    listing = questions(docket, ("a?", "Agent1", "F1", True), ("b?", "Agent2", "F1", True))
    cp, console, reader = make_checkpoint(docket, lines=["skip H1", "H2: later"])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.skipped == ["H1"] and outcome.answers == {"H2": "later"}
    assert any("H1: declined" in line for line in console.lines)
    assert len(reader.asked) == 2, "the checkpoint kept asking after a one-question decline"


def test_declining_a_question_that_is_not_open_is_corrected_not_swallowed():
    docket = build()
    listing = questions(docket, ("a?", "Agent1", "F1", True))
    cp, console, _reader = make_checkpoint(docket, lines=["skip H9", "skip"])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.skipped == ["H1"]
    assert any("no open question H9" in line for line in console.lines)


def test_skip_can_decline_several_named_questions_at_once():
    """`skip H1 H2` is the shape a human reaches for with three questions on the table, so it is the grammar."""
    docket = build()
    listing = questions(docket, ("a?", "Agent1", "F1", True), ("b?", "Agent2", "F1", True), ("c?", "Agent1", "F1", True))
    cp, _console, reader = make_checkpoint(docket, lines=["skip H1 H3", "H2: later"])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.skipped == ["H1", "H3"] and outcome.answers == {"H2": "later"}
    assert len(reader.asked) == 2


def test_answering_a_question_after_declining_it_takes_it_back():
    """The last line about a question is the human's word on it, and one question gets exactly one of them."""
    docket = build()
    listing = questions(docket, ("a?", "Agent1", "F1", True), ("b?", "Agent2", "F1", True))
    cp, console, reader = make_checkpoint(docket, lines=["skip H1", "H1: keep it", "H2: later"])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.answers == {"H1": "keep it", "H2": "later"}, "the later line is the answer"
    assert outcome.skipped == [], "the decline is gone, not merely contradicted"
    assert not set(outcome.answers) & set(outcome.skipped), "one question, one disposition"
    assert any("the decline you gave is withdrawn" in line for line in console.lines), "the human hears the change of mind land"
    assert len(reader.asked) == 3


def test_declining_a_question_after_answering_it_takes_the_answer_back():
    """The rule runs both ways, or `skip H1` after an answer is the same trap wearing the other coat."""
    docket = build()
    listing = questions(docket, ("a?", "Agent1", "F1", True), ("b?", "Agent2", "F1", True))
    cp, console, _reader = make_checkpoint(docket, lines=["H1: keep it", "skip H1", "H2: later"])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.answers == {"H2": "later"} and outcome.skipped == ["H1"]
    assert not outcome.answer_options, "the withdrawn answer takes any selection it carried with it"
    assert not set(outcome.answers) & set(outcome.skipped)
    assert any("the answer you gave is withdrawn" in line for line in console.lines)


def test_answering_a_question_this_checkpoint_does_not_hold_is_corrected():
    """An answer buys a round, so it has to be for something on the table — a real `H` id is not enough."""
    docket = build()
    settled, open_one = questions(docket, ("settled?", "Agent1", None, False), ("open?", "Agent2", None, False))
    settled.state = state.ANSWERED
    cp, console, _reader = make_checkpoint(docket, lines=["H1: a late second thought", "H2: later"])
    outcome = asyncio.run(cp.run([open_one], ask_set=[open_one], round_=2, terminal=True, waiting=True))
    assert outcome.answers == {"H2": "later"} and not outcome.skipped
    assert any("no open question H1" in line for line in console.lines)


def test_a_second_answer_to_the_same_question_wins_and_drops_the_option_it_took():
    """The last line the human types about their own answer is their answer — and `answer_option` names only that line."""
    docket = build()
    listing = questions(docket, ("a?", "Agent1", "F1", True), ("b?", "Agent2", "F1", True))
    question = offered(docket, "key it on the tenant", "drop the cache", text="a?")
    assert question is listing[0], "the menu belongs to the question the human answers first"
    cp, console, _reader = make_checkpoint(docket, lines=["H1: 2", "H1: keep it, but only for new installs", "H2: later"])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.answers == {"H1": "keep it, but only for new installs", "H2": "later"}, "the second line is the answer"
    assert outcome.answer_options == {}, "an answer in the human's own words took no option"
    assert any('H1: answered "drop the cache" (option 2)' in line for line in console.lines)


def test_a_near_miss_command_is_corrected_and_the_checkpoint_keeps_waiting():
    """`skip the cache one` declines nothing and, above all, answers nothing; the human is told the grammar instead."""
    docket = build()
    listing = questions(docket, ("a?", "Agent1", "F1", True))
    cp, console, reader = make_checkpoint(docket, lines=["skip the cache one", "H1: keep it"])
    outcome = asyncio.run(cp.run(listing, ask_set=listing, round_=1, terminal=True, waiting=True))
    assert outcome.answers == {"H1": "keep it"} and outcome.skipped == []
    assert any("`skip H1` declines one" in line for line in console.lines)
    assert len(reader.asked) == 2, "the near miss cost a retype, not an answer the human never gave"


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
