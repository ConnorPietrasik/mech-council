# mech-council — Build Plan

Implementation plan for the `council` CLI specified in `SPEC-PY314.md`. The spec is
normative; this plan maps it onto modules, a build order, and tests, and records a
decision for every ambiguity the spec leaves open. Spec section references (`§…`)
are to SPEC-PY314.md throughout.

---

## 0. Target machine (verified)

Single target, no compatibility layers (§1, §14.4). Verified on this box:

| Fact | Value |
|---|---|
| Python | 3.14.7 (`python3` → 3.14; `/usr/sbin/python3.14`) |
| pi | **1.4.0** (`pi --version`) at `/usr/sbin/pi`, a symlink into `@earendil-works/pi-coding-agent/dist/bundle/cli.js`; RPC docs (`rpc.md`, `rpc-commands.md`, `json.md`) installed under pi's `docs/` |
| Kernel | CachyOS, `7.3.0-2-cachyos`, x86_64 |
| User/uid | `crazybot` (1000), `CapEff 0`, seccomp off, AppArmor off |
| Userns | `kernel.unprivileged_userns_clone = 1`; `os.unshare(CLONE_NEWUSER \| CLONE_NEWNS)` verified working from Python |
| Filesystems | `/dockerx` is **ext4**, mount propagation `private`; `/` and `/tmp` are **overlay** (container root) |
| Prior art | `probes/1–5, 7–9` (no `6`; `2b` is 2's shared-parent twin) + `probe-sandbox.py` — the set `probes/run-all.sh` drives, the measured prototypes cited by §10/§13.9 (`9-lower-shapes.py` is the §3.5/§13.9 lower-layer matrix) |

Consequences:

- §13.9's fixture root is **plain-fs ground chosen at run time, not a constant**
  (`probes/9-lower-shapes.py`): a scratch dir under `/dockerx` (ext4) when the suite
  runs on the host, `$TMPDIR` (the member's scratch tmpfs) when it runs from inside a
  member — where the project path *is* the run's view, so a fixture under `/dockerx`
  sits inside a view, trips §13.9's skip, and the family reports green having tested
  nothing. The canary runs root follows the same rule: §3.5 refuses a runs root
  inside the project, and inside a member the project is the whole view.
- The two `EINVAL` arguments in §3.5 and §13.9 are conservative **fstype proxies,
  not predictions** (`probes/9-lower-shapes.py`): with the project on ext4, a lower
  inside a mounted view mounts *fine*, and a runs root inside the project mounts
  *fine* too — the view then lists its own `up`/`work`, and the `workdir` leaves a
  `work/work` at mode `000` in the tree that its owner cannot list without chmod'ing
  it first. That, not an errno, is the harm §3.5 refuses. No §13.9 assertion may be
  written as "this mount must fail with `EINVAL`".
- The 6b(4) mask test's **`/tmp` arm**: §13.9's original method — "put the fixture
  inside and outside `/tmp`" — cannot run here, so **D25** takes the arm through
  `<runs_dir>` instead and §13.9 now says so. Why the fixture must not move: `/tmp`
  here is overlay, so a fixture there trips §13.9's fstype proxy and the proxy skips
  it — not because the mount fails (`COUNCIL_PROBE_TMP=/tmp python3 probe-sandbox.py`
  is green with `lower=/tmp/…`, and that is the arm `probes/run-all.sh` runs: mask
  skipped, host `/tmp` left shared, `<runs_dir>`/`run.json` still masked, `EROFS` on a
  write to one). 6b(4)'s condition is a **disjunction** — `/tmp` is masked "only when
  `/tmp` holds neither `<project>` nor `<runs_dir>`" (§10, and `probe-sandbox.py:596`
  reads it that way) — so the family takes the arm through the other half: the fixture
  stays on plain ground and **`<runs_dir>` is what sits under `/tmp`**. On the host
  that is the real `/tmp`; from inside a member it cannot be — there `/tmp` is already
  6b(4)'s own `size=1m` tmpfs, namespace-private and far too small for a fixture runs
  root (writing it out is §10's `ENOSPC` case, not a test fixture), so the helper
  mounts its own scratch tmpfs **at `/tmp`** and puts the runs root under it — measured
  from §13.9's exact helper shape (mapped, never exec'd, caps intact): the mount
  succeeds, host `/tmp` disappears in that namespace, `/tmp/runs` is usable and the
  plain lower stays reachable. Both routes keep the lower
  plain, so §13.9's skip stays silent. What must never be written is "fixture in some
  other scratch dir": that leaves both disjuncts false, so the case quietly re-runs the
  arm the host run already covers and `/tmp` reports green having never been taken.
  (Spec patched to match: §13.9's per-member-isolation case now states both arms
  through `<runs_dir>`, and the skip paragraph says outright why a `/tmp` fixture is
  the one shape that silently skips the family.)
- `probes/` measurements are the baseline, not scripture: the sandbox family ports
  their assertions rather than re-deriving them, and a measurement that does not
  reproduce (§3.5's `EINVAL` did not, on ext4) gets re-probed and the spec line
  corrected — never quietly kept. §3.5's nested-run line is the second casualty: "a
  member's `CapEff` is `0`, so no map can be written" is false here — **D26** — and
  the measurement is now written into §3.5 itself.

---

## 1. Deliverable and scope

- One Python 3.14 package, console script `council` (project name
  `mech-council`), Linux-only, no Windows paths, no provider logic, no
  compatibility shims for other hosts/kernels (§1, §14.4–14.5). Nothing in the
  spec asks for more than the standard library (`tomllib` §3.3, the RPC child
  plumbing §4, `ctypes` `mount(2)` §10), so the runtime carries **zero
  dependencies** — recorded as an outcome, not a rule: a dependency is added
  the day it removes real work, and no §13/§14 rule forbids that. Tests use
  **pytest** — a plan choice, not a spec one: §13 fixes methods (case lists,
  byte-for-byte reports, canaries) and names no runner; see plan §6 — from a `test`
  extra, which is never a runtime dep.
- Both modes (`review`, `plan`) ship in v1; the engine is mode-generic from the
  first commit so future modes are adapters, not forks (§2 "Item", §12.1.3–4).
- Every exit path honors §3.2's contract; every §13 family is the acceptance
  list for the milestone that owns it.

---

## 2. Package layout — exactly one owner per §12.1 invariant

```
pyproject.toml                 # name mech-council, script council, requires-python >=3.14,
                               # no runtime deps; [project.optional-dependencies].test =
                               # pytest + pytest-timeout; [tool.pytest.ini_options] (plan §6)
src/council/
  __init__.py                  # __version__
  __main__.py                  # dispatch: "--setup-seat" → launcher.py, else cli.main()
  cli.py                       # §3.1 flags/positional/usage errors; table-driven flag set
  config.py                    # §3.3 loader: strict keys/types, validation, selection, flatten; hints via suggest.py
  envpaths.py                  # §3.5 runs-root resolution + containment + nested guard
  naming.py                    # §4 file_base + slugify: pure; spawn/launcher/sandbox/runjson/render all read it
  suggest.py                   # §3.1/§3.3's one suggestion function (flags, config keys, council names)
  startup.py                   # §12.1.11 pipeline owner (ordering invariant lives here)
  runjson.py                   # §3.4 run.json create / record / finalize
  console.py                   # §11.1 [council] lines + events.jsonl writer (snake_case event names)
  run.py                       # top-level flow: signal handlers, phases, exit paths, flush/os._exit
  supervisor.py                # §10 steps 1–3: fork parent; no loop, no state (§12.1.1 exception)
  pi_rpc.py                    # §4/§12.1.7 the one pi dialect boundary (framing, ids, commands, events)
  launcher.py                  # §10 6b member launcher (--setup-seat): namespace, then execvp
  asks.py                      # §12.1.8 two lanes: agent pool (max_concurrency) + chair single slot
  extraction.py                # §5.3 lenient JSON extraction, synonym tables, repair classification
  humanio.py                   # §12.1.2 stdin reader thread → thread-safe queue
  humaninput.py                # §8 checkpoint: ask set, wait rule, answer-line parsing, explain
  chairwork.py                 # §9 merge/compare/watchdog pass drivers (via chair lane)
  watchdog.py                  # §9 digest build + tick scheduler (no overlap rules)
  sandbox/
    mount.py                   # the design's only ctypes/raw-syscall module: mount(2), umount2, and step 4b's prctl (§10, §12.1.9)
    setup.py                   # §10 steps 4, 4b, 5, 6, 7 — all orchestrator side and all before the run dir (ms-private, dumpable via mount.py's prctl, tmpfs, per-member slot dirs + overlay, lower fd); only 6b is the launcher's
    memberns.py                # §10 6b five steps (runs inside launcher.py)
    journal.py                 # upper listing → created/modified/deleted + per-member bytes, and the §10 lower-fd question that flags `sandbox_artifact`
    infer.py                   # sandbox_full / sandbox_limit_hit text match (pump pass) + statvfs backstop
  process/
    spawn.py                   # §4 member spawn form (launcher argv, env, fds, ns-fd capture)
    lifecycle.py               # §4 dispose: stdin-close → killpg TERM → KILL; guards; ns sweep + the honest "which scope was killed" line §4 owes (emitted through console.py)
  deliberation/                # §12.1.10 pure: no subprocess/asyncio/path imports (test-enforced)
    state.py                   # Docket, Item, Stance, Escalation, Question, QA — wire-name dataclasses
    dispositions.py            # §5.1 derivation, closure/reopen (§5.2.3)
    escalations.py             # §5.2.8 ledger (states, challenge/defense bookkeeping, effective class)
    gaps.py                    # §6.2 gap detection, cause routing, ladder plan (pure part)
    stalemate.py               # §5.2.6 signature
    termination.py             # §5.2.7 checks, both orders + direction rule
    engine.py                  # the one round loop (§12.1.3); I/O only via injected AskPort
  modes/
    base.py                    # ModeAdapter protocol + registry (mode literal → adapter)
    review.py                  # review data/hooks only (§12.1.4)
    plan.py                    # plan data/hooks only
  prompts/
    common.py                  # shared briefing blocks (protocol rules, sandbox paragraph §5.4)
    review.py plan.py chair.py # all prompt/persona-adjacent text as data (§12.1.6)
  render/
    docket.py                  # §12.1.5 the one per-viewer skeleton (+ you/[yours] rewrite)
    report_review.py           # §6.3 report.md
    report_plan.py             # §7.2 plan.md
    preview.py                 # §9 tool-call preview (middle-out, project-relative, cd-collapse)
    text.py                    # severity sort, ID→name captions (file_base/slugify → naming.py)
tests/
  conftest.py                  # pytest fixtures over harness.py: child env scrub, canary dir,
                               # scratch tmpfs + mount teardown, CLI runner (own process group,
                               # killpg on teardown), no-unexpected-skip hook, clock/paths
  harness.py                   # fake-pi driver, canary, env scrub, CLI runner, sandbox helper
  fakepi/                      # scripted fake pi + detached tool-shell spawner (§13.7/§13.8)
  test_cli.py test_config.py test_deliberation.py test_extraction.py test_render.py
  test_rpc.py test_process.py test_sandbox.py test_startup.py test_engine.py
  test_human.py test_reports.py test_smoke.py
testdata/toy-project/          # planted-bug project for the smoke family (§13.12)
```

| §12.1 invariant | Owner |
|---|---|
| 1 one loop (supervisor exception) | `run.py` + `supervisor.py` |
| 2 stdin never blocks the loop | `humanio.py` |
| 3 one deliberation engine | `deliberation/engine.py` |
| 4 adapters hold no logic/text | `modes/` (checked by grep-test) |
| 5 one docket renderer | `render/docket.py` |
| 6 prompts as data | `prompts/` |
| 7 pi dialect behind one boundary | `pi_rpc.py` |
| 8 two mediated lanes | `asks.py` |
| 9 process/sandbox isolated, pi-free-testable | `process/`, `sandbox/`, `launcher.py` |
| 10 deliberation does no I/O | `deliberation/` (import-isolation test) |
| 11 config work precedes run dir | `startup.py` |
| 12 reports read run.json + state | `runjson.py` + `render/` |

Two more single owners that are one-function rules of the spec rather than §12.1
invariants: §3.1/§3.3's one suggestion function → `suggest.py` (needed by
`cli.py`, by `config.py` for keys and by selection for council names — writing
it twice is how hint text drifts apart), and §4's `file_base`/slugify →
`naming.py` (needed by `spawn.py`, `launcher.py`, `sandbox/` and `runjson.py` as
much as by the renderers, so it must not live under `render/`).

---

## 3. Spec interpretations and fixes (binding decisions)

The spec is internally consistent in its load-bearing rules; the items below are
gaps, contradictions, and under-specified corners found while reading, each with
the resolution this plan builds to. Where an item *changes* what the spec
literally says, it is marked **[fix]**.

- **D1 — chair spawn timing and ID assignment `[fix: applied — §1.2, §2, §3.3
  point 3, §3.4, §5.4, §6.1, §7.1, §9, §13.6]`.** Three coupled problems, one
  decision.
  **(a) The chair spawns eagerly, with the seats.** §9's tick rule fires "every
  `watchdog_sec` while asks are in flight" and phase 0 has asks — but with a lazy
  chair that tick has nowhere to go, and the watchdog is the *only* bound on a wedged
  seat: nothing in the design puts a deadline on an agent ask and §14.5 forbids
  adding timer caps, so a lazily-spawned chair would leave a hung phase-0 seat
  outranked only by a signal. Eagerness costs nothing — a spawn is checked by
  `get_state`, not a prompt (§3.3 point 2) — and the chair's first *deliberative*
  act is still the publication pass, so §9's independence claim is now written as
  scoped to deliberation rather than to time.
  **(b) So IDs are assigned at convening and revealed after phase 0.** An eager
  chair needs a legal label for a phase-0 digest, and `AgentN` was unavailable until
  phase 0 ended. Rather than invent a second label vocabulary, §1.2/§2/§3.4/§6.1/
  §7.1 now assign `Agent1..N` when the council is convened and reveal them in round
  1's dockets. That keeps one vocabulary everywhere (digest labels `AgentN` in every
  phase, §5.4's resolver stays "exactly those forms, nothing fuzzier", and
  `watchdog_unmatched` keeps meaning "the chair named a non-member" instead of
  firing on our own label choice). The deferral never hid anything anyway: `AgentN`
  ≡ `slot<N>` by construction (§4's `file_base`), and §4's
  `--name council-<file_base>` plus every sibling's `--append-system-prompt` are
  readable in one pid namespace (§10 admits the `/proc` channel — verified: a
  same-uid sibling's full argv reads back; D12 now clears its own slot var before
  `execvp`, which removes one leak of `slot<N>` and leaves this one, which §4 fixes
  in argv). §1.2 therefore
  states the real rule: **the phase-0 briefing is byte-identical for every seat and
  carries no ID, no slot and no peer name** (§5.4, §6.1), enforced by an assertion
  (M9) and not by a calendar.
  **(c) Unusable-chair keying.** With (a) the first chair ask may be a digest, so
  §3.3's unusable test keys on the chair's first **publication** pass and a failing
  digest is an ordinary §4 restart-and-re-ask. Otherwise "exit 1 after one prompt"
  versus "restart once first" would depend on whether a tick beat the merge, and
  §13.6's *exactly one prompt each* would be a coin flip at the default
  `watchdog_sec = 600`.
- **D2 — review goal optional.** §7.1 requires a goal for plan ("a `plan`
  council with no goal stated anywhere is a config error") and §3.3 makes that a
  load-time check on the convened council, not a flag; §3.3 also says the goal "is
  what the seats are actually briefed with". **The spec defines no default goal
  text anywhere**, so review with no goal anywhere is a plan decision: the §5.4
  review briefing goes out with no goal line, and the console says the run has no
  stated goal — never an invented one. (For contrast, the spec's own example
  council writes `goal = "See if there is anything here that should be fixed or
  improved."` — that is a config file's text, not a built-in, and nothing may
  quote it as a default.) Plan mode: missing goal → config error, exit 1, at load.
- **D3 — human questions exist in review too, and phase-0 `open_questions` [fix].**
  One rule covers both gaps: **asking the human is not a plan privilege.**
  (a) The spec fenced `human_input` into plan verdicts — §2's Verdict row read
  "(plan) human_input/revised_plan/backing", §5.3 "Plan verdicts additionally carry
  `revised_plan`, `backing`, `human_input`" — while §5.2.8's item-less rule and §8's
  ask set were already mode-generic, so a review seat could put a question to the
  human only by stancing `needs_human` **on an item**. Built: every seat verdict
  carries `human_input` in both modes, and the only plan-only verdict fields are
  `revised_plan` and `backing`. A review question naming an item is an escalation
  request under §5.2.8's bar exactly as in plan; one naming no item is item-less —
  never effective-blocking, asked at the terminal checkpoint — so review still cannot
  stop the run by going around its items. Review's docket shows the open requests'
  effective class through §12.1.5's shared skeleton: a section-list change for the
  adapter (plan §4.2 already lists `human_input` as both-modes), never a second renderer.
  (b) §6.1 had phase 0 emit `open_questions` and never said where they go: they are
  ingested as **item-less human-input requests** (H-ids, round 0), so ingested this
  way they *are* §5.2.8's item-less questions, which §8's ask set already enumerates —
  §13.10's ask-set-exactness case counts them and §6.3's per-question fields (asker,
  round, effective class, end state) are all populated; §6.3 prints an item-less
  question on its own, with no item line. (Spec patched to match: §2's Docket and
  Verdict rows, §5.3's schema list and verdict bullet, §5.4's review escalation
  briefing, §6.1 steps 1 and 3, §6.3, §12.1.5, and §13.2's new review-seat-question
  case.)
- **D4 — signature ordering [fix].** §6.1's review round list omitted the
  stalemate-signature recording that §5.2.6 mandates before the checkpoint;
  §7.1 had it. Built uniformly: …closure pruning → round summary → **signature
  recorded** → checkpoint → termination check, both modes. Stalemate fires iff
  `sig(N) == sig(N−1)` **and** the checkpoint just taken recorded no answers and
  made no retirements (an answer *or* a retirement is movement, §5.2.6/§8). (Spec
  patched to match: §6.1 step 3 now carries the signature step, so review and plan
  read the same way.)
- **D5 — checkpoint wait rule via provisional evaluation.** The checkpoint
  evaluates §5.2.7 provisionally on pre-checkpoint state (§8): whole ask set
  waited if provisional outcome ∈ {consensus, needs_human, stalemate};
  effective-blocking-only if keep-going; print-only, never wait, if
  `round_limit_reached`.
- **D6 — "asked and declined" forces `needs_human`.** §5.2.7's blocking check
  reads "unanswered — still standing, or asked and declined", and §13.2's
  chair-question case confirms: a skipped effective-blocking question forces the
  `needs_human` outcome even if the item then derives `agreed` (retirement makes
  the asker abstain-equivalent for *item derivation*, not for the *outcome*).
  An answered question never forces it. The check consults the question record,
  not just escalation state.
- **D7 — transcript content.** `agents/<file_base>.transcripts.jsonl` is the
  child's stdout stream verbatim, appended record-by-record as the RPC client
  reads it. Child stderr is always captured: streamed to
  `<file_base>.stderr.txt` when transcripts=full; buffered and dumped lazily
  (created `agents/`) when non-empty or on crash when off (§3.4 sink rule).
- **D8 — suggestion function.** One function (§3.1/§3.3), reached only for a name
  the parser has already refused — an accepted spelling never gets there, so there
  is no "it matched, so stay silent" case to define, and a **normalized** match is
  the hint's best answer rather than a reason to withhold one (§3.3 normalizes
  precisely so `chairTimeoutSec`, raw distance 4, still hints). Normalize
  (casefold, drop `_`/`-`, strip leading dashes) both sides, then, in order:
  1. **exact normalized match → that name.** This is §13.4's whole hint row:
     `maxRounds`, `maxrounds` and `chairTimeoutSec` each normalize onto
     `max_rounds`/`chair_timeout_sec`, and §3.1's `--p` "answers the short spelling
     exactly" is an exact normalized hit on the short form `p`, so the canonical
     entry it prints carries both spellings (`--project/-p`).
  2. **prefix match either way round → that name** — the input is a prefix of one
     known name (`--ro` → `--rounds`) *or a known name is a prefix of the input*
     (§3.3's `mode = "planning"` → `plan`: normalized distance 4, so no threshold
     reaches it and the one-directional reading of "prefix match" answers nothing).
  3. else **Levenshtein over the normalized forms**, ≤1 edit for tokens <6 chars,
     ≤2 for ≥6.
  An ambiguous step 2 is resolved by the closest step-3 neighbour among its
  candidates (`review-stagedd` → `review-staged`, not both), and two *known* names
  that normalize alike (possible for council names, which are user text; not for
  the fixed flag/key sets) print both rather than one being picked. No match at any
  step → no hint at all: §3.1's restraint is the point. Suggests the canonical
  spelling, never accepts. (Ladder checked against every example the spec states —
  `--p`, `--ro`, `--alownested`, junk, `maxRounds`, `maxrounds`, `chairTimeoutSec`,
  `maxRound`, `planning`, council-name typos — and against the one-directional
  prefix variant, which fails `planning`.)
- **D9 — provider/model pairing.** `provider` without `model` is a config
  error (§3.3); `model` without `provider` is allowed (pi resolves it).
- **D10 — `[defaults]` key set.** Exactly the scalar knobs: `max_rounds`,
  `watchdog_sec`, `chair_timeout_sec`, `max_concurrency`, `sandbox_mib`,
  `transcripts`, `pi_cli`. Mode/goal/agents/chair are council-level only.
- **D11 — exit mechanics.** Once the run directory exists, every exit finalizes
  `run.json` → flushes `sys.stdout`/`sys.stderr` → exits normally; only the
  last-resort sweep path uses flush → `os._exit`. Usage errors (exit 2) and
  pre-run-directory failures exit with no artifacts (§12.1.11 ordering).
- **D12 — member env [fix: applied — §3.5 (new row + the nested rule), §4's env
  sentence].** Members inherit the orchestrator env plus exactly three
  council vars: `COUNCIL_RUN_DIR` (basename — §3.5's nested marker),
  `COUNCIL_RUN_PATH=<runs_dir>/<run-dir>` and `TMPDIR=<project>/.council-scratch`.
  `COUNCIL_RUN_PATH` earns its place by doing one job nothing else can: §3.5's nested
  refusal must *print the parent's run directory*, and the only way to get that path
  inside a member is to be told it — re-resolving the inherited `COUNCIL_RUNS_DIR`
  resolves a relative value against **this** invocation's cwd, which inside a member is
  `<project>`, i.e. the view (§10 6b(3)+6b(5)), so a rebuilt path can name a directory
  that was never the parent's (the
  same cwd-derived accident D12b refuses for the launcher). It is **print-only**: never
  a marker (`COUNCIL_RUN_DIR` alone refuses), never a lookup, never a fallback source.
  That is why it belongs in §3.5's table rather than hiding beside D12b's three: unlike
  those, a seat can read it and a user-facing message prints it, so pretending otherwise
  would be the dishonesty this plan keeps refusing. The three paths 6b needs
  (D12b) are set for the launcher and **erased from the env before `execvp`**, so pi
  never sees them — which also stops handing a seat its own `slot<N>` and eroding
  §1.2's reveal rule from inside the sandbox. What a member *can* still read is
  stated honestly rather than wished away: `COUNCIL_RUN_PATH` (a path inside the
  region 6b(4) masks — readable, unreachable) and `TMPDIR` (its own scratch, which
  6b(3) has just rebound for it, and which nested council CLIs must see). No var
  claims to be invisible; §10's honesty list already says same-uid siblings read
  each other's `/proc`.
- **D12b — the launcher's paths are stated, never inferred.** 6b needs `<project>`
  (6b(3)'s rbind target, 6b(5)'s `chdir`, and the `/tmp`-ancestor test of 6b(4)),
  `<runs_dir>` and `<runs_dir>/sandbox` (6b(4)'s masks), and its own slot dir.
  Deriving any of them from `cwd` or from a parent directory is how a step ends up
  working by accident, so `spawn.py` sets `COUNCIL_LAUNCH_PROJECT`,
  `COUNCIL_LAUNCH_RUNS_ROOT` and `COUNCIL_LAUNCH_SLOT_DIR`; the launcher reads them,
  mounts from them, and clears all three before `execvp`. The `LAUNCH_` infix is
  deliberate: `COUNCIL_RUNS_ROOT` would sit one word from §3.5's user-facing
  `COUNCIL_RUNS_DIR`, and §3.3's naming rule exists so one grep finds a knob's every
  appearance — these three are not knobs. Each holds an absolute path the launcher
  mounts, never a relative one (§3.5's "nothing relative reaches §10"). Env rather
  than argv so §4's
  and §12.2's spawn shape — `--setup-seat --slot <file_base> -- <pi_cli> …` — stays
  exactly as written and §13.5's argv assertions keep seeing the shape production
  uses. All three are internal and deliberately absent from §3.5's table: that table
  is the user-facing contract, and `COUNCIL_RUN_DIR` stays the only marker a refusal
  keys on (§3.5's nested guard, whose `COUNCIL_RUN_PATH` row now says the same out
  loud: print-only, never a marker).
- **D13 — "cited evidence" made structural.** Rejection (§5.1), reopen
  (§5.2.3), and challenge (§5.2.8) all turn on §5.1's parenthetical — "a
  `disagree` with cited evidence, or a note that refutes it". Structural rule: a
  `disagree` whose `evidence` **or** `note` is non-empty. Unreasoned (both empty)
  → never rejects, never reopens, never challenges — recorded as a note on the
  closed item. Prompts demand evidence in the `evidence` field.
- **D14 — "fullest backer's plan" (§7.2).** If the converged token is
  `merged: <label>` and no `revised_plan` named `<label>` exists: among seats
  backing that token, print the plan artifact (latest `revised_plan`, else
  original approach) with the most stages; tie → lowest slot; labeled "the
  merge was never written down".
- **D15 — constants, not knobs.** `ABORT_TIMEOUT_S=30` (abort-response bound,
  §4), `TERM_GRACE_S=10` (killpg escalation), `DISPOSE_SWEEP_AFTER_S=15`
  (§3.2), `WATCHDOG_PASS_TIMEOUT_S=120` (§9), mask tmpfs `size=1m` (§10 6b(4)).
- **D16 — non-interactive stdin.** EOF or not-a-tty → every checkpoint
  immediately declines everything (logged), per §5.2.8; the run proceeds.
- **D17 — run-dir artifacts [fix].** §2 says the run directory holds "the
  manifest, log, report, human-input file and transcripts" but §3.4's table
  lists only three. Added: `events.jsonl` (one JSON object per log event;
  snake_case names: `run_aborted`, `item_reopened`, `verdict_unknown_findings`,
  `gap_declined`, `escalation_withdrawn`, `question_updated`,
  `human_checkpoint`, `watchdog_unmatched`, `sandbox_full`,
  `sandbox_limit_hit`, `tmp_shared`, `council_stderr_dump`, …) and
  `human_input.json` (requests, answers, skips, retirements). (Spec patched to
  match: §3.4 now carries both rows, so §13.11's case list has five artifacts.)
- **D18 — signature content [fix].** §5.2.6 listed answered-QA and
  answered-human-input counts as plan-only signature inputs; review Q&A exists
  too (§5.3), so a review round whose only movement was a new answer would have
  looked like no movement at all. Built mode-generic: item states + signoffs +
  (plan) backing map + answered-QA count + answered-human-input count.
  "Stalemate = no movement" is the intent; more movement signals, fewer false
  stalemates. (Spec patched to match: §5.2.6 now leaves only the backing map
  plan-only.)
- **D19 — backing gaps [fix].** §6.2's mechanical-cause row said "a *signed-off*
  plan verdict with no backing", while §6.2's own prose and §5.2.7's direction
  condition 1 make an omitted/unmappable token a gap whatever the sign-off.
  Built: missing or unmappable `backing` is always a mechanical gap. (Spec
  patched to match — the row was the narrower half, not the rule: §6.2's
  mechanical row now reads "signed off or not", so §13.2's cause table has
  exactly one answer.)
- **D20 — pi resolution.** Chain: the convened council's `pi_cli`, else
  `[defaults]`, else `pi` on PATH — **no env-var step** (§3.3 records the same,
  and §3.5 lists no pi variable: an override any config key outranks shadows only
  a run that stated nothing, which is PATH's job, and it would let a stray shell
  variable masquerade as the config's choice in `run.json`; on this box `pi`
  resolves to `/usr/sbin/pi` 1.4.0, so "config states nothing" is the working
  case, not a corner). `pi_cli` containing `/` must be an existing
  executable file; otherwise `shutil.which`. Record resolved path +
  `pi --version` output in `run.json` (§3.3).
- **D21 — launcher invocation.** Members spawn as
  `(sys.executable, "-m", "council", "--setup-seat", "--slot", <file_base>, "--", <pi_cli>, …)`
  exactly as §4 writes it, resolved once at load — never `sys.argv[0]`, never a
  PATH guess. Everything the launcher needs beyond `--slot` arrives in D12b's env,
  so no internal flag ever appears in a spawn argv.
- **D22 — RPC reading.** Raw chunk reads from the child's stdout into a buffer
  split on `b"\n"` only (§4's second blessed option): immune to the
  `str.splitlines()` trap, to asyncio `LimitOverrunError`-as-`ValueError`, and
  to line-size caps by construction. Continuous reading is the backpressure.
- **D23 — run directory naming.** `<runs>/<YYYYMMDD-HHMMSS>-<slug(project
  basename)>/`, collision suffix `-2`, `-3`, …; `COUNCIL_RUN_DIR` = basename.
- **D24 — chair merge passes in every mode [spec error, patched in place].**
  The original spec scoped dedup merge passes to review (§9 job (a); §7.1's
  round list had no chair step), but §9's own Questions line said "any chair
  pass — merge or compare, **either mode**", and §2 makes the Item abstraction
  shared and mandatory
  (§12.1.3: one engine, no second implementation). Left as written, an 8-seat
  plan council whose 5 seats each raise the same decision point publishes 5
  near-duplicate topics that every seat must stance every round — exactly the
  bloat review's merge pass exists to prevent, with the same
  "before the gap ladder, so gap prompts use canonical IDs" rationale (§6.1).
  **Fix:** new items raised in rounds route through a chair merge pass in
  **both** modes, at the same position (after verdicts, before the shown-set
  snapshot); §6.1's structural validation and conservative-merge rule apply
  unchanged; the pass runs only in rounds where new items arrived. Plan's
  publication pass remains the comparison, and the plan-mode chair persona
  gains review's constraint: merge only the same underlying decision, never
  wording or scope. (Spec patched to match: §9 job (a), §7.1 step 4's round
  order, and §5.2.8's pass enumeration.)
- **D25 — §13.9's `/tmp` arm is taken through `<runs_dir>`, never the fixture
  `[fix: applied — §13.9 (isolation case + the skip paragraph)]`.** The method for
  6b(4)'s two branches was "test both branches by putting the fixture inside and
  outside `/tmp`", which cannot run on this box in *either* direction: `/tmp` is the
  container root's overlay, so a fixture there trips §13.9's own fstype proxy and the
  family prints `SKIPPED` and exits 0 having tested nothing; and from inside a member
  `/tmp` is already 6b(4)'s own `size=1m` tmpfs, namespace-private and too small for a
  fixture runs root (writing one there out is §10's `ENOSPC` case). The condition is a
  **disjunction** over `<project>` *or* `<runs_dir>` (§10, `probe-sandbox.py:596`), so
  the arm is taken through the second half: fixture on plain ground, **`<runs_dir>`**
  under `/tmp` (the real one on the host, the helper's own tmpfs mounted at `/tmp`
  inside a member). Measured before writing this down: `COUNCIL_PROBE_TMP=/tmp
  python3 probe-sandbox.py` is green with `lower=/tmp/…`, mask skipped, host `/tmp`
  left shared, `<runs_dir>`/`run.json` still masks with `EROFS` — and it is the arm
  `probes/run-all.sh` runs. This is a **method** claim, and §13.12 says §13.9 owns the
  method claims the rest of the spec cannot derive, so leaving it unpatched was
  precisely the "suite written against a document the build contradicts" failure §9
  forbids.
- **D26 — what stops a nested run is the marker, not the map write
  `[fix: applied — §3.5]`.** §3.5 justified part of the nested refusal with "a
  member's `CapEff` is `0`, so no map can be written (§10)". Measured false here, in
  the member shape itself: an `exec`'d process at `CapEff 0000000000000000`, uid 1000
  in a 1:1-mapped userns, forked a child that `unshare`d `CLONE_NEWUSER|CLONE_NEWNS`
  and wrote that child's `setgroups=deny` + `uid_map` + `gid_map` 1:1 — all three OK.
  §10's step 3 already said why: the writer sits *outside* the new namespace and needs
  no privileges. §3.5 now states the three real facts instead — the `COUNCIL_RUN_DIR`
  marker is the guard (hence `--allow-nested` per invocation, never inherited); a
  nested run that gets past it with the masked runs root dies at §10 step 5, `EROFS`
  on the first write under `<runs_dir>`; and one whose runs root resolves elsewhere is
  **not** stopped by §10 at all, because `$HOME` is reachable by construction (§10's
  out-of-scope list). A wrong reason in the spec is a reason someone later leans on.

---

## 4. Core design notes

### 4.1 The engine (§12.1.3, §6.1, §7.1)

One round loop, mode-parameterized. I/O enters only through an injected
`AskPort` (agent pool / chair lane), so §13.2 drives it with a fake factory.

```
phase0  → one briefing, byte-identical for every seat and carrying no ID/slot/peer
          name (§1.2, §5.4): ask all seats (pool), classify unusable (§3.3), parse
          (2 repairs §5.3); <2 valid → aborted + minimal report (exit 1)
docket  → IDs were assigned at convening (§1.2) and run.json carried them from
          creation (§3.4); round 1's dockets are where seats first see them
publish → review: chair merge pass → canonical items   (validation per §6.1; refused plan → unmerged, continue)
          plan:   chair comparison → approaches + topics(T…) + agreed points
          plan only: checkpoint (§7.1)
rounds  → verdicts (pool) → chair merge of new items (both modes, D24;
          only when something new arrived) → shown-set snapshot
          → gap ladder (§6.2) → challenge/defense (§5.2.8) → closure pruning/reopening (§5.2.3)
          → round summary → signature (D4) → checkpoint (§8, D5) → termination (§5.2.7, D4/D6)
          → next round | outcome
report  → render(run.json, final state; injected clock/paths) → finalize run.json (D11)
```

Termination, first match wins — then stalemate, then round limit:

- *review:* not everyone signed off → go · `disputed`/`unseen` exist → go ·
  effective-blocking unanswered (standing, or asked-and-skipped, D6) →
  `needs_human` · else `consensus` (no standing escalation can survive the
  terminal checkpoint, §5.2.7).
- *plan:* not everyone signed off → go · effective-blocking unanswered
  (standing, or asked-and-skipped, D6) → `needs_human` — this check precedes
  the dispute checks · `disputed`/`unseen` topics → go · direction rule
  (§5.2.7, three conditions: every seat signed off and holds a `backing` named
  in the judged round — `no_preference` counts, `declined` fails; all current
  backings equal token T or `no_preference`; ≥1 seat named T for real) →
  `consensus` or go.

### 4.2 Mode adapters (§12.1.4) — the extensibility seam

Adapters are declarative; shared code owns all arithmetic and text:

| Adapter supplies (data) | Shared code that consumes it |
|---|---|
| `mode` literal, item noun ("finding"/"topic"), id prefix (`F`/`T`) | `dispositions.py`, `render/docket.py` |
| phase-0 schema name, synonym additions | `extraction.py` |
| verdict extra fields (plan: `revised_plan`, `backing`; both modes: `human_input`) | shared verdict applicator in `engine.py` |
| chair pass flavors (publication: merge / compare; in-round merge text per mode, D24) | `engine.py`, `chairwork.py` |
| consensus kind (`open_work` / `direction`) | `termination.py` owns both rules |
| docket/report section lists | `render/docket.py`, `render/report_*.py` |

A grep-test asserts no `import asyncio|subprocess|tomllib`, no file/network
I/O, and no prompt literals inside `modes/` (§12.1.4's "readable in one
sitting" smell test).

### 4.3 Ask lanes (§12.1.8, §5.3)

- Agent pool: semaphore `max_concurrency`; every ask records in-flight activity
  (elapsed, idle, event count, last tool call) for the watchdog; owns the two
  repair re-asks, the gap ladder's asks, and §4's restart-and-re-ask-once.
- Chair lane: one slot, outside the pool, bounded by `chair_timeout_sec`
  (merge/compare) or 120 s (watchdog pass); same repair rules; never queued
  behind agent work. The chair spawns eagerly with the seats (D1), so ticks cover
  phase 0 and digests label seats `AgentN` in every phase — IDs exist from
  convening (§1.2). A failing digest takes the ask-failure path below; only a
  failing **publication** pass is §3.3's unusable verdict.
- Ask failure (no message arrived) → §4 path: fresh spawn, re-ask once, second
  failure → dispose + minimal report + `aborted`/exit 1. A message that arrived
  and was rejected → gapped on everything shown (§6.2), never a §4 path.

### 4.4 Sandbox flow (§10)

`startup.py` order (§12.1.11, each step before the next, failures fatal with
one errno line, exit 1, nothing created):

config load+validate+select → runs-root resolution (pure) → pi discovery (D20)
→ **mkdir runs root 0700** → fork: supervisor (map 1:1, forward signals, relay
status) / orchestrator (`unshare`, `ms_private /`, `prctl PR_SET_DUMPABLE 0`,
tmpfs at `<runs>/sandbox` — §10 steps 2, 4, 4b, 5) → per member:
`up/work/view/scratch` + overlay (step 6, one pair per slot, mounted once and
reused across restarts) → keep the lower-layer dir fd (step 7) → **only then**
run dir + `run.json` → spawn through the launcher (6b inside). §12.1.11 puts
steps 1–7 *before* the run directory and defers only 6b to spawn, so a step-6
`mount` failure still owes §10's and §13.9's "no run directory": making the slot
dirs and overlays after the run dir exists would fail M8's injected-`EPERM`
inventory assertion for the one step most likely to fail.

Members spawn through `launcher.py`: 6b's five steps (unshare CLONE_NEWNS →
scratch rbind → view-over-project rbind → masks with the ancestor-skip rule →
chdir), report namespace id on the spawn pipe as the last act, then `execvp`.
One overlay pair per member, mounted once, reused across restarts (EBUSY
guard). Journal, inference pass, and statvfs backstop per §10/§13.9; `probes/`
assertions are ported, not reinvented.

Who owns §10's inference, said once so M6 and M8 cannot decide differently:
`pi_rpc.py` decodes `tool_execution_end` (`toolName`, `result`, `isError`) and
hands the record to **one injected callback**; `sandbox/infer.py` owns the
compiled alternation, the tail rule and the per-round `statvfs`; `console.py`
emits the line. Nothing in `pi_rpc.py` matches sandbox text — §12.1.7 stays the
only place pi's dialect is spoken — and nothing in `sandbox/` speaks it.

### 4.5 Human checkpoint (§8, §12.1.2)

`humanio.py` runs a daemon thread reading stdin lines into a queue; the
checkpoint awaits it while the loop stays live (explain round-trips keep
working; watchdog keeps covering in-flight asks; a parked wait issues no tick).
Lines: `H<n>: text` (answer), `explain H<n>` / `ask H<n>: <question>`
(asker-routed recommendation via mediated lane; chair-raised refuses), `skip`,
empty line ends the checkpoint. EOF → D16.

---

## 5. Build stages

Dependency-ordered; every stage ends with green tests and runs offline except
where noted. Families in bold are the stage's acceptance criteria.

### M1 — Skeleton + CLI contract *(§3.1, §3.2 usage paths)*
`pyproject.toml` (no runtime deps; `test` extra; `[tool.pytest.ini_options]`
per plan §6), `tests/conftest.py` (fixtures over `harness.py`: scrubbed child env,
canary, the CLI runner — which spawns into its **own process group and kills it
on teardown** (plan §6) — the no-unexpected-skip hook, injected clock/paths: built
once here, used everywhere), `suggest.py`, `naming.py`, package skeleton,
`cli.py` (table-driven flags: short forms, `=` forms, bare `--`, last-wins +
override note, one-page help; D8 lives in `suggest.py`), `__main__` dispatch,
version.
**test_cli.py**: §3.1's flag rules as the case list (`parametrize` — one node
id per row); help to stdout exit 0; two-line complaints to stderr exit 2;
`--p` → `--project`. Of §3.1's usage trio only the **second positional** is
flag-shaped work at this milestone: here `cli.py` checks the positional *count*
and hands the name to `config.py`, so the two rows that name councils (unknown
name, no name in a multi-council file) are asserted in M2, where selection lives.

### M2 — Config, env, startup ordering *(§3.3, §3.5, §12.1.11)*
`config.py` (tomllib binary-mode, strict keys/types at every level, §3.3's
validation list verbatim, precedence flag→council→defaults→builtin, council
selection + hints through `suggest.py`, `.json` refusal, D2/D9/D10), `envpaths.py`
(three resolution rules, relative handling, containment test pure + mkdir last,
nested guard), `startup.py` pipeline skeleton with ordering hooks.
**test_config.py** (§13.4: the §3.3 example is normative input), **test_cli.py**
addendum (§13.5: resolution rules with canary + inventory proving no run dir /
no mount call on refusal; nested-guard refusal **both ways** (§13.5: with
`COUNCIL_RUN_DIR` set it refuses and prints `COUNCIL_RUN_PATH` verbatim; with
`COUNCIL_RUN_PATH` alone it does not refuse at all); **the two council-name rows of
§3.1's usage trio** — exit 2, before anything spawns, one parse spent) and the
§13.6 ordering subset that needs no children: a config error or an unresolvable
`pi_cli` leaves the canary empty.

### M3 — Deliberation core, pure *(§5.1, §5.2, §5.2.6–8, §6.2)*
`state.py`, `dispositions.py`, `escalations.py`, `gaps.py` (planning only),
`stalemate.py`, `termination.py` (both orders + direction rule), import-
isolation test. Every §5.1 boundary case is an assertion (input → disposition).
**test_deliberation.py** (§13.1: the enumerations are the case lists; ledger
transitions; effective-class grant at/above/below the bar; signature churn on
reopen→reclose; render-unit tests for preview/slug/answer-parser move to M5).

### M4 — Extraction and leniency *(§5.3)*
`extraction.py`: balanced-JSON scan (every `{` until parse), fence tolerance,
case/separator folding, synonym tables (stances, backing grammar, withdraw
family), severity/stance degradation, repairable-vs-invalid classification,
`declined` never accepted from a seat, closed-ID positions never land in
`verdict_unknown_findings`.
**test_extraction.py** (§13.3, every clause on canned payloads).

### M5 — Renderers, pure *(§12.1.5, §6.3, §7.2, §9, §8)*
`render/docket.py` (skeleton + you/[yours] rewrite + closed one-liners with
IDs), `render/report_review.py`, `render/report_plan.py`, `render/preview.py`,
`render/text.py`, and the §8 answer-line parser (a pure function in
`humaninput.py`; the module is created here, wired in M10). All pure;
clock/paths injected; byte-for-byte comparable.
**test_render.py** (§13.1 render bits + §13.11 against synthetic state: no
sub-bar item under Consensus; skipped/unasked markers inline; chosen direction
resolution incl. D14; assumptions carry every deferred/skipped/unasked/
undefended question; sandbox_artifact honesty).

### M6 — pi RPC client + fake pi *(§4, §13.7)*
`pi_rpc.py`: the protocol over an injected child spawner (production passes
`process/spawn.py`'s launcher form; tests spawn the fake directly),
`get_state` first, subscribe-before-prompt, id correlation, `agent_settled`
ends an ask, `handled`/`queued` are
errors, `clear_queue`+`abort` with own bounded timeout (D15), event decoding
(`message_end`, `tool_execution_end`, auto_retry records), D22 reading.
`tests/fakepi/`: scripted JSONL scenarios (happy review/plan, spawn-fail,
first-prompt error shape with auto_retry×3 + stopReason on `message_end`,
unknown-model stderr, 3 MB line, U+2028/U+0085 line, wedged child, detached
tool-shell spawner, unknown-command reply). `fakepi/pi` is a **standalone
executable** (shebang + `chmod +x`, scenario chosen by `FAKEPI_SCENARIO`):
`pi_cli` resolves at config load to **one executable** (§3.3: "spawned directly, so
any executable form works; unresolvable = one clear error at load"; D20), and §13.5
asserts the launcher "execs whatever `pi_cli` resolved to and adds nothing to its
argv" — the module form `(<sys.executable>, "-m", <package>)` is §4's shape for
`<launcher>` (§4's Spawn shape) and for nothing else, so fake pi is a standalone executable
from the first commit and no fixture grows a spawner. Consequence for ordering:
resolution failure is a §12.1.11 config-stage refusal that leaves the canary empty
(§13.6), while an executable that resolves and then fails to `exec` stays §3.3's
shape 1 — after the run dir exists.
**test_rpc.py** (§13.7: §4's rules are the case list).

### M7 — Process lifecycle *(§4, §3.2, §13.8)*
`process/spawn.py`, `process/lifecycle.py`: `start_new_session=True`, stdin-close →
killpg TERM → KILL escalation, pid/pgid guards, namespace sweep keyed on held
ns fds (the orchestrator's own plus every fd recorded per spawn, §4/§3.4),
signal handlers → dispose without restart, second-signal no-op, ~15 s sweep,
flush/`os._exit`, `run_aborted` event, and the §4 line naming **which scope the
sweep actually killed** (the numbers are §3.4's `survivor_pids`/`survivor_ns`, the
line is `console.py`'s, emitted from here) (CLI wiring of these lands in M9/M11).
**test_process.py** (§13.8: detached tool-shell fake; one survivor after
dispose with cleanup suppressed; the sweep's namespace set matches §13.8's
"precisely those, never a host pid" — incl. a restarted member's descendant and a
setsid grandchild; recorded ns ≠
orchestrator's own; no host pid ever matched; the sweep line names the scope that
died and agrees with `run.json`'s survivor fields (§4)).

### M8 — Sandbox *(§10, §13.9)*
`supervisor.py`, `sandbox/mount.py|setup.py|memberns.py|journal.py|infer.py`,
launcher wiring, `runjson.py` (create/record ns inodes/file_base/finalize with
journal), lower-layer fd classification, statvfs backstop, pump inference
(`isError`-filtered, tail alternation), `tmp_shared` labeling.
**test_sandbox.py** (§13.9 in full: step-4 hazard built not diffed; 6b(3)
canonical-path coverage with byte-identical real tree; absolute-path
regression; per-member isolation across **both arms** of 6b(4)'s `/tmp` condition —
host `/tmp` masked (neither the project nor `<runs_dir>` under it) and mask-skipped
with `tmp_shared` (`<runs_dir>` under `/tmp`: the real one on the host, the helper's own
tmpfs mounted there from inside a member), fixture on plain ground either way (§0); journal
incl. 0:0 deletions and `.council-scratch` skip; errno table `EIO`/`EXDEV`/`mv`;
`EBUSY` remount + restart reuse; injected-EPERM setup failure → exit 1, zero
children, no run dir; 6b failure via spawn pipe).

### M9 — Ask lanes + engine *(§12.1.8, §6.1, §7.1, §9, §13.2, §13.6)*
`asks.py` (pool + chair slot + repair/restart rules), `chairwork.py`
(publication merge/compare + in-round merge passes in both modes (D24),
structural merge validation, D1), `watchdog.py`
(tick scheduler, no-overlap rules, digest via `render/preview.py`, `AgentN` labels
in every phase (D1)),
`modes/review.py` + `modes/plan.py` + `prompts/` (adapter data and text,
plan §4.2 — the modes grep-test lands here), `run.py` wiring,
`deliberation/engine.py` wiring, unusable-seat classification (§3.3 point 3),
phase 0, IDs, publication, rounds.
**test_engine.py** (§13.2 in full: cause table; 4-ask ceiling with 1 for
deliberate; zero-ask closed-only rounds; second ask failure → aborted exit 1
for seat *and* chair; refused merge plan continues; lanes starve-proof;
challenge economics; deferred-vs-blocking checkpoint behavior; answer-buys-a-
full-round in both modes; no escalation stands in a consensus report; a review
seat's item-less question and a phase-0 `open_questions` entry join the ask set and
are never effective-blocking however many seats raise them (D3);
in-round merge in both modes (D24): five near-duplicate new topics collapse to
one canonical item with `also_reported_by`, a refused merge plan leaves the
docket unmerged and the run continues; **§1.2/§5.4 hygiene**: the phase-0 briefing is
byte-identical across every seat and matches no `Agent\d+`, and a seat restarted in
phase 0 is re-asked with those same bytes (§13.6); **chair timing (D1)**: a chair
whose first *digest* settles on `stopReason: "error"` restarts and re-asks once (§4),
while a chair whose first *publication pass* does exits 1 naming the chair after
exactly one prompt (§3.3 point 3), and a forced phase-0 tick labels seats `AgentN`
and resolves with `watchdog_unmatched` empty (§9); `watchdog_sec = 0` asserts what
§9's off-bullet says — a wedged seat is then bounded by signals alone, so the case
documents the absence of a rescue rather than a rescue; §13.5's argv assertion: every spawn
argv carries `--approve` (and `--no-approve` with `--untrusted`) across seats,
chair and every restart, run otherwise identical), **test_startup.py**
(§13.6's member shapes: spawn failure and first-prompt `stopReason: "error"`
exit 1 naming member/provider/model after exactly one prompt each;
unknown-model stderr warned, never refused; a member failure leaves a run
directory holding only the minimal report and the finalized `run.json`; the chair's
unusable shape is presented on its first publication pass and its digest failure on
the restart path (D1)).

### M10 — Human checkpoint *(§8, §13.10)*
`humanio.py`, `humaninput.py`: ask-set exactness (D3's item-less review and phase-0
questions included), wait rule via D5, explain
round-trip (recommendation semantics; chair refusal), answer→retire→full-round,
skip semantics with D6, `question_updated`, non-interactive D16, logging.
**test_human.py** (§13.10: checkpoint-before-termination in both modes;
terminal checkpoint answers whole ask set; never-asked-again after
answer/skip; updated text is what prints; parked wait issues no tick).

### M11 — Manifest, transcripts, console, exit contract *(§3.4, §11, §3.2)*
`runjson.py` finalize paths wired to every exit; `console.py` completeness
(§11.1 line inventory incl. seat-lost as-it-happens + closing summary + the
dispose sweep-scope line (§4));
transcripts on/off + lazy stderr sink (D7); `events.jsonl`/`human_input.json`
(D17); exit-code table observed on every path.
**test_reports.py** (artifact part) + spread assertions in
test_engine/test_human (0 for a converged-but-disputed run; 130/143 relay via
supervisor; `run.json` finalized on abort and on unusable seat).

### M12 — End-to-end + smoke *(§13.12)*
Full runs against the fake pi in both modes (convergence, stalemate,
needs_human, round-limit paths); `testdata/toy-project/` plus its test config
(2 seats + chair, `max_rounds = 2`, transcripts off); the real-pi smoke:
`council review --project testdata/toy-project --allow-nested` (run from inside
an agent) → converges, correct
`report.md`, exit 0. Marked `slow` (deselected by default, plan §6) and
token-spending; run after pi upgrades with `-m slow`.
**test_smoke.py** + a full-suite pass from *inside* a council agent (harness
rule), which asserts the §13.9 family **ran** — case count > 0, not exit code 0:
inside a member the fixture root is `$TMPDIR` (§0/plan §6), so the overlay-lower skip
has no excuse to fire, and a silently-skipped sandbox family is exactly how this
milestone would pass while proving nothing. The smoke carries
`@pytest.mark.timeout(0)` (plan §6).

---

## 6. Test harness rules (§13, built once in M1/M6/M8, used everywhere)

- CLI driven as a subprocess with `pi_cli` aimed at the fake pi; argv
  assertions see exactly the production shape — the launcher adds nothing to any
  argv, its own inputs being D12b's env (§13).
- `COUNCIL_RUN_DIR` scrubbed from every spawned env (and `COUNCIL_RUN_PATH` with
  it — inert alone per §3.5, but a stray value lets a nested-refusal case print a
  parent the suite never made); `PI_CODING_AGENT_DIR` →
  throwaway dir; `PI_OFFLINE=1`, `PI_SKIP_VERSION_CHECK=1`;
  `COUNCIL_RUNS_DIR` → empty canary that fails the suite on intrusion (the
  §13.5 resolution cases move the canary to `XDG_CACHE_HOME` and unset
  `COUNCIL_RUNS_DIR`).
- Deliberation/engine assertions come from data with clock and paths injected;
  reports compare byte-for-byte.
- **Fixture root, chosen at run time** (§0, D25): plain-fs ground *outside the project
  under test* — a scratch dir under `/dockerx` (ext4) on the host, `$TMPDIR` when
  the suite runs from inside a member, where the project path *is* the run's view
  and `<runs_dir>` is masked. §13.9's overlay-lower rule is the single allowed
  skip, and "only" is enforced rather than hoped for: a `conftest.py` hook fails
  the run on any skip whose reason is not that rule, and M12 asserts the family's
  executed case count. The fixture root and §10 6b(4)'s `/tmp` condition are chosen
  **independently**: the condition is a disjunction over `<project>` *or*
  `<runs_dir>`, so the `/tmp` arm is taken by putting a runs root under `/tmp` (the
  helper's own tmpfs there when the suite runs inside a member) while the fixture
  stays on plain ground (§0; D25, and §13.9 now states the arms this way).
- **Runner: pytest** (`pip install -e '.[test]'`; `pytest` from the repo root).
  Still no sudo, still offline at run time (§13).
  - §13's rule that "**that enumeration is the case list**" is written as
    `@pytest.mark.parametrize` tables, so a failing row is re-runnable by node
    id — the §13 method depends on that, and `subTest` does not give it.
  - `tests/conftest.py` owns the scoped setup/teardown the sandbox and process
    families need: canary dir, scrubbed child env, throwaway
    `PI_CODING_AGENT_DIR`, scratch tmpfs and mount/umount pairs (`yield`
    teardown unmounts even when the case fails), injected clock/paths.
  - `pytest-timeout` bounds the suite itself — the only watchdog for a wedged
    child *in the test*, which M6/M7 create deliberately. `timeout = 120` and
    `timeout_method = signal` (the default on Linux) in
    `[tool.pytest.ini_options]`; `@pytest.mark.timeout(n)` per exception,
    `@pytest.mark.timeout(0)` for the `slow` smoke.
  - A timeout interrupts the **test**, never the process tree it spawned, so the
    CLI-runner fixture puts each CLI in its own process group and its teardown
    `killpg`s and waits. Without that, a timed-out case leaves a supervisor +
    orchestrator + members holding mounts and namespace fds, and the next case's
    §13.8 namespace-set or §13.9 inventory assertion fails for a reason that looks
    like a product bug. `method=thread` is never used in these
    families: it kills the pytest process itself, so no teardown runs at all.
  - Markers registered in `ini_options`: `slow` (M12 real-pi smoke; default
    `addopts = -m "not slow" -p no:cacheprovider`) and `sandbox` (§13.9 — the
    family needing plain-fs fixture ground, whose only skip stays §13.9's
    overlay-lower rule). No capability markers for userns/privileges: §0 verifies
    them and §14.4 forbids probing.
  - Report tests compare `actual == expected` on strings with pytest's diff on
    failure; the expected text stays in the repo as data — no snapshot plugin,
    so byte-for-byte (§13) is asserted against a committed literal.
- pytest's own litter must not reach the write journal when the suite runs from
  inside a member (§13.12): `no:cacheprovider` (above) plus `cache_dir` pointed
  at the throwaway dir rather than `<project>/.pytest_cache`, and
  `PYTHONDONTWRITEBYTECODE=1` for `__pycache__` — §10's journal skips only
  `.council-scratch`, while `.venv` and `tests/` sit inside the project the
  journal diffs. Reachability is not the problem: `.venv` lives under
  `/dockerx`, i.e. inside the view, and its interpreter symlinks to
  `/usr/sbin/python3.14`. Probe litter is the same hazard with worse teeth:
  `probes/run-all.sh` writes scratch inside the repo, and a probe killed by
  `SIGPIPE` (output piped into `head`) dies before its cleanup and leaves §3.5's
  `work/work` at mode `000` behind — run probes to a file, sweep `probes/.scratch-*`
  and any `proj-*`/`proj2-*` residue before committing, and keep M8's fixture root on
  run-time-chosen ground outside the project (plan §6) so the suite leaves none.

---

## 7. Extensibility contract (for the modes and features you'll add later)

- **New mode** (e.g. `audit`): add `modes/audit.py` (adapter data per plan §4.2) +
  `prompts/audit.py` + `render/report_audit.py`, register the literal in the
  mode registry, add its §5.3 synonym additions — config validation reads the mode
  list from the registry, so no code edit is needed there, but §3.3's "`review` or
  `plan` exactly" is a literal in the spec: a new mode is a spec edit first and a
  registry entry second. Engine, lanes, sandbox,
  checkpoint, run.json: untouched. Config validation picks the mode up from
  the registry automatically, and in-round dedup via the chair merge pass
  (D24) is engine-owned, so a new mode gets it without any extra code.
- **New knob**: one entry in the config key table (name, type, default,
  `[defaults]`-allowed) + one consumer. Hinting and strictness are generic.
- **New CLI flag**: one row in the flag table (long, short, type, override
  note). Parsing, errors, suggestions are generic.
- **New verdict/report field**: extend the shared verdict applicator + the
  mode's section list; extraction stays data-driven.
- **New log event**: snake_case name, one `console.py` emitter; `events.jsonl`
  is append-only so consumers never break.
- Non-negotiable floor for any new feature: §14's list (below) and the
  shared-abstraction rule — items are items; a new mode may not fork the
  disposition machine, the ladder, or the docket renderer.

---

## 8. §14 prohibition checklist (checked per milestone, not at the end)

1. No second deliberation/docket implementation; adapters hold no parsing,
   prompts, or termination math (grep-tests in M3/M5/M9, kept green forever).
2. No unbounded background work: watchdog ticks never overlap and skip
   merge/compare; no restarts once disposal began; no un-awaited aborts; every
   subscription has a handler.
3. No consensus/rejection by silence; frozen-N support; `abstain`/`declined`
   inert both ways; reasoned objection is the only killer (§5.1 case table is
   the regression net).
4. One sandbox mechanism: no probes, no tiers, no fallbacks; a failed step is
   exit 1 with the syscall and errno. (`probes/` are dev-time measurements run by
   hand and never consulted at runtime — §14.4 forbids a runtime capability
   probe, not a measurement sitting in a directory.)
5. No compatibility machinery beyond §3.2's flush rule and §4's unbounded
   lines; no last-known-stance ledger.
6. No vote on an escalation; only asker/human/challenge ends one; no re-ask of
   a retired question; below the bar no wait, no forced outcome, no
   "do NOT guess" line.
7. Every answered question is worked in a full round; only `round_limit_reached`
   ends a run with questions unasked.

---

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Checkpoint/stalemate/termination interplay is the subtlest logic | D4–D6 pinned as pure algorithms in M3 (§13.1's case lists, no I/O); §13.2's checkpoint cases then exercise them through the engine in M9 |
| Sandbox is kernel-shaped | `probes/` prove every step on this box and `probes/9-lower-shapes.py` pins the lower-layer shapes §3.5/§13.9 reason from; M8 ports those assertions and never asserts "this mount must `EINVAL`"; fixture root is plain-fs ground chosen at run time (plan §6) |
| pi RPC drift | One boundary file (§12.1.7); fake pi pins the shapes the installed **1.4.0** documents — re-checked in its `docs/`: `disposition: handled\|queued\|started` (`rpc-commands.md`), `agent_settled{aborted}`, `auto_retry_start{attempt,maxAttempts,errorMessage}` → `auto_retry_end{success,finalError}` (`json.md`), assistant `stopReason: "error"` + `errorMessage` (`message-types.md`) — and M6 re-verifies them against the live child before they are committed as fixtures; unknown-command reply = hard protocol error at spawn; after any pi upgrade re-run `probes/5-pi-startup.sh` (in `run-all.sh`) and the smoke |
| Byte-for-byte report tests brittleness | Purity from M3 onward (§12.1.10), injected clock/paths, synthetic-state fixtures |
| Wedged children / orphaned tool shells | §4's dispose order + namespace sweep, M7's fidelity rules (detached fake tool shell) before engine wiring |
| Runs inside an agent (self-hosting) | `--allow-nested` in the smoke invocation; harness env scrubbing; fixture and canary roots move to `$TMPDIR` inside a member (§0/plan §6) so §13.9 genuinely runs there, and M12 asserts the executed case count, not the exit code |
| A `[fix]` decision living only in this plan | Every `[fix]` is patched into SPEC-PY314.md in the same change that records it here — D1 (§1.2, §2, §3.3 point 3, §3.4, §5.4, §6.1, §7.1, §9, §13.6), D3 (§2, §5.3, §5.4, §6.1, §6.3, §12.1.5, §13.2), D4 (§6.1), D12 (§3.5 table row + nested rule, §4's env sentence), D17 (§3.4), D18 (§5.2.6), D19 (§6.2), D24 (§9 job (a), §7.1, §5.2.8), D25 (§13.9's isolation case and skip paragraph), D26 (§3.5's nested rule) — because §13's families take their case lists from the spec sections they cite, and an unpatched `[fix]` means the suite is written against a document the build contradicts. D25 is the case that shows the rule earning its keep: the defect was in a §13.9 **method** claim (§13.12: those are spec-owned), so the plan alone could not have licensed it. The invariant is "no unpatched `[fix]`", and D1 holds it the hard way: §1.2, §2, §3.4, §6.1 and §7.1 now say assignment at convening with reveal after phase 0, so spec and suite agree on what phase 0 must not contain |

---

## 10. Setup notes

The runtime needs nothing beyond Python 3.14.7 and pi 1.4.0, both present.
The one install is the test extra (`pytest`, `pytest-timeout`); without it the
suite cannot run. `pip install -e .` alone still yields a working `council` and
pulls in nothing else — pytest is a test dependency, and §13's offline rule
holds because pytest runs from the venv with no network.

```
cd /dockerx && python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/council --help
.venv/bin/pytest                      # everything except -m slow
```

The venv matters for self-hosting: members spawn via
`(sys.executable, "-m", "council")` (D21), so runs convened from the venv use
the same interpreter tree. The existing `/dockerx/council.toml` is the dogfood
config for running the council on this project once M12 lands.
