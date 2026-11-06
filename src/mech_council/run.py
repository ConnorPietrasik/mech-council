from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
import traceback
from dataclasses import asdict

from . import runjson
from .asks import AskUnusable, Lanes
from .chairwork import Chair, ChairUnusable
from .cli import DEBUG, DETAIL, FULL, SUMMARY, VERBOSITY_LEVELS
from .config import Council
from .console import Console, Transcripts
from .crews import Crews
from .deliberation.engine import Abort, Engine
from .deliberation.escalations import MOOT, SETTLED, effective_class, showable
from .deliberation.implement import Implement
from .deliberation.state import (
    ABORTED,
    CONSENSUS,
    IMPLEMENTED,
    NEEDS_OUTCOME,
    NO_PREFERENCE,
    PARTIAL,
    RETIRED,
    ROUND_LIMIT,
    STALEMATE,
    UNVETTED,
    Docket,
    Execution,
)
from .deliberation.termination import direction_rule
from .humaninput import Checkpoint
from .humanio import LineReader
from .interrupt import Brake, owed, signal_name
from .members import MemberGone, Members
from .port import Port
from .process import lifecycle
from .prompts.brief import Brief
from .render import context as report_context
from .render.context import Interrupted
from .sandbox import journal as journal_mod
from .startup import report_paths
from .watchdog import Watchdog

EXIT_OK, EXIT_ABORT = 0, 1
RESULTS = (CONSENSUS, UNVETTED, NEEDS_OUTCOME, STALEMATE, ROUND_LIMIT, IMPLEMENTED, PARTIAL)


def _held_by(verbosity: int) -> str:
    """What, besides the report, holds a run's record at this verbosity."""
    if verbosity >= DEBUG:
        return "`events.jsonl` holds the run"
    if verbosity >= SUMMARY:
        return "`run.json` holds the manifest"
    return "verbosity `quiet` left nothing else to hold it"


def _disk_line(run_dir: str, report_paths: list[str]) -> str:
    """What the run directory kept beyond the report, in one line."""
    skip = {os.path.abspath(p) for p in report_paths if os.path.dirname(os.path.abspath(p)) == os.path.abspath(run_dir)}
    total = 0
    for root, _dirs, files in os.walk(run_dir):
        for name in files:
            path = os.path.join(root, name)
            if path not in skip:
                with contextlib.suppress(OSError):
                    total += os.path.getsize(path)
    if total:
        return f"disk: {total} bytes beyond the report"
    return "disk: nothing beyond the report"


