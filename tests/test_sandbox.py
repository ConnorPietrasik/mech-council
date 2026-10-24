"""The sandbox on the box it runs on: coverage, isolation, scratch, journal, sweep.

Everything here forks and mounts for real: `mount(2)`, `unshare(2)`, the overlay and the namespace sweep are
the subject, so nothing is mocked.  The shape mirrors the sandbox's own shape — a mapped child plays the
orchestrator and establishes the sandbox *inside its own* mount namespace, then forks members that take a namespace
of their own — because a journal read from the wrong namespace reads an empty directory and proves nothing.

The fixture ground is `tests.support.ground.FIXTURE_ROOT`: `$COUNCIL_TEST_FS` when the operator says where the plain filesystem
is, else `$TMPDIR`, else `.council-test-fs` beside this checkout.  The family's one narrow skip is a fixture lower sitting
inside an overlay: on this box `/tmp` *is* the container root's overlay, so a run with `TMPDIR=/tmp` skips here by design
rather than reporting green having tested nothing, and the ext4 ground beside the checkout is what makes the family run.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

from council import launcher as launcher_mod
from council.process import lifecycle
from council.process.spawn import member_env
from council.sandbox import journal as journal_mod
from council.sandbox import mount as m
from council.sandbox import mount as mount_mod
from council.sandbox.memberns import SCRATCH_NAME, SetupFailure, setup_member_namespace
from council.sandbox.setup import _is_mounted, establish

# The family's ground: `fstype_under` decides the one skip below, so it belongs with the
# imports rather than after the marks.
from tests.support.ground import FIXTURE_ROOT, fstype_under

pytestmark = [pytest.mark.sandbox, pytest.mark.timeout(240)]

# The one narrow skip, decided once for the whole family rather than per case: a fixture lower inside an overlay cannot hold
# the uppers this family mounts, and the answer is `TMPDIR`/`COUNCIL_TEST_FS` pointing at plain ground, not a narrower test.
if fstype_under(FIXTURE_ROOT) == "overlay":
    pytest.skip(
        f"SKIPPED (lower inside an overlay): fixture ground {FIXTURE_ROOT} sits "
        "on an overlay, which cannot hold the lower layers it must mount; run with "
        "COUNCIL_TEST_FS pointing at a plain filesystem",
        allow_module_level=True,
    )


@pytest.fixture
def tree(ground):
    """A project, a runs root and a member roster, on plain ground."""
    project = os.path.join(ground, "proj")
    os.makedirs(os.path.join(project, "src"), exist_ok=True)
    with open(os.path.join(project, "hello.py"), "w", encoding="utf-8") as handle:
        handle.write("print('one')\n")
    with open(os.path.join(project, "src", "mod.py"), "w", encoding="utf-8") as handle:
        handle.write("VALUE = 1\n")
    runs = os.path.join(ground, "runs")
    os.makedirs(runs, exist_ok=True)
    return {"project": project, "runs": runs, "ground": ground, "members": ["seat1-a", "seat2-b", "chair"]}


# ------------------------------------------------------------------ the two helpers


def in_mapped_child(body, *args, mount_namespace=True):
    """Fork, unshare, let the **parent** write the map, then run `body(*args)` inside.

    The map write's only workable order — an unprivileged process cannot write its own `uid_map` — is the reason
    the product splits supervisor from orchestrator.  The map is `0 <uid> 1`, so the child is root *inside* and keeps
    its capabilities across `execve`; an identity map would drop them at every exec and no member could mount.
    """
    parent, child_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    os.set_inheritable(parent.fileno(), False)
    pid = os.fork()
    if pid == 0:
        parent.close()
        try:
            flags = mount_mod.CLONE_NEWUSER | (mount_mod.CLONE_NEWNS if mount_namespace else 0)
            os.unshare(flags)
            child_end.sendall(b"r")
            if child_end.recv(1) != b"m":
                os._exit(3)
            result = body(*args)
            child_end.sendall(b"|" + json.dumps(result, default=str).encode())
            child_end.close()
            os._exit(0)
        except BaseException as error:  # reported, not lost
            import traceback

            detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            try:
                child_end.sendall(b"!" + detail.encode()[-900:])
            except Exception:
                pass
            os._exit(1)
    child_end.close()
    uid, gid = os.getuid(), os.getgid()
    assert parent.recv(1) == b"r", "the child never reached the mapping handshake"
    for name, value in (("setgroups", "deny"), ("uid_map", f"0 {uid} 1"), ("gid_map", f"0 {gid} 1")):
        with open(f"/proc/{pid}/{name}", "w", encoding="utf-8") as handle:
            handle.write(value)  # written from outside: the only place it can be
    parent.sendall(b"m")
    payload = _drain(parent)
    parent.close()
    _, status = os.waitpid(pid, 0)
    if payload.startswith(b"!"):
        raise AssertionError(f"the sandboxed child failed:\n{payload[1:].decode()}")
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, f"the child did not exit cleanly: {status}"
    return json.loads(payload[1:] or b"null")


def _drain(sock) -> bytes:
    payload = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return payload
        payload += chunk


def run_sandboxed(tree, plan, *, members=None, sandbox_mib=32):
    """Run `plan(box, tell)` where the orchestrator's mounts have run, in the namespace they ran in.

    `plan` stands where `run.py::_inside_namespace` stands; whatever it `tell()`s comes back
    as JSON, and a crash in the child is an assertion failure out here, never a silence.
    """

    def body():
        collected: list = []
        box = establish(project=tree["project"], runs_dir=tree["runs"], sandbox_mib=sandbox_mib, members=members or tree["members"])
        try:
            plan(box, collected.append)
        finally:
            box.close()
        return collected

    return in_mapped_child(body)


HEADER = "import json, os, shutil, subprocess, sys\nPROBE = lambda **kw: print(json.dumps(kw))\n"


def probe(statements: str) -> str:
    """Member-side statements whose `PROBE(...)` becomes the answer we get back."""
    return HEADER + statements


def in_member(box, tree, file_base, source) -> dict:
    """Take the member's own namespace as `file_base`, run `source` there, and report its `PROBE`."""
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, False)
    os.set_inheritable(write_fd, True)  # the one fd the member may keep
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            setup_member_namespace(project=tree["project"], runs_dir=tree["runs"], slot_dir=box.slot_dir(file_base))
            # The member env comes from the product's own builder, TMPDIR included.
            env = member_env(
                base=dict(os.environ),
                run_dir=tree["runs"],
                run_path=tree["runs"],
                project=tree["project"],
                slot_dir=box.slot_dir(file_base),
                runs_root=tree["runs"],
            )
            env["PROBE_OUT"] = str(write_fd)
            done = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, cwd=tree["project"], env=env, check=False)
            os.write(write_fd, b"|" + json.dumps({"code": done.returncode, "stdout": done.stdout, "stderr": done.stderr[-700:]}).encode())
            os.close(write_fd)
            os._exit(0)
        except SetupFailure as failure:
            os.write(write_fd, b"!" + f"{failure.syscall} {failure.err}".encode())
            os._exit(1)
        except BaseException as error:
            os.write(write_fd, b"!" + f"{type(error).__name__}: {error}".encode()[:400])
            os._exit(1)
    os.close(write_fd)
    payload = b""
    while True:
        chunk = os.read(read_fd, 65536)
        if not chunk:
            break
        payload += chunk
    _, status = os.waitpid(pid, 0)
    os.close(read_fd)
    if payload.startswith(b"!"):
        raise AssertionError(f"member namespace setup failed: {payload[1:].decode()}")
    assert os.WIFEXITED(status), status
    report = json.loads(payload[1:] or b"{}")
    assert report.get("code") == 0, report
    lines = report["stdout"].strip().splitlines()
    return json.loads(lines[-1]) if lines else {}


