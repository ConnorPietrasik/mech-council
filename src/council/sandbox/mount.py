"""§12.1.9 — the design's only `ctypes` module: `mount(2)`, `umount2(2)`, `unshare(2)`, `prctl(2)`.

Python exposes no mount call, so it arrives here and nowhere else (§10 step 1).  Every
failure raises `MountError` naming the syscall and the `errno`, which is exactly the one
line §10 and §13.9 demand — never a traceback, never a fallback (§14.4).
"""
from __future__ import annotations

import ctypes
import errno as _errno
import os

CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000

MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REC = 1 << 14
MS_PRIVATE = 1 << 18
MS_SHARED = 1 << 19
MS_BIND = 1 << 12

MNT_DETACH = 2

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3

#: §10 step 6b(4)'s mask tmpfs size: a constant, never a knob (plan D15).
MASK_MIB = 1
TERM_GRACE_S = 10            # plan D15
DISPOSE_SWEEP_AFTER_S = 15   # plan D15
WATCHDOG_PASS_TIMEOUT_S = 120


class MountError(OSError):
    """A failed syscall, named, with its errno — the whole of §10's failure contract."""

    def __init__(self, syscall: str, err: int) -> None:
        super().__init__(err, f"{syscall}: {os.strerror(err)}")
        self.syscall = syscall
        self.err = err

    def __str__(self) -> str:            # "mount('/' …): Operation not permitted"
        return f"{self.syscall}: {os.strerror(self.err)}"


_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                        ctypes.c_ulong, ctypes.c_void_p]
_libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
_libc.unshare.argtypes = [ctypes.c_int]
_libc.prctl.argtypes = [ctypes.c_int] * 5


def _last_error() -> int:
    return ctypes.get_errno() or _errno.EINVAL


def mount(source: str | None, target: str, fstype: str | None = None,
          flags: int = 0, data: str | None = None) -> None:
    """`mount(2)`, or `MountError` naming it."""
    result = _libc.mount(source.encode() if source is not None else None,
                         target.encode(), fstype.encode() if fstype else None,
                         ctypes.c_ulong(flags), data.encode() if data else None)
    if result != 0:
        raise MountError(f"mount({source!r}, {target!r}, {fstype!r}, {flags:#x})",
                         _last_error())


def rbind(source: str, target: str) -> None:
    mount(source, target, None, MS_BIND | MS_REC)


def tmpfs(target: str, *, size_mib: int, read_only: bool = False) -> None:
    options = f"size={size_mib}m" + (",ro" if read_only else "")
    mount("tmpfs", target, "tmpfs", MS_RDONLY if read_only else 0, options)


def overlay(lower: str, root: str, view: str) -> None:
    """§10 step 6: `up/`, `work/`, `view/` under `root`, overlaying `lower`."""
    for name in ("up", "work", "view"):
        os.makedirs(os.path.join(root, name), exist_ok=True)
    mount("overlay", view, "overlay", 0,
          f"lowerdir={lower},upperdir={root}/up,workdir={root}/work")


def private_recursive(target: str = "/") -> None:
    """§10 step 4, unconditional: no mount of ours may propagate back to the host."""
    mount(None, target, None, MS_REC | MS_PRIVATE)



def shared_recursive(target: str) -> None:
    mount(None, target, None, MS_REC | MS_SHARED)


def umount2(target: str, flags: int = 0) -> None:
    if _libc.umount2(target.encode(), flags) != 0:
        raise MountError(f"umount2({target!r})", _last_error())


def unshare(flags: int) -> None:
    """`os.unshare` where it exists, `libc.unshare` where the flags are unusual."""
    try:
        os.unshare(flags)
        return
    except AttributeError:                     # pragma: no cover — 3.12+ has it
        pass
    if _libc.unshare(ctypes.c_int(flags)) != 0:
        raise MountError(f"unshare({flags:#x})", _last_error())


def prctl(option: int, value: int) -> int:
    result = _libc.prctl(option, value, 0, 0, 0)
    if result < 0:
        raise MountError(f"prctl({option})", _last_error())
    return result


def set_not_dumpable() -> None:
    """§10 step 4b: close the run's file objects to every member through `/proc`."""
    prctl(PR_SET_DUMPABLE, 0)


def is_dumpable() -> bool:
    return prctl(PR_GET_DUMPABLE, 0) == 1


def namespace_id(path: str = "/proc/self/ns/mnt") -> int:
    """The inode number that identifies a mount namespace (§4, §10 step 8)."""
    return os.stat(path).st_ino


def propagation_tags(prefix: str) -> list[str]:
    """§13.9: `shared:`/`master:` tags on this namespace's own sandbox mounts."""
    bad = []
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            fields, sep, rest = line.strip().partition(" - ")
            if not sep:
                continue
            mount_point = rest.split(" ")[0]
            tags = [field for field in fields.split()
                    if ":" in field and not field[0].isdigit()
                    and field.split(":")[0] in ("shared", "master")]
            if tags and (mount_point == prefix or mount_point.startswith(prefix + "/")):
                bad.append(f"{mount_point}: {','.join(tags)}")
    return bad
