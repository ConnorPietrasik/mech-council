"""The config loader and selection."""

from __future__ import annotations

import os
import shutil

import pytest

from mech_council import config as config_mod
from mech_council import suggest
from mech_council.cli import Args, ConfigError, UsageError

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

[councils.review-staged]                      # the same council, a narrower scope
extends = "review"
goal = "Another agent staged these changes; did they do a good job? (`git diff --cached`)"

[councils.review-staged.chair]
persona = """
You chair a review of changes that are staged and not yet committed.
"""

[councils.review-fast]                        # two of the three seats, same personas
extends = "review"
goal = "A fast pass: the defects you would fix today."
max_rounds = 3

[[councils.review-fast.agents]]
name = "Coworker"
drop = true

[[councils.review-fast.agents]]
name = "Metic"
persona = "One line per finding: the file, the line, the defect."

# A template: a `_` name is there to be extended, never convened, and never convenes by itself.
[councils._planners]

[[councils._planners.agents]]
name = "Metic"
provider = "omnissiah"
model = "Qwen-Flash-Next"
persona = """
You are a meticulous planner.
"""

[[councils._planners.agents]]
name = "Senior"

[councils._planners.chair]
name = "Chair"
persona = """
You are the chair of a planning council.
"""

[councils.plan]                               # a template's roster, its own goal and limit
extends = "_planners"
mode = "plan"
goal = "Make a plan for the implementation of the next TODO item"
max_rounds = 3
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


PI = os.path.abspath(os.path.join(os.path.dirname(__file__), "support", "fake_pi.py"))

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
    text = BODY.format(pi=PI)
    for find, with_ in replacements.items():
        assert find in text, find
        text = text.replace(find, with_)
    return text


# --------------------------------------------------------------------------- the reference config


def test_the_reference_config_loads_and_convenes_every_council(tmp_path):
    path = write(tmp_path, REFERENCE_CONFIG)
    loaded = config_mod.load_config(path)
    assert loaded.names() == ["review", "review-staged", "review-fast", "plan"], "a `_` template is not a council you convene"
    review = config_mod.convene(loaded, "review", Args(), str(tmp_path))
    assert [a.name for a in review.agents] == ["Metic", "Senior", "Coworker"]
    assert review.chair.provider == "omnissiah"
    assert review.max_rounds == 6 and review.max_concurrency == 3
    assert review.in_place is False, "the reference council states no sandbox decision and takes the built-in"
    assert review.extends == []
    staged = config_mod.convene(loaded, "review-staged", Args(), str(tmp_path))
    assert staged.extends == ["review"]
    assert [(a.name, a.provider, a.persona) for a in staged.agents] == [(a.name, a.provider, a.persona) for a in review.agents]
    assert staged.mode == "review", "the mode is inherited with everything else"
    assert staged.goal == "Another agent staged these changes; did they do a good job? (`git diff --cached`)"
    assert review.goal == "See if there is anything here that should be fixed or improved."
    assert staged.chair.persona == "You chair a review of changes that are staged and not yet committed.\n"
    assert staged.chair.model == review.chair.model, "a chair patch replaces the keys it states"
    assert staged.max_rounds == 6, "[defaults] reaches a council stating nothing, inherited or not"
    fast = config_mod.convene(loaded, "review-fast", Args(), str(tmp_path))
    assert [a.name for a in fast.agents] == ["Metic", "Senior"], "drop retires a seat, the rest keep their order"
    assert fast.agents[0].persona == "One line per finding: the file, the line, the defect."
    assert fast.agents[1].persona == review.agents[1].persona, "an untouched seat is inherited whole"
    assert fast.max_rounds == 3, "a council key beats [defaults]"
    plan = config_mod.convene(loaded, "plan", Args(), str(tmp_path))
    assert plan.extends == ["_planners"] and plan.mode == "plan"
    assert plan.agents[1].persona is None


def test_convening_assigns_ids_slots_and_file_bases(tmp_path):
    council = convene(tmp_path, body())
    assert [a.id for a in council.agents] == ["Agent1", "Agent2"]
    assert [a.slot for a in council.agents] == [1, 2]
    assert [a.file_base for a in council.agents] == ["slot1-a", "slot2-b"]
    assert council.chair.id is None and council.chair.file_base == "chair"
    assert council.member_file_bases == ["slot1-a", "slot2-b", "chair"]
    assert council.adapter.item_noun_plural == "findings"


