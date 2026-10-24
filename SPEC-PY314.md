# mech-council — Python 3.14 Rewrite Spec

---

## 1. What mech-council is

A CLI (`council`) that runs a **council of coding agents** which independently
review a project or plan a task, then deliberate — verify each other, challenge,
change their minds — until they **agree**, run out of things to argue about, or
hit the round limit. The user gets a report with consensus findings (or a converged
plan), verbatim disputes, and the questions that need a
human decision. The council **stops and asks the user** at its checkpoint: mid-run
for an effective-blocking question (§5.2.8), and for every open question on the round
it would otherwise end (§8). The council then deliberates the answers (§5.2.7).

Agents are long-lived `pi --mode rpc` child processes; the orchestrator — this
tool's own process, not an agent (§12.1) — mediates all communication and owns
all shared state. Every council also runs a **chair**: one further agent process
that deduplicates findings, compares plans, and watches for stuck seats (§9).

### Goals

1. **Falsification over trust.** A claim two agents reach independently is
   evidence; a claim only one agent makes is a hypothesis that must be verified
   against the code before it is believed. Findings survive because peers with
   workspace access failed to knock them down — not because a model asserted them.
2. **Independence.** Phase 0 runs all agents on the identical briefing with no
   knowledge of each other. IDs (`Agent1..N`) are assigned when the council is
   convened (§6.1) and **revealed to no seat until phase 0 ends**: what keeps the
   independent phase independent is that its briefing is byte-identical for every
   seat, so it can carry no ID, no slot, and no peer name (§5.4, §6.1, §13.6).
   This is prompt hygiene, not a sandbox boundary — §4 passes `--name
   council-<file_base>` and each seat's persona as argv, and same-uid siblings read
   each other's `/proc` (§10), so a seat that goes looking can reconstruct the
   roster during phase 0 whatever the calendar says. What the design forbids is a
   seat being *told* anything about the others before it answers.
3. **Evidence-backed stances.** Workspace evidence (files read, commands run, git history) outranks reasoning alone.
4. **Silence is never consent and never rejection** (see §5.2 — the load-bearing rules).
5. **The human is the escape hatch, not a bottleneck.** Agents flag questions
   they cannot settle; the council **stops and asks** at §8's checkpoint — mid-run
   when an **effective-blocking** question stands (§5.2.8), and for every open
   question before a run ends short of `round_limit_reached` — so no run finishes
   holding a question the human could have answered and nothing is guessed past
   silently. **An answer buys a full round of deliberation** (§5.2.7). Every
   question is asked at most once, an escalation is its asker's to retract, and peers
   may challenge it with cited evidence — so a question the council outgrows or
   cannot defend is reported, not nagged. The human can ask the asking-agent to
   explain itself before answering.
6. **Safety by blast radius.** Agents have unrestricted shell tools, so every
   member process — every seat and the chair — runs in a mount namespace of its
   own, where the project's canonical path **is** that member's overlay view. One
   tmpfs upper per member holds every write, delete and `pip install`, temp files
   included, and the run's write journal holds the same list. The user's tree is
   never touched, in no mode. The claim is exactly that the *project* is
   disposable: no network or `$HOME` isolation — §10's last bullets list what
   stays reachable.
7. **Self-hosting.** The council can review and test the harness that runs it: starting a
   *nested council* is refused without an explicit `--allow-nested` (§3.5).
8. **Bounded cost.** Transcripts are opt-in (§11.2) and the sandbox costs RAM
   with no cleanup (§10). An unusable seat is found at its own first prompt and
   ends the run instead of quietly thinning it; there is no
   pre-run probe of any kind (§3.3). The stance-gap ladder is capped per seat per
   round, and ending it never ends the run (§6.2).

### Non-goals

- No Windows support (Linux only).
- No dependency on a specific LLM provider; model/provider names come from pi's
  own config and are opaque to this tool.

---

## 2. Domain model (vocabulary)

| Concept | Meaning |
|---|---|
| **Council (named)** | One `[councils.<name>]` entry in `council.toml`: mode, goal, agents, a chair, and any knob it states for itself. A run convenes exactly one (§3.3). |
| **Mode** | `review` or `plan`: a property of the convened council, recorded as `mode` in the config and chosen by nothing else — no verb, no flag (§3.1, §3.3). |
| **Roster** | The convened council's agent list — "every roster agent", "roster size". |
| **Agent (seat/slot)** | One entry in the roster, spawned as one long-lived pi RPC child. Has a human `name` (never shown to agents to minimize bias) and a protocol `id` (`Agent1..N`, assigned at convening and **revealed after phase 0**, §1.2, §6.1). |
| **Chair** | A required member of every council (§3.3): one further agent process that deduplicates findings (review), compares plans (plan), and runs watchdog checks. **Never deliberates, never holds a roster ID** — not one of `N`, cannot move the consensus bar (§5.1). It fails like any other member and its *output* is what stays out of the deliberation (§9). |
| **Run directory** | `<runs>/<timestamp>-<project>/`, where `<runs>` is the **runs root** §3.5 resolves — outside the project by rule, never inside it (§3.5, §10). Holds the manifest, log, report, human-input file and transcripts. |
| **Sandbox (member / view / upper / scratch)** | A **member** is one process with a sandbox: a seat or the chair. Each is one overlay of the project in a mount namespace of its own (§10): the **view** is that member's overlay and the only project it can reach or write, and its cwd is `<project>`, which in that namespace *is* the view; the **upper** is its tmpfs layer, and listing it is the run's write journal; **scratch** is its `TMPDIR` — the member's `scratch/` bound into the view at `.council-scratch`. "Workspace" means the view. A member's namespace is created at spawn (§4) and released at teardown, when the orchestrator closes the fd it holds on it: a dead member's namespace outlives its process on purpose, because that is where its orphaned tool shells still live (§4's sweep). |
| **Item** | The unit of deliberation: a **finding** (review) or a **decision point/topic** (plan). Both behave identically (§5); this shared abstraction is mandatory (§12.1.3). |
| **Docket** | The orchestrator-owned shared state: all items, each agent's **current** stance per item with the round it was given, Q&A, human-input requests (**both modes** — asking the human is not a plan privilege, §5.2.8), and (plan) approaches/revisions/backing. |
| **Stance** | `agree` / `disagree` / `needs_human` / `abstain` on one item, with note, recorded with the round it was given. A seat's latest stance for an item is its **current** stance. `declined: true` marks the record the *run* wrote when a gap survived its §6.2 ladder: an abstention nobody chose, printed as what it is (§6.3). Two readers, one rule each: §5.1 counts a `declined` record as an abstention; §5.2.8 ignores it, so an escalation stands on the seat's last **stated** stance. |
| **Gap** | An open item — or, in plan mode, a `backing` token — a seat was shown this round and returned nothing for. §6.2's bounded ladder closes every gap; one that survives it becomes a `declined` stance. A seat whose **ask** failed is not gapped: §4. |
| **Backing** | (plan) One token per seat per round naming the plan it will build: `AgentN`, `merged: <label>`, or `no_preference` — plus `declined`, which only the run records for a seat whose §6.2 ladder ran out. Grammar in §7.1, consensus rule in §5.2.7. It is **not** a stance: `abstain` is never legal here (§7.1). |
| **Verdict** | One agent's structured round message: positions, new items, answers, questions, `human_input` (**both modes**, §5.2.8), signoff. Plan adds `revised_plan`/`backing` — the only plan-only verdict fields. |
| **Disposition** | State of an item given the seats' current stances and any standing escalation: `agreed / unheard / unseen / disputed / human / rejected` (§5.1). An **open** item's disposition is derived each round; `agreed` and `rejected` are **closed** and carry a stored closure record that nothing re-derives (§5.2.3). |
| **Outcome** | The run's one machine-readable result, stored in `run.json` and printed in the report header, and **never** an exit-code input (§3.2): `consensus`, `needs_human`, `stalemate`, `round_limit_reached` are **results** (exit 0); `aborted` is the absence of one (exit 1). No other value exists. |
| **Round** | One deliberation pass: every agent receives the docket and returns a verdict. |
| **Escalation** | One seat's `needs_human` on one item, tracked per (item, asker): `standing` while its asker's latest **stated** stance there is `needs_human`, then `withdrawn` (by its asker), `retired` (by the human) or `demoted` (by a challenge it did not answer). Never voted on, never ended by anyone but its asker or the human (§5.2.8). |
| **Human-input request** | A question flagged by an agent or chair (`H1, H2, …`), deduplicated by normalized question text and **asked at most once**. `blocking` is what the asker *requests*; `blocking` or `deferred` is the **effective** class the run derives (§5.2.8). §8's **ask set** is the set of its questions still waiting to be put; its **terminal checkpoint** is the one taken before an outcome. |

---

## 3. Product surface

### 3.1 CLI

```
council [<council>] [flags]      # <council> = a [councils.<name>] key in the config
council --help | --version

flags = [--project <path>] [--config <file>] [--rounds N] [--goal "text"]
        [--transcripts off|full] [--allow-nested] [--untrusted]
```

Contract (behavioral, not implementation):

- Every flag has a short form (`-p -c -r -g -t`, plus `-h`/`-V`)
  **except `--allow-nested` and `--untrusted`**, which are booleans with no alias
  that refuse the `=` form; the rest take `--flag=value`, a bare `--` ends flag
  parsing, and values may start with a dash via the `=` form.
- **`--untrusted` runs every seat and the chair with pi's `--no-approve` instead of
  `--approve`** (§4). It changes spawn args and nothing else — no outcome, no exit
  code — and is recorded in `run.json` (§3.4) and on the console (§11.1).
- **The first positional is the council name** — a `[councils.<name>]` key, the
  only handle on which council runs: mode, roster, goal, and knobs all come from the
  council it convenes unless overridden. Exactly **one** positional is accepted — a second word is a usage error, never a
  silently ignored argument. It may be omitted only when the file holds exactly one
  council (§3.3).
- **A flag passed twice keeps its last value**. An override that *changes* the
  value prints `[council] note: <new> overrides <old> (the last one wins)`; a no-op
  repeat is silent.
- `-h`/`--help` print to **stdout** and exit 0; any other usage error prints a
  two-line complaint + `run 'council --help'` pointer to stderr and exits 2 —
  never a 30-line usage dump. `--help` is one page: one positional, one flag set.
- Unknown flags fail loudly with a restrained "did you mean" hint (prefix
  match, else ≤1 edit for short tokens / ≤2 for ≥6-char tokens; `--p` answers
  the short spelling exactly). The **one** suggestion function, for flags, keys and
  council names alike, compares **normalized** forms — case folded, `_`/`-` dropped
  — so the wrong convention still hints (`chairTimeoutSec` → `chair_timeout_sec`, a
  raw distance of 4). It **suggests, never accepts** (§3.3).
- Numbers are whole numbers (`--rounds 1e3`, `0x10` are refused; the error
  names the *long* flag). `--rounds` has **no upper cap** — consensus and stalemate
  end runs — but must be a real count.
- The council name is resolved as soon as the file loads and before anything is
  spawned, so a typo'd name costs one parse, not a run directory or a token
  (§12.1.11). A name matching no council, a second positional, and no name when
  the file holds several councils are **usage errors (exit 2, §3.2)**: the file
  is fine, the invocation is not. Each names the councils the file offers; a
  near-miss gets §3.3's suggestion hint.

### 3.2 Exit

Standard convention, nothing clever. A review that ended with disputes,
open questions and no consensus is a review that *worked* and exits 0; the
outcome is machine-readable in `run.json` (§3.4, §12.1.12).

| Code | Meaning |
|---|---|
| **0** | A **result** was produced: `report.md`/`plan.md` carrying any real outcome (`consensus`, `needs_human`, `stalemate`, `round_limit_reached`), whatever findings, disputes or open questions it holds |
| **1** | No result to hand over: config error or unresolvable `pi` (§3.3), §10's setup failing at any step (`unshare`, a `mount`, the map write), an unusable seat — spawn failure or provider-error first prompt (§3.3), a seat lost mid-run (§4), protocol error, `aborted` for a non-signal cause |
| **2** | Usage error (§3.1), including a council name matching nothing, a second positional, and no name when the file holds more than one council |
| **130** | Terminated by SIGINT (128+2) |
| **143** | Terminated by SIGTERM (128+15) |

Findings, disputes and open questions never change a code. The minimal "here is why
nothing ran" report of an aborted run is not a result and goes with 1.

If aborted by a signal, the handler logs `run_aborted` naming the signal,
disposes all children **without** restarting them, and exits 128+signum — the
same status §10's supervisor relays to the shell. A second signal mid-dispose
changes nothing. If disposal never settles, a last-resort sweep SIGKILLs every
agent process group after ~15 s.