# --------------------------------------------------------------------------- coverage


def test_a_member_writes_at_the_canonical_path_and_the_lower_never_changes(tree):
    """The sandbox's coverage rule, judged from outside and never from the child's `errno`."""
    before = _inventory(tree["project"])

    def plan(box, tell):
        seen = in_member(
            box,
            tree,
            "seat1-a",
            probe("""
with open("written.txt", "w") as handle: handle.write("x\\n")
os.mkdir("dir")
os.unlink("hello.py")
os.rename(os.path.join("src", "mod.py"), os.path.join("src", "renamed.py"))
PROBE(cwd=os.getcwd(), wrote=os.path.exists("written.txt"))
"""),
        )
        tell({"seen": seen, "journal": journal_mod.journal(box.upper("seat1-a"), box.lower_fd)})

    [row] = run_sandboxed(tree, plan)
    assert row["seen"]["cwd"] == tree["project"], "the member's cwd is the project"
    assert row["seen"]["wrote"], "a write at the canonical path returns success by design"
    assert _inventory(tree["project"]) == before, "the lower is byte-identical afterwards"
    entries = row["journal"]
    assert "written.txt" in entries["created"] and "dir" in entries["created"]
    assert "hello.py" in entries["deleted"]
    assert "src/renamed.py" in entries["created"]
    assert os.path.join("src", "mod.py") in entries["deleted"]


