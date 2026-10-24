"""§10/§3.4 — the upper layer is the write journal.

One fixed name is skipped: `.council-scratch`, step 6b(2)'s mount point, which is not a
write.  Entries are read by **inode type, not name**: a deletion is a 0:0 character
device recorded under the deleted file's own name on this kernel, and a file created and
then deleted leaves no entry at all.  `up/` alone cannot tell created from modified, so
the question is asked through step 7's lower-layer fd.
"""
from __future__ import annotations

import os
import re
import stat

from .memberns import SCRATCH_NAME


def listing(upper: str, *, skip: str = SCRATCH_NAME) -> dict[str, str]:
    """`{relative path: dir | touched | deleted}` — everything that member touched."""
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
            if stat.S_ISCHR(info.st_mode) and os.major(info.st_rdev) == 0 \
                    and os.minor(info.st_rdev) == 0:
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
    """Candidate file paths inside a finding's prose, conservatively.

    §6.3's `sandbox_artifact` test needs the *paths* an item names, and members write them
    in every shape: `cache.py:12`, `/project/src/x.py`, `` `src/y.py` ``.  Anything that
    looks like a path is a candidate; the check against the uppers is what decides.
    """
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
    """Journal paths that exist only in a member's upper, each with its owner or None.

    Only a **created** path qualifies.  A modified or deleted one exists in the lower by
    definition — the project's own file, edited or hidden in one seat's view — so a finding
    that names it is naming *project evidence*, and stamping it `sandbox_artifact` would
    quietly demote a real finding.  And a created path that the lower also holds is the
    project's file recreated, not an invention.

    A path two seats both created is still nobody's confirmation, so it keeps the marker
    with no owner named: guessing which member wrote it would be the worse lie.
    """
    holders: dict[str, set[str]] = {}
    for entry in journal:
        base = entry.get("file_base") or entry.get("member") or ""
        for relative in entry.get("created") or []:
            holders.setdefault(relative, set()).add(base)
    owners: dict[str, str | None] = {}
    for relative, who in holders.items():
        if project and (os.path.lexists(os.path.join(project, relative))
                        or relative.startswith("../")):
            continue
        owners[relative] = next(iter(who)) if len(who) == 1 else None
    return owners


def mark_sandbox_items(docket, journal: list[dict], *, project: str = "") -> list[str]:
    """§6.3's two markers, stamped on the items whose evidence earned them.

    The renderer stays pure (§12.1.10) and the docket carries the fact, so the marker is
    computed once here — where the run's own write journal and the lower layer both exist —
    and read everywhere else.
    """
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
                    relative = relative[len(project) + 1:]
                relative = relative.lstrip("/")
                if "#" in relative:
                    relative = relative.split("#")[0]
                relative = relative.rsplit(":", 1)[0] if relative.count(":") == 1 \
                    and relative.rsplit(":", 1)[1].isdigit() else relative
                if relative in owners:
                    item.markers.append("sandbox_artifact")
                    item.artifact_by = owners[relative] or ""
                    marked.append(f"{item.id}:sandbox_artifact")
                    break
    return marked


def bytes_used(upper: str, *, skip: str = SCRATCH_NAME) -> int:
    """Per-member bytes for `run.json` and the console's sandbox line (§10, §11.1)."""
    total = 0
    for root, dirs, files in os.walk(upper, followlinks=False):
        dirs[:] = [d for d in dirs if os.path.relpath(os.path.join(root, d), upper)
                   != skip]
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def exists_only_in_uppers(relative: str, uppers: list[str], lower_fd: int) -> str | None:
    """§6.3's `sandbox_artifact`: a path that exists only in some member's upper.

    That file is that member's own artifact — no other member can create a file in your
    view — and is never independent confirmation of anything.
    """
    if _exists_in_lower(lower_fd, relative):
        return None
    held = [upper for upper in uppers
            if upper and os.path.lexists(os.path.join(upper, relative))]
    # Attribution needs uniqueness: with two uppers holding it, naming either would be a
    # lie, and §6.3 marks the artifact without owning it to a member.
    return held[0] if len(held) == 1 else None