async def orchestrate(launch, sandbox) -> int:
    council = launch.council
    run_dir, runs_dir = launch.run_dir, launch.runs_dir
    console = Console(run_dir=run_dir, verbosity=council.verbosity)
    lineage = f" extends {' → '.join(council.extends)}" if council.extends else ""
    is_implement = council.mode == "implement"
    console.say(f"council `{council.name}`{lineage} · mode {council.mode} · {len(council.agents)} seat(s) + a chair")
    if is_implement:
        crew_lines = [f"{label} ({len(crew_council.agents)} seat(s) + chair)" for label, crew_council in launch.crews]
        console.say("crews · " + " · ".join(crew_lines))
    console.say(f"roster {_roster(council)}")
    console.say(f"config {council.config_path}")
    console.say(f"run directory {run_dir}")
    if council.in_place:
        console.say(f"in place · members share {council.project} · scratch {sandbox.sandbox_dir}/<seat> · no write journal")
    else:
        console.say(f"sandbox {sandbox.sandbox_dir} · tmpfs {council.sandbox_mib} MiB shared by {len(council.agents) + 1} members")
    if council.human == "non_blocking":
        console.say("questions: the run never waits on them — you may answer while it works")
    elif council.human == "none":
        console.say("questions: none — the council will not be asked anything this run")
    if is_implement:
        console.say("an implement run never commits: you review the whole diff when it is done")
    console.say(
        f"verbosity {VERBOSITY_LEVELS[council.verbosity]} · project trust {'untrusted (no approval)' if council.untrusted else 'approve'}"
    )
    console.event(
        "run_started",
        project=council.project,
        mode=council.mode,
        council=council.name,
        sandbox_dir=sandbox.sandbox_dir,
        in_place=council.in_place,
        human=council.human,
    )
    manifest = runjson.create(
        os.path.join(run_dir, "run.json"),
        council,
        run_dir=run_dir,
        runs_dir=runs_dir,
        project=council.project,
        orchestrator_pid=os.getpid(),
        enabled=council.verbosity >= SUMMARY,
        crew_councils=launch.crews if is_implement else None,
    )
    transcripts = Transcripts(run_dir, "full" if council.verbosity >= FULL else "off")
    outcome, aborted = ABORTED, ""
    report, markdown_text = "", ""
    members: Members | None = None
    lanes: Lanes | None = None
    crews: Crews | None = None
    docket = _docket(council)
    if is_implement:
        # the run's docket is shared by every crew: global seat ids, the run's chair
        docket.roster = [spec.id for label, crew_council in launch.crews for spec in crew_council.agents]
        execution = Execution()
        for label, crew_council in launch.crews:
            execution.crews.append(
                {
                    "label": label,
                    "council": crew_council.name,
                    "mode": crew_council.mode,
                    "seats": [[spec.id, spec.name] for spec in crew_council.agents],
                    "chair": {"name": crew_council.chair.name, "file_base": crew_council.chair.file_base},
                }
            )
        docket.execution = execution
    # whether this run's own `run.json` write has started
    manifest_claimed = False
    baselines: dict[str, dict[str, str]] = {}

    def picture(signum: int) -> Interrupted:
        lanes_view = crews.lanes() if is_implement and crews is not None else lanes
        return Interrupted(
            name=signal_name(signum),
            elapsed=console.duration(),
            round=docket.round,
            phase=docket.phase,
            waiting_on=list(docket.parked_on),
            in_flight=lanes_view.digest() if lanes_view is not None else [],
        )

    def kill_now() -> None:
        """What the hurried exit does to the members once a closed stdin has not settled them."""
        if is_implement:
            targets = [member for crew in (crews.live if crews is not None else []) for member in crew.live_members.values()]
        else:
            targets = list(members.members.values()) if members is not None else []
        if not targets:
            return
        brake.skip("the members' stop grace")
        own_pgid = os.getpgid(0)
        for member in targets:
            lifecycle.send(member.pgid, signal.SIGKILL, own_pgid=own_pgid)

    def run_json_extra() -> dict:
        extra: dict = {}
        if aborted:
            extra["aborted_reason"] = aborted
        if brake.at is not None:
            extra["interrupted"] = asdict(brake.at)
        if brake.hard:
            extra["exit_path"] = "fast"
            extra["skipped_steps"] = brake.skipped
        return extra

    def write_run_json(survivors: lifecycle.SweepResult, journal: list[dict], report: str, *, swept: bool = True) -> None:
        nonlocal manifest_claimed
        # claimed before the write: an exit landing mid-write must not replace the manifest being written
        manifest_claimed = True
        extra = run_json_extra()
        if len(report_paths(launch)) > 1:
            md_path, html_path = report_paths(launch)
            extra["reports"] = {"md": md_path, "html": html_path}
        runjson.finalize(
            manifest,
            outcome=outcome,
            journal=journal,
            survivor_pids=survivors.pids,
            survivor_ns=survivors.namespaces,
            rounds=docket.round,
            report=report,
            namespace_sweep="none (in place)" if council.in_place else ("namespace" if swept else "not swept"),
            **extra,
        )

    def write_report(journal: list[dict]) -> tuple[str, str]:
        return _report(
            council,
            docket,
            console,
            run_dir,
            outcome,
            journal,
            manifest,
            aborted,
            brake.at,
            report_at=launch.output_path,
            sink_claim=transcripts.sink_claim(),
            transcript_bytes=transcripts.bytes_written,
            stderr_bytes=transcripts.stderr_bytes,
        )

    def abandon() -> None:
        """The last-resort exit's manifest, skipped once the full write has started."""
        if manifest_claimed:
            return
        write_run_json(lifecycle.SweepResult(), [], "", swept=False)

    brake = Brake(console=console, picture=picture, kill=kill_now, abandon=abandon, held=_held_by(council.verbosity))
    brake.arm()

    try:
        if sandbox.in_place or is_implement:
            baselines = {}
        else:
            baselines = {base: journal_mod.listing(sandbox.upper(base)) for base in _file_bases(council)}
        if is_implement:
            crews = Crews(
                launch.crews,
                sandbox=sandbox,
                run_dir=run_dir,
                runs_dir=runs_dir,
                console=console,
                manifest=manifest,
                transcripts=transcripts,
            )
            for label in crews.order:
                crews[label].prepare()
            planner = crews.get("planner")
            builder = crews["builder"]
            if planner is not None:
                # the planning half: the planners and the run's chair; the builder's seats join at the build
                await planner.start(seats=True)
                await builder.start(seats=False)
            else:
                # --plan: no planning crew is convened, so the builders are up from the start
                await builder.start(seats=True)
            outcome = await _implement_deliberate(crews, console, brake, docket, plan_file=launch.plan_file)
        else:
            members = Members(
                council=council,
                sandbox=sandbox,
                run_dir=run_dir,
                runs_dir=runs_dir,
                console=console,
                manifest=manifest,
                transcripts=transcripts,
            )
            await members.start_all()
            lanes = Lanes(council, members, console)
            outcome = await _deliberate(council, members, console, brake, lanes, docket)
    except (Abort, AskUnusable, ChairUnusable, MemberGone) as failure:
        aborted, outcome = str(failure), ABORTED
        console.say(aborted)
        console.event("run_aborted", reason=aborted)
    except Exception as failure:
        aborted = f"internal error: {type(failure).__name__}: {failure}"
        outcome = ABORTED
        console.say(aborted)
        console.event(
            "run_aborted", reason=aborted, kind=type(failure).__name__, frames=traceback.format_exc(limit=6).strip().splitlines()[-4:]
        )
    finally:
        # prompt counts while the lanes are still reachable; a disposed crew stashed its counts at dispose
        crew_prompts: list[tuple[str, dict]] = []
        if is_implement and crews is not None:
            for label in crews.order:
                crew = crews[label]
                counts = crew.lanes.prompts if crew.lanes is not None else crew.prompts
                if counts is not None:
                    crew_prompts.append((label, counts))
        if is_implement:
            survivors = await crews.dispose_all(fast=brake.hard) if crews is not None else lifecycle.SweepResult()
        else:
            survivors = await _teardown(members, console, in_place=council.in_place, brake=brake)
        journal = owed(console, "the write journal", _journal, council, sandbox, baselines, console, brake) or []
        report, markdown_text = owed(console, "the report", write_report, journal) or ("", "")
        if council.verbosity >= DETAIL:
            owed(console, "human_input.json", _write_human_input, run_dir, docket, console)
        for _label, counts in crew_prompts:
            owed(console, "prompt counts", manifest.record_prompts, counts)
        if lanes is not None:
            owed(console, "prompt counts", manifest.record_prompts, lanes.prompts)
        owed(console, "run.json", write_run_json, survivors, journal, report)
        owed(console, "the transcripts", transcripts.close)
        owed(console, "the disk tally", console.say, _disk_line(run_dir, report_paths(launch)))
        if brake.first:
            console.say(f"exiting on signal {brake.first}")
            if brake.skipped:
                console.say("skipped: " + ", ".join(brake.skipped))
        elif outcome in RESULTS:
            console.say(f"outcome: {outcome}")
        if report:
            owed(console, "the report's console copy", _print_report, markdown_text)
        brake.disarm()
    return 128 + brake.first if brake.first else (EXIT_OK if outcome in RESULTS and report else EXIT_ABORT)


