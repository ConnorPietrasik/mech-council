from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field

from ..config import MemberSpec
from ..internal_env import NS_REPORT_FAIL, NS_REPORT_OK, PIPE_ENV
from ..pi_rpc import PiRpc
from ..sandbox import mount


class LauncherSetupError(Exception):
    pass


def launcher_argv() -> tuple[str, ...]:
    return (sys.executable, "-m", "mech_council")


def pi_argv(spec: MemberSpec, pi_cli: str, *, file_base: str, untrusted: bool) -> list[str]:
    argv = [pi_cli, "--mode", "rpc", "--no-session", "--no-approve" if untrusted else "--approve", "--name", f"council-{file_base}"]
    if spec.provider and spec.model:
        argv += ["--provider", spec.provider, "--model", spec.model]
    if spec.persona:
        argv += ["--append-system-prompt", spec.persona]
    return argv


def member_env(*, base: dict[str, str], run_dir: str, run_path: str, project: str, slot_dir: str, runs_root: str) -> dict[str, str]:
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


async def spawn(
    *,
    file_base: str,
    member_id: str | None,
    spec: MemberSpec,
    pi_cli: str,
    project: str,
    runs_root: str,
    slot_dir: str,
    run_path: str,
    untrusted: bool,
    transcript=None,
    on_tool_result=None,
    env: dict[str, str] | None = None,
) -> Member:
    argv = (
        list(launcher_argv())
        + ["--setup-seat", "--slot", file_base, "--"]
        + pi_argv(spec, pi_cli, file_base=file_base, untrusted=untrusted)
    )
    child_env = member_env(
        base=env if env is not None else dict(os.environ),
        run_dir=run_path,
        run_path=run_path,
        project=project,
        slot_dir=slot_dir,
        runs_root=runs_root,
    )
    read_fd, write_fd = os.pipe()
    child_env[PIPE_ENV] = str(write_fd)  # the launcher's report channel
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            start_new_session=True,
            pass_fds=(write_fd,),
        )
    finally:
        os.close(write_fd)  # our copy: or the report would never EOF
    child_env.clear()  # the launcher's paths die with the spawn
    try:
        report = await _read_report(read_fd, proc)
    finally:
        os.close(read_fd)  # the cancel path owes this too
    if report[0] == NS_REPORT_FAIL:
        await _reap(proc)
        raise LauncherSetupError(f"member namespace failed for {file_base}: {report[1]}")
    namespace = report[1]
    if not isinstance(namespace, int):
        # An OK tag carrying no id is a launcher speaking some other protocol: the run ends as a
        # setup failure rather than keying the sweep on a value that is not a namespace.
        await _reap(proc)
        raise LauncherSetupError(f"{file_base}: the namespace report carried no id: {namespace!r}")
    if namespace == mount.namespace_id():
        # Reading /proc/<child>/ns/mnt in that window answers with *our* namespace. An id equal to ours means the
        # member's mounts never finished, which is why the launcher reports its own id rather than us reading it back.
        await _reap(proc)
        raise LauncherSetupError(
            f"{file_base}: reported the orchestrator's own mount namespace — its own namespace was never set up before the report"
        )
    ns_fd = os.open(f"/proc/{proc.pid}/ns/mnt", os.O_RDONLY)
    if proc.stdout is None or proc.stdin is None:
        # Both are PIPEd, so this only fires if the spawn's shape changed; it is checked here because the new fd
        # and the child both have to be released.
        os.close(ns_fd)
        await _reap(proc)
        raise LauncherSetupError(f"{file_base}: the launcher came up without its pipes")
    rpc = PiRpc(label=file_base, stdout=proc.stdout, stdin=proc.stdin, on_tool_result=on_tool_result, transcript=transcript)
    member = Member(
        file_base=file_base,
        id=member_id,
        pid=proc.pid,
        pgid=proc.pid,
        namespace=namespace,
        ns_fd=ns_fd,
        proc=proc,
        rpc=rpc,
        ns_records=[namespace],
    )
    member.stderr_task = asyncio.create_task(_drain_stderr(proc, member, transcript), name=f"stderr-{file_base}")
    rpc.start()
    return member


async def _read_report(read_fd: int, proc: asyncio.subprocess.Process) -> tuple[str, object]:
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
            return (NS_REPORT_FAIL, "the launcher never reported (ns report timeout)")
    finally:
        # The timeout and the cancel paths never reach `_ready`, and the caller closes the fd on the
        # way out: a watcher left on a closed fd number answers the next file that inherits the number.
        loop.remove_reader(read_fd)
    text = (data or b"").decode("utf-8", "replace").strip()
    if text.startswith(NS_REPORT_OK):
        try:
            return (NS_REPORT_OK, int(text.split(None, 1)[1]))
        except IndexError, ValueError:
            return (NS_REPORT_FAIL, f"unreadable namespace report {text!r}")
    if text.startswith(NS_REPORT_FAIL):
        return (NS_REPORT_FAIL, text.split(None, 1)[1] if " " in text else "unspecified")
    code = proc.returncode
    return (NS_REPORT_FAIL, text or f"the launcher exited with {code} before reporting")


async def _drain_stderr(proc: asyncio.subprocess.Process, member: Member, transcript) -> None:
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
    except Exception:  # pragma: no cover
        pass


def unknown_model_warning(chunk: bytes) -> str:
    text = chunk.decode("utf-8", "replace")
    for line in text.splitlines():
        if "not found for provider" in line and line.lstrip().startswith("Model"):
            return line.strip()
    return ""
