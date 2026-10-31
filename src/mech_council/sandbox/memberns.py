from __future__ import annotations

import os

from . import mount as m

#: the scratch mount point inside the view
SCRATCH_NAME = ".council-scratch"


class SetupFailure(Exception):
    """A mount failed: reported as syscall + errno."""

    def __init__(self, syscall: str, err: int) -> None:
        super().__init__(f"{syscall}: {os.strerror(err)}")
        self.syscall = syscall
        self.err = err


def setup_member_namespace(*, project: str, runs_dir: str, slot_dir: str, mask_tmp: bool | None = None) -> int:
    """`mask_tmp=None` means: mask host `/tmp` only when it holds neither `<project>` nor `<runs_dir>`."""
    view = f"{slot_dir}/view"
    scratch = f"{slot_dir}/scratch"
    step = "unshare(CLONE_NEWNS)"
    try:
        m.unshare(m.CLONE_NEWNS)
        step = f"mkdir {view}/{SCRATCH_NAME}"
        os.makedirs(f"{view}/{SCRATCH_NAME}", exist_ok=True)
        step = f"mount --bind {scratch}"
        m.rbind(scratch, f"{view}/{SCRATCH_NAME}")
        step = f"mount --bind {view} {project}"
        m.rbind(view, project)  # the view is mounted at the project path
        step = f"mask {runs_dir}"
        for path, read_only in ((f"{runs_dir}/sandbox", True), (runs_dir, True)):
            m.tmpfs(path, size_mib=m.MASK_MIB, read_only=read_only)
        if mask_tmp is None:
            mask_tmp = not (_under(project, "/tmp") or _under(runs_dir, "/tmp"))
        step = "mask /tmp"
        if mask_tmp:
            m.tmpfs("/tmp", size_mib=m.MASK_MIB)
        step = f"chdir {project}"
        os.chdir(project)  # so cwd is not masked
    except m.MountError as failure:
        raise SetupFailure(_labelled(step, failure.syscall), failure.err) from failure
    except OSError as failure:
        raise SetupFailure(f"{step}: {failure.__class__.__name__}", failure.errno or 0) from failure
    return m.namespace_id()


def _labelled(step: str, syscall: str) -> str:
    """Which step failed, for the launcher's report: the syscall text alone would be a bare `mount(2)` repr."""
    return syscall if step.partition("(")[0] == syscall.partition("(")[0] else f"{step}: {syscall}"


def _under(path: str, parent: str) -> bool:
    resolved = os.path.realpath(path)
    head = os.path.realpath(parent)
    return resolved == head or resolved.startswith(head.rstrip("/") + "/")
