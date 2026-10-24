"""The injected facts a report prints that are not deliberation state.

`date`, paths and the sandbox/journal numbers arrive here from `run.json`, so a renderer
is a pure function of (state, context), and so its output is byte-for-byte reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RosterRow:
    id: str
    name: str
    model: str  # what the spawn actually requested, or pi's default
    phase0_count: int = 0
    warning: str = ""
    backing: str = ""  # plan: the token from the round that ended the run


@dataclass
class ReportContext:
    project: str = ""
    config_path: str = ""
    council: str = ""
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
    roster: list[RosterRow] = field(default_factory=list)
    chair_name: str = "Chair"
    chair_model: str = ""
    journal: list[dict] = field(default_factory=list)  # per member, from run.json
    report_name: str = "report.md"
    goal: str | None = None
    aborted_reason: str = ""
    captions: dict[str, str] = field(default_factory=dict)

    def header(self, title: str) -> list[str]:
        trust = "no-approve (untrusted)" if self.untrusted else "approve"
        return [
            f"# {title}",
            "",
            f"- project: `{self.project}`",
            f"- config: `{self.config_path}` · council `{self.council}`",
            f"- mode: {self.mode} · outcome: **{self.outcome}** · rounds: {self.rounds}",
            f"- date: {self.date}",
            f"- sandbox: `sandbox_mib = {self.sandbox_mib}` shared by "
            f"{self.member_count} members ({self.member_count - 1} seats + the chair)",
            f"- project trust: {trust} · transcripts: {self.transcripts} ({self.transcript_note})",
            f"- pi: `{self.pi_cli}` ({self.pi_version})",
        ] + ([f"- aborted: {self.aborted_reason}"] if self.aborted_reason else [])

    def roster_table(self, *, extra: str = "") -> list[str]:
        """The `abstain`/`declined` distinction belongs to the docket, not here; this table
        carries the model column, whose label is the config's string or pi's default."""
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
        """No artifact that does not exist.  `agents/` is lazy, and with
        transcripts off there are no transcripts to name — only the stderr sink."""
        lines = [
            "## Artifacts",
            "",
            "- `run.json` — the machine-readable manifest (outcome, write journal, namespaces, survivors)",
            f"- `{self.report_name}` — this report",
            "- `events.jsonl` — the run's events, one JSON object per line, append-only",
            "- `human_input.json` — every question, answer, recommendation and skip",
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
    """The minimal report an aborted run writes, naming what went wrong."""
    lines = context.header("Council run: no result") + ["", "# Why nothing ran", "", reason, ""]
    if context.roster or context.chair_model:
        lines += ["The council it convened:", ""] + context.roster_table() + [""]
    return "\n".join(lines) + "\n"