def _docket(council: Council) -> Docket:
    return Docket(
        mode=council.mode,
        goal=council.goal,
        roster=council.seat_ids,
        chair_name=council.chair.name,
        seconding=council.seconding,
        human=council.human,
    )


def _file_bases(council: Council) -> list[str]:
    return [spec.file_base for spec in council.agents] + [council.chair.file_base]


def _roster(council: Council) -> str:
    shown = [f"{name} ({agent_id})" for agent_id, name in council.seat_names.items()]
    chair, key = council.chair.name, council.chair.file_base
    shown.append(chair if chair.casefold() == key else f"{chair} ({key})")
    return " · ".join(shown)


async def _deliberate(council: Council, members: Members, console: Console, brake: Brake, lanes: Lanes, docket: Docket) -> str:
    names = council.roster_names
    brief = Brief(mode=council.mode, project=council.project, in_place=council.in_place, human=council.human)
    chair = Chair(council, lanes, console, brief)
    reader = LineReader()
    reader.start()
    checkpoint = Checkpoint(docket, reader, console=console, names=names)
    checkpoint.ask_seat = _messenger(lanes)
    port = Port(
        council=council, lanes=lanes, chair=chair, brief=brief, console=console, checkpoint_run=checkpoint, docket=docket, names=names
    )
    engine = Engine(
        docket,
        port,
        goal=council.goal,
        project=council.project,
        brief=brief,
        max_rounds=council.max_rounds,
        grace_rounds=council.grace_rounds,
        names=names,
        emit=_emitter(console, names, council.human),
    )
    watchdog = Watchdog(council=council, lanes=lanes, chair=chair, console=console)
    watchdog.start()
    try:
        return await _race(engine.run(), brake)
    finally:
        await watchdog.stop()
        await lanes.abort_all()


