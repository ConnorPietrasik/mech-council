"""The one question about the host the sandbox family's `/proc` case has to ask before it can judge anything.

Clearing dumpability re-owns `/proc/<pid>/{environ,fd,cwd,exe,mem}` to uid 0, and the check that hides them is a
comparison against uid 0 — so it holds only while the run's uid is not the uid a member's mapped root resolves to.
"""

from __future__ import annotations

import os

CAP_SYS_PTRACE = 1 << 19


def has_ptrace_power() -> bool:
    """Whether this run resolves to the owner of those files: uid 0, or `CAP_SYS_PTRACE` in its effective set."""
    if os.getuid() == 0:
        return True
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) & CAP_SYS_PTRACE)
    except OSError, ValueError:
        pass
    return False
