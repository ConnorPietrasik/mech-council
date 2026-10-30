from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
import traceback

from . import runjson
from .asks import AskUnusable, Lanes
from .chairwork import Chair, ChairUnusable
from .config import Council
from .console import Console, Transcripts
from .deliberation.engine import Abort, Engine
from .deliberation.escalations import MOOT, SETTLED, effective_class, showable
from .deliberation.state import ABORTED, CONSENSUS, NEEDS_OUTCOME, NO_PREFERENCE, RETIRED, ROUND_LIMIT, STALEMATE, Docket
from .deliberation.termination import direction_rule
from .humaninput import Checkpoint
from .members import MemberGone, Members
from .port import Port
from .prompts.brief import Brief
from .render import context as report_context
from .sandbox import journal as journal_mod
from .watchdog import Watchdog

EXIT_OK, EXIT_ABORT = 0, 1
RESULTS = (CONSENSUS, NEEDS_OUTCOME, STALEMATE, ROUND_LIMIT)


async def orchestrate(launch, sandbox) -> int:
    council = launch.council
    run_dir, runs_dir = launch.run_dir, launch.runs_dir
    console = Console(run_dir=run_dir)
    lineage = f" extends {' → '.join(council.extends)}" if council.extends else ""
    console.say(f"council `{council.name}`{lineage} · mode {council.mode} · {len(council.agents)} seat(s) + a chair")
    console.say(f"roster {_roster(council)}")
    console.say(f"config {council.config_path}")
    console.say(f"run directory {run_dir}")
    if council.in_place:
        console.say(f"in place · members share {council.project} · scratch {sandbox.sandbox_dir}/<seat> · no write journal")
    else:
        console.say(f"sandbox {sandbox.sandbox_dir} · tmpfs {council.sandbox_mib} MiB shared by {len(council.agents) + 1} members")
    console.say(f"transcripts {council.transcripts} · project trust {'untrusted (no approval)' if council.untrusted else 'approve'}")
    console.event(
        "run_started",
        project=council.project,
        mode=council.mode,
        council=council.name,
        sandbox_dir=sandbox.sandbox_dir,
        in_place=council.in_place,
    )
    manifest = runjson.create(
        os.path.join(run_dir, "run.json"),
        council,
        run_dir=run_dir,
        runs_dir=runs_dir,
        project=council.project,
        orchestrator_pid=os.getpid(),
    )
    transcripts = Transcripts(run_dir, council.transcripts)
    outcome, aborted = ABORTED, ""
    rounds, report_path = 0, ""
    signalled: list[int] = []
    stop = asyncio.Event()

    def on_signal(sig: int) -> None:
        if not signalled:
            signalled.append(sig)
            console.say(f"signal {sig}: disposing every child, finalizing run.json")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
            asyncio.get_running_loop().add_signal_handler(sig, on_signal, sig)

    members: Members | None = None
    lanes: Lanes | None = None
    docket: Docket | None = None
    baselines: dict[str, dict[str, str]] = {}
    try:
        baselines = {} if sandbox.in_place else {base: journal_mod.listing(sandbox.upper(base)) for base in _file_bases(council)}
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
        outcome, rounds, docket = await _deliberate(council, members, console, stop, lanes)
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
        # Each step is independent: one failing must not cancel the others.
        survivors = await _teardown(members, console, in_place=council.in_place)
        journal = _owed(console, "the write journal", _journal, council, sandbox, baselines, console) or []
        report_path = (
            _owed(console, "the report", _report, council, docket, console, run_dir, outcome, rounds, journal, manifest, aborted) or ""
        )
        _owed(console, "human_input.json", _write_human_input, run_dir, docket, console)
        if lanes is not None:
            _owed(console, "prompt counts", manifest.record_prompts, lanes.prompts)
        _owed(
            console,
            "run.json",
            runjson.finalize,
            manifest,
            outcome=outcome,
            journal=journal,
            survivor_pids=survivors.pids,
            survivor_ns=survivors.namespaces,
            rounds=rounds,
            report=report_path,
            namespace_sweep="none (in place)" if council.in_place else "namespace",
            **({"aborted_reason": aborted} if aborted else {}),
        )
        _owed(console, "the transcripts", transcripts.close)
        _owed(console, "the transcript tally", console.say, f"transcripts: {transcripts.tally()}")
        if signalled:
            console.say(f"exiting on signal {signalled[0]}")
        elif outcome in RESULTS:
            console.say(f"outcome: {outcome}")
        if report_path:
            _print_report(report_path)
    return 128 + signalled[0] if signalled else (EXIT_OK if outcome in RESULTS else EXIT_ABORT)