def test_a_child_of_the_namespace_cannot_undo_the_bind_that_covers_the_project(tree):
    """A member cannot take the project off itself, so the overlay always covers it.

    The raw syscall is tried rather than `/usr/bin/umount`, whose own namespace check would answer `EPERM`
    for reasons of its own.  Whether the kernel says `EPERM` (no power over the parent's mount) or `EBUSY`
    (the member's own cwd is the mount, and the chdir puts it there) is not the point: what the sandbox
    requires is that the overlay still covers the project after the attempt, which is what this asserts.
    """
    project = tree["project"]
    snippet = f"""
PROJ_PATH = {project!r}
import ctypes, ctypes.util
with open("planted", "w") as handle: handle.write("mine" + chr(10))
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
rc = libc.umount2(PROJ_PATH.encode(), 0)
err = ctypes.get_errno()
PROBE(refused=rc != 0, errno=err, planted=os.path.exists("planted"),
      lower_still_readable=os.path.exists("hello.py"))
"""

    def plan(box, tell):
        tell(in_member(box, tree, "seat1-a", probe(snippet)))

    before = _inventory(tree["project"])
    [row] = run_sandboxed(tree, plan)
    assert row["refused"], row
    assert row["errno"] in (1, 16), "EPERM or EBUSY; never success"
    assert row["planted"] and row["lower_still_readable"], "the overlay still covers the path"
    assert _inventory(tree["project"]) == before, "and the write still landed in the upper"


def test_absolute_paths_still_work_from_inside_a_member(tree):
    """The regression this shape exists for: an absolute `sys.path` entry still imports."""

    def plan(box, tell):
        tell(
            in_member(
                box,
                tree,
                "seat1-a",
                probe(f"""
src = {tree["project"]!r}
r = subprocess.run([sys.executable, "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import src.mod; print(src.mod.VALUE)", src],
                   capture_output=True, text=True, cwd=src)
PROBE(code=r.returncode, out=r.stdout.strip(), err=r.stderr[-200:])
"""),
            )
        )

    [row] = run_sandboxed(tree, plan)
    assert row["code"] == 0 and row["out"] == "1", row


# --------------------------------------------------------------------------- isolation


def test_a_file_planted_by_one_member_is_absent_from_its_peer(tree):
    def plan(box, tell):
        tell(
            in_member(
                box,
                tree,
                "seat1-a",
                probe("""
with open("planted-by-a", "w") as handle: handle.write("a\\n")
PROBE(sees=os.path.exists("planted-by-a"))
"""),
            )
        )
        tell(
            in_member(
                box,
                tree,
                "seat2-b",
                probe("""
PROBE(sees=os.path.exists("planted-by-a"), lower_readable=os.path.exists("hello.py"))
"""),
            )
        )

    first, second = run_sandboxed(tree, plan)
    assert first["sees"] and not second["sees"]
    assert second["lower_readable"], "both read the same lower content"


def test_the_runs_root_and_its_sandbox_are_masks_that_list_nothing(tree):
    """Every peer's `up/` and `run.json` are behind a mask: empty, and `EROFS` to write."""

    def plan(box, tell):
        tell(
            in_member(
                box,
                tree,
                "seat1-a",
                probe(f"""
runs = {tree["runs"]!r}
out = {{}}
for label, path in (("write", runs), ("sandbox_write", os.path.join(runs, "sandbox"))):
    try:
        open(os.path.join(path, "sneak"), "w").write("x")
        out[label] = "allowed"
    except OSError as e:
        out[label] = "EROFS" if e.errno == 30 else type(e).__name__
sandbox = os.path.join(runs, "sandbox")
PROBE(runs=os.listdir(runs),
      sandbox=sorted(os.listdir(sandbox)) if os.path.isdir(sandbox) else "absent", **out)
"""),
            )
        )

    [row] = run_sandboxed(tree, plan)
    assert row["runs"] == [], "a mask lists nothing"
    assert row["sandbox"] in ([], "absent"), "the peers' uppers are unreachable either way"
    assert row["write"] == "EROFS"
    assert row["sandbox_write"] in ("EROFS", "ENOENT", "FileNotFoundError")


