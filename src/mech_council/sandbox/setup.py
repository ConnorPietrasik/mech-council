from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import mount as m
from .memberns import SCRATCH_NAME


@dataclass
class Sandbox:
    project: str
    runs_dir: str
    sandbox_dir: str = ""
    members: list[str] = field(default_factory=list)
    lower_fd: int = -1
    sandbox_mib: int = 0
    in_place: bool = False

    def slot_dir(self, file_base: str) -> str:
        return f"{self.sandbox_dir}/{file_base}"

    def view(self, file_base: str) -> str:
        return f"{self.slot_dir(file_base)}/view"

    def upper(self, file_base: str) -> str:
        return f"{self.slot_dir(file_base)}/up"

    def scratch(self, file_base: str) -> str:
        return f"{self.slot_dir(file_base)}/scratch"

    def tmpdir(self, file_base: str) -> str:
        """What the member's `$TMPDIR` is set to: the scratch bind, seen from inside the view."""
        return f"{self.project}/{SCRATCH_NAME}"

    def prepare_member(self, file_base: str) -> None:
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


@dataclass
class InPlace:
    """No sandbox: the project is the real one and members share it.  What the run still provides is one
    scratch directory per seat, and the paths that say where it is."""

    project: str
    runs_dir: str
    sandbox_dir: str = ""
    members: list[str] = field(default_factory=list)
    sandbox_mib: int = 0
    in_place: bool = True
    lower_fd: int = -1

    def slot_dir(self, file_base: str) -> str:
        return f"{self.sandbox_dir}/{file_base}"

    def scratch(self, file_base: str) -> str:
        return f"{self.slot_dir(file_base)}/scratch"

    def tmpdir(self, file_base: str) -> str:
        return self.scratch(file_base)

    def prepare_member(self, file_base: str) -> None:
        os.makedirs(self.scratch(file_base), exist_ok=True)

    def close(self) -> None:
        return None


#: Either way members are spawned into one.
Seat = Sandbox | InPlace


def establish(
    *,
    project: str,
    runs_dir: str,
    sandbox_mib: int,
    members: list[str],
    scratch_cwd: str | None = None,
    sandbox_dir: str | None = None,
    in_place: bool = False,
) -> Seat:
    root = sandbox_dir or f"{runs_dir}/sandbox"
    if scratch_cwd:  # never the user's tree, in either mode: a stray relative write lands here
        os.chdir(scratch_cwd)
    if in_place:
        box = InPlace(project=project, runs_dir=runs_dir, sandbox_dir=root, sandbox_mib=sandbox_mib)
        os.makedirs(root, exist_ok=True)
        for file_base in members:  # one scratch directory per seat, and nothing else
            box.prepare_member(file_base)
        box.members = list(members)
        return box
    sandbox = Sandbox(project=project, runs_dir=runs_dir, sandbox_dir=root, sandbox_mib=sandbox_mib)
    m.private_recursive("/")  # nothing propagates back out
    m.set_not_dumpable()  # /proc closes over the run
    os.makedirs(sandbox.sandbox_dir, exist_ok=True)
    m.tmpfs(sandbox.sandbox_dir, size_mib=sandbox_mib)  # the run's cap
    for file_base in members:  # one overlay per member
        sandbox.prepare_member(file_base)
    sandbox.lower_fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY)  # the journal's eye
    sandbox.members = list(members)
    return sandbox


def _is_mounted(path: str) -> bool:
    """The mountinfo line is `id parent id major:minor root opts - fstype source superopts`: the mount point is field 5, on the
    *left* of the `-`, and the field after the `-` is the *fstype*.
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
    """mountinfo escapes space, tab, newline and backslash as `\\040` &c."""
    for character, code in (("\\", "\\134"), (" ", "\\040"), ("\t", "\\011"), ("\n", "\\012")):
        path = path.replace(character, code)
    return path
