from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from .config import Council


@dataclass
class MemberRecord:
    file_base: str
    id: str | None
    name: str
    kind: str  # seat | chair
    provider: str | None
    model: str | None
    model_warning: str | None = None
    namespaces: list[int] = field(default_factory=list)
    prompts: int = 0

    def as_dict(self) -> dict:
        return {
            "file_base": self.file_base,
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "model_warning": self.model_warning,
            "namespace": self.namespaces[-1] if self.namespaces else None,
            "namespaces": list(self.namespaces),
            "prompts": self.prompts,
        }


@dataclass
class RunManifest:
    path: str
    data: dict = field(default_factory=dict)
    members: dict[str, MemberRecord] = field(default_factory=dict)

    @property
    def members_of(self) -> dict[str, MemberRecord]:
        return self.members

    def record_prompts(self, counts: dict[str, int]) -> None:
        for file_base, count in (counts or {}).items():
            record = self.members.get(file_base)
            if record is not None:
                record.prompts = int(count)
                self.data.setdefault("members", {})[file_base] = record.as_dict()

    def record_namespace(self, file_base: str, namespace: int) -> None:
        record = self.members.get(file_base)
        if record is not None and namespace not in record.namespaces:
            record.namespaces.append(namespace)
            self.data.setdefault("members", {})[file_base] = record.as_dict()

    def set_model_warning(self, file_base: str, warning: str) -> None:
        record = self.members.get(file_base)
        if record is not None:
            record.model_warning = warning
            self.data.setdefault("members", {})[file_base] = record.as_dict()


def create(path: str, council: Council, *, run_dir: str, runs_dir: str, project: str, orchestrator_pid: int) -> RunManifest:
    members: dict[str, MemberRecord] = {}
    roster = []
    for slot, spec in enumerate(council.agents, 1):
        members[spec.file_base] = MemberRecord(
            file_base=spec.file_base, id=spec.id, name=spec.name, kind="seat", provider=spec.provider, model=spec.model
        )
        roster.append(
            {
                "slot": slot,
                "id": spec.id,
                "name": spec.name,
                "file_base": spec.file_base,
                "model": spec.model or "pi default",
                "provider": spec.provider,
            }
        )
    chair = council.chair
    members[chair.file_base] = MemberRecord(
        file_base=chair.file_base, id=None, name=chair.name, kind="chair", provider=chair.provider, model=chair.model
    )
    data = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config_path": council.config_path,
        "config_layers": list(council.config_layers),
        "council": council.name,
        "extends": list(council.extends),
        "mode": council.mode,
        "goal": council.goal,
        "project": project,
        "run_dir": run_dir,
        "runs_dir": runs_dir,
        "roster": roster,
        "chair": {"name": chair.name, "file_base": chair.file_base, "model": chair.model or "pi default", "provider": chair.provider},
        "settings": {
            "max_rounds": council.max_rounds,
            "grace_rounds": council.grace_rounds,
            "seconding": council.seconding,
            "watchdog_sec": council.watchdog_sec,
            "chair_timeout_sec": council.chair_timeout_sec,
            "max_concurrency": council.max_concurrency,
            "transcripts": council.transcripts,
            "sandbox_mib": council.sandbox_mib,
            "in_place": council.in_place,
            "project_trust": "no-approve" if council.untrusted else "approve",
        },
        "pi_cli": council.pi_cli,
        "pi_version": council.pi_version,
        "pids": {"orchestrator": orchestrator_pid, "supervisor": os.getppid()},
        "members": {base: record.as_dict() for base, record in members.items()},
        "outcome": None,
    }
    manifest = RunManifest(path=path, data=data, members=members)
    _write(manifest)
    return manifest


def finalize(
    manifest: RunManifest,
    *,
    outcome: str,
    journal: list[dict],
    survivor_pids: list[int],
    survivor_ns: list[int],
    rounds: int,
    report: str | None,
    namespace_sweep: str = "namespace",
    **extra,
) -> None:
    manifest.data.update(
        {
            "outcome": outcome,
            "rounds": rounds,
            "report": report,
            "write_journal": journal,
            "namespace_sweep": namespace_sweep,
            "survivor_pids": list(survivor_pids),
            "survivor_ns": list(survivor_ns),
            "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )
    manifest.data.update(extra)
    _write(manifest)


def _write(manifest: RunManifest) -> None:
    # a unique tmp: the hurried exit can write while this write is mid-flight
    tmp = f"{manifest.path}.tmp-{os.getpid()}-{time.time_ns()}"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest.data, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")
    os.replace(tmp, manifest.path)


def read(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
