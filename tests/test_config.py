"""The config loader and selection.

The config loader's validation list is the case list, one assertion per rule
with the message it owes; precedence and comments are the only additions.
"""

from __future__ import annotations

import os

import pytest

from council import config as config_mod
from council.cli import Args, ConfigError, UsageError

REFERENCE_CONFIG = '''# council.toml — the reference config, taken verbatim by the loader.

[defaults]
max_rounds = 6
watchdog_sec = 600
max_concurrency = 3
sandbox_mib = 32768

[councils.review]
mode = "review"
goal = "See if there is anything here that should be fixed or improved."

[[councils.review.agents]]
name = "Metic"
provider = "omnissiah"
model = "Qwen-Flash-Next"
persona = "You hunt correctness bugs. Verify every claim before you report it."

[[councils.review.agents]]
name = "Senior"
provider = "omnissiah"
model = "Qwen-Flash-Next"
persona = "Smallest change that satisfies the goal; commit to a decision, never a menu."

[[councils.review.agents]]
name = "Coworker"
persona = "You inherit this in eighteen months: flag migrations, shims and config drift."

[councils.review.chair]
name = "Chair"
provider = "omnissiah"
model = "Qwen-Flash-Next"
persona = """
You deduplicate findings and never review code yourself. Be conservative: merge only
findings that describe the same underlying defect.
"""

[councils.review-staged]
mode = "review"
goal = "Another agent staged these changes; did they do a good job? (`git diff --cached`)"

[[councils.review-staged.agents]]
name = "Metic"
provider = "omnissiah"
model = "Qwen-Flash-Next"

[[councils.review-staged.agents]]
name = "Coworker"

[councils.review-staged.chair]
name = "Chair"

[councils.plan]
mode = "plan"
goal = "Make a plan for the implementation of the next TODO item"
max_rounds = 3

[[councils.plan.agents]]
name = "Metic"
provider = "omnissiah"
model = "Qwen-Flash-Next"
persona = """
You are a meticulous planner.
"""

[[councils.plan.agents]]
name = "Senior"

[councils.plan.chair]
name = "Chair"
persona = """
You are the chair of a planning council.
"""
'''


