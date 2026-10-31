# mech-council

A council of coding agents that reviews a project or plans a task together.

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
  own namespace. On Debian/Ubuntu this needs `kernel.unprivileged_userns_clone=1`; Docker needs
  `--privileged` or a seccomp profile that allows `unshare(2)`. `--in-place` is the default otherwise;
  a host that cannot fork at all is refused.

## Install

```sh
pipx install mech-council
mech-council --help
```

## Quick start

Put the smallest council in `<project>/council.toml` — or once in `~/.config/council/council.toml`, for
every project. Two seats and a chair; `provider` and `model` are optional, and without them a seat takes
whatever pi is configured with.

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
   `disagree`, `needs_human`, `abstain` — with evidence or a note. An item needs a majority
   to be `agreed`; an objection with cited evidence reopens a settled one. A seat that leaves an item
   unanswered is re-asked — up to four times in the round when its reply was unusable or incomplete,
   once when it simply skipped the item — and its silence is then recorded as a *declined* abstention.
4. **You.** A seat raises an ask, and an ask is a proposal to the *council*, not a message to you:
   it reaches you only once a **second seat backs it** (`second`), because most of what a seat wants
   a peer can answer from the workspace (`answer_question`) or the asker drops again (`retract`).
   Two seats wanting one decision word it differently. Two seats holding **one item** raise one ask
   together, and the chair **folds asks that want the same answer** into the ask raised first — the
   askers, their declared stops and any peer answers go with it, and the wordings that did not
   survive are recorded beside it — so the fold is a second seat without anyone claiming it. A peer's
   answer holds an ask back from you, and only the seat that **raised** it can call that answer not
   enough — with `insist: ["H1"]`, in a round *after* the answer lands, since a seat cannot refuse an
   answer it was never shown. A second seat backing it cannot. And an answer holds an ask back
   only while the seat that wrote it is not asking: a seat that backs an ask it answered, or that
   answered one of two asks folded together, wants your word after all — what it wrote stays printed
   beside the ask, it just stops keeping the ask from you. Nothing stops the run but a
   **declared stop** — `blocking: true` on an ask, or `blocking_for: ["H1"]` on a peer's — and a
   majority has to declare the same stop on the same ask for it to bite; a planning council that will
   not converge on one direction stops the run by itself. A seat stancing `needs_human` holds its
   *item* — reported as not fully vetted, and open to a peer's challenge — but it no longer holds the
   run. Asks that are backed but not waited on are put to you when the run would otherwise end;
   unbacked ones are never put to you at all, only reported. Before that report the seats get one
   last look at those: a seat may answer one, back one, drop one, call a peer's answer to one it
   raised not enough, stance away from an item a peer's answer settled, or unsay a **note** of its own —
   and nothing else. Only a stance releases an item; dropping the ask raised about it never does. An ask goes the
   other way too: letting go of an item drops the ask your `needs_human` raised on it, and so does the council
   **settling that item** — the closure *is* the answer to a question about it — unless a majority
   declared a stop on it, which is the one claim that outranks a closure. So a settled item reaches
   you only as something more than one seat needed the run to wait for.
   What a seat wants you to **know** and not decide is a `note`: no second seat, and nothing for you to answer.
5. **Termination.** `consensus`, `needs_human`, `stalemate`, or `round_limit_reached` — but a run
   that would end with a question standing spends a **grace round** first: it asks you, and any
   answer buys the seats one more round to plan against it (`grace_rounds`). A question it did show
   you and you passed over is reported `unanswered`; only one it never showed you is `unasked`.

The chair never takes a stance and never gets a roster ID, so it can't move the majority bar. It
merges findings, folds asks that want the same answer, compares, words the council's split for you,
and judges the watchdog digest — and it asks you nothing of its own: every question comes from a
seat.

### At the checkpoint

