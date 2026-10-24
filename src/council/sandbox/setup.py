"""Everything the orchestrator mounts before a run dir exists: the private remount,
dumpability, the tmpfs cap, the per-member overlays and the lower-layer fd.

The member's own namespace is the launcher's (`memberns.py`); this module is the orchestrator's half, and it runs
to completion *before* the run directory is created, so a failure here leaves nothing behind but a `MountError`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import mount as m


@dataclass
class Sandbox:
    """The mounted shape the sandbox owns: one tmpfs, one overlay pair per member, one lower fd."""

    project: str
    runs_dir: str
    sandbox_dir: str = ""
    members: list[str] = field(default_factory=list)
    lower_fd: int = -1
    sandbox_mib: int = 0

    def slot_dir(self, file_base: str) -> str:
        return f"{self.sandbox_dir}/{file_base}"

    def view(self, file_base: str) -> str:
        return f"{self.slot_dir(file_base)}/view"

    def upper(self, file_base: str) -> str:
        return f"{self.slot_dir(file_base)}/up"

    def scratch(self, file_base: str) -> str:
        return f"{self.slot_dir(file_base)}/scratch"

    def prepare_member(self, file_base: str) -> None:
        """One `up`/`work` pair per member, mounted **once** and reused.

        A restart re-uses this mount; a second overlay on the same pair would
        be `EBUSY`, which is why this is idempotent rather than retried.
        """
        root = self.slot_dir(file_base)
        os.makedirs(f"{root}/scratch", exist_ok=True)
        if _is_mounted(self.view(file_base)):
            return
        m.overlay(self.project, root, self.view(file_base))

    def close(self) -> None:
        if self.lower_fd >= 0:
            try:
                os.close(self.lower_fd)
            except OSError:
                pass
            self.lower_fd = -1


def establish(
    *, project: str, runs_dir: str, sandbox_mib: int, members: list[str], scratch_cwd: str | None = None, sandbox_dir: str | None = None
) -> Sandbox:
    """The mounts, in order.  Any one raising is fatal: one line naming the syscall and its errno, no fallback.

    One tmpfs per **run**, at `<runs_root>/sandbox/<run>`: two councils on one host are
    then independent, and the directory the journal reads is the directory this run made.
    """
    sandbox = Sandbox(project=project, runs_dir=runs_dir, sandbox_dir=sandbox_dir or f"{runs_dir}/sandbox", sandbox_mib=sandbox_mib)
    m.private_recursive("/")  # nothing propagates back out
    m.set_not_dumpable()  # /proc closes over the run
    if scratch_cwd:  # never the user's tree
        os.chdir(scratch_cwd)
    os.makedirs(sandbox.sandbox_dir, exist_ok=True)
    m.tmpfs(sandbox.sandbox_dir, size_mib=sandbox_mib)  # the run's cap
    for file_base in members:  # one overlay per member
        sandbox.prepare_member(file_base)
    sandbox.lower_fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY)  # the journal's eye
    sandbox.members = list(members)
    return sandbox


def _is_mounted(path: str) -> bool:
    """Whether `path` is a mount point, read from mountinfo's **left** half.

    The line is `id parent id major:minor root opts - fstype source superopts`: the mount point is field 5
    of the left side, and the field after the `-` separator is the *fstype*. Reading the wrong half made
    every slot look unmounted, and the per-member overlay's idempotence — the one that keeps a restarted
    member on its own upper — became a second `mount(2)` on the same `up`/`work` pair, which is `EBUSY`.
    """
    wanted = _mountinfo_escape(path)
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            for line in handle:
                left, sep, _ = line.strip().partition(" - ")
                fields = left.split(" ")
                if sep and len(fields) > 4 and fields[4] == wanted:
                    return True
    except OSError:
        pass
    return False


def _mountinfo_escape(path: str) -> str:
    r"""mountinfo spells space, tab, newline and `\` as `\040` &c."""
    for character, code in (("\\", "\\134"), (" ", "\\040"), ("\t", "\\011"), ("\n", "\\012")):
        path = path.replace(character, code)
    return path


def statvfs_free_mib(path: str) -> int:
    """The per-round cap check (`sandbox_full` with nobody named)."""
    info = os.statvfs(path)
    return info.f_bsize * info.f_bavail // (1024 * 1024)