async def _implement_deliberate(crews: Crews, console: Console, brake: Brake, docket: Docket, *, plan_file: str) -> str:
    """The implement run: the orchestrator over the crews, with the watchdog on the run's chair."""
    reader = LineReader()
    reader.start()
    builder = crews["builder"]
    council = builder.council
    names = crews.names()

    checkpoint = Checkpoint(docket, reader, console=console, names=names, ask_seat=crews.send_message)
    impl = Implement(
        crews=crews,
        docket=docket,
        checkpoint=checkpoint,
        console=console,
        plan_file=plan_file,
        emit=_emitter(console, names, council.human),
    )
    chair = builder.chair
    if chair is None:
        raise MemberGone("the run's chair never came up")
    watchdog = Watchdog(council=council, lanes=crews.lanes(), chair=chair, console=console)
    watchdog.start()
    try:
        return await _race(impl.run(), brake)
    finally:
        await watchdog.stop()
        await crews.lanes().abort_all()


async def _race(run, brake: Brake):
    """A signal interrupts the current ask; the run still leaves through `finally`."""
    task = asyncio.ensure_future(run)
    waiter = asyncio.ensure_future(brake.stop.wait())
    done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        return task.result()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as failure:
        raise Abort(f"{_interruption(brake)}, and the loop also raised {type(failure).__name__}: {failure}") from failure
    raise Abort(_interruption(brake))


def _interruption(brake: Brake) -> str:
    at = brake.at
    if at is None:
        return "the run was signalled"
    where = f" ({at.where})" if at.where else ""
    return f"interrupted by {at.name} {at.elapsed:.1f} s in{where}"


def _messenger(lanes: Lanes):
    """The human's direct-message lane: `explain H1` and `<name>: <text>` both land here. The reply is
    prose, so nothing is parsed out of it."""

    async def send(agent: str, message: str, *, round_: int, phase: str):
        return await lanes.message(agent, message, round_=round_, phase=phase)

    return send


def _emitter(console: Console, names: dict[str, str], human: str):

    def emit(name: str, **fields) -> None:
        fields = {key: value for key, value in fields.items() if not callable(value)}
        console.event(name, **fields)
        line = _summary_line(name, fields, names, human=human)
        if line:
            console.say(line)

    return emit