# --------------------------------------------------------------------------- extends: one council's roster, another council's goal

CHAIN = """\
[defaults]
pi_cli = "{pi}"
max_rounds = 6

[councils._base]                                  # a template: no mode, no goal, convenes nothing
watchdog_sec = 90

[[councils._base.agents]]
name = "A"
persona = "pa"

[[councils._base.agents]]
name = "B"
persona = "pb"

[councils._base.chair]
name = "Chair"
persona = "pc"

[councils.mid]
extends = "_base"
max_rounds = 4

[councils.mid.chair]
name = "Chairman"

[councils.leaf]
extends = "mid"
goal = "g"
"""


def chain(**replacements) -> str:
    """The three-deep chain — template, middle, leaf — with one substitution per case."""
    text = CHAIN.format(pi=PI)
    for find, with_ in replacements.items():
        assert find in text, find
        text = text.replace(find, with_, 1)
    return text


def convene_chain(tmp_path, name, **replacements):
    return convene(tmp_path, chain(**replacements), name)


def test_a_deep_chain_inherits_the_roster_the_knobs_and_the_chair(tmp_path):
    leaf = convene_chain(tmp_path, "leaf")
    assert leaf.extends == ["mid", "_base"], "the chain is recorded, nearest first"
    assert [(a.name, a.persona) for a in leaf.agents] == [("A", "pa"), ("B", "pb")]
    assert leaf.chair.name == "Chairman" and leaf.chair.persona == "pc", "the chair merges key by key"
    assert leaf.watchdog_sec == 90, "a knob stated two hops up still reaches the leaf"
    assert leaf.max_rounds == 4, "an ancestor beats [defaults], and the leaf inherits that"
    assert leaf.mode == "review" and leaf.max_concurrency == 4, "unstated everywhere: [defaults], then the built-in"
    assert convene_chain(tmp_path, "mid").extends == ["_base"]
    # Slots and file bases are assigned after the merge, so an inherited roster needs no bookkeeping.
    assert [a.file_base for a in leaf.agents] == ["slot1-a", "slot2-b"]


def test_flags_still_beat_the_whole_chain(tmp_path):
    leaf = convene(tmp_path, chain(), "leaf", goal="--goal wins", rounds=1, transcripts="full")
    assert (leaf.goal, leaf.max_rounds, leaf.transcripts) == ("--goal wins", 1, "full")


# ------------------------------------------------------------- in place: the knob, and who stated it


def test_in_place_is_a_knob_of_its_own(tmp_path):
    assert convene(tmp_path, body()).in_place is False, "the built-in default is the sandbox"
    assert convene(tmp_path, body().replace("[defaults]\n", "[defaults]\nin_place = true\n")).in_place is True, "[defaults] may ask"
    assert convene(tmp_path, body().replace('goal = "g"', 'goal = "g"\nin_place = true')).in_place is True, "so may a council"


def test_a_template_hands_in_place_down_the_chain(tmp_path):
    asked = {"watchdog_sec = 90": "watchdog_sec = 90\nin_place = true"}
    assert convene_chain(tmp_path, "leaf", **asked).in_place is True, "stated two hops up, and it reaches the leaf"


def test_the_flag_turns_in_place_on_when_no_file_asks(tmp_path):
    assert convene(tmp_path, body(), in_place=True).in_place is True


def test_stated_by_names_the_layer_that_asked_for_in_place(tmp_path):
    """The note that lets an operator see a repo asking to be reviewed unsandboxed reads this, so it answers with
    the file — the highest-precedence one that states the key, `[defaults]` included."""
    user = layer(tmp_path, "user", body().replace("[defaults]\n", "[defaults]\nin_place = true\n"))
    project = layer(tmp_path, "proj", body())
    loaded = config_mod.load_layers([user, project], project_layer=project)
    council = config_mod.convene(loaded, "review", Args(), str(tmp_path))
    assert council.in_place is True, "the project layer never stated it and still inherits it"
    assert config_mod.stated_by(loaded, "review", council.extends, "in_place") == user
    assert config_mod.stated_by(loaded, "review", council.extends, "max_rounds") is None, "nothing states this one"