Every exit path **finalizes `run.json`** (§3.4: `outcome` + write journal) and
then **flushes `sys.stdout`/`sys.stderr` before leaving** (piped
streams are block-buffered, so a hard exit drops the run's last lines). No exit
path waits for the stdin reader (§12.1.2): a leave that cannot wait for disposal
(the last-resort sweep) is flush → `os._exit(code)` with the code that path owes
above (130/143 when a signal caused it, 1 otherwise). Teardown afterwards is
nothing but the sandbox namespace exiting (§10) and cannot change the outcome.

### 3.3 Config file (`council.toml`)

**One TOML file holds one or more named councils**, parsed with
stdlib `tomllib`. A **council** is a complete run definition — mode, goal, its
agents, its chair, and any knob it states differently from the project's shared
`[defaults]` — and a run convenes **exactly one** council. Every knob can be
stated once for the project in `[defaults]` and has a built-in default, so a
file that disagrees with nothing writes only its councils. A project's normal
shape is several councils sharing a mode, **not limited to one per mode**. The file is
hand-tuned for the life of the project and parsed once at load.

```toml
# council.toml — hand-tuned over the life of the project, so it is annotated.

[defaults]                        # knobs shared by every council: the scalar knobs only
max_rounds = 6
watchdog_sec = 600
max_concurrency = 3
sandbox_mib = 32768                # the one sandbox tmpfs, §10

[councils.review]                 # the table key IS the name: `council review`
mode = "review"
goal = "See if there is anything here that should be fixed or improved."

[[councils.review.agents]]        # a roster lives inside its council and
name = "Metic"                    # inherits nothing
provider = "omnissiah"
model = "Qwen-Flash-Next"
persona = "You hunt correctness bugs. Verify every claim before you report it."

[[councils.review.agents]]
name = "Senior"
provider = "omnissiah"
model = "Qwen-Flash-Next"
persona = "Smallest change that satisfies the goal; commit to a decision, never a menu."
# persona appends to the system prompt (§4); §5.4's briefing still arrives

[[councils.review.agents]]
name = "Coworker"
persona = "You inherit this in eighteen months: flag migrations, shims and config drift."
# no provider/model: pi's default seat, labeled as such (§4)


[councils.review.chair]           # required
name = "Chair"
provider = "omnissiah"
model = "Qwen-Flash-Next"
persona = """
You deduplicate findings and never review code yourself. Be conservative: merge only
findings that describe the same underlying defect.
"""

[councils.review-staged]          # same mode, different scope — the name is how you pick it
mode = "review"
goal = "Another agent staged these changes; did they do a good job? (`git diff --cached`)"

[[councils.review-staged.agents]]
name = "Metic"
provider = "omnissiah"
model = "Qwen-Flash-Next"

[[councils.review-staged.agents]]
name = "Coworker"

[councils.review-staged.chair]    # every council has one (§9)
name = "Chair"

[councils.plan]
mode = "plan"
goal = "Make a plan for the implementation of the next TODO item"
max_rounds = 3                    # beats [defaults] here

[[councils.plan.agents]]          # a persona belongs to the council that
name = "Metic"                    # states it
provider = "omnissiah"
model = "Qwen-Flash-Next"
persona = """
You are a meticulous planner. Attack internal correctness: preconditions, step ordering,
etc — anything that breaks when a step lands out of order or
half-runs. Verify every claim against the real code before you assert it.
"""

[[councils.plan.agents]]
name = "Senior"                   # no persona: §4's plain system prompt + §5.4's briefing

[councils.plan.chair]
name = "Chair"
persona = """
You are the chair of a planning council. You compare the planners' independent plans and build the decision map; you never plan anything yourself. Do not invent conflicts between plans that merely differ in wording or scope; a conflict is the same decision answered differently.
"""
```

The example is normative and is a §13.4 test input: if the loader rejects it,
the loader or this section is wrong, and the test says which.

**Keys.** A council (`[councils.<name>]`) takes: `mode` (default `review`),
`goal`, `max_rounds` (default 10), `watchdog_sec` (default 600, 0 = off),
`chair_timeout_sec` (default 300), `max_concurrency` (default 4), `transcripts`
(default `off`; only `off` and `full` exist, §11.2), `pi_cli`, `sandbox_mib`
(default 32768 — the tmpfs ceiling that holds every member's writes and scratch,
   seats and chair together, §10; the per-member mask sizes are constants, not knobs),
`[[councils.<name>.agents]]` (≥2, unique non-empty names;
`name/provider/model/persona`), and `[councils.<name>.chair]`
(**required**, single table), taking **exactly the same key set as an agent**.

**Naming is one rule with four cases, and it needs no translation table anywhere
in the design.** `snake_case` for every identifier this tool *names*: config keys
here, `run.json` fields, verdict/plan schema fields, enum values,
log event names. Identity, not style: the same string is a `council.toml` key, a
Python attribute, a `run.json` field and a report label, and one grep finds a
knob's every appearance. `kebab-case` for CLI flags (§3.1), `SCREAMING_SNAKE` for
the environment (§3.5). And **foreign names stay inside the module that meets
them** — pi's `agent_settled`/`stopReason` in §12.1.7, `uid_map`/`errno` in
§12.1.9 — never renamed into this vocabulary, never emitted as if it were ours.
§12.1.7 is the design's only rename site.

Precedence is one line: **flag → council key → `[defaults]` → built-in default**.

**Council names are table keys, never a `name` field**, so a key and a label
cannot drift apart, and declaring the same council twice is a parse
error (`TOMLDecodeError: Cannot declare ('councils', 'review') twice`).

Validation at load — file-level: ≥ 1 council, non-empty council names; then per **convened** council: agents ≥ 2,
unique names, `max_rounds` integer ≥ 1, `sandbox_mib` integer ≥ 1, mode is `review`
or `plan` exactly (an unknown value is a config error with the §3.1 hint, so
`mode = "planning"` answers `did you mean 'plan'?`), `chair` is a **required**
table — `[[councils.<name>.chair]]` is a config error, and so is a convened
council with no chair at all — and **`provider` without `model` is an error**
(pi refuses to start a seat configured that way). Config errors print one
user-fixable line, exit 1 — never a traceback.

**The file is strict: an unknown key is an error at every level** — `defaults`,
a council, an agent or a chair — and the message names the key and where it was.
`tomllib` polices neither keys nor schema types (an unknown key parses into the
dict like any other, and `max_rounds = "10"` is valid TOML that parses to a
string), so the loader owns the key set and the value **types**: "integer ≥ 1" and
"one of the named literals" are our rules, not the parser's. What the parser does
give, and must not be softened: duplicate keys are refused (`TOMLDecodeError: Cannot
overwrite a value (at line 2, column 14)`) rather than silently last-wins, and
syntax errors carry line/column.

Key matching is exact and case-sensitive: `maxRounds` and `maxrounds` are unknown
keys, not spellings of `max_rounds`. Near-miss hints reuse §3.1's one suggestion
function for keys *and* council names, so a typo'd key answers
`unknown config key 'maxRound' (did you mean 'max_rounds'?)`. Config stays strict
where §5.3 is lenient, and neither is a precedent for the other (§14.3): model
output is normalized into a canonical field, human input is refused.

**File resolution:** explicit `--config` wins (missing explicit file = hard
error) → `council.toml` in the project directory (`--project`, default the invoking cwd);
`--config` paths resolve against the invoking cwd. The chosen path is printed
first, recorded as the first log entry, and repeated in the report header.
**`.json` is never accepted as input**, not even via `--config` — the error names
the file and says the config is TOML.

**Council selection:** **the first positional is the council name**, an exact
match against the table keys (§3.1 for the CLI contract and its exit codes). It
is how two councils of the same mode are told apart, so scope belongs in it
(`[councils.review]` for the whole tree, `[councils.review-staged]` for what is
staged before a commit) and restated in that council's `goal`, which is what the
seats are actually briefed with.

Omitted, the selection is derived: exactly one council in the file → that
one (the run prints which it picked, §11.1); more than one → the §3.1 usage
error. **Never a silent pick** between councils of the same mode.

Name and mode are both reported (console, `run.json`, report header), and §7.1's
goal requirement is a load-time check on the convened council rather than a flag
to remember.

**Seat startup: no probe and no pre-run handshake pass.** What remains, in cost order:

1. **pi discovery at load** (below): an unresolvable `pi` is one clear error at
   load, exit 1, no run directory (§12.1.11).
2. **The seat's real spawn is the check.** Each seat spawns once with its real
   §4 args and `get_state` as its first RPC call, so an unknown provider (pi
   exits 1: `Unknown provider "X".`), a bad `-e` path, and any protocol drift
   fail on the child that had to be spawned regardless.
3. **A seat's first prompt classifies an unusable seat.** Measured shape for a
   refused connection: `prompt` → `success: true`, `disposition: "started"`, then
   `agent_end` → `auto_retry_start {attempt, maxAttempts: 3, errorMessage:
   "Connection error."}` three times, `auto_retry_end {success: false,
   finalError}`, then `agent_settled {aborted}` — which carries nothing else, so the
   cause comes from the run's **last assistant message**: `stopReason: "error"`,
   empty `content`, `errorMessage`, read off `message_end` (authoritative; the same
   message is the last assistant entry of `agent_end.messages`). The fake pi of §13
   emits exactly these shapes.
   Rule: **a first phase-0 prompt whose final assistant message settles on
   `stopReason: "error"` means the seat is unusable, not unhelpful** — dispose
   (§3.2), minimal report naming the seat, its provider/model and pi's
   `errorMessage`, exit 1. Missing credentials and a provider-side "model not found"
   arrive in the same field. The same test on a *later* ask is an ordinary ask
   failure and goes through §4's restart-and-re-ask-once. For the **chair**, whose
   first ask can be a watchdog digest rather than a publication pass (§9), this test
   keys on its first **publication** pass: a digest that settles on
   `stopReason: "error"` is an ordinary ask failure on §4's restart path, not an
   unusable-member verdict.
4. **The unknown-model warning is surfaced, never swallowed.** A seat stderr line
   matching `Model "…" not found for provider "…"` — pi's only signal — is echoed
   to the console at spawn, recorded in `run.json`, and printed on that seat's
   roster row. It stays a **warning**, never a refusal: undeclared custom ids are
   legitimate.

The chair is subject to all four (§9).

**All-or-nothing.** A config error or an unresolvable `pi` stops everything before
the run directory exists (§12.1.11); an unusable seat is found at phase 0's first
prompt, so the run directory exists and holds `run.json` (§3.4) and the minimal
report naming it. No seat is ever quietly dropped and no council convenes short of
its roster (§4).

**pi discovery** (resolved at config load, before any spawn): `pi_cli` from the
convened council, else `[defaults]`, else `pi` on PATH — spawned directly, so any
executable form works; unresolvable = one clear error at load. **No env-var step,
deliberately:** an override that any config key outranks shadows only a run that
stated nothing, which is what PATH is for, and §13's harness aims `pi_cli` at its
fake through the config — so a stray variable in a developer's or CI shell could
only ever change a run nobody configured, while looking like the config's choice
in `run.json`. Compatibility policy: record the resolved pi path **and
`pi --version`** in the run manifest, and make each child's first RPC call a
`get_state` (point 2
above) — which proves the child started and speaks JSONL, **not** that the
protocol matches, since RPC exposes no version field. Detect drift by treating
any `success: false` naming an unknown command — from the command set this tool
actually sends — as a hard protocol error that fails that child loudly at spawn
instead of mid-run.

### 3.4 Artifacts in a run directory

| Artifact | Contents |
|---|---|
| `run.json` | **New.** Machine-readable manifest, **created with the run directory and finalized on the way out** (§12.1.11, §3.2). **At creation:** config path **and convened council name**, mode, goal, roster (slot → id → name, model — IDs exist from convening, §1.2, so they are recorded with the rest), settings (rounds, transcripts level, `sandbox_mib`, timeouts, project-trust mode
   (`approve`/`no-approve`, §3.1)), resolved pi CLI + version, pid of the orchestrator (§10's child; the supervisor is its parent), the §3.3 unknown-model warning per seat, and **per member its `file_base` and every mount-namespace inode recorded for it** (one per spawn: §4, §10 step 6b; a restarted member adds one, and every one of them is in the sweep set). **At finalize:** the **`outcome`** (§2, §3.2) and the **write journal** (§10) — per member, the created/modified/deleted paths its upper layer holds (`.council-scratch` excluded) and its bytes against `sandbox_mib`. **Also at finalize: `survivor_pids` — the pids §4's namespace sweep killed — and `survivor_ns` — the distinct mount-namespace inodes it found them in, every one a namespace this run created.** They are the sweep's whole honest report in machine-readable form, written on **every** finalize: §4's sweep runs at the end of every dispose, so an empty pair is the normal case and proof it ran and found nothing, a non-empty pair is the escape §3.2's last-resort path exists for, and neither is ever an exit-code input (§3.2). §4's console line is built from these two fields and nothing else. Reports read **this**, not regex-scraped log lines. |
| `report.md` / `plan.md` | Final reports (§6.3, §7.2). An aborted run still writes a minimal report explaining why. |
| `agents/*.transcripts.jsonl` | Per-agent session transcripts — only when `transcripts` is `full` (§11.2). Child stderr and crash dumps (`council_stderr_dump`) are written untrimmed at **every** level: with transcripts off, `agents/` is created lazily to hold `<file_base>.stderr.txt`, so the default configuration has a defined crash sink. |
| `events.jsonl` | §2's "log", named: one JSON object per §11.1 event, snake_case names (`run_aborted`, `item_reopened`, `verdict_unknown_findings`, `gap_declined`, `escalation_withdrawn`, `question_updated`, `human_checkpoint`, `watchdog_unmatched`, `sandbox_full`, `sandbox_limit_hit`, `tmp_shared`, `council_stderr_dump`, …). Written by the one console emitter (§11.1) and **append-only**, so a consumer never breaks when a new event appears. |
| `human_input.json` | The §8 record: every request (id, asker(s), round raised, the question **as asked**, effective class), every answer and skip, and the retirements each caused (§5.2.8). Reports read `run.json` and final state (§12.1.12); this is the run directory's artifact for whoever reads it afterwards. |

**Hand-authored input is TOML (§3.3); machine-written and model-facing output is
JSON/JSONL** — `tomllib` has no writer, and the append-only files cannot be TOML.

### 3.5 Environment variables

| Variable | Effect |
|---|---|
| `COUNCIL_RUNS_DIR` | where run directories live; **unset, the runs root is `$XDG_CACHE_HOME/council/runs`** (rules below). The run prints its own directory on the console. **Never inside the project** — §10 step 5 is the reason, and it covers the runs root itself, not only `sandbox/` |
| `COUNCIL_RUN_DIR` | **set by the run, not by the user**: the run directory's *name* (basename), stamped into every agent's env — the nested-run marker; any invocation refuses when it is set (rule below) |
| `COUNCIL_RUN_PATH` | **set by the run, not by the user**: the run's **directory path** (`<runs_dir>/<name>`), stamped into every agent's env beside `COUNCIL_RUN_DIR` for one job — letting a nested refusal print the parent's run directory. **Never a marker**: only `COUNCIL_RUN_DIR` refuses. The path is printed, never used — inside a member it sits under §10 step 6b(4)'s mask, so it is readable and unreachable, like the rest of the run from inside a seat |
| `PI_CODING_AGENT_DIR` | pi's config dir; passed through to children (pi itself reads `models.json` there). This tool neither parses that file nor probes the catalogue it holds (§3.3) |
| `XDG_CACHE_HOME` | read only to resolve the runs root when `COUNCIL_RUNS_DIR` is unset — the design's **one** XDG conformance point. Unset or empty → `$HOME/.cache`, so the built-in default stays `$HOME/.cache/council/runs`; a relative value is invalid and ignored (rules below). Read through `os.environ`, no XDG helper library needed |

**Runs-root resolution is load-time work (§12.1.11): the value and the containment
test are settled with the config work, and the directory itself is made as load's
last action — after pi discovery, before the §10 sandbox.** Three rules:

1. `COUNCIL_RUNS_DIR` if set, else `$XDG_CACHE_HOME/council/runs`, else
   `$HOME/.cache/council/runs`. For both variables **unset and empty are the same
   case** (XDG's rule), so `XDG_CACHE_HOME=""` takes the `$HOME/.cache` fallback
   instead of naming `council/runs` under nothing; with no usable `$HOME` either
   there is no runs root and load fails naming `$HOME`.
2. **Nothing relative reaches §10.** A relative `XDG_CACHE_HOME` is invalid and
   ignored — XDG's own rule, default used. A relative `COUNCIL_RUNS_DIR` is made
   absolute against the invoking cwd, the rule §3.3 already gives `--config`.
   Two sources, two conventions, and no path is ever handed to `mount(2)` for it
   to resolve against the orchestrator's cwd.
3. **The runs root is not inside the project.** Normalize both sides (symlinks and
   `..` resolved, `--project` included) before the containment test; a hit is one
   user-fixable line naming both paths, exit 1 (§3.2), no sandbox step, no run
   directory. Refused at load and not left to the overlay: on this box a mount with
   `upperdir`/`workdir` under `lowerdir` **succeeds**, so there is no `EINVAL` to
   wait for, and what it leaves is worse than an error — the view then lists its own
   upper layer, and the `workdir` keeps a `work/work` at mode `000` in the user's
   tree that its own owner cannot list without chmod'ing it first
   (`probes/9-lower-shapes.py`). The refusal is about the write itself, because
   the runs root is **created** (`mkdir` mode `0700`, an existing directory left
   alone, per XDG) as the **last action of load** — after config, selection and pi
   discovery, before §10 forks, and step 5's precondition — and creating it inside
   the project would write to the user's tree (§1.6) at the one moment when
   nothing in the orchestrator's namespace can absorb the write. The test itself is pure (no `mkdir`, no
   `mount`), so it runs with the config work and nothing at all is created when
   load fails. The test is **one-directional**: a project
   sitting *under* the runs root is not the mount hazard step 5 exists to prevent
   (`upperdir`/`workdir` under `lowerdir`), so it needs no rule beyond §10's
   honesty about reachability.

**An agent cannot start a council** — a nested council inside a review would
recurse and hang the phase that started it. **Any invocation** refuses when
`COUNCIL_RUN_DIR` is set, unless `--allow-nested` for that one invocation (§3.1).
The refusal names the parent run (`COUNCIL_RUN_DIR`) and prints its run directory
(`COUNCIL_RUN_PATH`) — **never a path rebuilt** by resolving the inherited
`COUNCIL_RUNS_DIR` a second time: rule 2 makes a relative value absolute against the
*invoking* cwd, which inside a member is the view (§10 step 6b), so a rebuilt path can
name a directory that was never the parent's.

**The marker is the whole guard, and §10 is not a backstop.** A nested run that somehow
starts inside a member with the *masked* runs root dies at step 5: the first write under
`<runs_dir>` is `EROFS` there (§10 step 6b(4), measured). One whose runs root resolves
somewhere else is **not** stopped by §10 — `$HOME` is reachable by construction (§10's
out-of-scope list) — and `CapEff 0` does not stop it either: step 3's map writer lives
*outside* the new namespace and needs no privilege, measured from the member shape
itself (an `exec`'d member at `CapEff 0000000000000000` whose child's 1:1 maps all three
write OK). So the refusal is what prevents nesting; §10 only catches the masked-root
case. This is why `--allow-nested` is per-invocation and never inherited.

---

## 4. Agent runtime: talking to pi

Each agent is one long-lived `pi --mode rpc` child. The orchestrator speaks
pi's JSONL RPC protocol **directly** (no TS bridge):

- Framing: one JSON object per line on the child's stdin/stdout; LF-delimited
  only; stderr is diagnostics, never protocol. Read stdout **continuously** —
  pi honors client backpressure and a client that stops reading can stall the
  child — and keep the stream **binary**, splitting records on `b"\n"` only:
  `str.splitlines()` also breaks on `U+0085`/`U+2028`/`U+2029`, all legal inside
  JSON strings, so one tool-result line silently becomes several bogus records.
- **No line-size cap**: pi puts whole tool results and message payloads on single
  lines (multi-MB is normal), so a bounded line reader is a correctness bug.
  asyncio's 64 KiB default makes `readline()` raise **plain `ValueError` and clear
  the buffer** on a longer line, and 3.14 re-raises `LimitOverrunError` as
  `ValueError` *inside* `readline()` — so `except asyncio.LimitOverrunError`
  catches nothing and 0 bytes of the oversize line survive to be recovered. Pass a
  generous limit (`limit=1 << 30`, verified against a 3 MB record) or read raw
  chunks and split on LF yourself. `limit=0` is *not* available: `StreamReader`
  rejects it, and via `create_subprocess_exec(limit=0)` the failure surfaces
  asynchronously inside `connection_made` with `stdout` left `None`. A large limit
  also disables the transport pause (it fires at 2×limit), so continuous reading —
  not the limit — is the backpressure mechanism.
- Commands used, and only these: `prompt` (→ response `data.disposition`:
  `started` = a run began; `handled` = an extension consumed the prompt and **no
  run starts** — an error, not a success to wait on; `queued` = pi parked it
  behind an in-flight run, which must never happen here because each agent is
  asked one prompt at a time — treat it as an error and abort, or the
  `agent_settled` you then wait for belongs to the *previous* run), `abort`
  (preceded by `clear_queue`, so queued text is not resumed into a restart), and
  `get_state`.
- Lifecycle: subscribe to events **before** sending `prompt` (a fast
  completion is otherwise missed); wait for `agent_settled`, not merely
  `agent_end` — retries, overflow recovery, compaction, steering and queued
  follow-ups can all follow `agent_end`; correlate responses by request `id`.
  Consume `tool_execution_end` (`toolName`, `result`, `isError`) as well: §10's
  sandbox inference reads it there. **A resolved `abort` is the polite success**:
  `abort` waits for the session
  to become idle before responding, so its own response is the synchronization
  point, and waiting additionally for `agent_settled` risks burning the whole
  grace period and then tree-killing a child that had stopped cleanly. That
  response is *conditional on idleness*, so **the abort request needs its own
  bounded timeout**: an abort sent to a genuinely wedged child never returns.
- Canonical reference: pi's docs (`rpc.md`, `rpc-commands.md`, `json.md`) for
  the installed pi version; the RPC boundary is §12.1.7.

Spawn configuration per agent: `--no-session`, **`--approve` always (`--no-approve`
under `--untrusted`, §3.1)**,
`--name council-<file_base>`, **`--provider <p> --model <m>` when the spec sets
them** (this is how a seat's model is selected — omit both for pi's default,
and never print a roster model label the spawn did not actually request),
`--append-system-prompt <persona>` when a persona is set — the config text as
one argv string, never a file path. **No tool arg and no extension arg is ever
passed** (§3.3): a seat's toolset and extension set are whatever the pi install and,
under `--approve`, the project's own `.pi` resources provide (which is why the trust
decision is pinned, not inherited — RPC mode cannot prompt for it). `file_base` =
`slot<N>[-<slugified name>]`, `chair` for the chair.
The child's cwd is **`<project>`**, which in its own mount namespace *is* its view
(§10 step 6b) — no child is ever spawned before the sandbox is mounted — and its
env inherits the orchestrator's, carries `COUNCIL_RUN_DIR` and `COUNCIL_RUN_PATH` (§3.5),
and points `TMPDIR` at `<project>/.council-scratch`, that member's `scratch/` bound into
its view (§10).
The launcher reports its namespace id on the spawn pipe when step 6b finishes (§10 step
8), and the orchestrator records it in `run.json` (§3.4) and **keeps an open fd on it**
(`os.open("/proc/<child>/ns/mnt", os.O_RDONLY)`, legal once that report has arrived) until
after the sweep. Both halves matter and both are measured (`probes/8-ns-fd.py`): reading
`/proc/<child>/ns/mnt` from outside in that window answers with the **orchestrator's own**
namespace, because the child's does not exist yet; and a namespace is refcounted, so an id
nothing holds goes back into circulation and a later, unrelated namespace is handed it —
held, 200 later namespaces never took an id back; unreferenced, one did. Every spawn takes
its own fd, including a restart of a member that already died; they are closed after the
sweep, at the very end of teardown.

**Spawn shape.** Every member process, seat or chair, is one call:
`create_subprocess_exec(*<launcher>, "--setup-seat", "--slot", <file_base>, "--",
<pi_cli>, …, start_new_session=True, limit=…)`, where `<launcher>` is this tool's own
entry point resolved once at load the way `pi_cli` is (§3.3) — an absolute path or
`(<sys.executable>, "-m", "<package>")`, never a bare `sys.argv[0]` or a `PATH` guess —
run in its `--setup-seat` mode (§12.1.9). It **is** the member process: it
performs §10 step 6b, then `execvp`s exactly what followed the `--` — no second process,
no fork between the mounts and the exec. `preexec_fn` is never used (a thread may exist:
§12.1.2). `execvp` preserves session, pgid, fds and env, so nothing else in §4 changes,
and no code path spawns anything but the resolved `pi_cli` (§13). Cost is one interpreter
start (4–8 ms measured) plus ≈0.05 ms of mounts, **per member process**: nothing in §5–§7
spawns, so a clean run pays it `N+1` times and each restart per §4 or §9 adds one.

**Process safety.** Spawn each child with **`start_new_session=True`** — its own
session *and* process group, not plain `process_group=0`: both give a private pgid
for `os.killpg`, but a child that stays in the orchestrator's session keeps the
controlling terminal, where a pi tool shell that reads `/dev/tty` is stopped by
SIGTTIN — a silent, event-less stall indistinguishable from a wedged model. Because
these children do not share the orchestrator's group, terminal-generated signals
never reach them: the orchestrator's dispose path (§3.2) is the **only** reaper, and
it kills the child's **process group** (`os.killpg(<pgid>, SIGTERM)` → SIGKILL after
a grace period). That group is **pi alone**: pi spawns every tool shell `detached`,
in its own group (measured on the installed pi), so our `killpg` never reaches a
tool shell and pi's own shutdown must. Hence the order: **close the child's stdin
first** — pi's orderly shutdown disposes of its runtime, and that dispose aborts
every in-flight tool call, which SIGKILLs each tool shell's *own* group — then
group-SIGTERM (pi's signal handler runs the same sweep), then SIGKILL. Signals are
the escalation, not the first move: SIGKILLing the child first leaves its tool
shells running. Absolute guards: never signal a pid/pgid below 2, never signal the
orchestrator's own group, never treat a failed group lookup as "everyone". Two
classes of descendant escape every group kill: a tool shell whose pi never ran its
cleanup, and any descendant that `setsid`s or double-forks. Sweep them by **mount
namespace, not process tree**: once our own children are reaped, kill every pid
whose `/proc/<pid>/ns/mnt` is **one of the namespaces this run created** — the
orchestrator's own plus every namespace whose fd it still holds (excluding ourselves).
Descendants inherit the namespace of the member that spawned them, which is why the
set is per-spawn and not the orchestrator's alone: a restarted member's orphans live in
the namespace the dead spawn had, which is exactly why §4 keeps the fd. Holding those
references is also what proves the key: a namespace we hold cannot be freed, so no
unrelated namespace can have taken its inode number — membership really does mean "ours",
with no depth or pid cap (measured: exactly the escaped descendants match). **Report
honestly** which scope was killed — the pids and the namespaces they were found in,
as §3.4's `survivor_pids`/`survivor_ns`, and printed from those two fields and
nothing else, because "killed 3 processes" hides the only fact that matters:
whether they were ours — and treat the sweep as unavailable where `/proc` is
absent (§1).

**Restarts produce a blank session, deliberately.** Phase prompts are
self-contained (identity, council framing, full docket text), so a restarted agent
needs nothing else; every spawn re-supplies the persona from the config. On ask
failure: restart the child (fresh spawn) and re-ask **once**.
**Restart never runs once disposal has begun:** from the moment §3.2's dispose path
starts the run is *stopping*, and a restart request arriving then — from an ask
failure or a watchdog decision — is dropped (§13.8). A second failure ends the run:
dispose (§3.2), write the minimal report naming the seat, its provider/model and
the round it went down in, and end with the `aborted` outcome (exit 1, §3.2).

There is therefore **no "absent seat" state anywhere in this spec**: no mid-run
retry of a dead endpoint, no per-seat liveness in `run.json`, no degraded mode,
and no liveness qualifier on any quantifier in §5. A seat either answers or the
run ends.

**Concurrency & timeouts.** Agent asks go through one mediated path
(watchdog-visible; a direct ask bypassing it is a bug), through a worker pool
capped at `max_concurrency`; the chair's lane sits outside that cap (§12.1.8).

---

## 5. The deliberation protocol

### 5.1 Item dispositions (single shared state machine)

Every **open** item's disposition is **derived** from the seats' current stances
and nothing else; nothing about it is stored. Reaching `agreed` or `rejected`
**closes** the item, and closure *is* stored: the pair (disposition, round earned)
— re-deriving a closed item would demote a consensus finding into "Not fully
vetted" for no reason but time passing. Closure is undone only by §5.2.3's
challenge.

**Completeness: every seat holds a current stance on every open item.** Each round
every seat is shown every open item and must return a stance for it (§6.1); what it
returns becomes its current stance there, recorded with this round. What it does not
return is a **gap**, and §6.2's ladder closes every gap before derivation runs. So
an item is judged either with all seats' current stances or as `unseen` — `unheard`
is never the name for a missing answer. The one carried-over value is a `declined`
record (§6.2), which is inert.

**Support is measured, not merely present.** The denominator is `N` — the roster as
convened — for support and for completeness alike. The consensus bar is a majority:
**`agree` count ≥ ⌊N/2⌋ + 1** (N=2 → 2, N=3 → 2, N=4 → 3, N=8 → 5), counted over
current stances. `abstain` — a `declined` one included — neither supports, nor
objects, nor rejects (§5.2.1). Support below the bar is `unheard`, not `agreed`. A
seat that fails outright ends the run (§4).

**An escalation is a request, not a claim.** A **standing** `needs_human` (§5.2.8)
counts as a stance for the table like any other. Only its asker ends it — by
**sending** another stance — and a `declined` record the run wrote for the asker is
not a sent stance (§6.2, §5.2.8). A closed item's support is fixed at its closure
round (§5.2.3).

An escalation that is `retired` or `demoted` is not standing, and its asker's
`needs_human` there — still its latest **stated** stance, since it sent nothing to
replace it (§5.2.8) — is then **abstain-equivalent** in every count of this section:
never support, never an objection, never a rejection, and never a blocker. A
`withdrawn` escalation needs no such rule: withdrawing means the asker restanced the
item (§5.2.8).

**First match wins.** Table row order is *not* the evaluation order — this
line is, and it is what §13's exactly-one-state test asserts against:
`human` → `disputed` → `agreed` → `rejected` → `unheard` → `unseen`: an objection
beats `unheard`, and a **standing** escalation (§5.2.8) beats both.

| State | Meaning | Effect |
|---|---|---|
| `agreed` | **open** item: every seat holds a current stance; **≥ ⌊N/2⌋+1 `agree`** (frozen `N`); no objection, no standing escalation | consensus item; **closes** (disposition + round stored), renders as one *addressable* line in later dockets (§6.1, §12.1.5), and reopens only on §5.2.3's challenge |
| `rejected` | every seat holds a current stance, nobody backed it, **and at least one seat objected with a reason** (a `disagree` with cited evidence, or a note that refutes it) — abstentions, declined or not, never reject an item | dropped from the result, preserved in a "Rejected" section with the notes that killed it; **closes** the same way and reopens only on §5.2.3's challenge |
| `disputed` | some seat's current stance `agree`s and another's `disagree`s | argued until it collapses, else reported |
| `human` | a **standing** escalation exists for it — any seat, any round, even alongside disagreement (§5.2.8) | never `agreed`, never `rejected` while one stands; forces the `needs_human` outcome **only** through §5.2.8's effective-blocking test; otherwise §8's terminal checkpoint **asks it before the run may end**, and unanswered it is an open question in the report (§6.3) |
| `unheard` | every seat holds a current stance and support is **below the majority bar** — including the case where every seat abstained or declined (a shrug is not a refutation) | does not block; reported with every seat's stance and the round it was given, and "not vetted by: …" naming every seat whose current stance is not `agree` |
| `unseen` | some seat holds no current stance for it — reachable only in the round the item is published or reopened (§5.2.3), never again after that round's ladder (§6.2) | blocks consensus — an unexamined claim has not passed the filter |

The six states partition every answer pattern reachable under that invariant for
an **open** item; a closed item is not in the enumeration at all (§5.2.3).

**The boundary cases to test** — each line is one §13.1 assertion, input →
disposition:

- every seat stanced, nobody backed, someone gave a reason → `rejected`
- every seat stanced, all abstained → `unheard` (a shrug is not a refutation,
  §5.2.1)
- a `declined` abstention is inert, never a dissent: 3 seats, 2 `agree` + 1
  `declined` → `agreed`; 2 seats (bar 2), 1 `agree` + 1 `declined` → `unheard`
- a `declined` stance carries no objection: 3 seats, one `disagree` in round 2 from
  a seat that declines in round 3 → `unheard`, **not** `disputed` (§6.2)
- a `needs_human` raised in round 1, its asker still holding it → `human` in round 5
  (only its asker ends an escalation, §5.2.8)
- same item, but its asker records a `disagree` in round 4 → `disputed` in
  round 4: the author withdrew its own escalation, no other seat voted (§5.2.8)
- same item, challenged with cited evidence in round 3, asker's defense addresses it
  → still `human`, and both the challenge and the defense print (§6.3)
- same item, challenged, the defense repeats the question without touching the cited
  evidence → escalation `demoted`, item derives from the remaining stances (never
  `human`), question prints marked `undefended`
- a standing escalation held by one seat of 4 → `human` and **deferred**: no
  mid-run wait, no forced outcome; asked at the terminal checkpoint before the run
  may end (§5.2.8, §8)
- the same question at the bar, or chair-raised → **effective-blocking**: it makes
  the checkpoint wait and forces `needs_human`
- the human skipped it → escalation `retired`, item derives normally, the question is
  never asked again (§8)
- an escalation retired with its question **unanswered**, its asker's `needs_human`
  still the latest stated stance there → that stance is abstain-equivalent: 3 seats,
  2 `agree` + it → `agreed`, the finding printed with the unanswered question marked
  on it (§6.3); 2 seats (bar 2), 1 `agree` + it → `unheard`; and the same holds for a
  `demoted` asker
- 4-seat council (bar 3), one `agree` and the rest abstaining → `unheard`,
  **not** consensus
- 2-seat council, one `agree` + one `abstain` → `unheard` (the bar is 2 —
  independence is the point of the exercise)
- nobody has been shown it yet → `unseen`; a seat that **was** shown it and went
  silent is a §6.2 gap, never an input to `unseen` or `unheard`
- an item published, or reopened, this round → `unseen`, whatever those seats
  stanced in earlier rounds (§5.2.3)
- an item **closed** `agreed` in round 2 → still `agreed` in round 5: closed items
  are not re-derived (§5.2.3)
- with no roster at all (unit tests): `N = 0`, bar `⌊0/2⌋+1 = 1` is unreachable →
  agree nothing, reject nothing — unsupported items stay in play

### 5.2 Load-bearing rules (each one testable)

1. **Silence cannot kill and cannot pass an item.** Rejection needs a reasoned
   objection from at least one seat. `abstain` — a `declined` one included — counts as an answer (it lets the
   council finish) but **neither supports nor blocks**: it cannot create consensus
   (which needs a majority of `agree`, §5.1) and it cannot kill an item (which needs
   a reasoned objection). A unanimous shrug is `unheard`, never `rejected`. A
   missing answer is never a shrug either: §6.2 turns it into a stance or a
   `declined` record before anything is derived.
2. **A unanimous change of mind is a result, not a deadlock.** Items the whole
   council talked itself out of become `rejected` that round (consensus can
   proceed), preserved as proof a false alarm was suppressed. A change of mind
   is a `disagree` or a note that refutes — never an `abstain`.
3. **Closure is stored; only a challenge undoes it.** Nothing re-derives a closed
   item: rounds passing and unchanged stances leave a closed item exactly
   where it closed (§5.1). A closed item reopens automatically on a **challenge**
   recorded in the judged round:
   - a closed `rejected` item: on an `agree` or a `needs_human`;
   - a closed `agreed` item: on a `disagree` **with cited evidence**, or on
     a `needs_human` (an escalation always reopens: §5.2.8's bar gates the
     *blocking* privilege, never a reopen, which costs a round and no human).
   An unreasoned `disagree` on a closed `agreed` item does **not** reopen it — it
   is recorded as a note on the closed item and printed with it. Undoing consensus
   costs a refutation, not a re-vote (the reverse of rule 1's bar).
   A reopen is logged (`item_reopened`: id, prior disposition, prior closure
   round, the challenger), the item returns to the open set and is judged
   **anew** from that round with nothing carried over, and the closure history
   survives so the report can print "consensus in round 2, reopened in round 4 by
   Agent3" (§6.3). Reopening needs no sign-off machinery of its own: a reopened
   item is `unseen` in the round it reopens, and §5.2.7's *unseen → keep going*
   check is what puts the council back on it. Reopens are not capped: flip-flopping
   is bounded by rule 6 and `max_rounds`, and every turn is visible in the report.
4. **A stance that cannot be placed must not vanish.** A verdict position
   referencing an unknown/typo'd item ID is logged (`verdict_unknown_findings`)
   — it could be the dissent that should block consensus. A position against a
   **closed** item must never be filed with those: its ID is known and its
   disposition is challengeable (rule 3), so it resolves to a reopen or to a note
   on the closed item. Closed IDs stay in the docket rendering (§12.1.5).
5. **No silent sign-off.** The open items a seat was *shown* and did not answer are
   **gaps** (§6.2). A gap answer may withdraw the sign-off (a stance for an item
   shown in the gap round revokes that round's sign-off), never re-grant it. A gap
   the seat still refuses is recorded `declined` and shown as "not vetted by" (§6.2).
6. **Stalemate = no movement.** Round signature = (per item: id, closure record
   (disposition + round, §5.2.3 — the rejection round alone cannot show an item that
   closed, reopened and closed again), sorted stance strings each with its
   `declined` flag, sorted escalation states with each question's effective class
   (§5.2.8)) + sorted signoffs + answered agent-QA count + **answered
   human-input count** — the last two in **both** modes, since §5.3's `Q1…`
   question-and-answer traffic and §8's checkpoint exist in each (plan adds the
   backing map). A withdrawal, a demotion, an effective-class
   change and a stance turning `declined` are each movement. The signature is
   recorded *before* the human checkpoint, so an answer is itself movement.
   Identical consecutive signatures after round 1 → `stalemate`, subject to §8: an
unanswered question is asked before the run ends on it, and the answer — or the
retirement of every escalation it covers — is movement.
7. **Termination order** (first match wins, checked after each round; the
   order differs by mode and the difference is load-bearing).
   - *Review:* not everyone signed off → keep going; disputed or unseen items
     exist → keep going; an **effective-blocking** question is unanswered — still
     standing, or asked and declined (§8) → `needs_human`; else → `consensus`.
     `consensus` here means no disputed, unseen or unanswered-effective-blocking work
     is left; and because §8's terminal checkpoint has just been through the whole ask set,
     **no escalation stands when `consensus` is returned** — so no item is `human` in
     a `consensus` report, and a `human` item belongs to a `round_limit_reached` or
     aborted report only (§6.3).
   - *Plan:* not everyone signed off → keep going; an **effective-blocking**
     question is unanswered — still standing, or asked and declined (§8) →
     `needs_human` immediately — this check *precedes* the dispute checks;
     **deferred** questions force nothing mid-run and must land as named assumptions
     (§7.2); disputed or unseen topics exist → keep going; else → **direction rule**
     (below) → `consensus` or keep going. Then rule 6's
     `stalemate` and reaching `max_rounds` (outcome `round_limit_reached`, §3.2)
     end the loop.
   - **An answer buys a round** (both modes). The checks are evaluated **after**
     the checkpoint, on the state it left. Any answer → keep going, and the next
     round is a **full** one — every seat, the whole docket, the ordinary verdict,
     gap and `backing` obligations. A checkpoint where every question was declined or
     skipped settles nothing further and the checks return their outcome. No
     shortened re-stance pass, no separate post-answer budget, no checkpoint timeout:
     only rule 6 and `max_rounds` bound those rounds, and `round_limit_reached` —
     where the checkpoint does not wait and every open question prints `unasked`
     (§6.3, §7.2) — is the one exit exempt.
   - **Direction rule** — plan mode's last check, and the only consensus test in
     either mode that is not a majority over frozen `N`: a plan is **one artifact
     everyone present must build**, so the question is whether anybody objects,
     not how many like it. Consensus requires all three:
     1. every seat signed off (the first check) **and** holds a `backing` it named
        in the judged round — `no_preference` counts as naming one, a `declined`
        one does not; an omitted token is a gap for §6.2, never a shrug;
     2. every seat's current backing is either the same token `T` or
        `no_preference`. A seat naming a different token is not a lost vote,
        it is an objection: no consensus, and the disagreement belongs in a
        topic where it can be argued with evidence (§5.1's machinery, not a
        tally). A `declined` backing is neither value and settles nothing (§6.2);
     3. at least one seat named `T` **for real**. `no_preference` unlocks
        consensus and can never cause it: a round in which nobody proposed
        anything keeps going until `stalemate`/`max_rounds` say otherwise.
     Backing is a seat's current token, restated every round (§7.1). What this
     concedes out loud: **1 backer + 2
     `no_preference` does set the direction** — and §7.2 prints the split so no
     reader mistakes a shrug for an endorsement.
8. **An escalation is its asker's to drop and open to challenge.** One per
   (item, asker), with states `standing` / `withdrawn` / `retired` / `demoted`:
   - **Standing** while the asker's latest **stated** stance on that item is
     `needs_human` (a chair-raised question: until it is answered or declined). No
     count, vote or round passing makes it stop standing; `agree`, `disagree`,
     `abstain` and sign-offs from every other seat leave it standing, and so does a
     `declined` record the run wrote for its asker (§6.2).
   - **Withdrawn** when the asker **sends** any other stance on that item —
     `abstain` included — or says it no longer needs the human (§5.3 maps that to
     `abstain`). There is no fifth stance value and no withdraw field: retiring an
     escalation is the asker restancing the item with a stance of its own, so a
     `declined` record withdraws nothing (§6.2). Logged `escalation_withdrawn`
     (item, asker, round, cause). A seat that withdrew may escalate again in a later
     round.
   - **Retired** when the human answers the question (that answer is authoritative,
     §8) or declines it (`skip`, or non-interactive stdin); its asker's `needs_human`
     there becomes abstain-equivalent (§5.1). A retired question is never asked
     again, by anyone, in any phrasing (§8).
   - **Demoted** when it loses a **challenge** — a `disagree` with cited
     evidence, or a note that refutes, from a non-asking seat on that item in the
     judged round. One challenge earns **exactly one defense ask** to the asker, on
     the mediated lane (§12.1.8); it is not a gap, so §6.2's one-ask ladder for a
     deliberate refusal does not cover it, and the condition for spending it is the
     same cited evidence §5.2.3 requires to reopen. A defense that restates
     `needs_human` and answers the cited evidence (optionally with refreshed question
     text, §8) leaves the escalation standing. A defense that repeats the question,
     refuses to touch it, or is rejected by §5.3 after its two repair re-asks
     **demotes** it: the item derives from
     the remaining stances with nothing held back — the asker's `needs_human` there
     is abstain-equivalent (§5.1) — and the question still prints,
     marked `undefended` (§6.3). One defense per item per round; a later challenge
     earns another; the asker may not re-escalate that item in the round its own
     escalation was demoted. A defense answer merges into that round's verdict the way
     a gap answer does (§6.2).
   - **Effective class.** `blocking` is requested by the asker and **granted by the
     run**: a question is effective-blocking iff it is chair-raised, or the count of
     seats whose latest stated stance on its item is `needs_human` is ≥ ⌊N/2⌋+1 (§5.1's
     bar, frozen `N`), evaluated with the §5.1 derivation. Every other question is
     **deferred**. Only an effective-blocking question may make the checkpoint
     **wait** while the council still has work (§8), force the `needs_human` outcome
     (§5.2.7) or earn the docket's "do NOT guess" line (§6.1, §7.1). A deferred question
     never stops a run that is still working; it prints at every checkpoint and is
     **asked at the terminal checkpoint** (§8). An answer to it is authoritative and
     it is never dropped silently: it prints (§6.3) and, in plan mode, becomes a
     named assumption (§7.2).
   - **Item-less questions.** A seat's `human_input` entry naming no item sets no
     disposition and is never effective-blocking: to stop the council, name the item.
     It is still asked at the terminal checkpoint (§8). A chair-raised question is
     effective-blocking whether or not it names one (§9).
   - **The chair raises questions through `human_input` on any of its passes, in
     both modes** — merge and comparison passes, either mode (§9). Having
     no stance to restate it can never withdraw, and nothing challenges it, so it can
     never be demoted: the question stands until the human answers or declines it, so
     it ends `retired` and nothing else.
   - The chair never challenges an escalation and no seat challenges a chair-raised
     one (§9). Escalation state changes are console lines (§11.1).

### 5.3 Messages between orchestrator and agents

All structured content is JSON extracted from model output; prose is for
explanations only. Schemas (findings, verdict, plan, plan-verdict, merge-plan,
comparison, watchdog-decisions — `merge-plan` and `comparison` each carry a
`human_input` array, so either chair pass can raise a question, §9, and so does
every seat verdict and the phase-0 findings schema, **in both modes** (§5.2.8)) are
**lenient by design** — stochastic models
paraphrase field names and enum values (scoped to model output; the config
loader is strict in the opposite direction, §3.3 — do not unify them):

- **Case and separators are not identity**: matching folds case and drops `_`/`-`,
  so `validationGate` or `VALIDATION_GATE` lands on `validation_gate` with no alias
  listed. Only *different words* need the synonym table.
- Accept common synonyms per field (`claim/title/description`,
  `gate/validation_gate/verify`, `why_not/reason`, …) and stance
  paraphrases (`human/ask_human → needs_human`, `neutral/skip → abstain`, …).
  A dropped stance is a lost dissent; map it instead.
- `withdraw` / `withdraw_human` / `no_human_needed` / `drop_escalation` on an item a
  seat had escalated → `abstain`, a stance the seat sent, which **withdraws** that
  seat's escalation (§5.2.8).
  Never mapped to `agree`, and never treated as a stance the seat did not take.
- Unknown severities degrade to `minor`; unknown stances invalidate just that
  position entry. **Empty positions is valid**; *a positions array present with
  only invalid entries* is a repairable error.
- Extraction: find the first balanced JSON object in the text, tolerating code
  fences and prose (try every `{` until one parses).
- **Two repair re-asks** per message — the design's only repair count: §6.1, §6.2,
  §9 and §12.1.8 mean this one. On parse failure, re-ask the same agent
  with the error + schema hint + its truncated previous answer (through the
  mediated ask path). Third failure = the message is rejected and logged.
  Repairs and gap-answer parses run through the same `max_concurrency` pool as
  ordinary asks.
- Questions between agents carry **orchestrator-assigned IDs** (`Q1…`, shown
  in the docket); answers target `to` + question ID. An answer matching no
  question is logged, never silently dropped; an answer with only `to`
  resolves to the addressee's most recent unanswered question from the
  answerer.
- **Every seat verdict carries a `human_input` array in both modes** (§5.2.8): an
  entry naming an item is an escalation request, one naming none is an item-less
  question in §8's ask set. Review puts questions to the human exactly the way plan
  does; the only plan-only verdict fields are `revised_plan` and `backing`. The
  backing grammar and the merge rule are normative in §7.1. Backing
  paraphrases map onto the three literals and nowhere else (`defer` / `either` /
  `any` / `none` / `whatever` → `no_preference`, `me` / `my plan` → `Agent<you>`,
  `agent 2` / `Agent 2` → `Agent2`). A token that maps to none of them
  **invalidates the `backing` field only** — leaving a gap for §6.2's ladder —
  and is never defaulted to `no_preference`: a value the parser invented is not a
  shrug. `abstain` arriving in `backing` is that case, not a synonym (§7.1).

### 5.4 Prompt design

- Phase prompts are **self-contained** (restart-safe): council framing, the full
  docket rendering, the task for this round, and — **in rounds only** — the agent's
  own ID. The phase-0 briefing carries no ID, no slot and no peer name and is
  byte-identical for every seat (§1.2, §6.1); a seat restarted during phase 0 is
  re-asked with those same bytes. IDs exist from convening, so keeping them out of
  phase 0 is a rule about the text, and §13.6 asserts it rather than trusting it.
- The docket is rendered **per viewer**: the viewer's own ID reads `you`, its
  own entries are tagged `[yours]` — near-identical twins must not mistake
  self-agreement for independent confirmation.
- Protocol addresses are IDs only; human names are captions for console/
  reports and never enter a prompt.
- Review briefing: verify before claiming; empty findings is a valid answer;
  severity guide. Deliberation prompt: verify → stance → challenge; disagree
  requires cited evidence; change your mind explicitly when shown better evidence;
  stance **every open item in every round** — leaving one out is a gap and you will
  be asked for it; `abstain` is always valid, costs nothing and neither endorses nor
  blocks, so abstain rather than going quiet, and a refusal is recorded and printed as
  a `declined` abstention in your name (§6.2).
- Escalation rules, same briefing: `needs_human` names the item it blocks and says
  what evidence would settle it without a human; a question that no longer needs
  asking is dropped by **restancing that item**, not by going quiet; a challenged
  escalation is answered by answering the challenge (§5.2.8). Only an escalation
  several seats hold, or one the chair raises, stops the council mid-run to ask; any
  other question is put to the human at the checkpoint the run reaches when it would
  otherwise end, so an unanswered question is never a way to avoid stancing an item
  (§8). A review seat may also raise `human_input` naming **no** item — a question
  about the brief, the scope or the goal — which is asked at the terminal checkpoint
  and never stops the run; to stop the council it must stance `needs_human` on an
  item (§5.2.8).
- Plan briefing additionally: an unanswered deferred question goes into the plan's
  **assumptions** with the guess it stands for (§5.2.8, §7.2), never decided silently.
- Sandbox briefing (both modes, one short paragraph of §12.1.6 text): your cwd is
  the project, and an absolute path into it is your own copy of the project — a
  file another member created is not in your copy; project directories cannot be
  deleted or renamed here (`EIO`, `EXDEV`; `mv` copies and leaves both); what you
  created yourself is fine; `$TMPDIR` (`.council-scratch` in your cwd) has no such
  limits; an overlay errno is never a finding — say what you were trying to do
  instead (§10, §6.3).
- Plan briefing: a plan is a sequence of **gated stages** — final states +
  validation gates + exceptions (traps where the obvious mechanical change is wrong
  — every one that exists, only those), cross-stage constraints, risks, rejected
  alternatives, assumptions, human_input. **No exact diffs, no line numbers, no
  predicted output**; length tracks the task; prescriptive not descriptive; nothing
  post-processes the plan text.
- Chair prompts: both passes may end with `human_input` for what the pass cannot
  settle itself — a product decision two findings or two plans force, not a merge the
  chair was too lazy to make (§5.2.8, §9). Demand exact ID echo — the watchdog
  digest labels agents `AgentN` in every phase
  (IDs exist from convening, §1.2); the resolver accepts **exactly** those forms and
  nothing fuzzier — a reply naming anything else is `watchdog_unmatched` (§9), and no
  config name can ever be echoed because none reaches the chair (ban above). Forbid merging partial overlaps; judge
  plans by consequence (mechanism fair game; naming/phrasing not worth a round).

---

## 6. Review mode

### 6.1 Phase machine

1. **Phase 0 — independent review.** One briefing, byte-identical for every seat
   (§1.2, §5.4), sent in parallel (`max_concurrency`-capped). pi's default tool selection (§4); agents
   expected to run tests/grep/git. Output: structured findings JSON (+ summary, open_questions) —
   an `open_questions` entry arrives as a §5.2.8 **item-less** human-input request
   (`H` id, round 0): never effective-blocking, asked at the terminal checkpoint.
   Parse via the lenient pipeline (two repair re-asks, §5.3). Fewer than 2 valid findings
   messages → outcome `aborted` + minimal report.
2. **Docket + reveal.** `Agent1..N` were assigned in the convened council's agent
   order at convening (§1.2); round 1's dockets are the first text that shows a seat
   its own ID or any other's.
   Ingest findings; the chair dedups same-defect reports into canonical items
   (`also_reported_by`), one merge pass. Any merge pass may also raise `human_input`
   questions (§9). The merge plan is validated structurally (no
   ID reused across groups; unknown IDs skipped; groups whose duplicate already has
   stances are skipped — merging would discard verdicts; and a group whose canonical
   item is **closed** is skipped too, so a merge can never quietly widen a claim the
   council already settled); any inconsistency → the whole plan is refused, docket
   stays unmerged, and the console says so (§9).
3. **Rounds.** Each round every agent gets the per-viewer docket (open items
   spelled out; `agreed`/`rejected` closed items collapse to one line each,
   **ID included**, so a seat holding evidence against a settled claim can stance
   it and reopen it, §5.2.3, and open human-input requests with their effective
   class and any answer received, §8) and must stance
   every open item, optionally add findings, agent questions/answers and
   `human_input` (§5.2.8 — an item escalation request or an item-less question,
   **both modes**), and sign off.
   After verdicts (order matters — keep it): chair merge of new findings
   (before the gap ladder, so gap prompts use canonical IDs) → docket snapshot →
   stance-gap ladder (§6.2) → escalation challenge/defense pass (§5.2.8) → closure
   pruning/reopening (§5.2.3) → round summary logged → **stalemate signature
   recorded** (§5.2.6) → **human
   checkpoint** (§8) → termination check (§5.2.7) → next round's docket renders each
   stance with the round it was given. The signature step is in both modes (§7.1 has
   it; §5.2.6 records it *before* the checkpoint precisely so an answer counts as
   movement), and the checkpoint precedes the termination check,
   so a question raised in the round everyone signs off is asked before an outcome is
   chosen.
4. **Termination** per §5.2.7, reached only after §8's checkpoint; default outcome if
   the loop exits is `round_limit_reached`.
5. **Report** (`report.md`, §6.3).

### 6.2 Stance gaps and the ladder (the "no silence" gate)

After each round, for each seat whose ask delivered a message this round: recompute
the open items it was shown vs. the positions it returned; missing = **gap**. An
`abstain` is a stance, so it is **never a gap**, and neither is a `declined` record,
which carries forward without restatement. Closed items are never gaps either: the
shown-set for this pass is the **open** items in that seat's docket (a closed
one-liner is rendered only so it can be challenged, §5.2.3). In plan mode the pass
also covers **direction** (§7.1): a `backing` that is missing, or that mapped to none
of the three literals (§5.3), is a gap; `no_preference`, exactly like `abstain`,
never is. Merge gap answers into the agent's verdict and log resolution.

**Gap or ask failure, one test: did a message arrive.** A seat whose ask delivered
nothing (child down, provider error, protocol error) is never gapped — that is §4's
restart-and-re-ask-once path, and its second failure ends the run. A message that
arrived and was rejected by §5.3 is gapped on **everything** it was shown: the seat
holds no positions this round.

**Budget: one ladder per (seat, round), at most 4 asks** — a ceiling for the
seat-round, not a per-cause allowance, so a deliberate gap takes at most 1 of them.
The budget is not refilled by a restart, a watchdog decision or a later chunk. Each
ask lists only what is still missing for that seat — its open items, plus the missing
`backing` in plan mode — and a docket too large for one reply is **chunked** across
successive asks of the same ladder, still at most one ask per item or backing. Every
ladder ask goes through the mediated lane (§12.1.8), counts against `max_concurrency`
and is watchdog-visible; an ask that fails *as an ask* takes §4's path instead.
§5.3's two repair re-asks **are** ladder asks 1 and 2 of that ceiling when the cause
is a rejected message, leaving two for gaps. Derivation, closure pruning and
termination all run **after** the ladder settles.

**Route by cause — the cause sets the length:**

| Cause | Signal | Ladder |
|---|---|---|
| **Mechanical** | a message §5.3 rejected after its two repair re-asks; a verdict that arrived and covered *some* items but not others; truncated output; a plan verdict with no `backing` — signed off or not, which is what §5.2.7's first condition needs (§6.2's prose says the same) — or one whose token mapped to none of the three literals (§5.3) | **Up to 4 asks**, each naming only what is still missing |
| **Deliberate** | the seat refused in prose; or an earlier gap answer already refused | **One ask**, saying that `abstain` is a valid answer, costs nothing, and neither endorses nor blocks |