def test_tmp_is_masked_when_it_holds_neither_the_project_nor_the_runs_root(tree):
    """The mask's first arm: host `/tmp` is gone and the member's is a private 1 MiB tmpfs."""
    marker = os.path.join("/tmp", f"council-host-marker-{os.getpid()}")
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("host")
    try:

        def plan(box, tell):
            tell(
                in_member(
                    box,
                    tree,
                    "seat1-a",
                    probe(f"""
PROBE(hidden=not os.path.exists({marker!r}),
      private=os.path.exists({marker!r}) is False and os.access('/tmp', os.W_OK))
"""),
                )
            )

        [row] = run_sandboxed(tree, plan)
        assert row["hidden"] and row["private"], row
    finally:
        os.unlink(marker)


def test_tmp_is_shared_when_it_holds_the_runs_root_and_the_other_masks_are_not_skipped(ground, tree):
    """Masking is a disjunction, and skipping one mask must not skip the others."""
    runs = f"/tmp/council-runs-under-tmp-{os.getpid()}"
    os.makedirs(runs, exist_ok=True)
    try:
        other = dict(tree, runs=runs, members=["seat1-a"])

        def plan(box, tell):
            tell(
                in_member(
                    box,
                    other,
                    "seat1-a",
                    probe(f"""
runs = {runs!r}
sandbox = os.path.join(runs, 'sandbox')
PROBE(tmp_kept=os.access('/tmp', os.W_OK), runs=os.listdir(runs),
      sandbox=sorted(os.listdir(sandbox)) if os.path.isdir(sandbox) else "absent")
"""),
                )
            )

        [row] = run_sandboxed(other, plan)
        assert row["tmp_kept"], "/tmp holds the runs root: the /tmp mask is skipped"
        assert row["runs"] == [] and row["sandbox"] in ([], "absent"), "and the other two masks are not skipped with it"
    finally:
        shutil.rmtree(runs, ignore_errors=True)


def test_scratch_is_per_member_writable_and_never_appears_in_the_journal(tree):
    """`TMPDIR` is `<project>/.council-scratch`, usable, and excluded from the journal."""

    def plan(box, tell):
        for base in ("seat1-a", "seat2-b"):
            tell(
                in_member(
                    box,
                    tree,
                    base,
                    probe("""
scratch = os.environ['TMPDIR']
assert scratch == os.path.join(os.getcwd(), '.council-scratch'), scratch
target = os.path.join(scratch, 'work')
os.makedirs(target, exist_ok=True)
with open(os.path.join(target, 'f'), 'w') as handle: handle.write('x\\n')
here = os.path.exists(os.path.join(target, 'f'))
shutil.rmtree(target)
PROBE(tmpdir=os.environ.get('TMPDIR', ''), mktemp=here,
      rm_rf=not os.path.exists(target),
      peer_invisible=not os.path.exists(os.path.join(scratch, 'other-member')))
"""),
                )
            )
        for base in ("seat1-a", "seat2-b"):
            tell(journal_mod.journal(box.upper(base), box.lower_fd))

    rows = run_sandboxed(tree, plan)
    for row in rows[:2]:
        assert row["tmpdir"].endswith(".council-scratch"), row
        assert row["mktemp"] and row["rm_rf"] and row["peer_invisible"]
    for entries in rows[2:]:
        flat = entries["created"] + entries["modified"] + entries["deleted"]
        assert not any(p.startswith(SCRATCH_NAME) for p in flat), flat