```
H1: keep it 30 days          # an answer is authoritative, and buys a round
H1: 2                        # takes that option verbatim; to take it with a qualifier,
                             # answer in your own words
explain H2: what breaks?     # routes your question to the seat that asked
skip                         # decline everything on the table
skip H1 H2                   # decline these two and answer the rest
                             # a blank line ends the checkpoint
```

The last line you type about a question is your word on it: answering one you declined takes it
back, and declining one you answered withdraws the answer — until the answer is applied, after which
that id answers nothing further. From the next round on, the docket every seat is shown prints your
answer as authoritative — but nothing settles the run but the seats' own stances, so a council that
keeps planning against your answer ends `stalemate` or out of rounds, and the report says so.

You can type any of those lines **while the seats are working**, not only when the run stops for
you: an answer is read at the next round boundary, so it is already in the docket the seats are
shown next, and it buys the round exactly as a checkpoint answer does — the same grace budget, so an
answer typed once the grace rounds are spent is recorded and reported but keeps the run no longer. A
`skip` typed mid-round declines the asks open at that moment, and `explain H1` waits for that same
boundary because it costs an ask of the seat that raised it.

Only backed questions appear here. The ones no second seat wanted are in the report and in
`human_input.json`, marked `unseconded`; an ask a seat answered in your place is marked `settled`,
with that answer beside it; and an ask about an item the council settled is reported `moot` — it was
never going to be put to you.

Notes (`N1`) print once at the next checkpoint and are listed again in the report; they exist to stop the agent from spamming pointless questions.

### Interrupting a run

One `Ctrl+C` stops the deliberation and still writes the report. It leads with **Cut short by
`SIGINT`** — the round, the pass it was in, what it was asking you, and what each seat was doing when
the signal landed — and then reports the docket exactly as it stood, write journal and `run.json`
included. Nothing in it is settled: a plan whose direction a majority held when the signal arrived
says so beside that direction.

A second `Ctrl+C`, a second or more after the first, is the way out when that is taking too long: the
members get a closed stdin and a moment to take their own tool shells down with them and are killed where
they stand; the write journal walk ends where it had got to; and the whole hurried exit gets two seconds
before the process is abandoned where it stands — leaving a `run.json` that says the namespace sweep never
ran, never one that says less than the manifest already written. What those two seconds reach is what you
get: usually the report, `human_input.json` and a `run.json` with `exit_path: "fast"` and the list of what
was given up. When they don't, **the report is what the abandoning costs you** — `run.json` and
`events.jsonl` hold what the run knew, and the console says it ran out of time. Press again a second or
more later and the two seconds start over from there, so the bound cannot run out while the run can still
be reached, and every press says what it did rather than being swallowed. One keystroke arrives twice —
once from the terminal, whose process group the orchestrator stays in so it can still read the keyboard,
and once relayed by the supervisor — so any press within a second of the last counted one is that same
press, and nothing you do with the keyboard in that second hurries a run that is already stopping.

## What you get

| File | What's in it |
|---|---|
| `report.md` / `plan.md` | The result: consensus findings, what was never fully vetted and by whom, disputes verbatim, rejections with the notes that killed them, and the questions you never answered. Plans get stages, gates and named assumptions instead. |
| `run.json` | Config, roster, settings, pi version, every member's namespaces and prompt count, the write journal, the outcome, and any process the cleanup left behind. An interrupted run adds where it stood and what a second signal gave up. |
| `events.jsonl` | Append-only event log — one JSON object per line, the whole run in order. |
| `human_input.json` | Every question and whatever became of it: who asked, who backed it, what a seat answered instead, whether the run waited on it, and the answer, skip, or reason it was never asked. Plus every note and the round each one was shown at. |
| `agents/` | Per-member stderr, and transcripts if you asked for them (`--transcripts full`). |

