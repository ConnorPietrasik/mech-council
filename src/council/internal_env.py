"""The internal variables and the spawn-pipe report codes shared by spawn and launcher.

D12b's three paths plus the report channel.  These are **not** knobs, which is why they
are not in §3.5's table and why the `LAUNCH_` infix keeps `COUNCIL_LAUNCH_RUNS_ROOT` a
word away from the user-facing `COUNCIL_RUNS_DIR`: §3.3's naming rule exists so one grep
finds a knob's every appearance, and these must not look like knobs.  Each holds an
absolute path the launcher mounts, never a relative one (§3.5: nothing relative reaches
§10).  They live in a leaf module because the spawner and the launcher both read them and
neither may import the other.
"""
from __future__ import annotations

LAUNCH_ENV = ("COUNCIL_LAUNCH_PROJECT", "COUNCIL_LAUNCH_RUNS_ROOT",
              "COUNCIL_LAUNCH_SLOT_DIR")
PIPE_ENV = "COUNCIL_LAUNCH_PIPE"
INTERNAL_ENV = (*LAUNCH_ENV, PIPE_ENV)

#: §10 step 8: the launcher's last act before `execvp`, or its failure report.
NS_REPORT_OK = "ns"
NS_REPORT_FAIL = "err"