def test_a_member_cannot_read_the_orchestrators_run_state(tree):
    """Dumpability is cleared so `/proc` closes the run's own file objects.

    A member is a same-uid child in one pid namespace, so `/proc` is the one hole a per-member mount namespace
    does not plug.  Clearing dumpability is the run's only defence on `cwd`, `environ`, `exe` and `fd` — the four
    that hold the members' pipes, the lower fd the journal runs on and the run directory — while `status`,
    `cmdline` and `mountinfo` stay readable so the run is not blinded to itself.  The flag itself is unreadable
    across a user namespace here, so the claim is asserted as access, which is what the sandbox actually promises.
    """

    def plan(box, tell):
        orchestrator = os.getpid()
        snippet = f"""
ORCH = {orchestrator}
closed = {{}}
# `environ` and `exe` are read as files, `cwd` is a link and `fd` is a directory: the
# entry points differ, and dumpability has to close all four shapes.
probes = {{"cwd": lambda: os.readlink(f'/proc/{{ORCH}}/cwd'),
          "environ": lambda: open(f'/proc/{{ORCH}}/environ', 'rb').read(),
          "exe": lambda: open(f'/proc/{{ORCH}}/exe', 'rb').read(1),
          "fd": lambda: os.listdir(f'/proc/{{ORCH}}/fd')}}
closed = {{}}
for name, probe in probes.items():
    try:
        probe()
        closed[name] = "readable"
    except OSError as e:
        closed[name] = "EACCES" if e.errno == 13 else type(e).__name__
readable = {{}}
for name in ("status", "cmdline", "mountinfo"):
    try:
        readable[name] = len(open(f'/proc/{{ORCH}}/{{name}}').read()) > 0
    except OSError as e:
        readable[name] = f"{{type(e).__name__}} {{e.errno}}"
PROBE(closed=closed, readable=readable)
"""
        tell(in_member(box, tree, "seat1-a", probe(snippet)))

    [row] = run_sandboxed(tree, plan)
    assert set(row["closed"].values()) == {"EACCES"}, row["closed"]
    assert all(row["readable"].values()), "clearing dumpability does not blind the run"


# --------------------------------------------------------------------------- the sweep


def test_the_sweep_kills_a_stray_left_running_in_a_namespace_this_run_created(tree):
    """The namespace sweep, on a stray that ignores `SIGTERM` and leaves its process group.

    The namespace is handed over as an open descriptor rather than as an id the parent reads back: clearing dumpability
    makes this process non-dumpable, and a non-dumpable process's `/proc/<pid>/ns/*` is `EACCES` to its own parent.  That
    is the sweep's point — the reference the run holds *is* the key, and holding it is what makes the id unambiguous.
    """

    def plan(box, tell):
        slot, project, runs = box.slot_dir("seat1-a"), tree["project"], tree["runs"]
        parent_end, child_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        os.set_inheritable(parent_end.fileno(), False)
        os.set_inheritable(child_end.fileno(), True)
        pid = os.fork()
        if pid == 0:
            parent_end.close()
            keep = child_end.fileno()
            for fd in {fd for fd in range(3, 512) if fd != keep}:
                with contextlib.suppress(OSError):
                    os.close(fd)  # only the report survives
            inode = setup_member_namespace(project=project, runs_dir=runs, slot_dir=slot)
            # The other half: a member that execs is dumpable again, so the orchestrator can open a descriptor on the namespace it reports.
            m.prctl(m.PR_SET_DUMPABLE, 1)
            ns_fd = os.open("/proc/self/ns/mnt", os.O_RDONLY)
            _send_fd(child_end, ns_fd, inode)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            os.setpgrp()
            time.sleep(120)
            os._exit(0)
        child_end.close()
        ns_fd, inode = _recv_fd(parent_end, timeout=25)
        parent_end.close()
        assert inode > 0 and ns_fd >= 0, "the stray never handed over its namespace"
        stray = _find_in_namespace(inode, exclude={os.getpid()})
        blind = lifecycle.sweep({inode}, keep_fds=[])  # the race, unguarded
        result = lifecycle.sweep(set(), keep_fds=[ns_fd])  # as written
        deadline = time.monotonic() + 15
        while stray and _alive(stray) and time.monotonic() < deadline:
            time.sleep(0.2)
        if stray:
            try:
                os.waitpid(stray, os.WNOHANG)
            except ChildProcessError:
                pass
        os.close(ns_fd)
        tell(
            {
                "ns": inode,
                "stray": stray,
                "blind_pids": blind.pids,
                "pids": sorted(result.pids),
                "namespaces": result.namespaces,
                "alive": bool(stray and _alive(stray)),
                "line": lifecycle.survivor_line(result),
            }
        )

    [row] = run_sandboxed(tree, plan)
    assert row["stray"] > 0, "the stray is found by the namespace the run holds"
    assert row["namespaces"] == [row["ns"]]
    assert row["pids"] == sorted([row["stray"]]), row
    assert not row["alive"], "the sweep killed the survivor"
    assert str(row["stray"]) in row["line"] and f"ns {row['ns']}" in row["line"]