def _summary_line(name: str, fields: dict, names: dict[str, str], human: str = "blocking") -> str:
    from .render.text import direction_label, name_of

    if name == "phase0_done":
        return f"phase 0: {fields['valid']} seat(s) in, {fields.get('items', 0)} item(s) on the docket"
    if name == "chair_merge":
        return f"chair merged {fields['groups']} duplicate group(s)"
    if name == "chair_compare":
        return f"chair comparison: {fields['agreed']} agreed point(s), {fields['conflicts']} conflict(s), {fields['uncovered']} uncovered"
    if name == "direction_ask":
        held = ", ".join(direction_label(token, names) for token in fields["directions"])
        tail = {
            "blocking": "asking the human to choose",
            "non_blocking": "the human may choose while the run keeps working",
            "none": "the split is recorded for the report; no human is on the run to choose",
        }[human]
        return f"the seats will not converge ({held}) — {tail}"
    if name == "chair_direction_unreadable":
        return "the chair could not word the split — the run asks in its own words"
    if name == "chair_ask_merge_refused":
        return f"the chair's ask fold was refused: {fields['reason']}"
    if name == "chair_ask_merge_unreadable":
        return "the chair could not read the ask fold — every ask keeps its own wording"
    if name == "round_summary":
        states = ", ".join(f"{count} {state}" for state, count in sorted((fields.get("states") or {}).items()))
        return f"round {fields['round']}: {fields['signed_off']} signed off · {states or 'nothing open'}"
    if name == "human_checkpoint":
        return f"checkpoint: {fields['answered']} answered, {fields['skipped']} declined, {'waited' if fields['waited'] else 'kept going'}"
    if name == "human_answer_off_cycle":
        said = " · ".join(
            f"{', '.join(ids)} {verb}" for ids, verb in ((fields["answered"], "answered"), (fields["declined"], "declined")) if ids
        )
        if not said:
            return ""
        return f"{said} while the seats were working"
    if name == "termination":
        return f"outcome: {fields['outcome']} — {fields['reason']}"
    if name == "merge_refused":
        return f"merge plan refused ({fields['reason']}) — the docket is printed as raised"
    if name == "merge_canonical_unlisted":
        return f"the chair folded into {fields['canonical']}, which it was never shown — nothing in that group is merged"
    if name == "merge_duplicate_unlisted":
        return f"the chair folded {fields['duplicate']} into {fields['into']}, an id it was never shown — that entry keeps its claim"
    if name == "merge_canonical_replaced":
        return (
            f"the chair named {fields['chair_named']} the group's claim, but {fields['canonical']} was raised first — "
            "the earlier entry is the one whose claim the human reads"
        )
    if name == "answer_buys_a_round":
        return f"{fields['answered']} answer(s) settled: round {fields['round']} is a full one"
    if name == "grace_round_bought":
        return f"{fields['answered']} answer(s) at the round limit: one more round, {fields['grace_left']} to spare"
    if name == "answer_buys_no_round":
        return (
            f"{fields['answered']} answer(s) with no grace round left: the answer is on the record and in the report, "
            "and the round limit still closes the run"
        )
    if name == "peer_pass_start":
        before = "the council asks you" if human == "blocking" else "the run ends"
        return f"the seats get one last look at {', '.join(fields['asks'])} before {before}"
    if name == "question_seconded":
        said = {
            "blocking": "the human is asked",
            "non_blocking": "the human is shown it while the run keeps working",
            "none": "it is recorded for the report",
        }[human]
        return f"{name_of(fields['seat'], names)} backed {fields['id']} — {said} ({_seats(fields['askers'], names)})"
    if name == "question_seconded_but_held_back":
        backed = f"{name_of(fields['seat'], names)} backed {fields['id']}"
        if fields["held"] == SETTLED:
            return f"{backed}, but {_seats(fields['answered_by'], names)} answered it: only a seat that raised it can call that not enough"
        if fields["held"] == MOOT:
            if human == "blocking":
                return f"{backed}, but its item is settled: only a majority declaring the stop for it puts it to the human"
            return f"{backed}, but its item is settled: the closure is the council's answer about it"
        return f"{backed}, and the human is not asked ({fields['held']})"
    if name == "question_stop_requested":
        return f"{name_of(fields['seat'], names)} wants the run to wait on {fields['id']} ({_seats(fields['holders'], names)})"
    if name == "question_insisted":
        return f"{name_of(fields['seat'], names)} says the answer to {fields['id']} was not enough"
    if name == "question_merged":
        dropped = f"{fields['id']} wanted what {fields['into']} wants — now asked by {_seats(fields['askers'], names)}"
        if (named := fields.get("chair_named")) and named != fields["into"]:
            return f"{dropped}; {fields['into']} keeps its words, though the chair named {named}"
        return dropped
    if name == "question_retracted":
        still_asking = fields["still_asking"]
        dropped = f"{name_of(fields['seat'], names)} dropped {fields['id']}"
        return dropped + (f" — still asked by {_seats(still_asking, names)}" if still_asking else " — nobody asks it")
    if name == "question_mooted":
        return f"{fields['id']} asked about {fields['item']}, which the council settled ({fields['disposition']}) — the human is not asked"
    if name == "hold_released":
        who = name_of(fields["seat"], names)
        return f"{who} let go of {fields['item']} ({fields['stance']}): the council settles it, the human is not asked"
    if name == "question_answered_by_seat":
        said = "in the human's place" if human != "none" else "from the workspace: it is settled, never asked"
        return f"{name_of(fields['seat'], names)} answered {fields['id']} {said}"
    if name == "question_unseconded":
        return f"{fields['id']} has no second seat: it is reported, never asked"
    if name == "verdict_ask_refused":
        named = "item" if fields["field"] == "positions" else "ask"
        return f"{name_of(fields['member'], names)} named {named} {fields['id']} in `{fields['field']}`: {fields['reason']}"
    if name == "answer_option_rejected":
        return f"the checkpoint reported option {fields['option']} for {fields['id']}: {fields['reason']}"
    if name == "gap_declined":
        return f"{name_of(fields.get('member', ''), names)} declined {len(fields.get('items') or [])} item(s) after its ladder"
    return ""


