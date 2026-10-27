"""The sandbox on the box it runs on: coverage, isolation, scratch, journal, sweep."""

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

from mech_council import launcher as launcher_mod
from mech_council.process import lifecycle
from mech_council.process.spawn import member_env
from mech_council.sandbox import journal as journal_mod
from mech_council.sandbox import mount as m
from mech_council.sandbox import mount as mount_mod
from mech_council.sandbox.memberns import SCRATCH_NAME, SetupFailure, setup_member_namespace
from mech_council.sandbox.setup import Sandbox, _is_mounted, establish
from tests.support.ground import FIXTURE_ROOT, fstype_under
from tests.support.namespaces import in_mapped_child

pytestmark = [pytest.mark.sandbox, pytest.mark.timeout(240)]

# One ground decision for the whole family, never per case.
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


# -------------------------------------------------- the box every case runs its plan in


def run_sandboxed(tree, plan, *, members=None, sandbox_mib=32):
    """Run the `plan` callback — a box and a report-to-parent function — in the namespace the orchestrator's mounts ran in."""

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
    return HEADER + statements


def in_member(box, tree, file_base, source) -> dict:
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
    """A member cannot take the project off itself, so the overlay always covers it."""
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
    """Dumpability is cleared so `/proc` closes the run's own file objects."""

    def plan(box, tell):
        orchestrator = os.getpid()
        snippet = f"""
ORCH = {orchestrator}
closed = {{}}
# Four shapes to close: `environ` and `exe` are files, `cwd` a link, `fd` a directory.
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
    """Two strays that ignore `SIGTERM` and leave their process group, one per member namespace: the sweep's own scope
    — a descriptor the run holds — kills one, and the unguarded scan by reported number kills the other.  Each scan
    gets a target nothing has touched yet, and a live stray in a namespace the other scan is not pointed at."""

    def plan(box, tell):
        def stray(file_base) -> tuple[int, int, int]:
            """Fork the member, take back the namespace it made, and leave it running."""
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
                inode = setup_member_namespace(project=tree["project"], runs_dir=tree["runs"], slot_dir=box.slot_dir(file_base))
                # A member that execs is dumpable again, so the orchestrator can open the namespace it reports.
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
            return pid, ns_fd, inode

        def gone(pid: int) -> bool:
            """Wait for the kill to land and reap: a zombie still answers `kill(pid, 0)`, so looking without reaping
            is a 15 s poll, and inside this namespace the stray is our own child.
            """
            deadline = time.monotonic() + 15
            while pid and _alive(pid) and time.monotonic() < deadline:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    break
                time.sleep(0.2)
            return bool(pid and _alive(pid))

        held_pid, ns_fd, held_ns = stray("seat1-a")
        assert ns_fd >= 0 and held_ns > 0, "the stray in the held namespace never handed it over"
        reported_pid, reported_fd, reported_ns = stray("seat2-b")
        assert reported_fd >= 0 and reported_ns > 0, "the stray in the reported namespace never handed it over"
        os.close(reported_fd)  # the unguarded path holds nothing: a number is the whole of its scope
        found_held = _find_in_namespace(held_ns, exclude={os.getpid()})
        found_reported = _find_in_namespace(reported_ns, exclude={os.getpid()})

        # As written: what this run holds a descriptor for. The other stray is running in a namespace the run holds
        # no descriptor for, so this same scan must pass it by.
        result = lifecycle.sweep(set(), keep_fds=[ns_fd])
        # The race, unguarded: a reported inode and nothing to re-verify it against. That stray is still running
        # here, so its kill is this scan's doing and no earlier one's.
        blind = lifecycle.sweep({reported_ns}, keep_fds=[])
        os.close(ns_fd)

        tell(
            {
                "ns": held_ns,
                "stray": found_held,
                "started": held_pid,
                "ns_reported": reported_ns,
                "reported": found_reported,
                "reported_started": reported_pid,
                "pids": sorted(result.pids),
                "namespaces": result.namespaces,
                "blind_pids": blind.pids,
                "blind_namespaces": blind.namespaces,
                "alive": gone(held_pid),
                "alive_reported": gone(reported_pid),
                "line": lifecycle.survivor_line(result),
            }
        )

    [row] = run_sandboxed(tree, plan)
    assert row["stray"] == row["started"] > 0, "the stray is found by the namespace the run holds"
    assert row["reported"] == row["reported_started"] > 0, "the other stray is found by the namespace it reported"
    assert row["pids"] == sorted([row["stray"]]), f"the held namespace was swept and the foreign one left running:\n{row}"
    assert row["namespaces"] == [row["ns"]], "the sweep credited a namespace it was not pointed at"
    assert row["blind_pids"] == [row["reported"]], "a reported inode alone scopes the kill: that is the race"
    assert row["blind_namespaces"] == [row["ns_reported"]], "and it credits only the namespace that number named"
    assert not row["alive"], "the sweep killed the survivor"
    assert not row["alive_reported"], "and the unguarded scan killed its own target"
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


# The other half of that pair — an inode that matches no namespace at all, and a host pid — lives in
# `test_process.py::test_a_pid_in_another_namespace_is_left_alone`.


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
                "mech_council",
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


# ----------------------------------------------------- the other half of `establish`: no sandbox


def test_establish_in_place_mounts_nothing_and_hands_each_seat_scratch(tree):
    """In place the run provides scratch and paths only: no tmpfs, no overlay, no lower fd for the journal to read."""
    box = establish(
        project=tree["project"],
        runs_dir=tree["runs"],
        sandbox_mib=32,
        members=tree["members"],
        sandbox_dir=os.path.join(tree["runs"], "inplace"),
        in_place=True,
    )
    assert box.in_place is True and box.lower_fd == -1, "no lower layer means no eye for the journal"
    assert sorted(os.listdir(box.sandbox_dir)) == sorted(tree["members"]), "one slot each, holding scratch"
    for file_base in tree["members"]:
        assert os.path.isdir(box.scratch(file_base))
        assert box.tmpdir(file_base) == box.scratch(file_base), "the member's own directory, outside the project"
        assert not _is_mounted(box.slot_dir(file_base)), "a slot is a directory in place, not a mount"
    assert not _is_mounted(box.sandbox_dir)
    sandboxed = Sandbox(project=tree["project"], runs_dir=tree["runs"], sandbox_dir=box.sandbox_dir)
    assert sandboxed.tmpdir("chair") == f"{tree['project']}/{SCRATCH_NAME}", "inside the view, the bind is still the scratch"
    box.close()


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