def _send_fd(sock, fd: int, payload: int) -> None:
    socket.send_fds(sock, [str(payload).encode()], [fd])


def _recv_fd(sock, *, timeout: float):
    sock.settimeout(timeout)
    try:
        _, fds, _, _ = socket.recv_fds(sock, 32, 1)
    except TimeoutError:
        return -1, 0
    if not fds:
        return -1, 0
    fd = fds[0]
    return fd, os.fstat(fd).st_ino


def _find_in_namespace(inode: int, *, exclude: set[int]) -> int:
    """The pid whose namespace this run holds a descriptor for — the sweep's own test."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in exclude:
            continue
        try:
            fd = os.open(f"/proc/{entry}/ns/mnt", os.O_RDONLY)
        except OSError:
            continue
        try:
            if os.fstat(fd).st_ino == inode:
                return int(entry)
        finally:
            os.close(fd)
    return 0


def test_the_sweep_leaves_alone_a_namespace_this_run_did_not_make():
    keep = os.fork()
    if keep == 0:
        time.sleep(5)
        os._exit(0)
    try:
        result = lifecycle.sweep({0x7FFFFFFF}, keep_fds=[])
        assert result.pids == [] and result.empty
        assert lifecycle.survivor_line(result).startswith("no survivors")
    finally:
        os.kill(keep, signal.SIGKILL)
        os.waitpid(keep, 0)


def _start_stray(tree, box, file_base, ready) -> int:
    """A member's child that outlives its group kill and ignores SIGTERM (the sweep's case).

    It reports its own namespace id over `ready` once the mounts are up: the parent cannot stat `/proc/<pid>/ns/mnt`
    from outside the user namespace and guess, and a sweep of a namespace nobody created must kill nothing.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        os.closerange(3, 512)  # a stray holds no inherited fd
        inode = setup_member_namespace(project=tree["project"], runs_dir=tree["runs"], slot_dir=box.slot_dir(file_base))
        os.write(write_fd, str(inode).encode())
        os.close(write_fd)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        os.setpgrp()
        time.sleep(300)
        os._exit(0)
    os.close(write_fd)
    inode = int(os.read(read_fd, 32) or b"0")
    os.close(read_fd)
    ready(pid, inode)
    return pid


def _ns_of(pid: int) -> int:
    try:
        return os.stat(f"/proc/{pid}/ns/mnt").st_ino
    except OSError:
        return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ------------------------------------------------------------------ the member overlay


def test_a_second_overlay_on_one_slot_is_busy_and_the_idempotence_guard_sees_it(tree):
    """One mount per slot; a restarted member keeps using the same upper."""

    def plan(box, tell):
        slot = box.slot_dir("seat1-a")
        tell({"mounted": _is_mounted(box.view("seat1-a"))})
        box.prepare_member("seat1-a")  # the restart path: idempotent, not EBUSY
        tell({"still_mounted": _is_mounted(box.view("seat1-a"))})
        try:
            mount_mod.overlay(tree["project"], slot, box.view("seat1-a"))
            tell({"second": "mounted"})
        except mount_mod.MountError as failure:
            tell({"second": "EBUSY" if failure.err == 16 else f"errno {failure.err}"})

    mounted, still, second = run_sandboxed(tree, plan)
    assert mounted and still, "mountinfo's mount point is on the left of the separator"
    assert second["second"] == "EBUSY", second