def _seats(ids: list[str], names: dict[str, str]) -> str:
    from .render.text import name_of

    return ", ".join(name_of(one, names) for one in ids) or "nobody"


async def _teardown(members: Members | None, console: Console, *, in_place: bool = False, brake: Brake):
    if members is None:
        result = lifecycle.SweepResult()
        console.say(lifecycle.NO_SWEEP_LINE if in_place else lifecycle.survivor_line(result))
        return result
    try:
        if brake.hard:
            brake.skip("the members' stop grace")
            return await members.dispose_fast()
        return await members.dispose_all()
    except Exception as failure:  # pragma: no cover - defensive
        console.say(f"dispose failed: {failure}")
        return lifecycle.SweepResult()


def _journal(council: Council, sandbox, baselines: dict, console: Console, brake: Brake) -> list[dict]:
    if not baselines:
        return []
    from .render.text import name_of

    out = []
    for base in _file_bases(council):
        if brake.hard:
            brake.skip("the rest of the write journal walk")
            break
        who = name_of(base, council.display_names)
        try:
            changes = journal_mod.journal(sandbox.upper(base), sandbox.lower_fd)
            used = journal_mod.bytes_used(sandbox.upper(base))
        except OSError as failure:
            console.say(f"journal: {who} unreadable ({failure})")
            continue
        before = set(baselines.get(base) or {})
        rows = {key: sorted(set(values) - before) for key, values in changes.items()}
        entry = {"member": base, "file_base": base, "bytes": used, "sandbox_mib": council.sandbox_mib, **rows}
        touched = sum(len(rows.get(key, [])) for key in ("created", "modified", "deleted"))
        if touched:
            console.say(
                f"journal: {who} wrote {used} bytes · "
                + ", ".join(f"{len(rows[key])} {key}" for key in ("created", "modified", "deleted") if rows.get(key))
            )
        out.append(entry)
    return out


