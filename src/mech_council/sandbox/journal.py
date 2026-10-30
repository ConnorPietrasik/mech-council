"""The upper layer is the write journal."""

from __future__ import annotations

import os
import re
import stat

from .memberns import SCRATCH_NAME


def listing(upper: str, *, skip: str = SCRATCH_NAME) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.isdir(upper):
        return out
    for root, dirs, files in os.walk(upper, followlinks=False):
        for name in list(dirs) + list(files):
            path = os.path.join(root, name)
            relative = os.path.relpath(path, upper)
            if relative == skip or relative.startswith(skip + os.sep):
                continue
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISCHR(info.st_mode) and os.major(info.st_rdev) == 0 and os.minor(info.st_rdev) == 0:
                out[relative] = "deleted"
            elif stat.S_ISDIR(info.st_mode):
                out[relative] = "dir"
            else:
                out[relative] = "touched"
    return out


def _exists_in_lower(lower_fd: int, relative: str) -> bool | None:
    if lower_fd < 0:
        return None
    try:
        os.stat(relative, dir_fd=lower_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def journal(upper: str, lower_fd: int, *, skip: str = SCRATCH_NAME) -> dict[str, list[str]]:
    entries = listing(upper, skip=skip)
    created, modified, deleted = [], [], []
    for relative, kind in sorted(entries.items()):
        if kind == "deleted":
            deleted.append(relative)
        elif _exists_in_lower(lower_fd, relative):
            modified.append(relative)
        else:
            created.append(relative)
    return {"created": created, "modified": modified, "deleted": deleted}


def evidence_paths(text: str) -> list[str]:
    out: list[str] = []
    for token in re.split(r"[\s,;()\"'`\[\]{}<>|]+", text or ""):
        token = token.strip(".,:!?")
        if not token or len(token) < 2:
            continue
        if "/" not in token and not re.search(r"\.\w{1,6}$", token):
            continue
        if token.startswith(("-", "--")) or "://" in token:
            continue
        out.append(token)
    return out


def _artifact_owners(journal: list[dict], project: str) -> dict[str, str | None]:
    holders: dict[str, set[str]] = {}
    for entry in journal:
        base = entry.get("file_base") or entry.get("member") or ""
        for relative in entry.get("created") or []:
            holders.setdefault(relative, set()).add(base)
    owners: dict[str, str | None] = {}
    for relative, who in holders.items():
        if project and (os.path.lexists(os.path.join(project, relative)) or relative.startswith("../")):
            continue
        owners[relative] = next(iter(who)) if len(who) == 1 else None
    return owners


def mark_sandbox_items(docket, journal: list[dict], *, project: str = "") -> list[str]:
    from .infer import evidence_is_limit

    marked: list[str] = []
    owners = _artifact_owners(journal, project)
    for item in docket.items.values():
        prose = " ".join(filter(None, (item.evidence, item.detail, item.claim)))
        if "sandbox_limit" not in item.markers and evidence_is_limit(prose):
            item.markers.append("sandbox_limit")
            marked.append(f"{item.id}:sandbox_limit")
        if "sandbox_artifact" not in item.markers:
            for path in evidence_paths(prose):
                relative = path
                if project and relative.startswith(project.rstrip("/") + "/"):
                    relative = relative[len(project) + 1 :]
                relative = relative.lstrip("/")
                if "#" in relative:
                    relative = relative.split("#")[0]
                relative = relative.rsplit(":", 1)[0] if relative.count(":") == 1 and relative.rsplit(":", 1)[1].isdigit() else relative
                if relative in owners:
                    item.markers.append("sandbox_artifact")
                    item.artifact_by = owners[relative] or ""
                    marked.append(f"{item.id}:sandbox_artifact")
                    break
    return marked


def bytes_used(upper: str, *, skip: str = SCRATCH_NAME) -> int:
    total = 0
    for root, dirs, files in os.walk(upper, followlinks=False):
        dirs[:] = [d for d in dirs if os.path.relpath(os.path.join(root, d), upper) != skip]
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def exists_only_in_uppers(relative: str, uppers: list[str], lower_fd: int) -> str | None:
    if _exists_in_lower(lower_fd, relative):
        return None
    held = [upper for upper in uppers if upper and os.path.lexists(os.path.join(upper, relative))]
    # with two uppers holding it, naming either is a guess
    return held[0] if len(held) == 1 else None