**A gap that survives its ladder becomes a `declined` record, never a run failure.**
The item is recorded `abstain` + `declined: true` (plan: `backing: declined`), logged
`gap_declined` naming seat, round and items, and printed on the console (§11.1) and
in the report (§6.3, §7.2); the flag records that the run asked and was not answered,
whichever the reason. Nothing else changes: no exit code, no outcome, and no second
ladder for that (seat, item) while the record stands — the item stays in that seat's
docket and a stance for it in any later verdict clears the flag. A reopen (§5.2.3)
clears every stance record for the item, `declined` included, so the ladder applies
again. A recorded `declined` never withdraws the seat's sign-off for that round
(§5.2.5, §5.2.7).

**`declined` is inert.** It is an abstention: never support, objection, rejection or
credit in "accepted-by" or `also_reported_by` (§5.1, §6.3); never a stance the seat
*stated*, so it neither withdraws its asker's escalation nor counts toward §5.2.8's
blocking bar. In plan direction it is explicit: a `declined` backing is never
`no_preference`, so §5.2.7's first condition fails outright.

The cause table governs **gaps** only. The escalation defense ask (§5.2.8) is
neither a gap nor an ask of a deliberate refusal: it is its own bounded ask on the
mediated lane (§12.1.8), triggered by a peer's cited evidence.