def _report(
    council: Council,
    docket: Docket | None,
    console: Console,
    run_dir: str,
    outcome: str,
    journal: list[dict],
    manifest,
    aborted: str,
    interrupted: Interrupted | None = None,
    *,
    report_at: str = "",
    sink_claim: str = "",
    transcript_bytes: int = 0,
    stderr_bytes: int = 0,
) -> tuple[str, str]:
    """Render the report once and write it in every format the council asked for.

    Returns the primary report's path and its markdown text (the console copy, even when the
    run keeps no markdown file).
    """
    from .render import html as html_render
    from .render import json_report as json_render
    from .render import markdown as markdown_render
    from .render import report_implement, report_plan, report_review

    files = council.adapter.report_files(council.report_format)
    html_file = next((name for name in files if name.endswith(".html")), "")
    if report_at:
        paths = [report_at] if len(files) == 1 else [os.path.join(report_at, name) for name in files]
    else:
        paths = [os.path.join(run_dir, name) for name in files]
    context = None
    doc = None
    page_title = ""
    try:
        context = _report_context(
            council,
            outcome,
            journal,
            manifest,
            docket,
            aborted,
            interrupted,
            report_name=files[0],
            report_path=paths[0] if report_at else "",
            report_html_name=html_file if html_file != files[0] else "",
            report_html_path=paths[files.index(html_file)] if report_at and html_file and html_file != files[0] else "",
            sink_claim=sink_claim,
            transcript_bytes=transcript_bytes,
            stderr_bytes=stderr_bytes,
        )
        if docket is None or docket.blank:
            doc = report_context.build_minimal(reason=aborted or outcome, context=context)
        else:
            from .sandbox.journal import mark_sandbox_items

            mark_sandbox_items(docket, journal, project=council.project)
            if council.mode == "implement":
                doc = report_implement.build(docket, context, run_dir=run_dir)
            elif council.mode == "plan":
                doc = report_plan.build(docket, context, run_dir=run_dir, converged=_converged(docket))
            else:
                doc = report_review.build(docket, context, run_dir=run_dir)
        page_title = f"{council.name} · {council.project}"
        markdown_text = markdown_render.render_md(doc)  # the console copy is always the markdown of the same document
    except Exception as failure:
        console.say(f"the report could not be rendered ({type(failure).__name__}: {failure}) — writing what can be said instead")
        console.event(
            "report_render_failed",
            kind=type(failure).__name__,
            reason=str(failure),
            frames=traceback.format_exc(limit=4).strip().splitlines()[-3:],
        )
        held = _held_by(council.verbosity)
        markdown_text = (
            f"# Council report — `{council.name}`\n\nThe run ended "
            f"({aborted or outcome or 'no outcome'}). The report renderer failed: "
            f"{type(failure).__name__}: {failure}. {held}\n"
        )
    texts: dict[str, str] = {}
    for name in files:
        if name.endswith(".json"):
            if context is None:
                texts[name] = json_render.fallback(outcome, "the run's own context could not be read")
            else:
                try:
                    texts[name] = json_render.render(docket, context, reason=aborted or outcome)
                except Exception as failure:  # the report still lands, as valid JSON
                    texts[name] = json_render.fallback(outcome, f"{type(failure).__name__}: {failure}")
        elif doc is None:
            if name == files[0]:  # the fallback lands in the primary file only
                texts[name] = markdown_text
        elif name.endswith(".html"):
            try:
                texts[name] = html_render.render_html(doc, page_title)
            except Exception as failure:  # the report still lands, as markdown
                console.say(f"the report could not be rendered ({type(failure).__name__}: {failure}) — writing what can be said instead")
                console.event(
                    "report_render_failed",
                    kind=type(failure).__name__,
                    reason=str(failure),
                    frames=traceback.format_exc(limit=4).strip().splitlines()[-3:],
                )
                texts[name] = markdown_text
        else:
            texts[name] = markdown_text
    primary = paths[0]
    for name, path in zip(files, paths):
        text = texts.get(name)
        if text is None:  # the render failed and the primary is not this file: nothing to write here
            continue
        tmp = f"{path}.tmp-{os.getpid()}-{time.time_ns()}"
        try:
            with open(tmp, "w", encoding="utf-8", errors="backslashreplace") as handle:
                handle.write(text)
            os.replace(tmp, path)
        except OSError as failure:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            console.say(f"could not write the report: {failure}")
            return "", ""
    if report_at:
        for path in paths:
            console.say(f"report {path}")
    return primary, markdown_text


def _converged(docket) -> str | None:
    satisfied, token, _reason = direction_rule(docket)
    return token if satisfied and token and token != NO_PREFERENCE else None


def _report_context(
    council: Council,
    outcome: str,
    journal: list[dict],
    manifest,
    docket: Docket | None = None,
    aborted: str = "",
    interrupted: Interrupted | None = None,
    *,
    report_name: str = "",
    report_path: str = "",
    report_html_name: str = "",
    report_html_path: str = "",
    sink_claim: str = "",
    transcript_bytes: int = 0,
    stderr_bytes: int = 0,
) -> report_context.ReportContext:
    roster = []
    for agent_id, spec in zip(council.seat_ids, council.agents):
        record = manifest.members.get(spec.file_base)
        roster.append(
            report_context.RosterRow(
                id=agent_id,
                name=spec.name,
                model=spec.model or "pi default",
                phase0_count=(docket.phase0_counts.get(agent_id, 0) if docket else 0),
                warning=record.model_warning if record else "",
                backing=(docket.backing.get(agent_id, "") if docket else ""),
            )
        )
    member_count = len(_file_bases(council))
    if docket is not None and docket.mode == "implement" and manifest.data.get("crews"):
        # every crew's seats and chairs, not just the run's own roster
        member_count = sum(len(crew["seats"]) + 1 for crew in manifest.data["crews"])
    return report_context.ReportContext(
        project=council.project,
        config_path=council.config_path,
        council=council.name,
        council_lineage=f"extends {' → '.join(council.extends)}" if council.extends else "",
        outcome=outcome,
        mode=council.mode,
        rounds=docket.round if docket else 0,
        date=time.strftime("%Y-%m-%d %H:%M:%S"),
        sandbox_mib=council.sandbox_mib,
        member_count=member_count,
        pi_cli=council.pi_cli,
        pi_version=council.pi_version,
        verbosity=council.verbosity,
        sink_claim=sink_claim,
        transcript_bytes=transcript_bytes,
        stderr_bytes=stderr_bytes,
        untrusted=council.untrusted,
        in_place=council.in_place,
        human=council.human,
        roster=roster,
        chair_name=council.chair.name,
        chair_model=council.chair.model or "pi default",
        journal=journal,
        report_name=report_name or council.adapter.report_name,
        report_path=report_path,
        report_html_name=report_html_name,
        report_html_path=report_html_path,
        goal=council.goal,
        aborted_reason=aborted,
        captions=(_implement_captions(manifest) if docket is not None and docket.mode == "implement" else council.roster_names),
        interrupted=interrupted,
        docket_execution=docket.execution if (docket is not None and docket.mode == "implement") else None,
    )


