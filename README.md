# council

A council of coding agents that reviews a project or plans a task together.

You name a council in `council.toml` — a few seats and a chair, each a [`pi`](https://github.com/earendil-works/pi)
session on whatever provider and model you like — and `council` convenes them. The seats
read the code, argue about it, and have to sign off; the chair merges duplicates, compares
plans, and watches for a seat that has stopped making progress. When the council is done
you get a report on disk, printed to your terminal, and a record of every question it
wanted to ask you.

The point is disagreement. A single agent reviewing code tells you what it thinks; a
council has to *convince* the other seats, and a claim that never persuades anybody is
reported as exactly that instead of quietly dropped.

---

## Requirements

- **Python 3.14+**
- **`pi`** on your `PATH`, or a `pi_cli` path in `council.toml`. Members are driven through
  pi's RPC mode over pipes — one long-lived child per seat, JSONL in both directions.
- **Linux with unprivileged user and mount namespaces.** Each member runs in its own
  namespace. On Debian/Ubuntu this needs `kernel.unprivileged_userns_clone=1`; Docker needs
  `--privileged` or a seccomp profile that allows `unshare(2)`.

If the kernel won't cooperate you get it straight, from the code that will actually run: a
member's launcher names the failing syscall and its errno — `mount('overlay', '…/view',
'overlay', 0x0): Operation not permitted` — and the run ends as a setup failure with exit 1
instead of half-starting. To check a container before convening anything, run the sandbox
family (`pytest tests/test_sandbox.py -q`): it builds the real sandbox — namespaces, overlays,
masks, the exit sweep — through the product's own code.

## Install

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e .            # console script: council
council --help
```

## Quick start

```toml
# council.toml, in the project root
[defaults]
max_rounds = 4

[councils.review]
goal = "Review src/ for defects a caller would actually hit."
mode = "review"

[councils.review.chair]
name = "Chair"

[[councils.review.agents]]
name = "Metic"
model = "…"          # optional; provider too

[[councils.review.agents]]
name = "Senior"
```

```sh
council review            # or: council -g "focus on the parser" review
```

You'll see the run narrate itself — what it's about to let the members touch before it
starts them, each seat coming up in its own namespace, asks landing with their durations,
the checkpoint when the council wants a human, and the sweep at the end that proves nothing
was left running. Those lines are prefixed `[council]` and go to **stdout**; usage errors go
to stderr. The report is printed last and unprefixed, so a pager shows a document rather
than a log.

## What a run does

1. **Phase 0.** Every seat gets the *same bytes* — no names, no IDs — and reports findings
   (review) or a proposed approach (plan).
2. **Merge.** The chair deduplicates: two seats describing one defect produce one item with
   two reporters, which is the difference between a finding and a hunch.
3. **Rounds.** Every seat is shown every open item and must take a stance — `agree`,
   `disagree`, `needs_human`, `abstain` — with evidence or a note. An item needs a majority
   (`⌊N/2⌋ + 1`, counted against the roster as convened) to be `agreed`; an objection with
   cited evidence reopens a settled one. A seat that stops answering gets asked, once, and
   then its silence is recorded as a *declined* abstention — inert: it neither supports nor
   objects nor holds an item open forever.
4. **You.** When a seat says `needs_human`, or the chair raises a question, you see it at a
   checkpoint. Only a question that's effectively *blocking* makes the run wait; deferred
   ones print and get asked at the end. Answering buys a full round.
5. **Termination.** `consensus`, `needs_human`, `stalemate`, or `round_limit_reached`. All
   four are results, not failures: they all exit 0.

The chair never has a stance and never gets a roster ID, so it can't move the majority bar.
It merges, compares, and judges the watchdog digest — that's all.

### At the checkpoint

```
H1: keep it 30 days          # an answer is authoritative, and buys a round
explain H2: what breaks?     # routes your question to the seat that asked; its reply is
                             # a recommendation, not an answer
skip                         # decline everything on the table
                             # a blank line ends the checkpoint
```

## What you get

