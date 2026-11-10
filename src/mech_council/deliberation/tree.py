"""The run's git over the project: snapshots in the run directory's private object store,
tree diffs, the rollback of a failed pass, and the run's own commits."""

from __future__ import annotations

import os
import shutil
import subprocess

#: how much of one tree's diff a prompt or a record carries
DIFF_CAP = 12000
#: the snapshot's git calls walk the worktree, so they get a long leash
_SNAPSHOT_TIMEOUT_S = 600

#: the run's commit waits out the project's hooks, then gives up
_COMMIT_TIMEOUT_S = 120

#: the run's commit identity: greppable, never the repo's own
COMMIT_NAME = "mech-council"
COMMIT_EMAIL = "mech-council@localhost"

#: the add failure's detail when git reports none: no hook can refuse an add, so it is the timeout
NO_ADD_DETAIL = f"no error was reported — the {_SNAPSHOT_TIMEOUT_S:g} s timeout was reached"

#: the commit failure's detail when git reports none: a silent hook refusal, or the timeout
NO_COMMIT_DETAIL = f"no error was reported — a git hook is the likely cause, or the {_COMMIT_TIMEOUT_S:g} s timeout was reached"


def snapshot_env(run_dir: str) -> dict[str, str]:
    """The git env the run's tree snapshots run under: a private index and object directory in the
    run directory, so the run's trees never enter the repo's object store."""
    return {
        "GIT_INDEX_FILE": os.path.join(run_dir, "review_index"),
        "GIT_OBJECT_DIRECTORY": os.path.join(run_dir, "review_objects"),
    }