**The ladder is a seat construct.** The chair holds no items, stances or backing
(§9): its messages keep §5.3's two repair re-asks and nothing more, and a message
rejected after both is a chair failure on §4's path (§9, §13.2).

### 6.3 Report (`report.md`)

Sections: header (project, config path + council name, mode, outcome, rounds,
sandbox line (`sandbox_mib` + member count `N+1`, from `run.json`), date); roster table (ID,
name, model, phase-0 finding count); chair model; **Consensus findings** (`agreed`
items only, severity-sorted; claim, evidence, raised-by, accepted-by, and **the
round it closed**, §5.2.3, plus its reopen history if anything challenged it
afterwards — an item reopened and never re-closed appears under Disputed / Not fully
vetted / Never vetted with that history attached, never silently under either, and
any `declined` seat on a consensus item is named there (§6.2));
**Not fully vetted** (`unheard` items — support short of the bar
because seats abstained, declined or disagreed without refuting, §5.1 — each with
every agent's
actual stance, the round it was given, `declined` where the run recorded one, and
"not vetted by: …"; an item that lacks a majority of `agree` must never print under
"Consensus"); **Disputed findings** (human calls — both sides verbatim); **Rejected
findings** (with the notes that killed them; "a false alarm suppressed, not missed")
— an item nobody objected to with a reason belongs under "Not fully vetted", never
here (§5.2.1); **Never vetted** (unseen); **Sandbox writes** (per member, from its
upper layer: created / modified / deleted, §10). Two markers: `sandbox_artifact` on
an item whose evidence names a file that exists only in a member's upper (that
member's own artifact, never project evidence — no other member can create a file
in your view, §10), and `sandbox_limit` on an item whose evidence *is*
one of §10's overlay refusals — printed with the error text under a "sandbox limits"
note, never as consensus evidence; **Open questions for the human** — per question:
asker(s), round raised and round of its latest text update, effective class, the
question **as updated**, every peer challenge's note verbatim, the asker's defense or
its absence, its end state (`standing` / `withdrawn` / `retired` / `demoted`,
§5.2.8), and the disposition each affected item reached with its section heading, so
an **item-less** question (§5.2.8) prints on its own with no item line, so
a question never stands in for the finding it was raised on; an item still `human`
(§5.2.7: only a `round_limit_reached` or aborted report holds one) renders under
**Not fully vetted** with that standing question inline, and an item whose escalation
was withdrawn, retired or demoted renders under its own §5.1 disposition and never as
an open blocker. Markers: `undefended`, `withdrawn`, `skipped`, `deferred`,
`unasked` — the last two print **inline on the item**, so a `skipped` question on an
`agreed` item is a caveat under Consensus findings, never a clean line (§5.1);
**Round-by-round verdicts**; **Artifacts** pointers. Agent-ID → name captions
everywhere ("Senior (Agent2)"); unknown IDs render as-is. Renderers are **pure
functions of the state plus injected clock/paths** — the header date is a parameter,
not a `datetime.now()` inside the builder, so §13.11 can compare reports
byte-for-byte.