def _implement_captions(manifest) -> dict[str, str]:
    """Every crew's roster keys to the names a human reads: global seat ids, each crew's chair by file_base."""
    captions: dict[str, str] = {}
    for crew in manifest.data.get("crews") or []:
        for agent_id, name in crew["seats"]:
            captions[agent_id] = name
        captions[crew["chair"]["file_base"]] = crew["chair"]["name"]
    return captions


def _write_human_input(run_dir: str, docket: Docket | None, console: Console) -> None:
    if docket is None or docket.blank:
        return
    record = {
        "questions": [
            {
                "id": q.id,
                "askers": list(q.askers),
                "blocked_by": list(q.blocked_by),
                "item": q.item_id,
                "raised_round": q.raised_round,
                "question": q.text,
                "context": q.context,
                "options": list(q.options),
                "effective": effective_class(docket, q),
                "showable": showable(docket, q),
                "peer_answers": dict(q.peer_answers),
                "insisted": q.insisted,
                "retracted_by": q.retracted_by,
                "retracted_round": q.retracted_round,
                "moot_with": q.moot_with,
                "state": q.state,
                "answer": q.answer,
                "answer_round": q.answer_round,
                "answer_option": q.answer_option,
                "answer_option_text": q.answer_option_text,
                "recommendation": q.recommendation,
                "markers": list(q.markers),  # unasked / unanswered / settled / unseconded / moot / merged / skipped / no_human / not_waited
                "merged_into": q.merged_into,
                "direction_ask": q.direction_ask,
                "retired_escalations": [list(k) for k, e in docket.escalations.items() if e.question_id == q.id and e.state == RETIRED],
            }
            for q in (docket.questions[i] for i in docket.question_order)
        ],
        "notes": [
            {
                "id": note.id,
                "seat": note.by,
                "item": note.item_id,
                "raised_round": note.round,
                "note": note.text,
                "state": note.state,
                "shown_round": note.shown_round,
                "retracted_round": note.retracted_round,
                "retract_reason": note.retract_reason,
                "retraction_shown_round": note.retraction_shown_round,
            }
            for note in (docket.human_notes[nid] for nid in docket.note_order)
        ],
        "messages": [
            {
                "id": m.id,
                "to": m.to,
                "to_name": m.to_name,
                "round": m.round,
                "message": m.text,
                "reply": m.reply,
                "failure": m.failure,
            }
            for m in docket.human_messages
        ],
        "escalations": [
            {
                "item": item_id,
                "asker": asker,
                "state": e.state,
                "question": e.question_id,
                "raised_round": e.raised_round,
                "challenges": e.challenges,
                "defense": e.defense,
                "defended_round": e.defended_round,
            }
            for (item_id, asker), e in docket.escalations.items()
        ],
    }
    try:
        with open(os.path.join(run_dir, "human_input.json"), "w", encoding="utf-8", errors="backslashreplace") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except OSError as failure:
        console.say(f"could not write human_input.json: {failure}")


def _print_report(text: str) -> None:
    if text:
        sys.stdout.write("\n" + text)
