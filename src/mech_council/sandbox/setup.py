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
        """The member's `$TMPDIR`: the scratch bind, seen from inside the view."""
        return f"{self.project}/{SCRATCH_NAME}"

    def prepare_member(self, file_base: str) -> None:
        root = self.slot_dir(file_base)
        os.makedirs(f"{root}/scratch", exist_ok=True)
        if m.is_mounted(self.view(file_base)):
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
    """No sandbox: the project is the real one and members share it."""

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
    if scratch_cwd:  # a stray relative write lands here, not in the user's tree
        os.chdir(scratch_cwd)
    if in_place:
        box = InPlace(project=project, runs_dir=runs_dir, sandbox_dir=root, sandbox_mib=sandbox_mib)
        os.makedirs(root, exist_ok=True)
        for file_base in members:
            box.prepare_member(file_base)
        box.members = list(members)
        return box
    sandbox = Sandbox(project=project, runs_dir=runs_dir, sandbox_dir=root, sandbox_mib=sandbox_mib)
    m.private_recursive("/")
    m.set_not_dumpable()
    os.makedirs(sandbox.sandbox_dir, exist_ok=True)
    m.tmpfs(sandbox.sandbox_dir, size_mib=sandbox_mib)
    for file_base in members:
        sandbox.prepare_member(file_base)
    sandbox.lower_fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY)
    sandbox.members = list(members)
    return sandbox