def test_a_child_patches_the_inherited_seats_by_name(tmp_path):
    text = chain().replace(
        '[councils.leaf]\nextends = "mid"\ngoal = "g"',
        '[councils.leaf]\nextends = "mid"\ngoal = "g"\n\n'
        '[[councils.leaf.agents]]\nname = "B"\npersona = "patched"\n\n'
        '[[councils.leaf.agents]]\nname = "A"\ndrop = true\n\n'
        '[[councils.leaf.agents]]\nname = "C"\npersona = "new seat"\n',
    )
    leaf = convene(tmp_path, text, "leaf")
    assert [(a.name, a.persona) for a in leaf.agents] == [("B", "patched"), ("C", "new seat")], "dropped, patched, appended"


def test_a_drop_that_names_no_inherited_seat_says_so(tmp_path):
    """`drop` is a patch verb: it needs an inherited seat to retire, and it leaves the chair alone."""
    nowhere = chain().replace('goal = "g"', 'goal = "g"\n\n[[councils.leaf.agents]]\nname = "Z"\ndrop = true', 1)
    with pytest.raises(ConfigError, match=r"drops 'Z', which inherits no seat of that name"):
        convene(tmp_path, nowhere, "leaf")
    not_a_bool = chain().replace('goal = "g"', 'goal = "g"\n\n[[councils.leaf.agents]]\nname = "A"\ndrop = 1', 1)
    with pytest.raises(ConfigError, match=r"\[councils\.leaf\.agents\]\[1\]\.drop wants true or false, got int"):
        convene(tmp_path, not_a_bool, "leaf")
    chair = chain().replace(
        '[councils.leaf]\nextends = "mid"\ngoal = "g"',
        '[councils.leaf]\nextends = "mid"\ngoal = "g"\n\n[councils.leaf.chair]\ndrop = true',
    )
    with pytest.raises(ConfigError, match=r"\[councils\.leaf\.chair\] drops the chair"):
        convene(tmp_path, chair, "leaf")


def test_a_patch_states_each_seat_once(tmp_path):
    """A seat is stated once per council; to rebuild an inherited one, `drop` it first and state it again."""

    def patched(*entries: str) -> str:
        head = '[councils.leaf]\nextends = "mid"\ngoal = "g"'
        return chain().replace(head, head + "".join(entries))

    twice = patched(
        '\n\n[[councils.leaf.agents]]\nname = "B"\npersona = "one"\n',
        '\n\n[[councils.leaf.agents]]\nname = "B"\nmodel = "m"\n',
    )
    with pytest.raises(ConfigError, match=r"patches 'B' twice"):
        convene(tmp_path, twice, "leaf")
    rebuilt = patched(
        '\n\n[[councils.leaf.agents]]\nname = "B"\ndrop = true\n',
        '\n\n[[councils.leaf.agents]]\nname = "B"\npersona = "reborn"\n',
    )
    assert [(a.name, a.persona) for a in convene(tmp_path, rebuilt, "leaf").agents] == [("A", "pa"), ("B", "reborn")]


def test_a_drop_that_retires_nothing_is_still_stripped(tmp_path):
    """The `drop` verb never survives the merge, so `drop = false` costs nothing wherever it is written."""
    text = chain().replace('name = "A"\npersona = "pa"', 'name = "A"\npersona = "pa"\ndrop = false')
    for name in ("mid", "leaf"):
        assert [a.name for a in convene(tmp_path, text, name).agents] == ["A", "B"], name


