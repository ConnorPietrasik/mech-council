from __future__ import annotations

from dataclasses import dataclass, field

from ..cli import DEBUG, DETAIL, FULL, QUIET, SUMMARY, VERBOSITY_LEVELS
from .doc import Block, Heading, List, ListItem, Paragraph, Report, T, Table, Text
from .markdown import render_md
from .text import caption


@dataclass
class RosterRow:
    id: str
    name: str
    model: str  # what the spawn actually requested, or pi's default
    phase0_count: int = 0
    warning: str = ""
    backing: str = ""  # plan: the token from the round that ended the run


@dataclass
class Interrupted:
    """Where the run stood when the signal landed."""

    name: str = ""
    elapsed: float = 0.0
    round: int = 0
    phase: str = ""
    #: the asks the run was parked on when the signal landed
    waiting_on: list[str] = field(default_factory=list)
    #: `Lanes.digest()` rows: what each seat was doing
    in_flight: list[dict] = field(default_factory=list)

    @property
    def where(self) -> str:
        return " · ".join(part for part in (f"round {self.round}" if self.round else "", self.phase) if part)


@dataclass
class ReportContext:
    project: str = ""
    config_path: str = ""
    council: str = ""
    council_lineage: str = ""  # `extends review`, when the council inherits its roster
    outcome: str = ""
    mode: str = "review"
    rounds: int = 0
    date: str = ""
    sandbox_mib: int = 0
    member_count: int = 0
    pi_cli: str = ""
    pi_version: str = ""
    sink_claim: str = ""  # what the sinks hold, in the bytes-counting object's words
    transcript_bytes: int = 0  # verbatim protocol bytes written under agents/
    stderr_bytes: int = 0  # per-member stderr bytes written under agents/
    untrusted: bool = False
    in_place: bool = False
    human: str = "blocking"  # the run's posture toward the council's questions
    roster: list[RosterRow] = field(default_factory=list)
    chair_name: str = "Chair"
    chair_model: str = ""
    journal: list[dict] = field(default_factory=list)  # per member, from run.json
    report_name: str = "report.md"
    #: The report's path when `--output` puts it outside the run directory.
    report_path: str = ""
    report_html_name: str = ""
    report_html_path: str = ""
    goal: str | None = None
    aborted_reason: str = ""
    verbosity: int = 0  # what the run directory kept beyond the report
    captions: dict[str, str] = field(default_factory=dict)
    interrupted: Interrupted | None = None
    #: the implement run's spine, when the mode is implement
    docket_execution: object = None

    def meta(self) -> list[tuple[str, Text]]:
        trust = "no-approve (untrusted)" if self.untrusted else "approve"
        lineage = T(" (", self.council_lineage, ")") if self.council_lineage else T()
        out: list[tuple[str, Text]] = [
            ("project", T((self.project, "code"))),
            ("config", T((self.config_path, "code"), " · council ", (self.council, "code"), lineage)),
            ("mode", T(self.mode, " · outcome: ", (self.outcome, "bold"), f" · rounds: {self.rounds}")),
            ("date", T(self.date)),
        ]
        if self.in_place:
            out.append(
                (
                    "workspace",
                    T(
                        "in place — ",
                        str(self.member_count),
                        " members share ",
                        (self.project, "code"),
                        "; nothing they wrote is journaled or discarded",
                    ),
                )
            )
        else:
            out.append(
                (
                    "sandbox",
                    T(
                        (f"sandbox_mib = {self.sandbox_mib}", "code"),
                        f" shared by {self.member_count} members ({self.member_count - 1} seats + the chair)",
                    ),
                )
            )
        sinks = T(" (", self.sink_claim, ")") if self.sink_claim else T()
        out.append(("project trust", T(trust, " · verbosity: ", VERBOSITY_LEVELS[self.verbosity], sinks)))
        out.append(("pi", T((self.pi_cli, "code"), f" ({self.pi_version})")))
        if self.human != "blocking":
            posture = "the run never waited on the council's questions" if self.human == "non_blocking" else "the council was never asked"
            out.append(("questions", T(self.human, " — ", posture)))
        if self.aborted_reason:
            out.append(("aborted", T(self.aborted_reason)))
        return out

    def interrupted_blocks(self) -> list[Block]:
        cut = self.interrupted
        if cut is None:
            return []
        where = f", with the run at {cut.where}." if cut.where else ", before the first round."
        blocks: list[Block] = [
            Heading(2, T(f"Cut short by {cut.name}")),
            Paragraph(T(f"Nothing below is settled: {cut.name} arrived {cut.elapsed:.1f} s in{where}")),
        ]
        items: list[ListItem] = []
        if cut.waiting_on:
            items.append(ListItem(T("it was asking you: ", ", ".join(cut.waiting_on))))
        for row in cut.in_flight:
            who = caption(str(row.get("agent", "?")), self.captions)
            tool = str(row.get("tool_name") or "")
            last = str(row.get("last_tool") or "")
            if tool:
                doing = T((tool, "code"), f" {last}") if last else T((tool, "code"))
            else:
                doing = T(f"last event: {row.get('last_event') or 'none yet'}")
            items.append(ListItem(T(f"in flight: {who} — ", doing, f" ({row.get('elapsed', 0)} s, idle {row.get('idle', 0)} s)")))
        if items:
            blocks.append(List(tuple(items)))
        return blocks

    def roster_table(self, *, extra: str = "") -> Table:
        noun = "plans" if self.mode == "plan" else "findings"
        header = (T("ID"), T("name"), T("model"), T(f"phase-0 {noun}")) + ((T(extra),) if extra else ())
        rows: list[list[Text]] = []
        for row in self.roster:
            cells = [T(row.id), T(row.name), T((row.model, "code")), T(str(row.phase0_count), f" {row.warning}" if row.warning else "")]
            if extra:
                cells.append(T(getattr(row, extra, "") or "—"))
            rows.append(cells)
        chair = [T("—"), T(f"{self.chair_name} (chair)"), T((self.chair_model, "code")), T("—")]
        if extra:
            chair.append(T("—"))
        rows.append(chair)
        return Table(tuple(header), tuple(tuple(row) for row in rows))

    def artifacts(self, run_dir: str) -> list[Block]:
        manifest = (
            "outcome, in place: no write journal, no namespaces, no sweep"
            if self.in_place
            else "outcome, write journal, namespaces, survivors"
        )
        items: list[ListItem] = []
        if self.verbosity >= SUMMARY:
            items.append(ListItem(T(("run.json", "code"), f" — the machine-readable manifest ({manifest})")))
        report_line = " — this report" + (" (outside the run directory)" if self.report_path else "")
        items.append(ListItem(T((self.report_path or self.report_name, "code"), report_line)))
        if self.report_html_name:
            items.append(
                ListItem(
                    T(
                        (self.report_html_path or self.report_html_name, "code"),
                        " — this report in HTML",
                        " (outside the run directory)" if self.report_html_path else "",
                    )
                )
            )
        if self.verbosity >= DETAIL:
            items.append(
                ListItem(
                    T(
                        ("human_input.json", "code"),
                        " — every question, answer, recommendation and skip, and every note with where it was shown",
                    )
                )
            )
        if self.verbosity >= DEBUG:
            items.append(ListItem(T(("events.jsonl", "code"), " — the run's events, one JSON object per line, append-only")))
        if self.verbosity >= FULL and self.transcript_bytes:
            items.append(ListItem(T(("agents/<file_base>.transcripts.jsonl", "code"), " — the verbatim protocol stream, per member")))
        if self.stderr_bytes:
            tail = T() if self.verbosity >= FULL else T(" (the verbatim transcripts are written at ", ("full", "code"), " only)")
            items.append(ListItem(T(("agents/<file_base>.stderr.txt", "code"), " — per-member stderr, untrimmed", tail)))
        if self.verbosity == QUIET:
            items.append(
                ListItem(
                    T(
                        "nothing else: verbosity ",
                        ("quiet", "code"),
                        " writes no ",
                        ("run.json", "code"),
                        ", ",
                        ("human_input.json", "code"),
                        " or ",
                        ("events.jsonl", "code"),
                    )
                )
            )
        return [Heading(2, T("Artifacts")), List(tuple(items)), Paragraph(T("Run directory: ", (run_dir, "code")))]


def build_minimal(*, reason: str, context: ReportContext) -> Report:
    blocks: list[Block] = list(context.interrupted_blocks())
    blocks += [Heading(1, T("Why nothing ran")), Paragraph(T(reason))]
    if context.roster or context.chair_model:
        blocks += [Paragraph(T("The council it convened:")), context.roster_table()]
    return Report(title=T("Council run: no result"), outcome=context.outcome, meta=context.meta(), blocks=tuple(blocks))


def minimal_report(*, reason: str, context: ReportContext) -> str:
    return render_md(build_minimal(reason=reason, context=context))