def write(tmp_path, text: str) -> str:
    path = os.path.join(str(tmp_path), "council.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def convene(tmp_path, text: str, name: str = "review", **kw):
    path = write(tmp_path, text)
    loaded = config_mod.load_config(path)
    chosen = config_mod.select_council(loaded, name)
    return config_mod.convene(loaded, chosen, Args(**kw), str(tmp_path))


BODY = """\
[defaults]
pi_cli = "{pi}"

[councils.review]
goal = "g"

[councils.review.chair]
name = "Chair"

[[councils.review.agents]]
name = "A"

[[councils.review.agents]]
name = "B"
"""


def body(**replacements) -> str:
    """The smallest convenable council, with one line added or edited per case."""
    text = BODY.format(pi=os.path.abspath(os.path.join(os.path.dirname(__file__), "support", "fake_pi.py")))
    for find, with_ in replacements.items():
        assert find in text, find
        text = text.replace(find, with_)
    return text


# --------------------------------------------------------------------------- the reference config


def test_the_reference_config_loads_and_convenes_every_council(tmp_path):
    path = write(tmp_path, REFERENCE_CONFIG)
    loaded = config_mod.load_config(path)
    assert loaded.names() == ["review", "review-staged", "plan"]
    review = config_mod.convene(loaded, "review", Args(), str(tmp_path))
    assert [a.name for a in review.agents] == ["Metic", "Senior", "Coworker"]
    assert review.chair.provider == "omnissiah"
    assert review.max_rounds == 6 and review.max_concurrency == 3
    staged = config_mod.convene(loaded, "review-staged", Args(), str(tmp_path))
    assert staged.max_rounds == 6, "[defaults] reaches a council stating nothing"
    plan = config_mod.convene(loaded, "plan", Args(), str(tmp_path))
    assert plan.max_rounds == 3, "a council key beats [defaults]"
    assert plan.mode == "plan" and plan.agents[1].persona is None


def test_convening_assigns_ids_slots_and_file_bases(tmp_path):
    council = convene(tmp_path, body())
    assert [a.id for a in council.agents] == ["Agent1", "Agent2"]
    assert [a.slot for a in council.agents] == [1, 2]
    assert [a.file_base for a in council.agents] == ["slot1-a", "slot2-b"]
    assert council.chair.id is None and council.chair.file_base == "chair"
    assert council.member_file_bases == ["slot1-a", "slot2-b", "chair"]
    assert council.adapter.item_noun_plural == "findings"


# -------------------------------------------------- The config loader's validation list


@pytest.mark.parametrize(
    "text, wanted",
    [
        (body().replace('[[councils.review.agents]]\nname = "B"\n', ""), "convenes 1 agent(s)"),
        (body().replace('name = "A"', 'name = "B"', 1), "lists the agent name 'B' twice"),
        (body().replace('name = "B"', 'name = "Chair"', 1), "for both a seat and the chair"),
        (body().replace("[councils.review.chair]", "[[councils.review.chair]]"), "the chair is the single table"),
        (body().replace('[councils.review.chair]\nname = "Chair"\n', ""), "has no [councils.review.chair]"),
        (body().replace('goal = "g"', 'goal = "g"\nmode = "planning"', 1), "did you mean 'plan'"),
        (body().replace('goal = "g"', 'goal = "g"\nmax_rounds = 0', 1), "must be ≥ 1"),
        (body().replace('goal = "g"', 'goal = "g"\nmax_rounds = "10"', 1), "wants an integer, got str"),
        (body().replace('goal = "g"', 'goal = "g"\nsandbox_mib = 0', 1), "must be ≥ 1"),
        (body().replace('goal = "g"', 'goal = "g"\ntranscripts = "everything"', 1), "'off' or 'full'"),
        (body().replace('goal = "g"', 'goal = "g"\nwatchdog_sec = -1', 1), "must be ≥ 0"),
        (body().replace('name = "A"', 'name = "A"\nprovider = "p"', 1), "states 'provider' without 'model'"),
        (body().replace('name = "A"', 'name = ""', 1), "needs a non-empty 'name'"),
        (body().replace('name = "A"', 'name = "A"\nnickname = "x"', 1), "unknown config key 'nickname'"),
        (body().replace('goal = "g"', 'goal = "g"\nmaxRounds = 4', 1), "'maxRounds' in [councils.review] (did you mean max_rounds?"),
        (
            body().replace('goal = "g"', 'goal = "g"\nchairTimeoutSec = 4', 1),
            "'chairTimeoutSec' in [councils.review] (did you mean chair_timeout_sec?",
        ),
        (body().replace("[defaults]", "[defaults]\nmaxrounds = 4"), "unknown config key 'maxrounds'"),
    ],
)
def test_each_validation_rule_says_so(tmp_path, text, wanted):
    with pytest.raises(ConfigError) as raised:
        convene(tmp_path, text)
    assert wanted in str(raised.value), str(raised.value)


def test_a_plan_council_without_a_goal_is_refused(tmp_path):
    text = body().replace('goal = "g"', 'mode = "plan"', 1)
    with pytest.raises(ConfigError, match="plan council with no goal"):
        convene(tmp_path, text)
    # ... and `--goal` is a legitimate way to state it.
    council = convene(tmp_path, text, goal="now it has one")
    assert council.mode == "plan" and council.goal == "now it has one"


def test_file_level_rules(tmp_path):
    path = write(tmp_path, "# nothing but a comment\n")
    with pytest.raises(ConfigError, match="declares no councils"):
        config_mod.load_config(path)
    path = write(tmp_path, body() + '\n[councils."  "]\n')
    with pytest.raises(ConfigError, match="must not be empty"):
        config_mod.load_config(path)
    with pytest.raises(ConfigError, match="the config is TOML"):
        config_mod.load_config("/tmp/whatever.json")
    with pytest.raises(ConfigError, match="no config file"):
        config_mod.load_config(os.path.join(str(tmp_path), "missing.toml"))


def test_syntax_errors_keep_line_and_column(tmp_path):
    path = write(tmp_path, "[councils.review\n")
    with pytest.raises(ConfigError, match=r"line .*column"):
        config_mod.load_config(path)


def test_duplicate_council_is_a_parse_error_not_last_wins(tmp_path):
    text = REFERENCE_CONFIG + '\n[councils.plan]\ngoal = "again"\n'
    with pytest.raises(ConfigError, match="not valid TOML"):
        config_mod.load_config(write(tmp_path, text))


def test_comments_are_comments(tmp_path):
    text = body().replace('goal = "g"', 'goal = "a # keeps working"  # and this one ends here')
    council = convene(tmp_path, text)
    assert council.goal == "a # keeps working"


# --------------------------------------------------------------------------- selection


def test_selection_is_exact_or_the_only_one(tmp_path):
    path = write(tmp_path, REFERENCE_CONFIG)
    loaded = config_mod.load_config(path)
    assert config_mod.select_council(loaded, "review-staged") == "review-staged"
    with pytest.raises(UsageError, match="did you mean review-staged"):
        config_mod.select_council(loaded, "review-stagwd")
    with pytest.raises(UsageError, match="name the council"):
        config_mod.select_council(loaded, None)
    single = config_mod.load_config(write(tmp_path, body()))
    assert config_mod.select_council(single, None) == "review"


# --------------------------------------------------------------------------- pi discovery


def test_pi_cli_resolution(tmp_path):
    fake = os.path.join(os.path.dirname(__file__), "support", "fake_pi.py")
    resolved, version = config_mod.resolve_pi(fake, str(tmp_path))
    assert resolved == os.path.realpath(fake) and "fake-pi" in version
    with pytest.raises(ConfigError, match="not an executable file"):
        config_mod.resolve_pi(os.path.join(str(tmp_path), "nope"), str(tmp_path))
    with pytest.raises(ConfigError, match="not on PATH"):
        config_mod.resolve_pi("definitely-not-a-cli-here", str(tmp_path))


def test_an_unresolvable_pi_leaves_nothing_behind(tmp_path, canary):
    """The startup ordering: a config-stage failure creates no run directory anywhere."""
    text = body().replace(os.path.join(os.path.dirname(__file__), "support", "fake_pi.py"), "no-such-pi-cli")
    with pytest.raises(ConfigError):
        convene(tmp_path, text)
    assert os.listdir(canary) == []