def test_a_template_composes_with_the_council_that_extends_it(tmp_path):
    """A template may state less than a council: the council that inherits it is what has to add up."""
    text = (
        f'[defaults]\npi_cli = "{PI}"\n\n'
        '[councils._half]\n\n[[councils._half.agents]]\nname = "A"\n\n'
        '[councils.half]\nextends = "_half"\ngoal = "g"\n\n'
        '[[councils.half.agents]]\nname = "C"\n\n'
        '[councils.half.chair]\nname = "Chair"\n'
    )
    loaded = config_mod.load_config(write(tmp_path, text))
    half = config_mod.convene(loaded, "half", Args(), str(tmp_path))
    assert [a.name for a in half.agents] == ["A", "C"] and half.chair.name == "Chair"
    assert loaded.names() == ["half"], "a file of templates convenes the one real council in it"
    assert config_mod.select_council(loaded, None) == "half"
    with pytest.raises(UsageError, match="is a template: it is there to be extended, not convened"):
        config_mod.select_council(loaded, "_half")


def test_a_file_of_nothing_but_templates_convenes_nothing(tmp_path):
    """A file of only templates holds no council to convene, and says that rather than offering an empty list."""
    text = f'[defaults]\npi_cli = "{PI}"\n\n[councils._bench]\n\n[[councils._bench.agents]]\nname = "A"\n'
    loaded = config_mod.load_config(write(tmp_path, text))
    assert loaded.names() == []
    for named in (None, "_bench", "bench"):
        with pytest.raises(UsageError, match="states no council to convene") as raised:
            config_mod.select_council(loaded, named)
        assert "template" in str(raised.value), str(raised.value)


def test_only_the_chain_you_asked_for_has_to_be_right(tmp_path):
    """A council nobody extends is not read: the file convenes while another table is nonsense."""
    text = chain() + '\n[councils.sideways]\nmode = "sideways"\nextends = "nowhere"\n\n[councils._unused]\nagents = 3\n'
    assert [a.name for a in convene(tmp_path, text, "leaf").agents] == ["A", "B"]