def test_a_setup_failure_is_reported_as_syscall_and_errno_and_the_member_never_execs(ground):
    """The ns report: the launcher answers on its report channel and never execs."""
    project = os.path.join(ground, "proj")
    os.makedirs(project, exist_ok=True)
    marker = os.path.join(ground, "execed")
    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    env = dict(
        os.environ,
        COUNCIL_LAUNCH_PROJECT=project,
        COUNCIL_LAUNCH_RUNS_ROOT=os.path.join(ground, "runs"),
        COUNCIL_LAUNCH_SLOT_DIR=os.path.join(ground, "no-such-slot"),
        COUNCIL_LAUNCH_PIPE=str(write_fd),
        COUNCIL_LAUNCH_DEBUG="",
    )
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "council",
                "--setup-seat",
                "--slot",
                "seat1-x",
                "--",
                sys.executable,
                "-c",
                f"open({marker!r}, 'w').write('ran')",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            pass_fds=(write_fd,),
            check=False,
        )
        os.close(write_fd)  # only the launcher's copy is left open
        report = os.read(read_fd, 4096).decode()
    finally:
        os.close(read_fd)
    assert proc.returncode == 1, proc
    assert report.startswith(launcher_mod.NS_REPORT_FAIL + " "), repr(report)
    steps = ("unshare(CLONE_NEWNS)", "mkdir", "mount --bind", "mask", "chdir")
    assert any(step in report for step in steps), repr(report)  # names what failed
    assert not os.path.exists(marker), "the member never execed"


# --------------------------------------------------------------------------- the journal


def test_created_and_modified_are_decided_by_the_lower_and_a_whiteout_is_a_delete(ground):
    """A 0:0 char device is an overlay whiteout: a delete, whichever name encoding."""

    def probe_dir():
        return _on_tmpfs(ground, _read_journal_with_whiteout)

    entries = in_mapped_child(probe_dir)
    assert entries["created"] == ["new"]
    assert entries["modified"] == ["dir", "file"]
    assert entries["deleted"] == ["gone"]


def _read_journal_with_whiteout(root):
    lower, upper = f"{root}/lower", f"{root}/upper"
    os.makedirs(os.path.join(lower, "dir"))
    os.makedirs(os.path.join(upper, "dir"))
    with open(os.path.join(lower, "file"), "w", encoding="utf-8") as handle:
        handle.write("a\n")
    with open(os.path.join(upper, "file"), "w", encoding="utf-8") as handle:
        handle.write("b\n")
    with open(os.path.join(upper, "new"), "w", encoding="utf-8") as handle:
        handle.write("c\n")
    os.mknod(os.path.join(upper, "gone"), 0o020000)  # CAP_MKNOD: the mapped child has it
    fd = os.open(lower, os.O_RDONLY)
    try:
        return journal_mod.journal(upper, fd)
    finally:
        os.close(fd)


def test_a_path_held_by_two_uppers_is_nobodys_and_one_is_the_owner(ground):
    def probe_dir():
        return _on_tmpfs(ground, _read_attribution)

    assert in_mapped_child(probe_dir) == {"one": "mine", "two": None}


def _read_attribution(root):
    lower, a, b = f"{root}/l", f"{root}/a", f"{root}/b"
    os.makedirs(lower)
    os.makedirs(a)
    os.makedirs(b)
    with open(os.path.join(a, "mine"), "w", encoding="utf-8") as handle:
        handle.write("x\n")
    fd = os.open(lower, os.O_RDONLY)
    try:
        one = journal_mod.exists_only_in_uppers("mine", [a, b], fd)
        with open(os.path.join(b, "mine"), "w", encoding="utf-8") as handle:
            handle.write("x\n")
        two = journal_mod.exists_only_in_uppers("mine", [a, b], fd)
        return {"one": "mine" if one == a else str(one), "two": two}
    finally:
        os.close(fd)


def _on_tmpfs(ground, read):
    """Run a journal reader on a tmpfs the caller's mapped child mounted itself.

    `mknod` of a device node and `mount(2)` of an overlay are both refused on this box's
    overlay-backed `/tmp`, so a fixture sitting there would be the wrong filesystem.
    """
    work = f"{ground}/fs"
    os.makedirs(f"{work}/mnt", exist_ok=True)
    mount_mod.tmpfs(f"{work}/mnt", size_mib=32)
    return read(f"{work}/mnt")


def _inventory(root: str) -> dict[str, tuple]:
    out = {}
    for here, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(here, name)
            stat = os.lstat(path)
            out[os.path.relpath(path, root)] = (stat.st_size, stat.st_mode)
    return out
