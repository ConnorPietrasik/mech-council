from __future__ import annotations

import os
import shutil
import sys

from . import cli

#: The starter user council.toml: `review`, `plan` and `build` (implement) councils, no provider or model
#: stated, so every seat takes whatever pi is configured with.
STARTER = '''\
# council.toml — the user councils: one config that serves every project.
# Knobs not stated here take their built-in defaults.
#
# `extends` inherits another council's roster, chair and knobs wholesale.  A council that
# then states `[[agents]]` patches the inherited seats by name — a stated key replaces it,
# the rest carry over, `drop = true` retires a seat, and a name the parent lacks joins the
# end — and `[chair]` merges key by key.  Precedence: built-in, `[defaults]`, the extended
# chain nearest-first, this council, the flags.  A `_` name is a template: something to
# extend, never something you convene.

[councils.review]                   # `mech-council review`
mode = "review"
goal = "See if there is anything that should be fixed / improved in this project."

[councils.review.chair]
name = "Chair"

[[councils.review.agents]]
name = "Metic"
persona = """
You are a meticulous code reviewer. Hunt for correctness bugs.
"""

[[councils.review.agents]]
name = "Senior"
persona = """
You are a pragmatic senior engineer.
"""

[[councils.review.agents]]
name = "Pro"
persona = """
You are a professional code reviewer.
"""

[[councils.review.agents]]
name = "Coworker"
persona = """
You are a coworker of the author. Make sure the code is readable and correct so you won't hate working on it for future improvements / feature adds.
"""

[councils.plan]
mode = "plan"
goal = "Make a plan for the implementation of the next TODO item"

[councils.plan.chair]
name = "Chair"

[[councils.plan.agents]]
name = "Metic"
persona = """
You are a meticulous planner. Attack the internal correctness of a plan: preconditions, step ordering, partially-applied migrations, and anything that breaks when a step lands out of order or half-runs.
"""

[[councils.plan.agents]]
name = "Senior"
persona = """
You are a pragmatic senior engineer. Find the smallest change that actually satisfies the task, prefer deleting to adding, and commit to a decision instead of presenting a menu. Push back on plans that leave the risky step for last. Challenge feasibility wherever you see it, including in your own plan.
"""

[[councils.plan.agents]]
name = "Pro"
persona = """
You are a professional architect on a planning council. Re-read the task before endorsing anything and make sure the plan satisfies every requirement.
"""

[[councils.plan.agents]]
name = "Coworker"
persona = """
You are the engineer who inherits this codebase in eighteen months. Flag migrations, data formats, compatibility shims and config drift that nobody will want to own later.
"""

[councils.build]                   # `mech-council build`
mode = "implement"
goal = "Implement the next TODO item in this project."
planner = "plan"
reviewer = "review"
human = "non_blocking"

[councils.build.chair]
name = "Chair"

[[councils.build.agents]]
name = "Forge"
role = "both"
persona = """
You are the builder. Implement the stage exactly as the plan states it, keep the change as small as the stage allows, and re-run the stage's gate until it passes.
"""

[[councils.build.agents]]
name = "Wordsmith"
role = "prose"
persona = """
You write the user-visible text the run ships: docs, messages, names. You match the project's existing voice, and you never touch code.
"""
'''

HELP = """\
usage: mech-council init [--overwrite]

Write the starter user council.toml to the user location: $COUNCIL_CONFIG, else
$XDG_CONFIG_HOME/council/council.toml, else ~/.config/council/council.toml.  The starter holds a
`review`, a `plan` and an `implement` (build) council; no seat states a provider or a model,
so every seat takes whatever pi is configured with.

flags:
      --overwrite    replace the user council.toml that is already there
  -h, --help         show this help and exit
"""


def main(argv: list[str]) -> int:
    overwrite = False
    for token in argv:
        if token in ("-h", "--help"):
            sys.stdout.write(HELP)
            return 0
        if token == "--overwrite":
            overwrite = True
        else:
            sys.stderr.write(f"unknown argument {token!r} for init\n")
            sys.stderr.write("run 'mech-council init --help'\n")
            return 2
    path = cli._user_config_path(dict(os.environ))
    if path is None:
        sys.stderr.write(
            "no user config location: COUNCIL_CONFIG is unset, XDG_CONFIG_HOME is unset, empty or relative, and HOME is unset\n"
        )
        return 1
    parent = os.path.dirname(path)
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    except OSError as failure:
        sys.stderr.write(f"could not create the user config directory {parent}: {os.strerror(failure.errno or 0)}\n")
        return 1
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        sys.stderr.write(f"the user council.toml already exists: {path} — pass --overwrite to replace it\n")
        return 1
    except OSError as failure:
        sys.stderr.write(f"could not create the user council.toml {path}: {os.strerror(failure.errno or 0)}\n")
        return 1
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(STARTER)
    print(f"wrote {path}")
    print("convene it from any project: mech-council review (or: mech-council plan)")
    if shutil.which("pi") is None:
        print("note: 'pi' is not on PATH — a run will refuse until it is, or pi_cli is stated in council.toml")
    return 0