def _owed(console: Console, what: str, call, *args, **kw):
    """One owed exit-path step: its failure is a console line and an event, and the caller falls back on None."""
    try:
        return call(*args, **kw)
    except Exception as failure:
        frames = traceback.format_exc(limit=4).strip().splitlines()[-3:]
        console.say(f"{what} failed on the way out ({type(failure).__name__}: {failure})")
        console.event("exit_step_failed", step=what, kind=type(failure).__name__, reason=str(failure), frames=frames)
        return None


def _file_bases(council: Council) -> list[str]:
    return [spec.file_base for spec in council.agents] + [council.chair.file_base]


def _roster(council: Council) -> str:
    shown = [f"{name} ({agent_id})" for agent_id, name in council.seat_names.items()]
    chair, key = council.chair.name, council.chair.file_base
    shown.append(chair if chair.casefold() == key else f"{chair} ({key})")
    return " · ".join(shown)


async def _deliberate(council: Council, members: Members, console: Console, stop: asyncio.Event, lanes: Lanes):
    from .humanio import LineReader

    roster = council.seat_ids
    names = council.roster_names
    docket = Docket(mode=council.mode, goal=council.goal, roster=roster, chair_name=council.chair.name, seconding=council.seconding)
    brief = Brief(mode=council.mode, project=council.project, in_place=council.in_place)
    chair = Chair(council, lanes, console, brief)
    reader = LineReader()
    reader.start()
    checkpoint = Checkpoint(docket, reader, console=console, names=names)
    checkpoint.ask_seat = _explainer(lanes, council, docket)
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
        emit=_emitter(console, names),
    )
    watchdog = Watchdog(council=council, lanes=lanes, chair=chair, console=console)
    watchdog.start()
    try:
        outcome = await _race(engine.run(), stop)
    finally:
        await watchdog.stop()
        await lanes.abort_all()
    return outcome, docket.round, docket


async def _race(run, stop: asyncio.Event):
    """A signal interrupts the current ask; the run still leaves through `finally`."""
    task = asyncio.ensure_future(run)
    waiter = asyncio.ensure_future(stop.wait())
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
        raise Abort(f"the run was signalled, and the loop also raised {type(failure).__name__}: {failure}") from failure
    raise Abort("the run was signalled")


def _explainer(lanes: Lanes, council: Council, docket: Docket):
    async def explain(agent: str, message: str, *, round_: int, phase: str):
        return await lanes.ask_agent(
            agent,
            message,
            round_=round_,
            phase=phase,
            kind="verdict",
            parse_kw={
                "mode": council.mode,
                "me": agent,
                "known_items": set(docket.items),
                "closed_items": {i for i in docket.items if docket.items[i].closed},
            },
        )

    return explain


def _emitter(console: Console, names: dict[str, str]):

    def emit(name: str, **fields) -> None:
        fields = {key: value for key, value in fields.items() if not callable(value)}
        console.event(name, **fields)
        line = _summary_line(name, fields, names)
        if line:
            console.say(line)

    return emit


def _summary_line(name: str, fields: dict, names: dict[str, str]) -> str:
    from .render.text import direction_label, name_of

    if name == "phase0_done":
        return f"phase 0: {fields['valid']} seat(s) in, {fields.get('items', 0)} item(s) on the docket"
    if name == "chair_merge":
        return f"chair merged {fields['groups']} duplicate group(s)"
    if name == "chair_compare":
        return f"chair comparison: {fields['agreed']} agreed point(s), {fields['conflicts']} conflict(s), {fields['uncovered']} uncovered"
    if name == "direction_ask":
        held = ", ".join(direction_label(token, names) for token in fields["directions"])
        return f"the seats will not converge ({held}) — asking the human to choose"
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
        return f"the seats get one last look at {', '.join(fields['asks'])} before the council asks you"
    if name == "question_seconded":
        return f"{name_of(fields['seat'], names)} backed {fields['id']} — the human is asked ({_seats(fields['askers'], names)})"
    if name == "question_seconded_but_held_back":
        backed = f"{name_of(fields['seat'], names)} backed {fields['id']}"
        if fields["held"] == SETTLED:
            return f"{backed}, but {_seats(fields['answered_by'], names)} answered it: only a seat that raised it can call that not enough"
        if fields["held"] == MOOT:
            return f"{backed}, but its item is settled: only a majority declaring the stop for it puts it to the human"
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
        return f"{name_of(fields['seat'], names)} answered {fields['id']} in the human's place"
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


