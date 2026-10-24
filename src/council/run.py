"""The orchestrator's body (§12.1.1): the loop owner, inside the run's own namespaces.

Everything after §10 step 7 happens here, in the namespace the supervisor mapped: the run
directory's artifacts, the members, the loop, the dispose, the sweep, the journal, the
report.  The order below is the order §3.2's exit contract needs, because **every** path
out of this function owes the same five things: dispose, the sweep, a finalized
`run.json`, a written report, and a flushed stdout.
"""
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
from .deliberation.state import Docket, NO_PREFERENCE
from .deliberation.escalations import effective_class
from .deliberation.termination import direction_rule
from .humaninput import Checkpoint
from .members import Members, MemberGone
from .port import Port
from .prompts.brief import Brief
from .render import context as report_context
from .sandbox import journal as journal_mod
from .watchdog import Watchdog

EXIT_OK, EXIT_ABORT = 0, 1
RESULTS = ("consensus", "needs_human", "stalemate", "round_limit_reached")


async def orchestrate(launch, sandbox) -> int:
    """Run one council to its outcome and return the process exit code (§3.2)."""
    council = launch.council
    run_dir, runs_dir = launch.run_dir, launch.runs_dir
    console = Console(run_dir=run_dir)
    console.say(f"council `{council.name}` · mode {council.mode} · "
                f"{len(council.agents)} seat(s) + a chair")
    console.say(f"config {council.config_path}")
    console.say(f"run directory {run_dir}")
    # §11.1: the sandbox, the transcript level and the trust decision are each a line, so
    # an operator reading the console knows what a member could touch before it runs.
    console.say(f"sandbox {sandbox.sandbox_dir} · tmpfs {council.sandbox_mib} MiB shared by "
                f"{len(council.agents) + 1} members")
    console.say(f"transcripts {council.transcripts} · project trust "
                f"{'untrusted (no approval)' if council.untrusted else 'approve'}")
    console.event("run_started", project=council.project, mode=council.mode,
                  council=council.name, sandbox_dir=sandbox.sandbox_dir)
    manifest = runjson.create(os.path.join(run_dir, "run.json"), council,
                              run_dir=run_dir, runs_dir=runs_dir,
                              project=council.project, orchestrator_pid=os.getpid())
    transcripts = Transcripts(run_dir, council.transcripts)
    members: Members | None = None
    docket = None
    outcome, aborted = "aborted", ""
    rounds, report_path = 0, ""
    signalled: list[int] = []
    stop = asyncio.Event()
    baselines: dict[str, dict] = {}

    def on_signal(sig: int) -> None:
        # §3.2: the run disposes everything it created and leaves on 128+signum, not by dying.
        if not signalled:
            signalled.append(sig)
            console.say(f"signal {sig}: disposing every child, finalizing run.json")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
            asyncio.get_running_loop().add_signal_handler(sig, on_signal, sig)

    # §12.1.11: every name the exit path touches exists before the `try`, or a failure in
    # setup would trade the run's honest finalization for a NameError.
    members: Members | None = None
    lanes: Lanes | None = None
    docket: Docket | None = None
    baselines: dict[str, dict[str, str]] = {}
    try:
        baselines = {base: journal_mod.listing(sandbox.upper(base))
                     for base in _file_bases(council)}
        members = Members(council=council, sandbox=sandbox, run_dir=run_dir,
                          runs_dir=runs_dir, console=console, manifest=manifest,
                          transcripts=transcripts)
        await members.start_all()
        # Built here, not inside the loop: §13.6's prompt count is read on the way out,
        # and a lane created in a frame that already returned would be unreadable there.
        lanes = Lanes(council, members, console)
        outcome, rounds, docket = await _deliberate(council, members, console, stop, lanes)
    except (Abort, AskUnusable, ChairUnusable, MemberGone) as failure:
        aborted, outcome = str(failure), "aborted"
        console.say(aborted)
        console.event("run_aborted", reason=aborted)
    except _SetupFailure as failure:
        aborted, outcome = str(failure), "aborted"
        console.say(aborted)
    except Exception as failure:                       # §3.2: a bug ends the run too
        aborted = f"internal error: {type(failure).__name__}: {failure}"
        outcome = "aborted"
        console.say(aborted)
        # §11.1: "nothing may swallow an internal error".  The console line is for the
        # terminal; the frames belong in events.jsonl, where they survive the scrollback.
        console.event("run_aborted", reason=aborted, kind=type(failure).__name__,
                      frames=traceback.format_exc(limit=6).strip().splitlines()[-4:])
    finally:
        # §3.2/§12.1.11: each owed step is independent, because one failing must not cancel
        # the others — a renderer that raises may not also cost the operator the finalized
        # `run.json` or the sweep result.  Each reports itself when it fails.
        # `_teardown` is awaited directly (and already reports its own failure): the sweep
        # result has to exist before the manifest can be finalized.
        survivors = await _teardown(members, console)
        journal = _owed(console, "the write journal", _journal, council, sandbox,
                        baselines, console) or []
        report_path = _owed(console, "the report", _report, council, docket, console,
                            run_dir, outcome, rounds, journal, manifest, aborted) or ""
        _owed(console, "human_input.json", _write_human_input, run_dir, docket, console)
        if lanes is not None:
            # §13.6: "exactly one prompt each" is asserted from run.json, not from a log.
            _owed(console, "prompt counts", manifest.record_prompts, lanes.prompts)
        _owed(console, "run.json", runjson.finalize, manifest, outcome=outcome,
              journal=journal, survivor_pids=survivors.pids,
              survivor_ns=survivors.namespaces, rounds=rounds, report=report_path,
              **({"aborted_reason": aborted} if aborted else {}))
        _owed(console, "the transcripts", transcripts.close)
        # §11.2: "every run ends by printing what it wrote" — including when it wrote none.
        _owed(console, "the transcript tally", console.say,
              f"transcripts: {transcripts.tally()}")
        if signalled:
            console.say(f"exiting on signal {signalled[0]}")
        elif outcome in RESULTS:
            console.say(f"outcome: {outcome}")
        if report_path:
            _print_report(report_path)
    return 128 + signalled[0] if signalled else (
        EXIT_OK if outcome in RESULTS else EXIT_ABORT)