class Tree:
    """One project's worktree, as the run reads and commits it."""

    def __init__(self, project: str, run_dir: str) -> None:
        self.project = project
        # the snapshot env is read by git, and a relative store path breaks repo discovery
        self.run_dir = os.path.abspath(run_dir)

    def git(self, *args: str, env: dict | None = None, timeout: float = 60) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                ["git", "-C", self.project, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **(env or {})},
            )
        except OSError, subprocess.TimeoutExpired:
            return False, ""
        return proc.returncode == 0, (proc.stdout or proc.stderr or "").strip()

    def snapshot(self) -> str | None:
        """The worktree as a tree object, in the run directory's private index and object store.

        The add is scoped to the project: a subdirectory of a larger repo snapshots only what the
        run may touch. The index persists across snapshots, so the stat cache skips files no pass
        touched; it compares ctime as well, and a restore cannot carry one, so an old mtime cannot
        hide a change.
        """
        env = snapshot_env(self.run_dir)
        # git refuses a repo whose object directory does not exist yet, so the private store is created first
        os.makedirs(env["GIT_OBJECT_DIRECTORY"], exist_ok=True)
        if not self.git("add", "-A", "--", ".", env=env, timeout=_SNAPSHOT_TIMEOUT_S)[0]:
            return None
        ok, tree = self.git("write-tree", env=env, timeout=_SNAPSHOT_TIMEOUT_S)
        return tree if ok else None

    def diff(self, from_tree: str, to_tree: str | None = None, *, cap: int = DIFF_CAP, stat: bool = False) -> str:
        """`from_tree` against `to_tree` (the worktree when None), scoped to the project."""
        env = snapshot_env(self.run_dir)
        args = ["diff"]
        if stat:
            args.append("--stat")
        args.append(from_tree)
        if to_tree is not None:
            args.append(to_tree)
        args += ["--", "."]
        ok, out = self.git(*args, env=env, timeout=_SNAPSHOT_TIMEOUT_S)
        return out[:cap] if ok else ""

    def status(self) -> str:
        """The worktree's dirt, scoped to the project: ``--porcelain`` lines."""
        return self.git("status", "--porcelain", "--", ".")[1]

    def restore(self, tree: str) -> tuple[bool, str]:
        """Roll the worktree back to `tree`: modified and deleted paths from the snapshot,
        paths a pass added removed. The next pass starts from a verified tree."""
        post = self.snapshot()
        if not post:
            return False, "the worktree could not be snapshotted"
        env = snapshot_env(self.run_dir)
        # -z: NUL-delimited, names raw — a name git would C-quote (a quote or a backslash in it)
        # still reads as the pathspec and the filesystem path;
        # --relative: the diff prints repo-root paths, and the pathspecs and the removals are project-relative
        ok, status = self.git(
            "diff",
            "--name-status",
            "-z",
            "--relative",
            tree,
            post,
            env=env,
            timeout=_SNAPSHOT_TIMEOUT_S,
        )
        if not ok:
            return False, "the diff against the pre-tree could not be read"
        restore_paths: list[str] = []
        remove_paths: list[str] = []
        fields = status.split("\0")
        if fields and fields[-1] == "":  # every -z record ends in its NUL
            fields.pop()
        i = 0
        while i < len(fields):
            code = fields[i]
            count = 2 if code[0] in "RC" else 1  # a rename or a copy record carries two paths
            paths = fields[i + 1 : i + 1 + count]
            i += 1 + count
            if count == 2:  # a rename or a copy: the old path is restored, the new one removed
                restore_paths.append(paths[0])
                remove_paths.append(paths[1])
            elif code == "A":
                remove_paths.extend(paths)
            else:  # M, D, T: the pre-tree's content is what the path wants
                restore_paths.extend(paths)
        if restore_paths:
            ok, out = self.git(
                "restore",
                f"--source={tree}",
                "--staged",
                "--worktree",
                "--",
                *restore_paths,
                env=env,
                timeout=_SNAPSHOT_TIMEOUT_S,
            )
            if not ok:
                return False, f"git restore failed: {out}"
        for path in remove_paths:
            full = os.path.join(self.project, path)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)
                else:
                    os.unlink(full)
            except OSError as failure:
                return False, f"could not remove {path}: {failure}"
        for path in dict.fromkeys(remove_paths):
            # a removed file leaves its now-empty parents behind: sweep them up to the project root
            parent = os.path.dirname(os.path.join(self.project, path))
            while len(parent) > len(self.project):
                try:
                    if os.path.isdir(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                        parent = os.path.dirname(parent)
                    else:
                        break
                except OSError:
                    break
        return True, ""

    def commit(self, message: str) -> tuple[str, str]:
        """The run's commit of a passed finding: project-scoped add and commit, fixed identity, the
        sha back. The commit's pathspec: staged dirt the user holds elsewhere in a larger repo is
        not swept in, and the pathspec's add is what makes a pass's new files committable.

        The repo's index is stowed before the add and put back on any failure: a refused commit
        leaves the index as the run found it.

        An empty tree is not a failure: the pass proved the gate with no change, and the finding
        passes uncommitted.
        """
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": COMMIT_NAME,
            "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
            "GIT_COMMITTER_NAME": COMMIT_NAME,
            "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
        }
        stowed = self._stow_index()
        ok, out = self.git("add", "-A", "--", ".", env=env, timeout=_SNAPSHOT_TIMEOUT_S)
        if not ok:
            self._unstow_index(stowed)
            return "", f"git add failed: {out or NO_ADD_DETAIL}"
        ok, out = self.git("commit", "-m", message, "--", ".", env=env, timeout=_COMMIT_TIMEOUT_S)
        if ok:
            # the commit kept the new index: the stowed copy is done for
            if stowed is not None:
                try:
                    os.remove(stowed)
                except OSError:
                    pass
            return self.git("rev-parse", "HEAD", env=env)[1], ""
        if not self.status():
            self._unstow_index(stowed)
            return "", "nothing to commit"
        self._unstow_index(stowed)
        return "", f"git commit failed: {out or NO_COMMIT_DETAIL}"

    def _stow_index(self) -> str | None:
        """The repo's index copied into the run directory, so a failed commit can put it back.
        None when there is no index to protect or the copy cannot be made."""
        ok, where = self.git("rev-parse", "--git-path", "index")
        if not ok:
            return None
        # `--git-path` prints relative in a plain repo, absolute in a linked worktree; the join takes both
        real = os.path.realpath(os.path.join(self.project, where))
        if not os.path.isfile(real):
            return None
        stowed = os.path.join(self.run_dir, "commit_index")
        try:
            os.makedirs(self.run_dir, exist_ok=True)
            shutil.copyfile(real, stowed)
        except OSError:
            return None
        return stowed

    def _unstow_index(self, stowed: str | None) -> None:
        """The repo's index back from the run directory, the stow copy removed as it lands;
        a put-back that cannot be made keeps the stow copy and says nothing — the run reports
        the commit's failure, not this second one."""
        if stowed is None:
            return
        ok, where = self.git("rev-parse", "--git-path", "index")
        if not ok:
            return
        real = os.path.realpath(os.path.join(self.project, where))
        try:
            shutil.copyfile(stowed, real)
        except OSError:
            return
        try:
            os.remove(stowed)
        except OSError:
            pass