async def _teardown(members: Members | None, console: Console, *, in_place: bool = False):
    from .process import lifecycle

    if members is None:
        result = lifecycle.SweepResult()
        console.say(lifecycle.NO_SWEEP_LINE if in_place else lifecycle.survivor_line(result))
        return result
    try:
        return await members.dispose_all()
    except Exception as failure:  # pragma: no cover - defensive
        console.say(f"dispose failed: {failure}")
        return lifecycle.SweepResult()


def _journal(council: Council, sandbox, baselines: dict, console: Console) -> list[dict]:
    if not baselines:
        return []
    from .render.text import name_of

    out = []
    for base in _file_bases(council):
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
    council: Council, docket, console: Console, run_dir: str, outcome: str, rounds: int, journal: list[dict], manifest, aborted: str
) -> str:
    from .render import report_plan, report_review

    try:
        if docket is not None:
            from .sandbox.journal import mark_sandbox_items

            mark_sandbox_items(docket, journal, project=council.project)
        context = _report_context(council, console, outcome, rounds, journal, manifest, docket, aborted)
        if docket is None:
            text = report_context.minimal_report(reason=aborted or outcome, context=context)
        elif council.mode == "plan":
            text = report_plan.render(docket, context, run_dir=run_dir, converged=_converged(docket))
        else:
            text = report_review.render(docket, context, run_dir=run_dir)
    except Exception as failure:
        console.say(f"the report could not be rendered ({type(failure).__name__}: {failure}) — writing what can be said instead")
        console.event(
            "report_render_failed",
            kind=type(failure).__name__,
            reason=str(failure),
            frames=traceback.format_exc(limit=4).strip().splitlines()[-3:],
        )
        text = (
            f"# Council report — `{council.name}`\n\nThe run ended "
            f"({aborted or outcome or 'no outcome'}). The report renderer failed: "
            f"{type(failure).__name__}: {failure}. `events.jsonl` holds the run.\n"
        )
    path = os.path.join(run_dir, council.adapter.report_name)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as failure:
        console.say(f"could not write the report: {failure}")
        return ""
    return path


def _converged(docket) -> str | None:
    satisfied, token, _reason = direction_rule(docket)
    return token if satisfied and token and token != NO_PREFERENCE else None


def _report_context(
    council: Council, console: Console, outcome: str, rounds: int, journal: list[dict], manifest, docket=None, aborted: str = ""
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
    return report_context.ReportContext(
        project=council.project,
        config_path=council.config_path,
        council=council.name,
        council_lineage=f"extends {' → '.join(council.extends)}" if council.extends else "",
        outcome=outcome,
        mode=council.mode,
        rounds=rounds,
        date=time.strftime("%Y-%m-%d %H:%M:%S"),
        sandbox_mib=council.sandbox_mib,
        member_count=len(_file_bases(council)),
        pi_cli=council.pi_cli,
        pi_version=council.pi_version,
        transcripts=council.transcripts,
        transcript_note=console.transcript_note(),
        untrusted=council.untrusted,
        in_place=council.in_place,
        roster=roster,
        chair_name=council.chair.name,
        chair_model=council.chair.model or "pi default",
        journal=journal,
        report_name=council.adapter.report_name,
        goal=council.goal,
        aborted_reason=aborted,
        captions=council.roster_names,
    )


def _write_human_input(run_dir: str, docket, console: Console) -> None:
    if docket is None:
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
                "markers": list(q.markers),  # unasked / unanswered / settled / unseconded / moot / merged / skipped: the report's words
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
        with open(os.path.join(run_dir, "human_input.json"), "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except OSError as failure:
        console.say(f"could not write human_input.json: {failure}")


def _print_report(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as handle:
            sys.stdout.write("\n" + handle.read())
    except OSError:
        return