def _owed(console: Console, what: str, call, *args, **kw):
    """Run one owed exit-path step; a failure is a console line and an event, not a gap.

    Returns the call's value, or None when it failed, so a caller falls back to whatever
    it can still do without it.
    """
    try:
        return call(*args, **kw)
    except Exception as failure:                       # §11.1: nothing may swallow this
        frames = traceback.format_exc(limit=4).strip().splitlines()[-3:]
        console.say(f"{what} failed on the way out ({type(failure).__name__}: {failure})")
        console.event("exit_step_failed", step=what, kind=type(failure).__name__,
                      reason=str(failure), frames=frames)
        return None


class _SetupFailure(Exception):
    """A member could not be spawned at all; §3.3's point 1/2 owns the wording."""


def _file_bases(council: Council) -> list[str]:
    return [spec.file_base for spec in council.agents] + [council.chair.file_base]


async def _deliberate(council: Council, members: Members, console: Console,
                      stop: asyncio.Event, lanes: Lanes):
    """Wire the loop to the lanes and run it — §12.1.1's one job."""
    from .humanio import LineReader
    roster = [spec.id for spec in council.agents]
    names = {spec.id: spec.name for spec in council.agents}
    names["chair"] = council.chair.name
    docket = Docket(mode=council.mode, goal=council.goal, roster=roster,
                    chair_name=council.chair.name)
    brief = Brief(mode=council.mode, project=council.project, names=names)
    chair = Chair(council, lanes, console, brief)
    reader = LineReader()
    reader.start()
    checkpoint = Checkpoint(docket, reader, console=console, names=names)
    checkpoint.ask_seat = _explainer(lanes, council, docket)
    port = Port(council=council, lanes=lanes, chair=chair, brief=brief, console=console,
                checkpoint_run=checkpoint, docket=docket, names=names)
    engine = Engine(docket, port, goal=council.goal, project=council.project,
                    names=names, brief=brief, max_rounds=council.max_rounds,
                    emit=_emitter(console, names))
    watchdog = Watchdog(council=council, lanes=lanes, chair=chair, console=console)
    watchdog.start()
    try:
        outcome = await _race(engine.run(), stop)
    finally:
        await watchdog.stop()
        await lanes.abort_all()
    return outcome, docket.round, docket


async def _race(run, stop: asyncio.Event):
    """§3.2: a signal interrupts the current ask; the run still leaves through `finally`."""
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
        # §11.1: the signal is why the task stopped; it is not an excuse for losing a bug
        # the task raised on the way out.
        raise Abort(f"the run was signalled, and the loop also raised "
                    f"{type(failure).__name__}: {failure}") from failure
    raise Abort("the run was signalled")