def test_a_real_council_is_an_ordinary_parent(tmp_path):
    """Extending a council you also convene is the normal case; templates are the optional extra."""
    text = (
        chain()
        .replace('extends = "_base"', 'extends = "review"')
        .replace('[councils.leaf]\nextends = "mid"\ngoal = "g"', '[councils.leaf]\nextends = "mid"')
        .replace(
            "[councils.mid]",
            '[councils.review]\nextends = "_base"\ngoal = "the whole tree"\nmax_rounds = 9\n\n[councils.mid]',
        )
    )
    leaf = convene(tmp_path, text, "leaf")
    assert leaf.extends == ["mid", "review", "_base"]
    assert leaf.goal == "the whole tree", "a real council's goal comes with its roster"
    assert leaf.max_rounds == 4, "and a nearer ancestor still wins"
    review = convene(tmp_path, text, "review")
    assert review.max_rounds == 9 and review.goal == "the whole tree", "the parent is still convenable itself"


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
        (body().replace('goal = "g"', 'goal = "g"\nin_place = "yes"', 1), "wants true or false, got str"),
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
        (
            body().replace('goal = "g"', 'goal = "g"\nextends = "rewiew"', 1),
            "extends 'rewiew', which is not a council in",
        ),
        (body().replace('goal = "g"', 'goal = "g"\nextends = 4', 1), "[councils.review].extends wants a string, got int"),
        (body().replace('goal = "g"', 'goal = "g"\nextends = "review"', 1), "cannot extend in a cycle: review → review"),
        (
            body().replace('goal = "g"', 'goal = "g"\nextends = "y"', 1) + '\n[councils.y]\nextends = "review"\n',
            "cannot extend in a cycle: review → y → review",
        ),
        (body().replace('name = "A"', 'name = "A"\ndrop = true', 1), "drops a seat, but [councils.review] extends no council"),
        (
            body().replace('goal = "g"', 'goal = "g"\nextends = "_p"', 1)
            + '\n[councils._p]\n[[councils._p.agents]]\nname = "A"\nnickname = "x"\n',
            "unknown config key 'nickname' in [councils._p.agents][1]",
        ),
        (
            # A patched roster is still inherited, so a broken ancestor is an error even with a roster of its own.
            body().replace('goal = "g"', 'goal = "g"\nextends = "_p"', 1) + "\n[councils._p]\nagents = 3\n",
            "[councils._p.agents] is not a list of seat tables; agents are written [[councils._p.agents]]",
        ),
        (
            body().replace('goal = "g"', 'goal = "g"\nextends = "_p"', 1) + '\n[councils._p]\n[councils._p.agents]\nname = "A"\n',
            "[councils._p.agents] is a single table; agents are written [[councils._p.agents]]",
        ),
        (
            body()
            .replace('goal = "g"', 'goal = "g"\nagents = 3', 1)
            .replace('[[councils.review.agents]]\nname = "A"\n\n[[councils.review.agents]]\nname = "B"\n', ""),
            "[councils.review.agents] is not a list of seat tables",
        ),
        (
            # A knobs-only template inherits no seat to retire, so the verb is named, not an unknown seat key.
            body().replace('goal = "g"', 'goal = "g"\nextends = "_knobs"', 1)
            + '\n[councils._knobs]\nwatchdog_sec = 90\n\n[[councils.review.agents]]\nname = "Z"\ndrop = true\n',
            "[councils.review.agents] drops 'Z', which inherits no seat of that name to drop",
        ),
        (
            # A bad value in an inherited seat names the ancestor that carries it, not the council that inherited it.
            body().replace('goal = "g"', 'goal = "g"\nextends = "_p"', 1)
            + '\n[councils._p]\n[[councils._p.agents]]\nname = "A"\npersona = 7\n',
            "config key [councils._p.agents][1].persona wants a string",
        ),
        (
            body().replace('goal = "g"', 'goal = "g"\nextends = "_p"', 1) + '\n[councils._p]\n[[councils._p.agents]]\npersona = "x"\n',
            "[councils._p.agents][1] needs a non-empty 'name'",
        ),
        (
            # The same for the chair, which `_members` would otherwise report in the inheriting table.
            body().replace('goal = "g"', 'goal = "g"\nextends = "_p"', 1) + '\n[councils._p]\n[[councils._p.chair]]\nname = "Chair"\n',
            "[councils._p.chair] is written [[...]]",
        ),
        (
            body().replace('goal = "g"', 'goal = "g"\nextends = "_p"', 1) + "\n[councils._p]\nchair = 3\n",
            "[councils._p.chair] is int, not a table",
        ),
        (
            # A chair patch states no name, so only the merged chair can lack one — and it is named as a table.
            body(**{'[councils.review.chair]\nname = "Chair"': "[councils.review.chair]"}),
            "[councils.review.chair] needs a non-empty 'name'",
        ),
        (
            body()
            .replace('goal = "g"', 'goal = "g"\nextends = "_p"', 1)
            .replace('[councils.review.chair]\nname = "Chair"', "[councils.review.chair]")
            + '\n[councils._p]\n[councils._p.chair]\npersona = "p"\n',
            "[councils.review.chair] needs a non-empty 'name'",
        ),
        (
            body(**{'[councils.review.chair]\nname = "Chair"': '[councils.review.chair]\nname = "Chair"\nmodel = 7'}),
            "config key [councils.review.chair].model wants a string",
        ),
        (
            # Only the merged seat can be judged for this, since a patch may complete an inherited one.
            body().replace('goal = "g"', 'goal = "g"\nextends = "_p"', 1)
            + '\n[councils._p]\n[[councils._p.agents]]\nname = "A"\nprovider = "p"\n\n[[councils._p.agents]]\nname = "B"\n',
            "[councils.review.agents][1] states 'provider' without 'model' — pi refuses to start a seat configured that way "
            "[the roster merges _p → review: it can be stated in any of them]",
        ),
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


# --------------------------------------------------------------------------- the config layers


def layer(tmp_path, name: str, text: str) -> str:
    """One layer of the config: a `council.toml` in a directory of its own, as the user's and the project's are."""
    directory = os.path.join(str(tmp_path), name)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "council.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def convene_layers(tmp_path, paths, name, **kw):
    loaded = config_mod.load_layers(paths, project_layer=paths[-1])
    return config_mod.convene(loaded, config_mod.select_council(loaded, name), Args(**kw), str(tmp_path))


def test_a_later_layer_replaces_a_same_named_council_whole(tmp_path):
    user = layer(tmp_path, "user", body().replace('goal = "g"', 'goal = "the user goal"'))
    council = convene_layers(tmp_path, [user, layer(tmp_path, "proj", body())], "review")
    assert council.goal == "g", "a same-named council is replaced, not merged: the project's goal is the whole table's"
    assert council.config_path == os.path.join(str(tmp_path), "proj", "council.toml")


