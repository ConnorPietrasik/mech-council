# mech-council

A council of coding agents that reviews a project, plans a task, or implements one — the third
mode takes a direction from a planner crew or a plan document, then has builder seats land it in
the real tree stage by stage while a reviewer crew watches.

You name a council in `council.toml` — seats and a chair, each a [`pi`](https://github.com/earendil-works/pi)
session on whatever provider and model you like — and `mech-council` convenes them. The seats
read the code, argue about it, and have to sign off; the chair merges duplicates, compares
plans, and watches for a seat that has stopped making progress. When the council is done
you get a report with the findings.

## Requirements

- **Python 3.14+**
- **`pi`** on your `PATH`, or a `pi_cli` path in `council.toml`. Members are driven through
  pi's RPC mode over pipes — one long-lived child per member, the chair included, JSONL in both
  directions.
- **Linux with unprivileged user and mount namespaces**, to sandbox the members. Each member runs in its
  own namespace. On Debian/Ubuntu this needs `kernel.unprivileged_userns_clone=1`; Docker needs a seccomp
  profile that allows `unshare(2)`. A host without namespaces runs in place and says so in a note; a
  Linux where `unshare(2)` fails refuses and points at `--in-place`; a host that cannot fork at all is refused.

## Install

```sh
pipx install mech-council
mech-council --help
```

## Quick start

```sh
mech-council init              # writes your user council.toml: $COUNCIL_CONFIG, else $XDG_CONFIG_HOME/council/council.toml,
                               # else ~/.config/council/council.toml — one config that serves every project
```

The starter is a `review`, a `plan` and a `build` (implement) council. No seat states a
`provider` or a `model`, so every seat takes whatever pi is configured with — state them per seat
to point one somewhere else. `init` refuses to replace a file that is already there;
`mech-council init --overwrite` replaces it.

```sh
mech-council review            # from any project; or: mech-council -g "focus on the parser" review
mech-council plan
```

`mech-council list` shows the councils the config offers — roster, goal, the knobs that
differ from the defaults, and which file each comes from. `mech-council doctor` checks what
a run needs: the config, pi, the namespaces the sandbox stands on, and the runs root.

Prefer a council of your own? Put the smallest one in `<project>/council.toml` — two seats and a
chair; `provider` and `model` are optional, and without them a seat takes whatever pi is configured
with.

```toml
[councils.review]
mode = "review"
goal = "Find what should be fixed in this project."

[councils.review.chair]
name = "Chair"

[[councils.review.agents]]
name = "Metic"
persona = "You hunt correctness bugs."

[[councils.review.agents]]
name = "Senior"
persona = "Smallest change that satisfies the goal."
```

```sh
mech-council review            # or: mech-council -g "focus on the parser" review
```

Narration goes to **stdout**, usage errors to stderr, and the report is
printed last after a blank line.

## What a run does

1. **Phase 0.** Every seat gets the *same bytes* — no names, no IDs — and reports findings
   (review) or a proposed approach (plan).
2. **Merge.** The chair deduplicates: two seats describing one defect produce one item with
   two reporters.
3. **Rounds.** Every seat is shown every open item and must take a stance — `agree`,
   `disagree` (with cited evidence), `needs_human`, or `abstain`. An item needs a majority
   to be `agreed`; an objection with cited evidence reopens a settled one. A seat that leaves an item
   unanswered is re-asked — up to four times in the round when its reply was unusable or incomplete,
   once when it simply skipped the item — and its silence is then recorded as a *declined* abstention.
4. **You.** An ask is a proposal to the *council*, not a message to you: it reaches you only once a
   **second seat backs it** (`second`), because a peer can usually answer it from the workspace
   (`answer_question`) or the asker drops it again (`retract`). Two seats holding **one item** raise
   one ask together, and the chair **folds asks that want the same answer** into the one raised first
   — askers, declared stops and peer answers move with it, the wordings that did not survive are
   recorded beside it — so two wordings of one decision count as one backed ask. A peer's answer
   holds an ask back from you: only the seat that **raised** it can call that answer not enough
   (`insist: ["H1"]`, in a round *after* the answer lands, since a seat cannot refuse an answer it
   was never shown), and a seconding seat cannot. An answer stops holding the ask back once the seat
   that wrote it is asking too — a seat that backs an ask it answered, or that answered one of two
   asks the chair folded, wants your word after all; what it wrote stays printed beside the ask.
   Nothing stops the run but a **declared stop** — `blocking: true` on an ask, or `blocking_for:
   ["H1"]` on a peer's — and a majority has to declare the same stop on the same ask for it to bite.
   A planning council that will not converge on one direction stops the run by itself. A seat stancing
   `needs_human` holds its *item* — reported as not fully vetted, and open to a peer's challenge — but
   not the run.
   Backed asks the run does not wait on are put to you when the run would otherwise end; unbacked
   ones are only reported, never asked. Before that, the seats get one last look at them: a seat may
   answer one, back one, drop one, call a peer's answer to one it raised not enough, stance away from
   an item a peer's answer settled, or unsay a **note** of its own — and nothing else.
   Only a stance releases an item; dropping the ask raised about it never does. The reverse too:
   letting go of an item drops the ask your `needs_human` raised on it, and so does the council
   **settling that item** — the closure *is* the answer — unless a majority declared a stop on it,
   the one claim that outranks a closure. A settled item reaches you only as a stop at the bar.
   What a seat wants you to **know** and not decide is a `note`: no second seat, nothing to answer.
5. **Termination.** `consensus`, `needs_human`, `stalemate`, `round_limit_reached`, or `unvetted` —
   the last when everyone signed off but an open item reached no disposition: no majority, no
   reasoned objection. At the round limit with a question still standing, the run asks you first
   while grace remains: each answer buys the seats one more round to plan against it
   (`grace_rounds`). A question the run showed you and you passed over is reported `unanswered`;
   only one it never showed you is `unasked`.

The chair never takes a stance and never gets a roster ID, so it can't move the majority bar. It
merges findings, folds asks that want the same answer, compares the plans in a plan run, and judges
the watchdog digest. It puts you one question of its own — the council's split, when a plan run will
not converge on one direction; every other question comes from a seat.

### At the checkpoint

```
H1: keep it 30 days          # an answer is authoritative, and buys a round
H1: 2                        # takes that option verbatim; to take it with a qualifier,
                             # answer in your own words
explain H2: what breaks?     # routes your question to the seat that asked
Metic: does it have a bound? # a direct message to a seat or the chair: prose in, prose out,
                             # nothing on the docket changes, no other seat sees it
skip                         # decline everything on the table
skip H1 H2                   # decline these two and answer the rest
                             # a blank line ends the checkpoint
```

A direct message costs the named seat one prompt and is answered in prose for you alone. It lands
where your input is read — at once while the run is parked, at the next boundary while the seats
work — when the named seat is idle; the chair waits out its watchdog ask rather than refuse you.

The last line you type about a question is your word on it: answering one you declined takes it
back, and declining one you answered withdraws the answer — until the answer is applied, after which
that id answers nothing further. From the next round on, the docket every seat is shown prints your
answer as authoritative — but only the seats' own stances settle the run, so a council that keeps
planning around your answer ends `stalemate` or out of rounds, and the report says so.

You can type any of those lines **while the seats are working**: an answer is read at the next
round boundary, already in the docket the seats are shown next, and it buys a round exactly as a
checkpoint answer does — the same grace budget, so an answer typed once it is spent is recorded and
reported, but keeps the run no longer. A `skip` typed mid-round declines the asks open at that
moment; `explain H1` is asked at that same boundary, when no other ask is in flight.

Only backed questions appear here — unless `seconding` is off, when every open question is put to
you. The ones no second seat wanted are in the report, and in
`human_input.json` when the verbosity keeps it (`detail` or higher), marked `unseconded`; an ask a seat answered in your place is marked `settled`,
with that answer beside it; and an ask about an item the council settled is reported `moot` — it was
never going to be put to you.

Notes (`N1`) print once, at the next checkpoint, and are listed again in the report.

### Interrupting a run

One `Ctrl+C` stops the deliberation and still writes the report. A second one kills the members and
ends the run within two seconds, with no guarantee the report lands.

## What you get

The report is the one artifact every run writes. The rest of the run directory is a verbosity
ladder — `quiet` (the default) keeps the report and per-member stderr, each level adds one more
artifact, and the levels exist mostly for debugging:

| Level | Keeps |
|---|---|
| `quiet` | the report (plus per-member stderr under `agents/`) |
| `summary` | + `run.json` |
| `detail` | + `human_input.json` |
| `debug` | + `events.jsonl` |
| `full` | + the verbatim protocol streams under `agents/` |

| File | What's in it |
|---|---|
| `report.md` / `plan.md` / `implementation.md` | The result: consensus findings, what was never fully vetted and by whom, disputes verbatim, rejections with the notes that killed them, and the questions you never answered. Plans get stages, gates and named assumptions instead; implementations get the crews, the adopted plan, the stage ledger, what landed, and the decision points the run recorded. `--format html` writes the same content as `report.html` / `plan.html` instead — see below. |
| `run.json` (`summary`+) | Config, roster, settings, pi version, every member's namespaces and prompt count, the write journal, the outcome, and any process the cleanup left behind. An interrupted run adds where it stood and what a second signal gave up. |
| `events.jsonl` (`debug`+) | The append-only event log — one JSON object per line, the whole run in order. The run never reads it back; it is there for you. |
| `human_input.json` (`detail`+) | Every question and whatever became of it: who asked, who backed it, what a seat answered instead, whether the run waited on it, and the answer, skip, or reason it was never asked. Plus every note, the round each one was shown at, and every direct message you sent with its reply or failure. |
| `agents/` | Per-member stderr, and the verbatim transcripts at `full`. |

The run directory is `$COUNCIL_RUNS_DIR`, else `$XDG_CACHE_HOME/council/runs`, else
`~/.cache/council/runs`; each run is a `<timestamp>-<project>` directory inside it.

### `--output`: the report somewhere else

`-o`/`--output <path>` writes the report there instead of the run directory — one file moves,
nothing else follows it. An existing directory keeps the mode's name, so `-o docs/` writes
`docs/plan.md`; anything else is the file itself. At `summary` verbosity or higher, `run.json`
stays in the run directory and names the path the report landed at.

```sh
mech-council review -o .                 # -> ./report.md
mech-council review -o docs/REVIEW.md    # -> exactly that file
```

This is the way to keep a result out of a container that is about to go away.

When the target already exists, the run asks before replacing it. With no
terminal to answer from the run refuses and says so; `--overwrite` skips the asking.

### `--format`: the report as a page

`--format <md|html|both>` (config key `report_format`) picks the report's format. The default,
`md`, is what every run has always written. `html` writes the same content as one self-contained
page — `report.html` / `plan.html` instead of the `.md` file: inline styles, a contents list,
an outcome badge, no scripts, nothing to fetch. It opens offline and prints cleanly. `both`
writes the markdown and the page side by side; at `summary` or higher, `run.json` names both
(`report` is the markdown, `reports` the pair).

```sh
mech-council review --format html          # -> report.html
mech-council plan --format both -o docs/   # -> docs/plan.md and docs/plan.html
```

A file `-o` target must wear the format's extension, and `-o <file>` with `--format both` is a
usage error — two reports, one name.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | A result was written — including `needs_human`, `unvetted` and `round_limit_reached`. |
| `1` | No result: bad config, an unusable member, an internal error, a nested-council refusal, a declined `--output` overwrite, a report that never landed, a host that cannot fork. |
| `2` | Usage error. |
| `130`/`143` | Interrupted. The first press reports where the run stood, disposes every child, sweeps every namespace this run created, and still writes the report (and `run.json` at `summary` or higher); a press a second later skips the waits, kills the members, and the process goes within two seconds — each press after that starts the two seconds over from there. |

## Configuration

`[defaults]` sets knobs; `[councils.<name>]` overrides them. Flags beat both. Precedence, lowest
first: built-in default, `[defaults]`, the `extends` chain (nearest ancestor first), the council,
the flags.

### Which files are read

Two files: the user's — `$COUNCIL_CONFIG`, else `$XDG_CONFIG_HOME/council/council.toml`, else
`~/.config/council/council.toml` — and `<project>/council.toml`. `mech-council init` writes the
user's file, refusing to replace one that is already there (`--overwrite` replaces it). The
project's file replaces any
council of the same name and patches `[defaults]` key by key, and `extends` reaches a parent stated in
either, so a project's file can inherit the roster your own home file keeps
([`extends`](#extends-one-roster-several-councils)).

`--config` names the only file read; nothing layers under it. A project's own file may not state a
`pi_cli` living inside that project — put that knob in the user's file, or convene your own file
with `--config`.

| Knob | Default | Meaning |
|---|---|---|
| `mode` | `review` | `review`, `plan` for a task plan, or `implement` for a build that lands in the real tree — see [below](#implement-plan-build-review-in-place). |
| `goal` | — | What the seats are briefed with. `--goal` overrides; a `plan` or `implement` council needs one. |
| `max_rounds` | `10` | Round limit. |
| `grace_rounds` | `1` | How many rounds past the limit an answer may buy. `0` ends the run on the limit with its questions unasked. |
| `seconding` | `true` | A question reaches you only once a second seat backs it. `false` puts every question the council left open in front of you. |
| `human` | `blocking` | The run's posture toward the council's questions. `blocking`: it waits where a declared stop reaches the bar. `non_blocking`: it never waits — questions are shown while it keeps working, and you may answer any of them. `none`: the council is never asked; its open questions are recorded in the report. `--human` overrides; a run with no terminal on stdin takes `none` unless a posture is stated, and says so in a note. |
| `watchdog_sec` | `600` | How often the chair is handed a digest of in-flight seats. `0` disables it. |
| `planning_rounds` | `4` | implement: rounds the planner crew gets to converge on a direction. |
| `fix_budget` | `3` | implement: review-fix ladders one stage may spend before it is blocked. |
| `replans` | `2` | implement: re-structures one stage may spend before it is blocked. |
| `planner` | — | implement: the name of a `plan` council to convene as the run's planning crew. |
| `reviewer` | — | implement: the name of a `review` council to convene as the run's per-stage reviewer crew. |
| `chair_timeout_sec` | `300` | Bound on one of the chair's passes (merge, fold, compare, direction, structure, pick). |
| `max_concurrency` | `4` | Seats asked at once. The chair has its own slot. |
| `sandbox_mib` | `32768` | tmpfs shared by all member upper layers. Ignored in place. |
| `in_place` | `false` | Run members in the real project: no namespaces, no overlays, no write journal. `--in-place` forces it on; an implement run is always in place. |
| `verbosity` | `quiet` | What the run directory keeps: `quiet` the report and per-member stderr, then `summary` (+`run.json`), `detail` (+`human_input.json`), `debug` (+`events.jsonl`), `full` (+ the verbatim protocol streams under `agents/`). `--verbosity` overrides. |
| `pi_cli` | discovered | Path or name of the pi CLI; probed with `--version`. |

Seats and the chair also take `provider`, `model` (`provider` without `model` is refused, since pi
will not start a seat configured that way), and `persona`. An unknown model warns rather than
failing the run. An implement builder seat also takes `role`: `code`, `prose`, or `both` (the default) —
`prose` seats are handed prose stages only.

### `implement`: plan, build, review, in place

`mode = "implement"` is a build, not a review: it ends with a changed tree and an
`implementation.md` report, never a commit — you review the whole diff when the run is done.
Because it edits the real project, it is always in place, and a dirty git tree is refused
unless `--dirty` is passed (the stage diffs and the report's diffstat are measured against the
tree as the run starts, clean or not; a project that is not a git repo runs anyway, with a weaker
diff and a note).

An implement council is a chair and builder seats, plus at most two borrowed councils:
`planner` names a `plan` council that convenes first and delivers the direction, and `reviewer`
names a `review` council whose crew reviews every stage before it passes. Borrowed crews keep
their own rosters, chairs and personas but inherit the run's knobs; their seats join the one
roster under crew-suffixed names — `Metic (planner)`, `Metic (reviewer)` — while the builders keep
their own, `Forge`.
The run's plan comes from one of two places: a `planner` crew that is convened, or a `--plan`
document the run's chair structures — a run with neither is refused, and either one lets a
single builder build.

`--plan <file>` skips the planning phase: the run's chair structures the document into gated
stages and the run goes straight to building. A stage is a final state with a **gate** — the
check that proves it: a command, or a statement of what must be true. The review crew runs and
verifies the gate at every review — the run never runs it itself, so a gate that is prose still
works — and the stage only passes when the crew's verdict is pass and it has nothing left to
report — it re-reports what it still finds, so a finding outlives the passes until it is fixed or
withdrawn. When the crew's verdicts do not read, the run's chair verifies the gate itself. A
stage that names no gate has nothing to verify: it passes when the review crew has nothing left
to report. New text a stage needs (docs, release notes) lands in the same stage —
the builder writes it and names it as `user_text` — and the report prints it. A `prose`-role
stage whose whole artifact is user-visible text is handed only to the seats that can take prose.

The repair ladders are finite, so the build half always ends: when a stage's gate fails, its
findings go to the builder as a fix pass (`fix_budget`), and when nothing works the stage is
replanned smaller (`replans`) — an unreadable builder reply spends a rung the same way; past
both, the stage is **blocked** — the run keeps going over the rest of the plan and ends
`partial` with what landed, where it stopped, and why. Seats may message each other as the
build goes: `say_to` takes a seat ID, and the roster each prompt carries is how a seat learns
them. The run's chair is on hand for structure, splits and stuck seats, and with
`human = "blocking"` a blocked stage asks you whether to carry on or hold. The approval shows the
adopted plan — every stage's final state and the gate that will be checked — so a
`proceed` agrees to a plan, not a yes/no. The run's two calls of
its own — the approval before the first edit and the carry-on-or-hold on a blocked stage — take
only an explicit selection: the option's number or its words, nothing else, since the run reads
one bit from each and a qualifier in your own words is not a decision.
An approval that does not pick `proceed` is a hold, and a blocked stage carries on unless the
answer says `hold`.

```sh
mech-council build                     # plan (if a planner is named), adopt, build, review, report
mech-council build --plan docs/PLAN.md # skip the planners; structure the document instead
```


### `extends`: one roster, several councils

A council that only wants a different goal says `extends` and inherits another's seats and chair:

```toml
[councils.review]                    # a goal, three seats and a chair, stated once
mode = "review"
goal = "See if there is anything here that should be fixed or improved."

[[councils.review.agents]]
name = "Metic"
persona = "You hunt correctness bugs."

[[councils.review.agents]]
name = "Senior"
persona = "Smallest change that satisfies the goal."

[[councils.review.agents]]
name = "Coworker"
persona = "You inherit this in eighteen months."

[councils.review.chair]
name = "Chair"

[councils.review-staged]             # the same council, a narrower scope — three lines
extends = "review"
goal = "Another agent staged these changes; did they do a good job?"

[councils.review-quick]              # …or the same council with a smaller bench
extends = "review"
goal = "The defects you would fix today."
max_rounds = 3

[[councils.review-quick.agents]]
name = "Senior"
drop = true                          # retire an inherited seat

[[councils.review-quick.agents]]
name = "Metic"
persona = "One line per finding."    # patch one key; the seat keeps everything else

[councils.review-quick.chair]
persona = "Be conservative: merge only the same defect."   # the chair merges key by key
```

`[[agents]]` entries match inherited seats **by name**: a stated key replaces that key, an
unmatched name joins the end of the roster, and `drop = true` retires a seat (a council still
needs at least two after the drops). `[chair]` merges key by key.

A council whose name starts with `_` is a **template**: `extends` reaches it, `mech-council <name>`
does not, and it is free to be a partial council — a roster with no chair, or a few knobs to pull
into several councils.

## The sandbox

Each member sees your project through an overlay: the project is the read-only lower layer,
its own writes land in an upper layer inside a tmpfs that is discarded, and its `$TMPDIR` is
scratch that never enters the write journal. Its run directory is masked, and `/proc` closes the
run's own file objects to it — reading the run's `cwd`, `environ` or `exe`, or listing its `fd`,
answers `EACCES`. The clearing holds while the run is unprivileged: at uid 0 or with
`CAP_SYS_PTRACE` a seat can list the run's open descriptors; the run notes it when it runs at uid 0.
When the run ends, every namespace it created is swept — including tool shells a member detached
into its own process group — and the survivors are named in `run.json` if any remain (at
`summary` verbosity or higher; the console says so either way).

The sandbox keeps the members from stepping on each other and from leaving writes in your
project. It is not a security boundary: the overlay only covers the project, and a seat reads the
rest of the host filesystem as you do. The recommended setup is a container with only the
directory you want reviewed mounted.

### In place

`--in-place`, or `in_place = true`, convenes the same council with nothing between the
seats and the project. What changes:

- Every seat works in the **same real tree**, and the briefings say a peer's half-finished edit is
  not a defect and not yours to finish.
- The **namespace sweep cannot run**: members share this namespace, so only their own process groups
  are signalled, and a tool shell that moved to a process group of its own can outlive the run.
  `run.json` records `"namespace_sweep": "none (in place)"` (at `summary` verbosity or higher).

## Development

```sh
pip install -e ".[test,dev]"
pytest tests -q              # offline: fake pi, real namespaces, real mounts
pytest tests/test_sandbox.py -q -rs
pytest tests -m slow         # spends tokens: one real pi review of testdata/toy-project
python -m ruff check .
python -m ruff format --check .
python -m mypy               # over src and tests
```

The offline suite drives real orchestrator, sandbox, RPC and dispose paths against a scripted fake
pi. The `sandbox` family is the only one with a ground requirement: its fixture lower must sit on
a plain filesystem (ext4, or a tmpfs such as a member's `$TMPDIR`); point `COUNCIL_TEST_FS` at a
plain directory to choose it explicitly. Its one `/proc` case asserts the unprivileged half only,
so a suite run at uid 0 (or with `CAP_SYS_PTRACE`) skips it, naming the uid rule in
[`The sandbox`](#the-sandbox), and still asserts everything it can prove there. Everything else,
`test_in_place.py` included, runs on any POSIX host that can fork — the namespace-sweep cases in
`test_process.py` need the same `unshare(2)` as the sandbox.

GitHub Actions runs ruff, mypy and the offline suite on every push to `master` and every pull
request ([`ci.yml`](https://github.com/ConnorPietrasik/mech-council/blob/master/.github/workflows/ci.yml)),
then runs the suite again from an unpacked sdist; `pytest -m slow` stays out of it. Tagging a
release publishes it ([`publish.yml`](https://github.com/ConnorPietrasik/mech-council/blob/master/.github/workflows/publish.yml)):
the suite on the tagged commit, a build whose version has to match the tag, then the upload through
PyPI's trusted publishing.

## License

`mech-council` — a council of coding agents that review a project or plan a task together.

Copyright (C) 2026 Connor Pietrasik <connorgpietrasik@gmail.com>

This program is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, either version
3 of the License, or (at your option) any later version. See [LICENSE](https://github.com/ConnorPietrasik/mech-council/blob/master/LICENSE).