def _explainer(lanes: Lanes, council: Council, docket: Docket):
    async def explain(agent: str, message: str, *, round_: int, phase: str):
        return await lanes.ask_agent(
            agent, message, round_=round_, phase=phase, kind="verdict",
            parse_kw={"mode": council.mode, "me": agent,
                      "known_items": set(docket.items),
                      "closed_items": {i for i in docket.items
                                       if docket.items[i].closed}})
    return explain


def _emitter(console: Console, names: dict[str, str]):
    """Every state change is one console line **and** one `events.jsonl` record (§11.1)."""

    def emit(name: str, **fields) -> None:
        fields = {key: value for key, value in fields.items() if not callable(value)}
        console.event(name, **fields)
        line = _summary_line(name, fields, names)
        if line:
            console.say(line)
    return emit


def _summary_line(name: str, fields: dict, names: dict[str, str]) -> str:
    from .render.text import caption
    if name == "phase0_done":
        return (f"phase 0: {fields['valid']} seat(s) in, "
                f"{fields.get('items', 0)} item(s) on the docket")
    if name == "chair_merge":
        return f"chair merged {fields['groups']} duplicate group(s)"
    if name == "chair_compare":
        return (f"chair comparison: {fields['agreed']} agreed point(s), "
                f"{fields['conflicts']} conflict(s), {fields['uncovered']} uncovered")
    if name == "round_summary":
        states = ", ".join(f"{count} {state}" for state, count in
                           sorted((fields.get("states") or {}).items()))
        return (f"round {fields['round']}: {fields['signed_off']} signed off · "
                f"{states or 'nothing open'}")
    if name == "human_checkpoint":
        return (f"checkpoint: {fields['answered']} answered, {fields['skipped']} declined, "
                f"{'waited' if fields['waited'] else 'kept going'}")
    if name == "termination":
        return f"outcome: {fields['outcome']} — {fields['reason']}"
    if name == "merge_refused":
        return f"merge plan refused ({fields['reason']}) — the docket is printed as raised"
    if name == "answer_buys_a_round":
        return (f"{fields['answered']} answer(s) settled: round {fields['round']} is a "
                "full one")
    if name == "gap_declined":
        return (f"{caption(_who(fields.get('member', '')), names)} declined "
                f"{len(fields.get('items') or [])} item(s) after its ladder")
    return ""


def _who(label: str) -> str:
    return label


async def _teardown(members: Members | None, console: Console):
    from .process import lifecycle
    if members is None:
        result = lifecycle.SweepResult()
        console.say(lifecycle.survivor_line(result))
        return result
    try:
        return await members.dispose_all()
    except Exception as failure:                       # pragma: no cover - defensive
        console.say(f"dispose failed: {failure}")
        return lifecycle.SweepResult()


def _journal(council: Council, sandbox, baselines: dict, console: Console) -> list[dict]:
    """§10's write journal, read where every upper layer is visible: the orchestrator."""
    if not baselines:
        return []
    out = []
    for base in _file_bases(council):
        try:
            changes = journal_mod.journal(sandbox.upper(base), sandbox.lower_fd)
            used = journal_mod.bytes_used(sandbox.upper(base))
        except OSError as failure:
            console.say(f"journal: {base} unreadable ({failure})")
            continue
        before = set(baselines.get(base) or {})
        entry = {"member": base, "file_base": base, "bytes": used,
                 "sandbox_mib": council.sandbox_mib}
        for key, values in changes.items():
            entry[key] = sorted(set(values) - before)
        touched = sum(len(entry.get(key, [])) for key in ("created", "modified", "deleted"))
        if touched:
            console.say(f"journal: {base} wrote {used} bytes · " + ", ".join(
                f"{len(entry[key])} {key}" for key in ("created", "modified", "deleted")
                if entry.get(key)))
        out.append(entry)
    return out


