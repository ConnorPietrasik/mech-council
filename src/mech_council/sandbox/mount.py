"""Python exposes no `mount(2)`/`unshare(2)` binding, so this is the design's only `ctypes` module."""

from __future__ import annotations

import ctypes
import errno as _errno
import os
import sys

CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000

MS_RDONLY = 1
MS_REC = 1 << 14
MS_PRIVATE = 1 << 18
MS_BIND = 1 << 12


PR_SET_DUMPABLE = 4

#: The mask tmpfs size, for every mask the member's namespace gets: a constant, never a knob.
MASK_MIB = 1
TERM_GRACE_S: float = 10  # a constant, never a knob
DISPOSE_SWEEP_AFTER_S = 15  # a constant, never a knob
WATCHDOG_PASS_TIMEOUT_S = 120


class MountError(OSError):
    def __init__(self, syscall: str, err: int) -> None:
        super().__init__(err, f"{syscall}: {os.strerror(err)}")
        self.syscall = syscall
        self.err = err

    def __str__(self) -> str:  # "mount('/' …): Operation not permitted"
        return f"{self.syscall}: {os.strerror(self.err)}"


#: Loaded on first call, never at import: this module also carries the run's timing constants, and a host
#: without `libc.so.6` still has to be able to read them.
_handle: ctypes.CDLL | None = None


def _libc() -> ctypes.CDLL:
    global _handle
    if _handle is None:
        lib = ctypes.CDLL("libc.so.6", use_errno=True)
        lib.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p]
        lib.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.unshare.argtypes = [ctypes.c_int]
        lib.prctl.argtypes = [ctypes.c_int] * 5
        _handle = lib
    return _handle


def supported() -> bool:
    """The only platform with the user and mount namespaces the sandbox is built from."""
    return sys.platform == "linux"


def _last_error() -> int:
    return ctypes.get_errno() or _errno.EINVAL


def mount(source: str | None, target: str, fstype: str | None = None, flags: int = 0, data: str | None = None) -> None:
    result = _libc().mount(
        source.encode() if source is not None else None,
        target.encode(),
        fstype.encode() if fstype else None,
        ctypes.c_ulong(flags),
        data.encode() if data else None,
    )
    if result != 0:
        raise MountError(f"mount({source!r}, {target!r}, {fstype!r}, {flags:#x})", _last_error())


def rbind(source: str, target: str) -> None:
    mount(source, target, None, MS_BIND | MS_REC)


def tmpfs(target: str, *, size_mib: int, read_only: bool = False) -> None:
    options = f"size={size_mib}m" + (",ro" if read_only else "")
    mount("tmpfs", target, "tmpfs", MS_RDONLY if read_only else 0, options)


def is_mounted(path: str) -> bool:
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


def overlay(lower: str, root: str, view: str) -> None:
    for name in ("up", "work", "view"):
        os.makedirs(os.path.join(root, name), exist_ok=True)
    if is_mounted(view):
        # A second overlay would shadow the upper the restarted member's own earlier writes sit in, and the kernel refuses one
        # only when it mounts with index=on — Ubuntu ships index=off — so this guard is what keeps one mount per slot.
        raise MountError(f"overlay({view!r} is already a mount point)", _errno.EBUSY)
    mount("overlay", view, "overlay", 0, f"lowerdir={lower},upperdir={root}/up,workdir={root}/work")


def private_recursive(target: str = "/") -> None:
    """No mount of ours may propagate back to the host."""
    mount(None, target, None, MS_REC | MS_PRIVATE)


def umount2(target: str, flags: int = 0) -> None:
    if _libc().umount2(target.encode(), flags) != 0:
        raise MountError(f"umount2({target!r})", _last_error())


def unshare(flags: int) -> None:
    try:
        os.unshare(flags)
        return
    except AttributeError:  # pragma: no cover — 3.12+ has it
        pass
    if _libc().unshare(ctypes.c_int(flags)) != 0:
        raise MountError(f"unshare({flags:#x})", _last_error())


def prctl(option: int, value: int) -> int:
    result = _libc().prctl(option, value, 0, 0, 0)
    if result < 0:
        raise MountError(f"prctl({option})", _last_error())
    return result


def set_not_dumpable() -> None:
    """Close the run's file objects to every member through `/proc`."""
    prctl(PR_SET_DUMPABLE, 0)


def namespace_id(path: str = "/proc/self/ns/mnt") -> int:
    return os.stat(path).st_ino