def test_defaults_patch_key_by_key_across_layers(tmp_path):
    user = layer(tmp_path, "user", body().replace("[defaults]\n", "[defaults]\nmax_rounds = 4\n"))
    council = convene_layers(tmp_path, [user, layer(tmp_path, "proj", body())], "review")
    assert council.max_rounds == 4, "a [defaults] key the project layer omits survives from the user layer"


def test_a_layer_of_nothing_but_defaults_leaves_the_council_where_it_was(tmp_path):
    user = layer(tmp_path, "user", body())
    project = layer(tmp_path, "proj", "[defaults]\nmax_rounds = 3\n")
    council = convene_layers(tmp_path, [user, project], "review")
    assert council.max_rounds == 3 and council.config_path == user, "a defaults-only layer states no council of its own"


def test_a_council_only_the_user_states_is_still_convenable_and_extends_crosses_the_layers(tmp_path):
    user = layer(tmp_path, "user", body())
    project = layer(tmp_path, "proj", '[councils.narrow]\nextends = "review"\ngoal = "narrower"\n')
    council = convene_layers(tmp_path, [user, project], "narrow")
    assert [agent.name for agent in council.agents] == ["A", "B"], "the roster came from the other layer"
    assert council.goal == "narrower" and council.extends == ["review"]
    assert council.config_layers == [user, project]


def test_the_layers_that_do_not_exist_are_not_read(tmp_path):
    user = layer(tmp_path, "user", body())
    missing = os.path.join(str(tmp_path), "proj", "council.toml")
    loaded = config_mod.load_layers([user, missing], project_layer=missing)
    assert loaded.paths == [user] and loaded.project_layer is None
    with pytest.raises(ConfigError, match="no council.toml anywhere"):
        config_mod.load_layers([missing, os.path.join(str(tmp_path), "nowhere", "council.toml")])


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


def test_a_project_layer_pi_cli_may_not_live_inside_the_project(tmp_path):
    """The one config a clone brings with it cannot name the executable every member runs."""
    project = os.path.realpath(str(tmp_path))
    planted = os.path.join(project, "pi-harness.py")
    shutil.copyfile(PI, planted)
    os.chmod(planted, 0o755)
    with pytest.raises(ConfigError, match="inside the project"):
        config_mod.resolve_pi(planted, project, from_project=True)
    assert config_mod.resolve_pi(planted, project)[0] == os.path.realpath(planted), "your own config may name it"
    assert config_mod.resolve_pi(PI, project, from_project=True)[0] == os.path.realpath(PI), "and a project may name one outside itself"


def test_an_unresolvable_pi_leaves_nothing_behind(tmp_path, canary):
    """The startup ordering: a config-stage failure creates no run directory anywhere."""
    text = body().replace(PI, "no-such-pi-cli")
    with pytest.raises(ConfigError):
        convene(tmp_path, text)
    assert os.listdir(canary) == []


# --------------------------------------------------------------------------- the "did you mean" hint


def test_a_refused_name_is_matched_exactly_or_by_prefix_before_any_edit_distance():
    known = ["max_rounds", "chair_timeout_sec", "sandbox_mib", "watchdog_sec"]
    assert suggest.suggest("--max-rounds", known) == "max_rounds", "dashes, underscores and case are the same name"
    assert suggest.suggest("--maxround", known) == "max_rounds", "a prefix matches either way round"


def test_the_edit_distance_a_typo_is_allowed_scales_with_the_name():
    known = ["goal", "docket", "max_rounds"]
    assert suggest.suggest("gool", known) == "goal", "one edit for a short name"
    assert suggest.suggest("gola", known) is None, "two is not a typo on a four-letter name"
    assert suggest.suggest("dkcxet", known) == "docket", "at six characters two edits are allowed"
    assert suggest.suggest("dkcxetx", known) is None, "three is not a typo, at six characters or at any other length"
    assert suggest.suggest("", known) is None, "nothing to suggest a name for"