---

## 7. Plan mode

### 7.1 Phase machine

1. **Phase 0 — independent planning.** Identical briefing (the goal from
   `--goal`/council; a `plan` council with no goal stated anywhere is a config error
   at startup — exit 1, §3.2). Agents investigate the workspace, then emit
   structured plans. Fewer than 2 valid plan messages → outcome `aborted` + minimal
   report.
2. **Chair comparison** (IDs exist from convening, §1.2). Chair maps the plans: `agreed` points (with the
   agents sharing them), `conflicts` (one entry per *same-decision-different-
   answer*, each with per-agent positions; no manufactured conflicts over
   wording), `uncovered` (what no plan covers), deduplicated `human_input` (the
   `blocking`
   request preserved; the effective class is derived per §5.2.8). Conflicts and
   uncovered points seed
   the docket as topics (`T1…`).
3. **Human checkpoint** (§8; also right after phase 0, before round 1): its wait
   rule is §8's, and it precedes every outcome (§5.2.7).
4. **Rounds.** Per-viewer plan docket: task, all approaches (+ revisions),
   agreed points, open decision points with stances-so-far and who hasn't
   answered, settled/rejected points, planner Q&A (questions carry IDs),
   human-input status (answers are **authoritative**; unanswered
   **effective-blocking** = "do NOT guess"; unanswered **deferred** = write it into
   assumptions, §5.2.8), current backing. Each round every agent: DECIDE (stance
   every open point, evidence-backed), CONVERGE — `backing` is **mandatory**
   (no sign-off without one) and is one of exactly **three** structured tokens,
   restated every round: `AgentN` (back agent N's current plan: their latest
   `revised_plan` if they submitted one, else their original approach; backing
   yourself is spelled `Agent<you>`), `merged: <label>` backed by a
   `revised_plan` whose name is exactly `<label>` and which carries the merged
   stages ("a merge nobody spelled out is not a plan"), or **`no_preference`** —
   no objection to the direction, and no claim of ownership of it, which unlocks
   consensus but cannot cause it (§5.2.7). Free-text approach
   names are never backing values. `abstain` is **never legal here** — §5.3's
   paraphrase mapping and §6.2's cause test both turn on that line; a missing or
   unmappable `backing` is a gap (§6.2), and a `declined` one is never
   `no_preference` (§5.2.7). IMPROVE
   (new topics, revisions, Q&A),
   ESCALATE (human_input). Then: the chair merge of new topics (before the gap
   ladder, so gap prompts use canonical IDs, §6.1) → the gap ladder (§6.2; open
   topics **and** a missing `backing`) → escalation challenge/defense pass (§5.2.8) →
   closure pruning/reopening (§5.2.3) → round
   summary → stalemate signature recorded → **human checkpoint** → termination
   check → next round. The checkpoint runs **before** the termination check (§5.2.7,
   §8).
5. **Termination** per §5.2.7 (plan order: effective-blocking-before-disputes, and
   consensus per the direction rule — every seat's current backing equal or
   `no_preference`, at least one real backer).
6. **Report** (`plan.md`, §7.2).

### 7.2 Report (`plan.md`)

Opens with **rules for the implementer** (prescriptive lines are settled outcomes;
descriptive lines are claims the code overrides; an impossible outcome is surfaced,
never silently followed; meet each stage's gate before the next). Then: roster table
with approaches; **Chosen direction** — the converged backing token resolves to a
concrete plan artifact: `AgentN` → agent N's latest plan (their latest
`revised_plan`, else their original approach); `merged: <label>` → the `revised_plan`
named `<label>` (latest round wins). If the council converged on a merge nobody
wrote down, print the fullest backer's plan **labeled as what it is** ("the merge
was never written down") — never invent a synthesis, and never name-match approach
titles. The roster table carries every seat's backing from the round that ended
the run —
`Agent2`, `merged: X`, **"no preference"**, or **"declined"**, never "abstained"
(that word belongs to item stances, §5.2.2) — so the implementer sees who proposed
the direction, who merely did not oppose it, and who never named one (§5.2.7).
**No converged token means no chosen direction**: the section prints the candidate
tokens with their backers side by side and says the council did not settle one; it
never picks one for the reader (§5.2.7). Stages with gates/scopes/exceptions,
constraints, risks, assumptions — and **every deferred, skipped, unasked or
undefended
question touching the chosen direction appears there as a named assumption spelling
the guess it stands for** (§5.2.8): never dropped, never presented as settled. Then **Agreed points**, **Decision points** (as
provenance: every topic with dispositions, the round each closed (§5.2.3), stances,
notes, raisers), **Human input** (who asked, when, the authoritative answers, and
every question that was deferred, skipped, left unasked or left undefended with what
the council
assumed in its place),
**Planner Q&A**, **Alternatives on the table** (all approaches + each one's rejected
alternatives), **Round-by-round verdicts**, **Artifacts**. The header date is an
injected parameter (§6.3).

---

## 8. Human input

- Requests carry `id` (`H1…`), asker(s), round, question, context, options, and the
  `blocking` **request**; the **effective** class (`blocking` / `deferred`, §5.2.8) is
  what the console, the docket and the reports print.
  **Deduplicated by normalized question text** — the same question from anyone is one
  entry, later raisers recorded as co-askers; the human answers once, and a question
  is asked at most once (§5.2.8's retirement).
- **The asker owns the text.** In a later round it may replace `question`, `context`
  and `options` — latest asker round wins, logged `question_updated`, `H` id stable —
  so the human reads the council's current question, not its first guess. A co-asker
  never rewrites it, and co-askers do not count toward §5.2.8's bar, which counts
  current `needs_human` stances on the item.
- **The checkpoint runs in both modes**, inside the round loop, **before** the
  termination check (§6.1, §7.1), plus once after phase 0 in plan mode (§7.1). Its
  **ask set** is exact: every question not yet put to the human that its asker still
  holds — an item escalation still **standing** (§5.2.8); a chair-raised or
  **item-less** question (§5.2.8), until it is answered or declined. A `withdrawn`,
  `retired` or `demoted` question is never printed or waited on again.
- **Wait rule.** The checkpoint waits when a question in the ask set is
  effective-blocking, and at the **terminal checkpoint** — the one whose §5.2.7
  checks, evaluated provisionally on the pre-checkpoint state, return `consensus`,
  `needs_human` or `stalemate` — where it waits for the **whole** ask set, deferred
  included. Mid-run it never waits for a deferred question (print and keep going),
  and it never waits at `round_limit_reached`: that
  is the only way a question ends a run unasked, and it prints marked `unasked`
  (§6.3, §7.2).
- Every checkpoint is logged (`human_checkpoint`: round, ask set, waited or not,
  answered and skipped counts).
- Waiting has **no timeout and no auto-skip**: the wait is not in-flight work — no
  tick arises from it, no seat is judged stuck, no restart and no deadline applies,
  however long the human takes (§9, §12.1.2). Only an `explain` round-trip keeps an
  ask in flight. Seats idle and spend nothing.
- Checkpoint flow (when it waits): print the **whole ask set** at the console → read
  lines until an empty line:
  `H1: text` answers (an answer to a deferred question is as authoritative as any
  other); `explain H1` / `ask H1: <question>` routes the question (plus
  follow-up) to the question's **asker**, whose
  long-lived session still holds the
  plan/docket context — it may re-read the workspace and answer with tradeoffs and a
  recommendation (routed through the mediated ask path so the watchdog covers it);
  the asker's reply is its **recommendation, not an authoritative answer**: it prints
  with the question and enters the next docket as the asker's own text, changing no
  stance and settling no escalation (§5.2.8).
  `skip` leaves everything unanswered. A chair-raised question refuses `explain`
  with one line naming why (the chair holds no roster ID and never deliberates, §9).
- Answering or skipping a question **retires** every escalation it covers (§5.2.8):
  the item is judged with that answer in it, or with nothing, and reaches its own
  disposition — `rejected` included, which only a *standing* escalation forbids.
- **An answer is worked on, not filed.** An answer enters the next docket as
  authoritative and the loop continues with a full round; a decline retires what it
  covers and buys nothing (§5.2.7, §5.2.8).

---

## 9. Chair

- **Required in every council** (§3.3): own process and session, **never
  deliberates, no roster ID** — so it is not one of `N` and cannot move the
  consensus bar (§5.1). For failure purposes it is a member like any other:
  spawned and first-prompted under §3.3's rules (an unusable chair is an exit 1
  naming the chair), restarted-and-re-asked once under §4, and a second failure
  ends the run. **Spawn timing: the chair spawns eagerly, with the seats**, so
  `watchdog_sec` covers phase 0 — (c)'s tick rule speaks of asks in flight and phase
  0 has them, and a lazily-spawned chair would leave a wedged phase-0 seat bounded
  by nothing but a signal (§14.5 forbids agent-ask timer caps). This spends nothing:
  a spawn is checked by `get_state`, not a prompt (§3.3). What is kept out of the
  deliberation is its **output**: it never stances, and §1.2's independence is
  untouched — its only pre-round act is keeping or restarting a seat, which reaches
  no seat and decides nothing about the work. It never challenges a seat's escalation and never defends one (§5.2.8);
  its own questions keep §8's `explain` refusal.
