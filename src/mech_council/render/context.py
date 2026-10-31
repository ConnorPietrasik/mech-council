from __future__ import annotations

from dataclasses import dataclass, field

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
    transcripts: str = "off"
    transcript_note: str = "none written"
    untrusted: bool = False
    in_place: bool = False
    roster: list[RosterRow] = field(default_factory=list)
    chair_name: str = "Chair"
    chair_model: str = ""
    journal: list[dict] = field(default_factory=list)  # per member, from run.json
    report_name: str = "report.md"
    goal: str | None = None
    aborted_reason: str = ""
    captions: dict[str, str] = field(default_factory=dict)
    interrupted: Interrupted | None = None

    def header(self, title: str) -> list[str]:
        trust = "no-approve (untrusted)" if self.untrusted else "approve"
        lineage = f" ({self.council_lineage})" if self.council_lineage else ""
        where = (
            f"- workspace: in place — {self.member_count} members share `{self.project}`; nothing they wrote is journaled or discarded"
            if self.in_place
            else (
                f"- sandbox: `sandbox_mib = {self.sandbox_mib}` shared by "
                f"{self.member_count} members ({self.member_count - 1} seats + the chair)"
            )
        )
        return [
            f"# {title}",
            "",
            f"- project: `{self.project}`",
            f"- config: `{self.config_path}` · council `{self.council}`{lineage}",
            f"- mode: {self.mode} · outcome: **{self.outcome}** · rounds: {self.rounds}",
            f"- date: {self.date}",
            where,
            f"- project trust: {trust} · transcripts: {self.transcripts} ({self.transcript_note})",
            f"- pi: `{self.pi_cli}` ({self.pi_version})",
        ] + ([f"- aborted: {self.aborted_reason}"] if self.aborted_reason else [])

    def interrupted_block(self) -> list[str]:
        """The cut short leads the report, so nothing below it reads as settled."""
        cut = self.interrupted
        if cut is None:
            return []
        out = [f"## Cut short by {cut.name}", ""]
        out.append(
            f"Nothing below is settled: {cut.name} arrived {cut.elapsed:.1f} s in"
            + (f", with the run at {cut.where}." if cut.where else ", before the first round.")
        )
        out.append("")
        if cut.waiting_on:
            out.append("- it was asking you: " + ", ".join(cut.waiting_on))
        for row in cut.in_flight:
            who = caption(str(row.get("agent", "?")), self.captions)
            tool = str(row.get("tool_name") or "")
            doing = f"`{tool}` {row.get('last_tool')}".strip() if tool else f"last event: {row.get('last_event') or 'none yet'}"
            out.append(f"- in flight: {who} — {doing} ({row.get('elapsed', 0)} s, idle {row.get('idle', 0)} s)")
        return out + [""]

    def roster_table(self, *, extra: str = "") -> list[str]:
        noun = "plans" if self.mode == "plan" else "findings"
        title = f"| ID | name | model | phase-0 {noun}" + (f" | {extra} |" if extra else " |")
        head = [title, "|---|---|---|" + "---|" * (1 + (1 if extra else 0))]
        for row in self.roster:
            tail = f" {row.warning}" if row.warning else ""
            line = f"| {row.id} | {row.name} | `{row.model}` | {row.phase0_count}{tail} |"
            if extra:
                line = line[:-1] + f" | {getattr(row, extra, '') or '—'} |"
            head.append(line)
        last = f"| — | {self.chair_name} (chair) | `{self.chair_model}` | — |"
        head.append(last + (" — |" if extra else ""))
        return head

    def artifacts(self, run_dir: str) -> list[str]:
        manifest = (
            "outcome, in place: no write journal, no namespaces, no sweep"
            if self.in_place
            else "outcome, write journal, namespaces, survivors"
        )
        lines = [
            "## Artifacts",
            "",
            f"- `run.json` — the machine-readable manifest ({manifest})",
            f"- `{self.report_name}` — this report",
            "- `events.jsonl` — the run's events, one JSON object per line, append-only",
            "- `human_input.json` — every question, answer, recommendation and skip, and every note with where it was shown",
        ]
        if self.transcripts == "full":
            lines.append("- `agents/<file_base>.transcripts.jsonl` — the verbatim protocol stream, per member")
            lines.append("- `agents/<file_base>.stderr.txt` — per-member stderr, untrimmed")
        else:
            lines.append(
                "- `agents/<file_base>.stderr.txt` — per-member stderr, untrimmed (transcripts are `off`, so nothing else is written there)"
            )
        lines += ["", f"Run directory: `{run_dir}`"]
        return lines


def minimal_report(*, reason: str, context: ReportContext) -> str:
    lines = context.header("Council run: no result") + [""] + context.interrupted_block()
    lines += ["# Why nothing ran", "", reason, ""]
    if context.roster or context.chair_model:
        lines += ["The council it convened:", ""] + context.roster_table() + [""]
    return "\n".join(lines) + "\n"
