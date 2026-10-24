"""§4's spawn shape: the launcher form, its env, and the namespace fd the sweep is keyed on.

Every member process — seat and chair alike — is one call, and nothing else in the
design spawns anything (§13).  The launcher is this tool's own entry point resolved once
at load (plan D21), never `sys.argv[0]` and never a PATH guess.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field

from ..config import MemberSpec
from ..internal_env import LAUNCH_ENV, PIPE_ENV, NS_REPORT_FAIL, NS_REPORT_OK
from ..pi_rpc import PiRpc
from ..sandbox import mount

class LauncherSetupError(Exception):
    """§10 step 8: 6b failed, so the member never execed and the run ends as a setup failure."""


def launcher_argv() -> tuple[str, ...]:
    """This tool's own entry point, resolved once at load (plan D21)."""
    return (sys.executable, "-m", "council")


def pi_argv(spec: MemberSpec, pi_cli: str, *, file_base: str, untrusted: bool) -> list[str]:
    """§4's spawn configuration.  `pi_cli` is what the launcher execs, and it adds nothing."""
    argv = [pi_cli, "--mode", "rpc", "--no-session",
            "--no-approve" if untrusted else "--approve",
            "--name", f"council-{file_base}"]
    if spec.provider and spec.model:
        argv += ["--provider", spec.provider, "--model", spec.model]
    if spec.persona:
        argv += ["--append-system-prompt", spec.persona]
    return argv


def member_env(*, base: dict[str, str], run_dir: str, run_path: str, project: str,
               slot_dir: str, runs_root: str) -> dict[str, str]:
    """§4's env: the orchestrator's, plus exactly three council vars (plan D12)."""
    env = dict(base)
    env["COUNCIL_RUN_DIR"] = os.path.basename(run_path.rstrip("/"))
    env["COUNCIL_RUN_PATH"] = run_path
    env["TMPDIR"] = f"{project}/.council-scratch"
    env["COUNCIL_LAUNCH_PROJECT"] = project
    env["COUNCIL_LAUNCH_RUNS_ROOT"] = runs_root
    env["COUNCIL_LAUNCH_SLOT_DIR"] = slot_dir
    return env

@dataclass
class Member:
    """One live member process: the child, its RPC session, and its namespace record."""

    file_base: str
    id: str | None
    pid: int
    pgid: int
    namespace: int
    ns_fd: int
    proc: asyncio.subprocess.Process
    rpc: PiRpc
    stderr: list[bytes] = field(default_factory=list)
    stderr_task: asyncio.Task | None = None
    ns_records: list[int] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.id or self.file_base


async def spawn(*, file_base: str, member_id: str | None, spec: MemberSpec, pi_cli: str,
                project: str, runs_root: str, slot_dir: str, run_path: str,
                untrusted: bool, transcript=None,
                on_tool_result=None, env: dict[str, str] | None = None) -> Member:
    """Spawn one member through §4's launcher form and wait for its namespace report."""
    argv = list(launcher_argv()) + ["--setup-seat", "--slot", file_base, "--"] + \
        pi_argv(spec, pi_cli, file_base=file_base, untrusted=untrusted)
    child_env = member_env(base=env if env is not None else dict(os.environ),
                           run_dir=run_path, run_path=run_path, project=project,
                           slot_dir=slot_dir, runs_root=runs_root)
    read_fd, write_fd = os.pipe()
    child_env[PIPE_ENV] = str(write_fd)          # the launcher's report channel (§10 step 8)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=child_env, start_new_session=True,
            pass_fds=(write_fd,))
    finally:
        os.close(write_fd)                     # our copy: or the report would never EOF
    child_env.clear()                           # D12b's paths die with the spawn
    report = await _read_report(read_fd, proc)
    os.close(read_fd)
    if report[0] == NS_REPORT_FAIL:
        await _reap(proc)
        raise LauncherSetupError(f"§10 step 6b failed for {file_base}: {report[1]}")
    namespace = report[1]
    if namespace == mount.namespace_id():
        # §4: reading /proc/<child>/ns/mnt in that window answers with *our* namespace;
        # an id equal to ours means the report never came, which is the race §4 closes.
        await _reap(proc)
        raise LauncherSetupError(f"{file_base}: reported the orchestrator's own mount "
                                 "namespace — step 6b did not complete before the report")
    ns_fd = os.open(f"/proc/{proc.pid}/ns/mnt", os.O_RDONLY)
    rpc = PiRpc(label=file_base, stdout=proc.stdout, stdin=proc.stdin,
                on_tool_result=on_tool_result, transcript=transcript)
    member = Member(file_base=file_base, id=member_id, pid=proc.pid, pgid=proc.pid,
                    namespace=namespace, ns_fd=ns_fd, proc=proc, rpc=rpc,
                    ns_records=[namespace])
    member.stderr_task = asyncio.create_task(_drain_stderr(proc, member, transcript),
                                             name=f"stderr-{file_base}")
    rpc.start()
    return member


async def _read_report(read_fd: int, proc: asyncio.subprocess.Process) -> tuple[str, object]:
    """§10 step 8: the launcher reports the failing syscall + errno, or its namespace id."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _ready() -> None:
        try:
            data = os.read(read_fd, 4096)
        except OSError:
            data = b""
        queue.put_nowait(data)
        loop.remove_reader(read_fd)

    loop.add_reader(read_fd, _ready)
    try:
        try:
            async with asyncio.timeout(60):
                data = await queue.get()
        except TimeoutError:
            return (NS_REPORT_FAIL, "the launcher never reported (§10 step 8 timeout)")
    finally:
        pass
    text = (data or b"").decode("utf-8", "replace").strip()
    if text.startswith(NS_REPORT_OK):
        try:
            return (NS_REPORT_OK, int(text.split(None, 1)[1]))
        except (IndexError, ValueError):
            return (NS_REPORT_FAIL, f"unreadable namespace report {text!r}")
    if text.startswith(NS_REPORT_FAIL):
        return (NS_REPORT_FAIL, text.split(None, 1)[1] if " " in text else "unspecified")
    code = proc.returncode
    return (NS_REPORT_FAIL, text or f"the launcher exited with {code} before reporting")


async def _drain_stderr(proc: asyncio.subprocess.Process, member: Member,
                        transcript) -> None:
    """§3.3 point 4's stderr half: always captured, never swallowed (§11.2's sink rule)."""
    stream = proc.stderr
    if stream is None:
        return
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        member.stderr.append(chunk)
        if transcript is not None:
            transcript.append_stderr(chunk)


async def _reap(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    except Exception:                              # pragma: no cover
        pass


def unknown_model_warning(chunk: bytes) -> str:
    """§3.3 point 4: pi's only signal for an unknown model, surfaced as a warning."""
    text = chunk.decode("utf-8", "replace")
    for line in text.splitlines():
        if 'not found for provider' in line and line.lstrip().startswith("Model"):
            return line.strip()
    return ""