- **Questions**: any chair pass — merge or compare, either mode — may emit
  `human_input` (§5.3), which joins §8's ask set and is effective-blocking (§5.2.8).
- Jobs: (a) dedup merge passes in **both modes** (publication + any round with
  new items — review merges same-defect findings, plan merges same-decision
  topics, both under §6.1's structural validation); (b) plan comparison pass
  (publication); (c) **watchdog** —
  every `watchdog_sec` while asks are in flight, the chair receives a digest per
  in-flight agent (elapsed, idle seconds, event count, last tool call; labeled
  `AgentN` per §5.4) and decides keep/restart. **Tool-call previews** in that
  digest are rendered by one shared function: middle-out truncation (head and
  tail, elision marker in the middle — a long `npm test` tail matters more than
  its head), project-relative paths (stripped against `<project>`, the seat's cwd,
  §10), collapsed `cd … && …` prefixes. That function formats previews and nothing
  else — §10's `sandbox_full` / `sandbox_limit_hit` inference runs in the RPC pump,
  so a watchdog that is off or between ticks cannot blind the run (§10, §13.9).
  Conservative
  by prompt design: long tool output is legitimate; restart only clearly-stuck
  agents ("if unsure, keep"). A restart = abort → group-kill → fresh spawn;
  the pending ask fails through the normal restart-and-re-ask path. Unmatched
  chair references are logged (`watchdog_unmatched`), never silently dropped;
  no overlapping watchdog ticks, and no tick at all while any other chair pass
  (merge/compare) is in flight; no seat is judged stuck because the run is parked on
  §8's checkpoint, and an `explain` round-trip is covered like any other ask
  (§12.1.2). A merge plan refused by §6.1's
  structural checks
  is a validation outcome, printed and moved past — refusing to apply a garbled
  merge is not a chair failure.
- **`watchdog_sec = 0` is load-bearing, not cosmetic.** With the watchdog off nothing
  bounds a wedged ask but a signal: no deadline sits on an agent ask anywhere in this
  design and §14.5 forbids adding timer caps. Turning it off is choosing that, and
  §13.2 asserts the shape rather than leaving it to be discovered.
- Chair asks get their own bounded timeout (`chair_timeout_sec`, default 300 s —
  merges are cheap summarization) and the same two repair re-asks as agents (§5.3).
  Their lane is §12.1.8's single slot outside `max_concurrency`. The **watchdog pass
  is separately bounded to 120 s**. A wedged chair goes through §4's path — abort →
  group-kill → fresh spawn, re-ask once.

---

## 10. Sandbox: one overlay, one tmpfs, one namespace per member

The project is the **lower** layer of an overlay mount and a **tmpfs is the upper**; each
member sees the project at its own canonical path, and what stands there is its own
overlay. Members run whatever they like, and every byte they write — every `pip install`,
`--snapshot-update`, `git reset --hard`, planted repro script — lands in RAM and
disappears with the run. Nothing is copied, so no copy machinery exists anywhere
in this design: no skip list, no cleanup policy, no copied tree to walk into.

**A requirement, not a mode.** Unprivileged user namespaces + one mount namespace per
member + overlayfs + tmpfs-as-upper, established at load (§12.1.11: after config, before
the run directory, before any spawn). There is no capability probe and no dry-run mount:
steps 1–7 below (4b and 6b included) *are* the check, performed on the real project. Any
step that fails is fatal — one line naming the syscall and its `errno`, exit 1, nothing
spawned, nothing spent, no run directory. This tool targets one ext4 Linux box
where every step succeeds; a host that fails one gets exit 1, not a second code
path.

**Setup: two processes, because the kernel leaves no other shape.** An
unprivileged process cannot write its own `uid_map`. After `unshare` its euid is
the unmapped overflowid and it holds no `CAP_SETUID` in the parent namespace, so
the write is `EPERM` for *every* map shape; a process **outside** the new
namespace writes the same file with no privileges at all. Measured on the target
box at `uid 1000`, `CapEff 0`, seccomp off, AppArmor off: the whole sequence
passes, 1:1 map included.

1. `os.fork()`. The **supervisor** — the process the user launched — holds no
   event loop, no state, and no child but the one it made. Python has no
   `os.mount`, so `mount(2)` arrives through `ctypes` — the only raw syscall
   wrapper in the design, and the only module that gets one (§12.1.9). The
   supervisor never calls it.
2. The **orchestrator** — the child, which owns everything from here: §12.1.1's
   loop, §4's children, the run — calls `os.unshare(CLONE_NEWUSER |
   CLONE_NEWNS)` (3.12+) and blocks on a pipe until it is mapped. It stays in
   the supervisor's process group, so a terminal Ctrl-C reaches it directly.
3. The supervisor writes the child's `setgroups = deny`, then a **1:1**
   `uid_map`/`gid_map` mapping the uid to itself, and closes the pipe.
   One-to-one is what keeps §4's `killpg` signalable from the orchestrator and
   keeps files a seat creates owned by you outside the namespace. After that the
   supervisor does two things: forward `SIGINT`/`SIGTERM` to the child (a signal
   aimed at its pid by `kill`, a CI runner or `docker stop` would otherwise die
   at the supervisor), and exit with the child's status, `128+signum` included
   (§3.2).
4. `mount(NULL, "/", MS_REC | MS_PRIVATE)` — **a hard requirement, unconditional.**
   Where the host's mounts are shared (a systemd desktop: `/` and `/home`
   shared), a mount made in the new namespace propagates back and the sandbox
   appears over your project *in your own shell* for the length of the run. One
   call removes the hazard on every host. §13 must test it in a way that can
   fail (§13.9): assert propagation tags on the sandbox's own mounts and *build*
   the shared-parent hazard, rather than diffing the host mount table, which
   passes with step 4 deleted.
4b. **`prctl(PR_SET_DUMPABLE, 0)`, before anything is spawned.** A member is a child of
   the orchestrator in the same pid namespace, so `/proc` is the one path a per-member
   mount namespace does not close. Measured here, an `exec`'d member is refused the
   orchestrator's `cwd`, `environ` and `exe` by the kernel's own rules but **can list
   `/proc/<orchestrator>/fd`** — every member's pipes and the lower-layer fd the journal
   classification runs on. Clearing dumpability makes that `EACCES` too, so the run's file
   objects are closed without depending on which kernel rule covers which entry.
   `status`, `cmdline` and `mountinfo` stay readable, `killpg` keeps working, and the
   orchestrator still has what it needs on each member — its ns fd (§4) and its pgid (all
   measured). It protects the
   orchestrator alone — `fork` hands each member the default flag again (measured), which
   is why the peer channel below exists. Cost: no core dump, and no user `gdb`/`strace` of
   the orchestrator without `CAP_SYS_PTRACE`.
5. Mount one tmpfs at `<runs_dir>/sandbox`, `size=<sandbox_mib>m`. It lives
   **outside the project**, and the runs root itself must stay outside the tree for
   the same reason (§3.5): a runs dir inside the project sits inside every seat's
   lower layer, so seats could read and "write" the run's own artifacts into their
   private uppers, and `upperdir`/`workdir` would end up underneath `lowerdir`.
   `<runs_dir>` is the runs root §3.5 resolved and **already created** (`0700`,
   outside the namespace, load's last action) — this step mounts into it, never
   makes it, so a misconfigured runs root is §3.5's load error and never a `mount`
   errno.
6. Per **member** — every seat **and the chair**, from one code path: `up/`,
   `work/`, `view/` and `scratch/` under that tmpfs, and an overlay mount
   (`lowerdir=<project>`, `upperdir=…/up`, `workdir=…/work`) onto its `view`. The
   orchestrator makes and reads these (its journal, §3.4); no member ever sees
   these paths (6b(4)). One pair per member, mounted **once** and reused by every restart
   of that member: a second overlay on an `up`/`work` pair already mounted is `EBUSY`
   (measured), and sharing one `<project>` as the lower of many overlays is what this step
   does every run.
6b. **The member's own namespace**, performed by the launcher of §4 in itself, before
   it execs (§12.1.9). Five steps, and the order is the rule:
   1. `unshare(CLONE_NEWNS)` — a mount namespace only. No new user namespace, so no
      map write and no privilege work: the orchestrator's `CAP_SYS_ADMIN` in the
      run's user namespace is all `mount` needs here. Cost: five `mount` calls,
      ≈0.05 ms measured; §4 states where and how often it is paid.
   2. `mkdir <view>/.council-scratch`, then `mount --rbind <slot>/scratch
      <view>/.council-scratch`: the member's `TMPDIR` (§4). The `mkdir` is on the
      **view** path — at that moment `<project>` is still the lower layer, and making the
      mount point there would create a directory in the user's tree (measured). Without
      the bind, `mktemp`, test-suite tmpdirs and `pip` caches go to the **host** `/tmp`,
      shared between members and still there after the run (measured). Inside the view it
      is outside `up/`'s listing (the journal pass skips that one mount-point name) and
      inside the cap, so heavy temp work spends `sandbox_mib` and its `ENOSPC` is the
      `sandbox_full` path below.
   3. `mount --rbind <view> <project>`: **the project's canonical path becomes this
      member's own view.** Absolute paths into the project keep working for that
      member — `.venv/bin/*` shebangs, editable-install path entries, `git -C`,
      `cd "$(pwd)"` (all measured) — and every write they make lands in that
      member's upper and so in its journal. Directory surgery at that path is the
      same overlay `EIO`/`EXDEV` as anywhere else: the real tree is protected by
      the overlay, not by a mask.
   4. `mount -t tmpfs -o ro,size=1m` over `<runs_dir>/sandbox` and over `<runs_dir>`:
      the run's artifacts and every other member's upper are gone — the path survives as
      an empty read-only tmpfs that lists nothing, returns `ENOENT` under it and `EROFS`
      on a write, never a swallowed success — and `mount -t tmpfs -o size=1m` over
      `/tmp`, **but only when `/tmp` holds neither `<project>` nor `<runs_dir>`**: a mask
      over an ancestor of the cwd hides the member's own working directory, which is what
      (3) just made it (§13.9 pins both branches; when skipped the console logs
      `tmp_shared`). A tool that ignores `TMPDIR` and writes `/tmp` by name spends that
      1 MiB and gets `ENOSPC` — the same condition as a full cap, reported the same way.
      The binds of (2) and (3) exist already, and a mount under a masked path stays
      mounted but unreachable by name, which is why `TMPDIR` lives in the view.
   5. `chdir(<project>)`. A member's cwd therefore resolves: nothing it needs lies
      under a masked path.
   The mask sizes are constants, not knobs.
7. **Keep one directory fd on `<project>`, opened before any member exists.** It is
   the only way to ask the lower layer "did this path exist before the run", which
   §3.4's created-vs-modified rule needs.
8. **A member never execs before its step 6b completes.** The launcher reports the
   failing syscall and `errno` on the spawn pipe and does not exec; the run ends as
   a §10 setup failure (exit 1, no fallback, §14.4). On success it reports its own
   namespace id (`readlink /proc/self/ns/mnt`) on the same pipe as the last thing before
   `execvp` — that report is §4's record of the member's namespace, because reading it
   from outside in that window answers with the orchestrator's own (measured). A member
   that could see the unwrapped tree is a bug, never a fallback.

**Teardown is namespace exit.** When the last process in the namespace goes, the
tmpfs and every overlay are gone: no cleanup step, and no delete that can throw
and fail a finished run (§3.2's rule holds trivially). A member's own namespace is
torn down the same way, with its masks and binds, when its process goes. A SIGKILLed orchestrator
leaves no files behind — only RAM, and only while an orphaned child holds the
namespace open, which is exactly what §4's dispose path prevents. Killing the
supervisor is not killing the run: `PR_SET_PDEATHSIG` does not survive
`unshare(CLONE_NEWUSER)` on this kernel (measured — the child outlived its
parent), so an orchestrator whose supervisor was `SIGKILL`ed finishes the run,
writes its report and exits with nobody watching. What is lost is the exit code,
not the result.

**The upper layer is the write journal.** Listing `up/` is exactly the set of
paths that member touched — writes made through `<project>` included, since step
6b(3) made that path its own view. One fixed name is skipped by the pass:
`.council-scratch`, which step 6b(2) creates in the upper as a mount point and is not
a write. Read entries by **inode type, not by name**: a deletion is a
character device 0:0, which this kernel records under the deleted file's own name
rather than the `.wh.`-prefixed spelling, and a file created and then deleted
leaves no entry at all — the journal says what that member touched, not what would
still exist had the run been committed. It is recorded per member (§3.4) and printed in
the report (§6.3). `up/` alone cannot separate **created** from
**modified** (both are plain files) and a copied-up directory looks exactly like
a created one, so the classification is one question asked through step 7's
lower-layer fd: does this relative path exist in the project? Unprivileged
`listxattr` on the upper returns nothing, so `trusted.overlay.*` is not an
available signal. Its other job: evidence naming a file that exists
only in some member's upper came from the run, not the project — flagged
(`sandbox_artifact`) and never independent confirmation (§6.3, §1.2, §5.1).

Load-bearing facts, one line each:

- Copy-up is a page-cache copy (~20 ms per 100 MB measured on this box): the
  cost of touching a big file is RAM, not time.
- Directory surgery on the **lower** layer is an overlayfs limit with no fix
  available here (measured; no mount option helps — `index=on` changes nothing,
  `redirect_dir=on` is `EPERM` for an unprivileged mount):
  `rm -rf <project dir>` fails `EIO` part-way and leaves it half-emptied;
  `rename` gives `EXDEV`; `/bin/mv` copies the tree, fails to delete the source,
  exits non-zero and leaves **both copies**. A directory the member created removes
  normally, in the view and in `$TMPDIR`.
- Those limits are briefed, not left to inference: §5.4 tells the seats, §6.3 marks
  such an item `sandbox_limit`, the console logs `sandbox_limit_hit` naming the member.
  Both markers come from one pass **in the RPC pump** over `tool_execution_end`
  records it has already decoded (§4, §12.1.7): filtered to `isError` — which pi sets
  for every non-zero shell exit — and matched against one compiled alternation on the
  text tail. Successful output is never scanned, and the pass never runs in §9's
  watchdog: neither `watchdog_sec = 0` nor the gap between ticks may change what the
  run reports (§13.9). Nothing else here sees a seat's syscalls, and the console line
  says it is an inference.
- The cap is `sandbox_mib` (§3.3), one ceiling for all members together. Filling it
  returns `ENOSPC` to the member that hit it: the pump pass names it and logs
  `sandbox_full`. One `statvfs` per round on the sandbox tmpfs (§12.1.9's module) catches
  a spent cap no member quoted the errno for, logging `sandbox_full` with nobody named.
  Per-member bytes come from the teardown journal pass (§3.4) and go to `run.json` and
  the console. Either way: keep
  the run going — never crash, and never let it read as "the project is broken".
- Reads pass through to the real tree, so `.git` history, `.venv`,
  `node_modules` and installed dependencies are all *there*, reachable without
  exposing the user's tree to the write path.
- Hardlinks in the project copy up independently, and a suite that needs real
  root, device nodes or a nested container will fail inside the sandbox. Both
  surface as what they are — a member's reported failure — not as harness bugs.
- A member sees one project — its own view — and nothing of the run: `<runs_dir>`
  and everything under it, and host `/tmp`, are masked (6b(4)). It cannot undo any
  of it: `execvp` clears the capabilities the run's user namespace gave it, so after
  that exec `CapEff` is `0` and `umount` of its own bind or mask is `EPERM` (measured,
  including from an `exec`'d child). Its `bash` tool inherits the namespace, so tool
  shells are inside it too.
- Members stay same-uid processes in one pid namespace, and **`fork` gives every member
  the default dumpable flag** even when the orchestrator cleared its own (measured:
  `PR_GET_DUMPABLE` is 1 in a child of a cleared orchestrator), so members read each
  other's `/proc` — `cwd` included — and can plant a file in a **peer's upper** through
  `/proc/<peer>/cwd/…` (measured). They cannot reach the real tree that way: a peer's cwd
  is that peer's own view, and the orchestrator's `/proc` is closed by step 4b, which
  therefore protects the orchestrator **alone**. So §1.2's claim is about the project,
  §6.3's `sandbox_artifact` is an honesty measure and not a boundary, and same-uid
  `chmod` is no substitute: members run as you.
- Out of scope by construction: the network (a `git push` still reaches the
  remote), `$HOME`, and `PI_CODING_AGENT_DIR` (a member can still read
  `auth.json`). And §4's `--approve` loads the project's own `.pi` extensions, skills
  and MCP servers into every member, so reviewed code runs as member tooling — inside the
  sandbox, with the reachabilities above intact; `--untrusted` is the opt-out. The
  sandbox makes the **project** disposable, which is all §1.6 claims.

---

## 11. Observability

### 11.1 Console

- `[council]`-prefixed lines for everything the run itself says (config file and
  council chosen,
  run dir, the §10 sandbox line (tmpfs cap, member count `N+1`, per-member bytes at the
  end), any `sandbox_full` or `sandbox_limit_hit` (§10: pump-inferred from `isError` tool
  results, `sandbox_full` also from the per-round `statvfs`, and labelled so), and
  `tmp_shared` once per run when §10 step 6b(4) cannot mask host `/tmp`,
  transcripts level, project-trust mode (§3.1), per-seat and chair spawn results and
  any §3.3 unknown-model warning,
  watchdog interval (§9), phase
  headers, per-ask completions with durations, rejections, watchdog decisions,
  gap ladders and `gap_declined` (§6.2 — one line naming seat, round and items),
  escalation withdrawals, demotions and effective-class changes (§5.2.8 — one line
  naming item, asker and cause),
  every §8 checkpoint (questions printed, whether the run waited, answers and
  declines recorded) and the round it unlocked when an answer arrived (§5.2.7),
  termination, report path, transcript tally).
- A seat lost mid-run is printed **as it happens** and again in the closing
  summary: `Agent2 · Senior failed in round 3: <pi errorMessage>` — and the run
  stops (§4).

### 11.2 Transcripts (`transcripts`)

- `off` (default): no transcript files. `agents/` appears only if a
  child produced stderr/crash output (§3.4's sink rule).
- `full`: verbatim stream, no trimming of anything. Crash dumps are always
  written untrimmed.
- Exactly two levels exist.
- Every run ends by printing what it wrote (bytes or "none written").

---

## 12. Architecture (Python 3.14)

### 12.1 Shape: invariants, not a module map

Each invariant is checkable by reading the tree, and each is load-bearing:
without it some other section of this spec stops being true.

1. **One process, one asyncio event loop.** Agents are subprocess children
   driven as tasks. No thread or process per agent, no orchestration outside
   that loop. §4's read-stdout-continuously backpressure rule only holds under
   this shape. The single exception is §10's supervisor, which runs no loop and
   holds no state: fork, map the child, forward two signals, relay the status.
2. **Interactive input never blocks the loop and is never joined on the way
   out.** A checkpoint waits for a human line while the loop stays live (an
   `explain H1` round-trip in flight, §8) and the run must
   still be able to leave while that reader is parked (§3.2). A parked checkpoint is
   never a stuck seat (§8, §9). Daemon thread,
   `add_reader`, or anything else — the rewrite plan's call, not this spec's.
3. **One deliberation engine, written once.** Round loop, dispositions, the gap
   ladder (§6.2), the escalation ledger (§5.2.8: states, challenge/defense pass, effective
   class), closure pruning/reopening (§5.2.3), termination order, stalemate
   signature: exactly one implementation for both modes.
4. **A mode adapter holds no protocol logic and no prompt text.** It supplies
   mode *data* (item shape, severity/stance vocabulary, which sections a docket
   or report has) and mode *hooks* (phase-0 inputs, verdict application,
   termination hook) — no schema parsing, no prompt literals, no termination
   arithmetic. Check: none of those appear inside a mode adapter.
5. **Per-viewer docket rendering exists once.** The shared skeleton — items,
   stances with the round they were given and their `declined` flag, who holds no
   current stance,
   collapsed `agreed`/`rejected` one-liners **that still carry their item ID, so a
   seat can challenge a closed item (§5.2.3)**, §8's open human-input requests with
   their effective class (**both modes**), the §5.4 `you`/`[yours]` rewrite —
   is shared; a mode adds *sections*, never a second renderer.
6. **Prompt and persona text is data in one obvious place**, greppable. It does not become string literals
   scattered through logic.
7. **The pi RPC dialect sits behind one boundary:** framing, id correlation and
   the command set (§4) in one place, so a protocol change is a one-file fix.
8. **Every ask is mediated, through two bounded lanes.** Agent asks go through
   the pool that enforces `max_concurrency`, records in-flight activity for the
   watchdog and owns restart-and-re-ask. **Chair asks have their own single-slot
   lane outside `max_concurrency`** — still mediated (watchdog-visible, bounded by
   `chair_timeout_sec`, two repair re-asks (§5.3), §4's restart-and-re-ask), never queued
   behind agent work. Sharing one pool makes the stall backstop
   starvation-prone: when `max_concurrency` ≤ the number of wedged agents, every
   slot is held by exactly the work the watchdog exists to interrupt.
9. **Process safety is isolated enough to test without pi:** pid/pgid guards,
   the stdin-close → SIGTERM → SIGKILL escalation, group kill, the
   namespace-scoped orphan sweep (§4), drivable against a fake child (§13). The
   §10 sandbox is the same kind of module and gets the same isolation
   (handshake, `unshare`, `mount(2)`, the dumpable `prctl`, view paths, the **step 6b
   member launcher and its spawn pipe** (its failure report and namespace id), the member
   namespace fds the sweep is keyed on, the lower-layer fd, member `scratch`, upper
   listing, per-round cap `statvfs`) — its **only** owner, supervisor lines included,
   testable against a temp dir and a throwaway tree with no pi and no model: the
   launcher is exercised by pointing it at a `sleep` child instead of pi.
10. **Deliberation does no I/O.** All of §5/§6/§7 state — dispositions,
    termination, stalemate, report text — is computable from data with no child
    process, no network, no tokens, clock and paths injected (§6.3, §13). This is
    what makes §13 offline.
11. **All config work precedes the run directory:** load, validate, select and
    flatten the council (§3.3), resolve the runs root, discover pi, *then* create
    the runs root (§3.5 — load's last action, so a config error or an
    unresolvable `pi` creates nothing anywhere), establish the §10 sandbox (steps
    1–7; 6b runs per member, at spawn) — and **only then** create the run directory and write
    `run.json` (§3.4; the point §13.6 and §13.9 assert against), so a bad config, an
    unresolvable `pi`, or a host where `unshare` or `mount` fails never leaves a run
    directory behind. Every exit path then finalizes
    `run.json` with the `outcome` and the write journal (§3.2). A seat that turns out
    to be unusable is found later, at its own first
    prompt (§3.3), so it owes a minimal report rather than silence
12. **Reports read `run.json` and final state, never the protocol log** (§3.4) —
    and `run.json` is plain JSON on disk, readable without the tool.

The rewrite plan owes this spec one thing in return: a map with **exactly one
owner per invariant**, modules small enough to test alone, and no assumed
pre-existing file names. A single useful smell test — a mode
adapter should be readable in one sitting; if it grows protocol logic, §12.1.3
and §12.1.4 have been broken.

### 12.2 Python 3.14 specifics to use

- `asyncio.TaskGroup` + `asyncio.timeout` for structured concurrency; a semaphore
  for `max_concurrency`.
- `asyncio.create_subprocess_exec(..., start_new_session=True, limit=…)` for
  children, always through §4's launcher form
  (`--setup-seat --slot <file_base> -- <pi_cli> …`) —
  never a blocking `subprocess.Popen` on the loop and never `preexec_fn`; the
  session/group separation is
  what enables §4's pi group kill.
- `os.unshare(CLONE_NEWUSER | CLONE_NEWNS)` (3.12+) for the sandbox namespace,
  and `mount(2)` through `ctypes` because Python exposes no mount call — behind
  §12.1.9's sandbox module and nowhere else. Verified on the target box as
  `uid 1000` with `CapEff 0`: unprivileged userns, a 1:1 map and an overlay with
  a **tmpfs** upper all work, `exec` from the upper included; an **overlay**
  upper is refused (`not supported as upperdir`), so `/tmp` inside a container is
  not automatically usable. §10 holds the rest of the measurements.
- **t-strings (PEP 750)** are available for prompt assembly; plain f-strings are
  equally acceptable — the rewrite plan decides.
- Deferred annotations (PEP 649): annotate freely, no `from __future__` dance.
- **Wire names are the field names** (§3.3): a `@dataclass` that serializes to
  `run.json` or a verdict carries its snake_case name directly — no
  `field(metadata=…)`, no alias table, no camelCase attributes.
- `tomllib` (stdlib) parses `council.toml` and nothing else; every writer emits
  JSON/JSONL (§3.3, §3.4). `load` takes a **binary-mode file only**
  (`TypeError: File must be opened in binary mode` otherwise), and
  `TOMLDecodeError` carries a line/column that belongs in the user-fixable line,
  never in a traceback (§3.3).

---

## 13. Verification (offline-first; the suite must run safely *inside* an agent)

A family's assertions are the acceptance criteria for the section it cites; where
a section enumerates its rules, **that enumeration is the case list** and the
family adds only what it does not state. **Done means:** every family green offline on
the target box, §13.12 green against the installed pi, and §3.2's exit contract
observed on every path — no outcome ever changing a code.

**Harness rules.** The whole suite passes when run from inside a council agent: §13.9's
fixture is a fresh plain directory, and a fresh overlay over a plain lower mounts fine
from inside a member's namespace, mount point on an overlay included (measured).

- Drive the CLI with `pi_cli` aimed at a **scripted fake pi** (canned JSONL, the
  child of §13.7). `pi_cli` is resolved once per council (§3.3), so every child a
  run spawns — seat, chair, every restart, including seats with no explicit
  `provider` — is the fake binary, and no code path spawns `pi` any other way. The
  §4 launcher execs whatever `pi_cli` resolved to and adds nothing to its argv, so
  the fixture asserts the same argv it would without the launcher and no §4 rule is
  tested against a shape production never uses.
- **Scrub `COUNCIL_RUN_DIR`** from every spawned environment: an inherited
  marker trips §3.5's nested guard. Scrub **`COUNCIL_RUN_PATH`** with it — inert
  on its own (§3.5), but a stray value lets a nested-refusal case print a parent
  run this suite never made.
- Set `PI_CODING_AGENT_DIR` to a throwaway dir, `PI_OFFLINE=1`,
  `PI_SKIP_VERSION_CHECK=1`, and point `COUNCIL_RUNS_DIR` at an empty **canary**
  that fails the suite if anything appears where a family must not write one
  (§13.9 asserts its ordering rule by inventory instead). The one exception is
  §3.5's runs-root resolution (a §13.5 case), which has to run with
  `COUNCIL_RUNS_DIR` **unset** and moves the canary to `XDG_CACHE_HOME`.
- §12.1.10 holds: everything is asserted from data with clock and paths injected,
  so reports compare **byte-for-byte** (§6.3, §13.11).

### 13.1 Deliberation units (pure, no I/O)

**Cites §5.1, §5.2 and §5.2.7 (both modes) — their enumerations are the case
list.** Adds: every pattern reachable under §5.1's judgment invariant for an open
item yields **exactly one** state; no open item ever derives with a seat holding no
current stance except `unseen`; a `declined` record is inert in every count; and a
**closed** item is never re-derived (a round-2 `agreed`/`rejected` still holds in
round 5), while reopen→reclose churn moves the §5.2.6 signature. The escalation ledger is
a second asserted axis (§5.2.8): every transition — `standing` → `withdrawn` /
`retired` / `demoted`, and the effective-class grant at the bar, below it, and
chair-raised — is one input → state assertion. A withdrawn, retired or demoted
escalation never leaves an item `human`, a `declined` record for its asker leaves it
standing (§5.2.8, §6.2), and a retired or demoted asker's `needs_human` is
abstain-equivalent in every §5.1 count, so it neither blocks nor creates consensus. Pure renderers: §9's
tool-call digest, file-base slugs (§4), §8's answer-line parser, the
`you`/`[yours]` rewrite (§5.4).

### 13.2 Engine (round loop over a scripted fake agent factory)

**Cites §6.2's cause table and §5.2.7's three direction conditions — one case
each, including `abstain` and `no_preference`, which are stances and never gaps
(§6.2).** Beyond them:

- a second ask failure ends the run — dispose, minimal report naming the member,
  its provider/model and the round, `aborted`, exit 1, **no other outcome
  reachable** — and a **chair** ask takes the identical path, naming `chair` (§9).
- a merge plan refused by §6.1's structural checks: docket left unmerged, printed,
  run continues.
- a **review**-mode chair question from a merge pass joins §8's ask set, is
  effective-blocking, and forces `needs_human` if declined (§5.2.8, §9).
- a **review**-mode **seat** question: an item-less `human_input` entry, and a
  phase-0 `open_questions` entry (§6.1), join §8's ask set, are **never**
  effective-blocking however many seats raise them, are asked at the terminal
  checkpoint, and an answer is authoritative and buys a full round (§5.2.7, §5.2.8,
  §6.3 renders one with no item line).
- **lanes** (§12.1.8): with `max_concurrency` ≤ the number of in-flight agents a
  watchdog tick is still issued; ticks never overlap each other or a merge/compare
  pass; an unmatched chair reference logs `watchdog_unmatched`; and no ask, agent
  or chair, bypasses the mediated path.
- a round whose only un-stanced entries are closed one-liners takes **zero** asks.
- **gaps (§6.2):** one ladder per (seat, round) with a **4**-ask ceiling for the
  seat-round (**1** of those for a deliberate gap, not 4 + 1), none bypassing the
  mediated lane; no item or `backing` is asked twice in the same round; an exhausted
  ladder records `declined`, logs `gap_declined`, moves the §5.2.6 signature, and
  changes **no** exit code and ends **no** run; a `declined` item is not reasked
  while the record stands, and a stance for it in a later verdict clears the flag;
  an answer that restances an item shown in the gap round **revokes that round's
  sign-off** (§5.2.5); a seat whose ask failed outright goes through §4, not the
  ladder; and a restart or
  watchdog replacement mid-ladder does **not** refill the budget.
- a `declined` `backing` is never `no_preference`: it fails §5.2.7's first condition,
  so no consensus is reachable while it stands, and §7.2 prints who it was.
- escalations (§5.2.8): a challenge **without** cited evidence costs **zero** defense
  asks, one with it costs **exactly one** per item per round, and none of them bypasses
  the mediated lane; a demotion or a withdrawal moves the §5.2.6 signature; an item
  whose escalation the human retired can close `rejected` in the round it is answered.
- a `deferred` question never makes the checkpoint wait mid-run and never forces an
  outcome, while a bar-held or chair-raised one does both (§5.2.7, §5.2.8).
- **both modes** run the checkpoint before the termination check (§6.1, §7.1, §8):
  the terminal checkpoint answers the whole ask set, the answer moves the §5.2.6
  signature and is followed by a **full** round (all seats, whole docket, sign-off and
  `backing` due) while an all-declined checkpoint returns the re-evaluated outcome,
  and only rule 6 and `max_rounds` bound those rounds; no escalation
  stands in a `consensus` report (§5.2.7).

### 13.3 Extraction and leniency (§5.3)

Every clause of §5.3 is a test on canned payloads. The guards leniency makes
easy to get wrong: a stance paraphrase is mapped, never
dropped; an unmappable `backing` invalidates that field alone and is **never**
defaulted to `no_preference`; `declined` is never accepted from a seat, only
recorded by the run (§6.2); a position against a **closed** ID never lands in
`verdict_unknown_findings` (§5.2.4).

### 13.4 Config loader and selection (§3.3)

The §3.3 fixture is the test input and convenes `review`, `review-staged` and
`plan` by name. §3.3's validation list **is** the case list: one exit-1 assertion
per rule with its exact message, loader-side types included. Add only precedence
(a council key wins whole, `[defaults]` reaches a council stating nothing, a
built-in default when its knob is omitted) and comment handling (comment-only
file, end-of-line comment, a comment holding `#`-ish text). Plus the hint half of
§3.3's matching rule: `maxRounds`, `maxrounds` and `chairTimeoutSec` are each
*refused* as unknown keys and each *hint* the right name (§3.1: hints, never
accepts).

### 13.5 CLI contract (§3.1, §3.2, §3.5)

Spawn the real CLI against the fake pi; §3.1's flag rules, §3.2's table and
§3.5's nested guard are the contract and the case list. The three assertions a table
cannot make by itself: **0 for a converged run whose report still holds disputes
and open questions**, and the usage trio (unknown name, second positional, no
name in a multi-council file) exiting 2 **before anything is spawned**.
The third: every spawn argv carries `--approve`, and every one carries
`--no-approve` with `--untrusted` — seat, chair and every restart — with the run
otherwise identical (§4).

The nested guard is **two** cases, not one: `COUNCIL_RUN_DIR` set → refusal naming the
parent run and printing `COUNCIL_RUN_PATH` **verbatim** (printing a path re-resolved
from the inherited `COUNCIL_RUNS_DIR` is the bug §3.5 forbids), and `COUNCIL_RUN_PATH`
set **alone** → no refusal at all, because it is not a marker.

Plus §3.5's three resolution rules, each with `COUNCIL_RUNS_DIR` unset and
`XDG_CACHE_HOME` aimed at a canary: `COUNCIL_RUNS_DIR` wins when set; unset →
`<XDG_CACHE_HOME>/council/runs`; `XDG_CACHE_HOME` unset **and** empty →
`$HOME/.cache/council/runs` (asserting the built-in default path, so a change to
it is a visible edit); `XDG_CACHE_HOME` relative → ignored, default used; a
relative `COUNCIL_RUNS_DIR` → absolute against the invoking cwd; and a runs root
under `--project` → exit 1 naming both paths with **no run directory and no
`mount` call at all**, proved by inventory the way §13.9 proves its setup-failure
ordering.

### 13.6 Startup and an unusable member (§3.3, §4, §9)

§3.3's three shapes, aimed at the fake: exit 1 at spawn; `get_state` then a first
prompt whose **last assistant message** settles with `stopReason: "error"`, empty
`content` and an `errorMessage`, emitted as pi does — on `message_end`, in
`agent_end.messages`, with `auto_retry_end {success: false, finalError}` and
**`agent_settled {aborted}` carrying nothing else** (a fake that puts `stopReason`
on `agent_settled` fails this family). Assert the
`auto_retry_start` records are seen and the member is classified
**unusable**, not repaired-and-re-asked; stderr carrying
`Model "X" not found for provider "Y"`. The first two exit 1 naming member,
provider, model and pi's message after **exactly one prompt each** — that count is
the operational meaning of "spends no tokens". The third runs, warned, never
refused. The **chair** shape is presented on its first **publication** pass (§9's
eager spawn): a chair that fails a phase-0 *digest* restarts once — §4's shape, not
a §3.3 failure — while one whose first publication pass settles on
`stopReason: "error"` exits 1 after exactly one prompt. One hygiene case belongs
here too: the phase-0 briefing is **byte-identical across every seat** and matches
no `Agent\d+` (§1.2, §5.4, §6.1) — a build that stamps IDs into the independent
phase fails here, and that assertion is the whole enforcement §1.2 has. Ordering both ways (§12.1.11): a config error or an unresolvable `pi_cli`
leaves the canary **empty**; a member failure leaves a run directory holding
**only** the minimal report and the `run.json` §3.4 finalizes.

### 13.7 pi RPC client (§4)

**A canned-JSONL child, no model; §4's rules are the case list** (`agent_settled`
ends an ask, `handled`/`queued` are errors, `clear_queue` precedes `abort`). The
traps a fake makes easy to get wrong: a multi-megabyte line survives; a line holding
`U+2028`/`U+0085` stays **one** record; `limit=0` is never used; `abort` resolves
on **its own** response, so waiting additionally for `agent_settled` is the bug §4
forbids, and an `abort` to a wedged child hits **its own** timeout; `success: false`
naming an unknown command fails the child at spawn.

### 13.8 Process safety and lifecycle (§4, §3.2)

**§4's and §3.2's rules are the case list** (pid guards, the stdin-close → TERM →
KILL order, no restarts while stopping, a second signal mid-dispose, the last-resort
sweep after ~15 s, flush before `os._exit`). Adds: `os.getsid(child) !=
os.getsid(orchestrator)`; and the fake pi **must spawn its tool shell detached, in
its own group** like the real one: a same-group fake makes "no orphaned tool shells
after dispose" pass for the wrong reason. With that fidelity, dispose with pi's
cleanup suppressed leaves exactly one survivor, and the **namespace sweep** reaps it
plus a `setsid`-ing grandchild — assert the namespace set of §4 (the orchestrator's plus
every fd it holds) matches precisely those, never a host pid, **and** that a descendant of
a *member* (whose mnt namespace is the member's, not the orchestrator's: §10 step 6b) is
matched too. The recorded id must be the **reported** one: assert a spawn whose recorded
namespace equals the orchestrator's own fails the run — that is the race §4 closes —
**and** that a descendant of a member that has since been restarted is matched: the held
fd keeps that dead spawn's namespace in the set, which is what stops its id from being
recycled into someone else's namespace — while a normal dispose (cleanup ran) or a
watchdog restart (§9) leave none, which is an **empty** `survivor_pids`, never a
missing one (§3.4). Finally the manifest and the line must agree:
assert `survivor_pids`/`survivor_ns` (§3.4) hold exactly the pids killed and the
namespaces they were found in, that every recorded namespace is in the run's sweep
set, and that the console line names that same scope rather than a bare count.

### 13.9 Sandbox (§10) — the only family with a platform requirement, on the target box

One helper: forks, unshares, is mapped 1:1 **by its parent**, and performs every
mount step including 6b; a second helper is §4's launcher, pointed at `sleep`.

**One narrow skip, measured.** Taking the fixture path's longest mount point from
`/proc/self/mountinfo`, if its fstype is `overlay` the lower would sit **inside** a
view — the family then prints `SKIPPED (lower inside an overlay)` and exits 0,
which is how a misconfigured fixture root (inside the project, where §3.5 already
forbids the runs root) becomes visible instead of mysterious. The rule is a
conservative **fstype proxy, not a prediction**: on an ext4-backed view such a
mount actually succeeds (`probes/9-lower-shapes.py`), and the skip is right
anyway, because the fixture would not be a plain lower and the journal would then
read a member's own view as the project. A plain lower — ext4, or a tmpfs such as
a member's `$TMPDIR` (`probes/9`, case 4) — works from anywhere, including from
inside a council agent and with the mount point itself on an overlay, so this is
the only skip in the suite: a test skipped for any other reason fails the suite.
It is also why 6b(4)'s `/tmp` arm below moves **`<runs_dir>`** into `/tmp` and never
the fixture: on this box `/tmp` is the container root's overlay, so a fixture there
trips this very proxy and the family reports green having tested nothing.

- **Step 4, a test that can fail:** its own `/proc/<pid>/mountinfo` shows no
  `shared:` tag on the tmpfs, overlay and mask mounts, **and** a case builds the
  hazard — a nested disposable namespace with `/` made `MS_REC | MS_SHARED`, then
  a child namespace skipping step 4 — and asserts the leak there.
- **Coverage (step 6b(3)):** a child of the namespace overwrites, appends and
  deletes at the project's **canonical path**, and a fake-pi child does the same
  through its view; the real tree is byte-identical afterwards (stat inventory),
  judged from **outside** the namespace and never from the child's `errno`, because
  a write there returns 0 by design. Every one of those paths appears in that
  member's `up/` and in `run.json`, and none of them in a peer's.
- **Absolute paths still work (the regression this shape exists for):** from
  inside a member, `<project>/.venv/bin/<console script>` execs (its shebang
  resolves), an absolute `sys.path` entry into `<project>/src` imports, and
  `getcwd()` == `<project>`.
- **Per-member isolation:** a file planted by Agent1 is absent from Agent2's view,
  both reading the same lower content; `<runs_dir>` and `<runs_dir>/sandbox` (every
  peer's `up/` and `run.json`) are masks that list nothing, a write to one is `EROFS`,
  and host `/tmp` is private **or** shared-and-labelled per 6b(4)'s condition. **Both
  arms, and neither puts the fixture in `/tmp`** (the skip above would eat the family).
  The condition is a **disjunction** — masked only when `/tmp` holds neither `<project>`
  nor `<runs_dir>` — so the two arms are taken by the second half of it: `<runs_dir>`
  outside `/tmp` (masked: the console says nothing about `/tmp`) and `<runs_dir>` under
  `/tmp` (mask skipped: `tmp_shared` logged, `/tmp` outside the member unchanged, and
  `<runs_dir>`/`<runs_dir>/sandbox` **still masks** — the case must show the other two
  masks are not skipped with it). The fixture stays on plain ground in both. From
  inside a member the real `/tmp` cannot host that runs root either — it is already
  6b(4)'s own `size=1m` namespace-private tmpfs, and writing a runs root into it is
  §10's `ENOSPC` case, not a fixture — so the helper mounts its own tmpfs **at
  `/tmp`** in its private namespace and puts the runs root under it (measured from
  this helper's exact shape — mapped, never `exec`'d, caps intact: the mount
  succeeds, host `/tmp` disappears in that namespace, `/tmp/runs` is usable, the
  plain lower stays reachable). After `execvp`, `umount` of
  the member's own bind is `EPERM`. §10 step 4b is pinned in both shapes: without it a
  member lists `/proc/<orchestrator>/fd`, with it `EACCES`, and a member forked from a
  cleared orchestrator reports `PR_GET_DUMPABLE` 1 — step 4b protects the orchestrator
  alone. The admitted peer channel is pinned too: a plant through `/proc/<peer>/cwd`
  **lands in that peer's upper and in no other upper, never in the lower** — so a kernel
  that changes either direction shows up as a failing assertion instead of a quieter
  sandbox (§10's honesty list).
- **Handshake reason, supervisor lines:** a map write attempted from *inside* the
  namespace fails; a clean child exits 0 and a SIGINTed one relays 130.
- **Journal:** the upper listing reconstructs created / modified / **deleted** (a
  0:0 character device, either name encoding), create-then-delete leaving no
  entry, created-vs-modified decided by the lower-layer fd and not the entry's own
  type, the `.council-scratch` mount point skipped, printed per member including the chair
  (§3.4, §6.3).
- **§10's measured errno table pinned:** `rm -rf` → `EIO` with the directory
  half-emptied — **at the project's canonical path, not only in a separate view
  mount**; `rename` → `EXDEV`; `/bin/mv` non-zero having copied the tree and
  left the original; a seat-created directory removing cleanly in the view and in
  `scratch`; `index=on` changing nothing, `redirect_dir=on` `EPERM`.
- **`TMPDIR` is `<project>/.council-scratch`:** distinct per member, usable for
  `mktemp` and `rm -rf`, still reachable after the 6b(4) masks, with nothing new
  appearing in the host `/tmp`.
- `sandbox_full` / `sandbox_limit_hit` inferred from a canned `tool_execution_end`
  line, naming the member, **asserted with `watchdog_sec = 0`** (§10): `isError` false
  mentioning `ENOSPC` logs nothing, `isError` true logs it, and a round that spends
  the cap with no member complaining still logs `sandbox_full` from the `statvfs`
  backstop, naming nobody. `sandbox_mib = 1` → logged, member named, run finishing
  with the exit code its outcome owes, per-member bytes reported at teardown.
- **One mount per slot (§10 step 6):** a second overlay on a member's already-mounted
  `up`/`work` pair is `EBUSY`, and it is the same mount a restarted member keeps using —
  assert a restart spawns without touching the mount and still writes to the same upper.
- Setup failing at any step (injected `mount(2)` `EPERM`) exits 1 naming the
  syscall and `errno` with **zero children spawned** and **no run directory** (the
  directory is created only after step 7 — §12.1.11) — by
  inventory, not the canary, which step 5 makes unsatisfiable once
  `<runs_dir>/sandbox` exists. A **step 6b** failure is the same shape one level
  down: the launcher reports syscall + `errno` on the spawn pipe, that member never
  execs, the run exits 1, no fallback spawn happens (§14.4).

### 13.10 Human input (§8, §6.1, §7.1)

**Cites §8 — its flow and the wait rule are the case list.** Adds what needs a test
rather than a rule: the checkpoint runs **before** the termination check **in both
modes** (§6.1, §7.1), so a question raised in the round everyone signs off is asked;
a **review** that would otherwise return `consensus` with questions standing asks
them first, and the retired escalation's item reaches a real disposition in the full
round that follows; a question answered or skipped is never asked again, by anyone,
in any phrasing; the asker's **updated** text is what the checkpoint prints and what
retirement acts on; a deferred question over unfinished work prints and costs no
wait; an `explain` reply is the asker's recommendation — no stance changes, no
escalation settles — and is the only ask in flight while the run is parked, which the
watchdog does cover, while the parked wait itself issues **no** tick and no restart
(§9); and a `skipped` or `unasked` question marks the finding it was raised on (§6.3).

### 13.11 Reports, artifacts and transcripts (§3.4, §6.3, §7.2, §11.2)

**Cites §3.4, §6.3, §7.2, §11.2 — their section and item rules are the case list**,
and §12.1.10's purity makes output byte-for-byte comparable. Adds the three ways
reports go wrong: an item without a majority of `agree` — a `declined` stance never
counts toward that bar — never prints under
"Consensus"; **Chosen direction** is the artifact the converged token names (or the
fullest backer's plan, labeled as what it is), never a synthesis (§7.2); and a
seat's own upper-layer artifact or overlay errno never prints as project evidence
(§1.2, §10). A fourth: a withdrawn, retired or demoted escalation never renders as an
open blocker, a question left undefended says so, and every deferred question touching
the chosen direction is in `plan.md`'s assumptions (§6.3, §7.2). A fifth: an `agreed`
item carrying a `skipped` or `unasked` question prints that question with it and never
as a clean consensus line (§6.3).

### 13.12 Smoke — the only family that spends tokens

`council review --project testdata/toy-project` (planted bugs; 2 seats + a chair,
2 rounds, transcripts off) converges, writes a correct `report.md`, and exits 0. Run it
after pi upgrades; from inside a council agent it needs `--allow-nested` — say so
in the test, or the criterion fails for the wrong reason.

**Sole home.** Four things in §13 exist nowhere else in the spec and are not
derivable from it: the harness rules above, the prompt-count assertion (§13.6),
`--allow-nested` for the smoke run, and every *method* claim in §13.9. Cutting one
means changing §3.3, §3.5, §4 or §10 first.

---

## 14. Prohibitions

A compact "must not" list to check a plan against in one pass; every rule is
normative in the section named.

1. **No second implementation of the deliberation protocol or of docket
   rendering** (§12.1.3, §12.1.5), and no mode adapter holding schema parsing,
   prompt text or termination arithmetic (§12.1.4).
2. **No unbounded background work**: no overlapping watchdog ticks (§9), no
   restarts once disposal has started (§3.2/§4), no un-awaited aborts, no
   abandoned event subscriptions without handlers (§4).
3. **No consensus and no rejection by silence**, and the council never runs on
   fewer seats than it convened: every seat holds a **current** stance on every open
   item (§6.2's ladder leaves no other state), support is measured over frozen `N`,
   `abstain` and `declined` are inert in both directions, and only a reasoned
   objection rejects anything. §5.1, §5.2.1, §5.2.3, §6.2 and §4 are the whole rule,
   and any new rule must preserve them.
4. **One sandbox mechanism: no modes, tiers, runtime capability probes or
   fallback paths** (§10). The per-member namespace of §10 step 6b is part of that
   one mechanism, never an optional layer or a `--sandbox` tier. A host that fails a §10 step gets exit 1; a weaker
   host would be a new §10 written for it, never an `if` inside this one.
5. **No compatibility machinery for problems this design does not have.** §3.2's
   flush-before-`os._exit` and §4's unbounded stdin limit are the whole answer at
   their edges; do not add timer caps, forced-sync writers or exit-flag hacks. Nor a
   last-known-stance ledger beside §5.1's current stances — §6.2 gives it no input,
   so it can only sit there untested and dead.
6. **No vote on an escalation, and no unaccountable one** (§5.2.8): no seat ends
   another seat's escalation by outvoting it, and no escalation outlives its own
   author changing its mind, a human declining the question, or a challenge it did
   not answer. No re-ask of a question already asked, and below §5.2.8's bar no
   mid-run wait, no forced outcome and no "do NOT guess" line.
7. **The council finishes what the human answered** (§5.2.7, §8). No run ends over an
   unasked question — `round_limit_reached` is the single exception — and no answer
   is filed into a report without a further **full** round of council deliberation on
   it. There is no shortened re-stance pass, no post-answer budget, no checkpoint
   timeout and no auto-skip anywhere in the design.