def _report(council: Council, docket, console: Console, run_dir: str, outcome: str,
            rounds: int, journal: list[dict], manifest, aborted: str) -> str:
    """§12.1.10: renderers are pure over (state, context); this builds the context.

    A renderer that raises on some docket shape must not cost the operator the report too:
    the fallback says what happened, because §3.4 promises a report on every exit and a
    run whose report is missing explains nothing about itself.
    """
    from .render import report_plan, report_review
    try:
        if docket is not None:
            # §6.3's two sandbox markers are earned from the write journal, here and nowhere
            # else: the renderers stay pure (§12.1.10) and the docket carries the fact.
            from .sandbox.journal import mark_sandbox_items
            mark_sandbox_items(docket, journal, project=council.project)
        context = _report_context(council, console, outcome, rounds, journal, manifest,
                                  docket, aborted)
        if docket is None:
            text = report_context.minimal_report(reason=aborted or outcome, context=context)
        elif council.mode == "plan":
            text = report_plan.render(docket, context, run_dir=run_dir,
                                      converged=_converged(docket))
        else:
            text = report_review.render(docket, context, run_dir=run_dir)
    except Exception as failure:
        console.say(f"the report could not be rendered ({type(failure).__name__}: "
                    f"{failure}) — writing what can be said instead")
        console.event("report_render_failed", kind=type(failure).__name__,
                      reason=str(failure),
                      frames=traceback.format_exc(limit=4).strip().splitlines()[-3:])
        text = (f"# Council report — `{council.name}`\n\nThe run ended "
                f"({aborted or outcome or 'no outcome'}). The report renderer failed: "
                f"{type(failure).__name__}: {failure}. `events.jsonl` holds the run.\n")
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


def _report_context(council: Council, console: Console, outcome: str, rounds: int,
                    journal: list[dict], manifest, docket=None, aborted: str = ""
                    ) -> report_context.ReportContext:
    roster = []
    for spec in council.agents:
        record = manifest.members.get(spec.file_base)
        roster.append(report_context.RosterRow(
            id=spec.id, name=spec.name, model=spec.model or "pi default",
            phase0_count=(docket.phase0_counts.get(spec.id, 0) if docket else 0),
            warning=record.model_warning if record else "",
            backing=(docket.backing.get(spec.id, "") if docket else "")))
    free = ""
    return report_context.ReportContext(
        project=council.project, config_path=council.config_path, council=council.name,
        outcome=outcome, mode=council.mode, rounds=rounds,
        date=time.strftime("%Y-%m-%d %H:%M:%S"),
        sandbox_mib=council.sandbox_mib, member_count=len(_file_bases(council)),
        pi_cli=council.pi_cli, pi_version=council.pi_version,
        transcripts=council.transcripts, transcript_note=console.transcript_note(),
        untrusted=council.untrusted, roster=roster, chair_name=council.chair.name,
        chair_model=council.chair.model or "pi default", journal=journal,
        report_name=council.adapter.report_name, goal=council.goal,
        aborted_reason=aborted)


def _write_human_input(run_dir: str, docket, console: Console) -> None:
    """§3.4's `human_input.json`: every request, answer, skip and the retirements caused."""
    if docket is None:
        return
    record = {
        "questions": [{
            "id": q.id, "askers": list(q.askers), "item": q.item_id,
            "raised_round": q.raised_round, "question": q.text, "context": q.context,
            "options": list(q.options),
            # §5.2.8's effective class is derived, not stored: the same question is
            # blocking for one docket shape and deferred for another.
            "effective": effective_class(docket, q), "state": q.state,
            "answer": q.answer, "answer_round": q.answer_round,
            "recommendation": q.recommendation,
            "markers": list(q.markers),       # unasked / skipped / deferred, §6.3's words
            "chair_raised": q.chair_raised,
            "retired_escalations": [list(k) for k, e in docket.escalations.items()
                                    if e.question_id == q.id and e.state == "retired"],
        } for q in (docket.questions[i] for i in docket.question_order)],
        "escalations": [{
            "item": item_id, "asker": asker, "state": e.state, "question": e.question_id,
            "raised_round": e.raised_round, "challenges": e.challenges,
            "defense": e.defense, "defended_round": e.defended_round,
        } for (item_id, asker), e in docket.escalations.items()],
    }
    try:
        with open(os.path.join(run_dir, "human_input.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except OSError as failure:
        console.say(f"could not write human_input.json: {failure}")


def _print_report(path: str) -> None:
    """§3.1: the report is printed to stdout at exit — the one exit exempt from the
    `[council]` prefix rule, so a terminal pager sees a document, not a log."""
    try:
        with open(path, encoding="utf-8") as handle:
            sys.stdout.write("\n" + handle.read())
    except OSError:
        return