The run directory is `$COUNCIL_RUNS_DIR`, else `$XDG_CACHE_HOME/council/runs`, else
`~/.cache/council/runs`; each run is a `<timestamp>-<project>` directory inside it.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | A result was written — including `needs_human` and `round_limit_reached`. |
| `1` | No result: bad config, an unusable member, an internal error, a nested-council refusal, a host that cannot fork. |
| `2` | Usage error. |
| `130`/`143` | Interrupted. The first press reports where the run stood, disposes every child, sweeps every namespace this run created, and finalizes `run.json` first; a press a second later stops waiting for the members and gives up the waits, and each one after that starts that two-second bound over. |

## Configuration

`[defaults]` sets knobs; `[councils.<name>]` overrides them. Flags beat both. Precedence, lowest
first: built-in default, `[defaults]`, the `extends` chain (nearest ancestor first), the council,
the flags.

### Which files are read

Two files: the user's — `$COUNCIL_CONFIG`, else `$XDG_CONFIG_HOME/council/council.toml`, else
`~/.config/council/council.toml` — and `<project>/council.toml`. The project's file replaces any
council of the same name and patches `[defaults]` key by key, and `extends` reaches a parent stated in
either, so a project's file can inherit the roster your own home file keeps
([`extends`](#extends-one-roster-several-councils)).

`--config` names the only file read; nothing layers under it. A project's own file may not state a
`pi_cli` living inside that project — put that knob in the user's file.

| Knob | Default | Meaning |
|---|---|---|
| `mode` | `review` | `review`, or `plan` for a task plan instead of a findings report. |
| `goal` | — | What the seats are briefed with. `--goal` overrides; a `plan` council needs one. |
| `max_rounds` | `10` | Round limit. |
| `grace_rounds` | `1` | How many rounds past the limit an answer may buy. `0` ends the run on the limit with its questions unasked. |
| `seconding` | `true` | A question reaches you only once a second seat backs it. `false` puts every question the council left open in front of you. |
| `watchdog_sec` | `600` | How often the chair is handed a digest of in-flight seats. `0` disables it. |
| `chair_timeout_sec` | `300` | Bound on a chair publication pass. |
| `max_concurrency` | `4` | Seats asked at once. The chair has its own slot. |
| `sandbox_mib` | `32768` | tmpfs shared by all member upper layers. Ignored in place. |
| `in_place` | `false` | Run members in the real project: no namespaces, no overlays, no write journal. `--in-place` forces it on. |
| `transcripts` | `off` | `off` or `full`. Full writes the verbatim protocol stream per member. |
| `pi_cli` | discovered | Path or name of the pi CLI; probed with `--version`. |

Seats and the chair also take `provider`, `model` (`provider` without `model` is refused, since pi
will not start a seat configured that way), and `persona`. An unknown model warns and falls back
rather than failing the run.

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
scratch that never enters the write journal. `/proc` shows it the orchestrator's process
information as unreadable, and its run directory is masked. When the run ends, every
namespace it created is swept — including tool shells a member detached into its own process
group — and the survivors are named in `run.json` if any remain.

The sandbox keeps members from confusing each other; it is not a cage for a hostile agent. Run it
in a VM, or in a container mounted to the project directory alone.

### In place

`--in-place`, or `in_place = true`, convenes the same council with nothing between the
seats and the project. What changes:

- Every seat works in the **same real tree**, and the briefings say a peer's half-finished edit is
  not a defect and not yours to finish.
- The **namespace sweep cannot run**: members share this namespace, so only their own process groups
  are signalled, and a tool shell that moved to a process group of its own can outlive the run.
  `run.json` records `"namespace_sweep": "none (in place)"`.

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
pi. The `sandbox` family is the only one with a platform requirement: its fixture lower must sit on
a plain filesystem (ext4, or a tmpfs such as a member's `$TMPDIR`); point `COUNCIL_TEST_FS` at a
plain directory to choose it explicitly. Everything else, `test_in_place.py` included, runs on any
POSIX host that can fork.

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