| File | What's in it |
|---|---|
| `report.md` / `plan.md` | The result: consensus findings, what was never fully vetted and by whom, disputes verbatim, rejections with the notes that killed them, and the questions you never answered. Plans get stages, gates and named assumptions instead. |
| `run.json` | Machine-readable: config, roster, settings, pi version, every member's namespaces and prompt count, the write journal, the outcome, and the sweep's survivors. |
| `events.jsonl` | Append-only event log — one JSON object per line, the whole run in order. |
| `human_input.json` | Every question whatever became of it: asker, effective class, answer or skip, and the escalations each one retired. |
| `agents/` | Per-member stderr, and transcripts if you asked for them (`--transcripts full`). Absent until a member says something. |

The run directory is `$COUNCIL_RUNS_DIR`, else `$XDG_CACHE_HOME/council/runs`, else
`~/.cache/council/runs`, timestamped by project name.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | A result was written — including `needs_human` and `round_limit_reached`. |
| `1` | No result: bad config, an unusable member, an internal error, a nested-council refusal. |
| `2` | Usage error. |
| `130`/`143` | Interrupted. Every child is disposed, every namespace swept, and `run.json` finalized first. |

## Configuration

`[defaults]` sets knobs; `[councils.<name>]` overrides them. Flags beat both.

| Knob | Default | Meaning |
|---|---|---|
| `mode` | `review` | `review`, or `plan` for a task plan instead of a findings report. |
| `goal` | — | What the seats are briefed with. `--goal` overrides. |
| `max_rounds` | `10` | Round limit. No cap is imposed on it. |
| `watchdog_sec` | `600` | How often the chair is handed a digest of in-flight seats. `0` disables it — and then a wedged seat hangs the run with no diagnosis. |
| `chair_timeout_sec` | `300` | Bound on a chair publication pass. |
| `max_concurrency` | `4` | Seats asked at once. The chair has its own slot, so a digest never queues behind seat work. |
| `sandbox_mib` | `32768` | tmpfs shared by all member upper layers. |
| `transcripts` | `off` | `off` or `full`. Full writes the verbatim protocol stream per member. |
| `pi_cli` | discovered | Path or name of the pi CLI; probed with `--version`. |

Seats and the chair also take `provider`, `model`, `persona`, and an unknown model warns
and falls back rather than failing the run.

## The sandbox, honestly

Each member sees your project through an overlay: the project is the read-only lower layer,
its own writes land in an upper layer inside a tmpfs that is discarded, and its `$TMPDIR` is
scratch that never enters the write journal. `/proc` shows it the orchestrator's process
information as unreadable, and its run directory is masked. When the run ends, every
namespace it created is swept — including tool shells a member detached into its own process
group — and the survivors are named in `run.json` if any remain.

**This makes your project disposable. It does not make a member harmless.** There is no
network isolation and no `$HOME` isolation: a member can reach paths outside its view,
resolve your provider credentials, and write where your user can write. `--untrusted`
spawns everyone with pi's `--no-approve` so project-local files can't configure it, which
is a real hardening and not a boundary. Reviewing a repository you don't trust is a
different exercise, and this tool doesn't pretend to be one.

## Tests

```sh
pytest tests -q              # offline: fake pi, real namespaces, real mounts
pytest tests/test_sandbox.py -q -rs
pytest tests -m slow         # spends tokens: one real pi review of testdata/toy-project
```

The offline suite drives real orchestrator, sandbox, RPC and dispose paths against a
scripted fake pi, so it needs no provider credentials. The `sandbox` family is the only one
with a platform requirement: its fixture lower must sit on a plain filesystem (ext4, or a
tmpfs such as a member's `$TMPDIR`), and it skips — loudly, once, for that reason alone —
when the ground turns out to be an overlay. Point `COUNCIL_TEST_FS` at a plain directory to
choose it explicitly. A test skipped for any other reason fails the suite, and a test
enforces that it can't be done quietly.

## License

`council` — a council of coding agents that review a project or plan a task together.

Copyright (C) 2026 Connor Pietrasik <connorgpietrasik@gmail.com>

This program is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, either version
3 of the License, or (at your option) any later version. See [LICENSE](LICENSE).
