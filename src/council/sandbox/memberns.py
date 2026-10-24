"""§10 step 6b — the member's own namespace, performed by the launcher in itself.

Five steps, and the order is the rule.  Runs inside `launcher.py`, in the process that is
about to `execvp` pi: there is no second process and no fork between the mounts and the
exec (§4).
"""
from __future__ import annotations

import os

from . import mount as m

#: §10 6b(2)'s one fixed name, skipped by the journal pass too (§10).
SCRATCH_NAME = ".council-scratch"


class SetupFailure(Exception):
    """A step failed: the launcher reports the syscall + errno and never execs (§10 8)."""

    def __init__(self, syscall: str, err: int) -> None:
        super().__init__(f"{syscall}: {os.strerror(err)}")
        self.syscall = syscall
        self.err = err


def setup_member_namespace(*, project: str, runs_dir: str, slot_dir: str,
                           mask_tmp: bool | None = None) -> int:
    """Mount the member's world and report the namespace id (`/proc/self/ns/mnt` inode).

    `mask_tmp=None` decides 6b(4)'s one condition: mask host `/tmp` only when it holds
    neither `<project>` nor `<runs_dir>`.
    """
    view = f"{slot_dir}/view"
    scratch = f"{slot_dir}/scratch"
    step = "6b(1) unshare(CLONE_NEWNS)"
    try:
        m.unshare(m.CLONE_NEWNS)                                  # 6b(1)
        step = f"6b(2) mkdir {view}/{SCRATCH_NAME}"
        os.makedirs(f"{view}/{SCRATCH_NAME}", exist_ok=True)      # 6b(2) — on the VIEW path
        step = f"6b(2) mount --bind {scratch}"
        m.rbind(scratch, f"{view}/{SCRATCH_NAME}")
        step = f"6b(3) mount --bind {view} {project}"
        m.rbind(view, project)                                    # 6b(3)
        step = f"6b(4) mask {runs_dir}"
        for path, read_only in ((f"{runs_dir}/sandbox", True), (runs_dir, True)):
            m.tmpfs(path, size_mib=m.MASK_MIB, read_only=read_only)   # 6b(4)
        if mask_tmp is None:
            mask_tmp = not (_under(project, "/tmp") or _under(runs_dir, "/tmp"))
        step = "6b(4) mask /tmp"
        if mask_tmp:
            m.tmpfs("/tmp", size_mib=m.MASK_MIB)
        step = f"6b(5) chdir {project}"
        os.chdir(project)                                         # 6b(5)
    except m.MountError as failure:
        raise SetupFailure(failure.syscall, failure.err) from failure
    except OSError as failure:
        raise SetupFailure(f"{step}: {failure.__class__.__name__}",
                           failure.errno or 0) from failure
    return m.namespace_id()


def _under(path: str, parent: str) -> bool:
    resolved = os.path.realpath(path)
    head = os.path.realpath(parent)
    return resolved == head or resolved.startswith(head.rstrip("/") + "/")
