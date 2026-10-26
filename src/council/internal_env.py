from __future__ import annotations

LAUNCH_ENV = ("COUNCIL_LAUNCH_PROJECT", "COUNCIL_LAUNCH_RUNS_ROOT", "COUNCIL_LAUNCH_SLOT_DIR")
PIPE_ENV = "COUNCIL_LAUNCH_PIPE"
INTERNAL_ENV = (*LAUNCH_ENV, PIPE_ENV)

#: The launcher's ns report: its last act before `execvp`, or its failure report.
NS_REPORT_OK = "ns"
NS_REPORT_FAIL = "err"
