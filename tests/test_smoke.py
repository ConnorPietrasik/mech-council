"""§13.12 — the only family that spends tokens.

Deselected by default (`-m 'not slow'`); run it after a pi upgrade with `-m slow`.
Everything here goes through the real `pi`, the real launcher and the real sandbox, which
is the point: the offline families prove the protocol, this one proves the wiring.

§13.12 says it in first person for a reason — run from inside a council agent the nested
guard (§3.5) refuses, so the smoke passes `--allow-nested`.  Without that flag the smoke
fails for the wrong reason and says nothing about pi.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.support.ground import GROUND_SKIP_PHRASE, SKIP_MARKERS

TOY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "testdata", "toy-project")
RESULTS = ("consensus", "needs_human", "stalemate", "round_limit_reached")

#: only the token-spending case carries `slow`; the two wiring checks below are free.


def _run(argv: list[str], cwd: str, env: dict, timeout: int) -> subprocess.CompletedProcess:
    process = subprocess.Popen([sys.executable, "-m", "council", *argv], cwd=cwd, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        raise AssertionError("the real-pi smoke hung; the watchdog should have ended it")
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


@pytest.mark.slow
@pytest.mark.timeout(0)
def test_a_real_council_reviews_the_toy_project_and_exits_zero(tmp_path):
    """§13.12: planted bugs, 2 seats + a chair, 2 rounds, transcripts off → exit 0."""
    runs = str(tmp_path / "runs")
    env = dict(os.environ, COUNCIL_RUNS_DIR=runs)
    result = _run(["review", "--project", TOY, "--allow-nested", "--rounds", "2"],
                  cwd=TOY, env=env, timeout=2400)
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stderr[-2000:]}"

    run_dirs = [name for name in os.listdir(runs) if name != "sandbox"]
    assert len(run_dirs) == 1, run_dirs
    run_dir = os.path.join(runs, run_dirs[0])
    manifest = json.load(open(os.path.join(run_dir, "run.json"), encoding="utf-8"))
    assert manifest["outcome"] in RESULTS, manifest["outcome"]
    assert manifest["rounds"] >= 1
    report = open(os.path.join(run_dir, "report.md"), encoding="utf-8").read()
    for heading in ("## Consensus findings", "## Never vetted", "## Round-by-round verdicts",
                    "## Artifacts"):
        assert heading in report, f"{heading} missing from a real report"
    assert "src/cache.py" in report, "a real council read the real files"
    for base, record in manifest["members"].items():
        assert record["namespaces"], f"{base} recorded no namespace"
        assert record["prompts"] >= 1, f"{base} spent no prompts"
    assert manifest["survivor_pids"] == [], "a real run left a survivor behind"


@pytest.mark.parametrize("council", ["review"])
def test_the_smoke_needs_the_nested_flag_when_it_runs_inside_an_agent(council):
    """§13.12's own method claim: without `--allow-nested` §3.5 refuses, and says so."""
    marker = {"COUNCIL_RUN_DIR": "/tmp/not-a-real-run-dir",
              "COUNCIL_RUN_PATH": "/tmp/not-a-real-run-dir"}
    env = dict(os.environ, **marker, COUNCIL_RUNS_DIR="/tmp/council-smoke-guard")
    result = _run([council, "--project", TOY], cwd=TOY, env=env, timeout=120)
    # §3.2: a refusal to convene is a setup failure (1), not a usage error (2) — the flags
    # were well-formed, and what the run refused to do was start.
    assert result.returncode == 1, result.stderr[-400:]
    assert "--allow-nested" in result.stderr, "the refusal must name the way out"
    assert not os.path.exists("/tmp/council-smoke-guard"), "a refusal creates nothing"


def test_the_sandbox_family_is_never_silently_empty():
    """§13.12: the harness rule is that §13.9 *ran*, not that it exited 0.

    A collection count is the honest form of that claim, and it is checked here rather
    than by running the suite from inside a member: that would run this module again,
    inside a member, which would want to run the suite again.  What can be observed
    without recursion is the failure the rule guards against — a §13.9 family skipped
    because the fixture ground turned out to be an overlay, which exits 0 while proving
    nothing.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    listing = subprocess.run([sys.executable, "-m", "pytest", "tests/test_sandbox.py",
                              "--collect-only", "-q", "-p", "no:cacheprovider"],
                             cwd=os.path.dirname(here), capture_output=True, text=True,
                             timeout=120)
    collected = [line for line in listing.stdout.splitlines() if "::" in line]
    assert len(collected) >= 12, f"§13.9 collected {len(collected)} cases"


def test_the_ground_skip_is_the_only_way_this_suite_proves_nothing():
    """§13.9: "a test skipped for any other reason fails the suite."

    Checked statically because the honest runtime version — run everything and count
    skips — is the suite itself.  One skip exists: §13.9's fstype proxy in the sandbox
    family.  Anything else that skips would pass the suite while testing nothing, which
    is the exact failure mode §13.9 names.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    offenders = []
    for name in sorted(os.listdir(here)):
        if not name.startswith("test_"):
            continue
        with open(os.path.join(here, name), encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                stripped = line.strip()
                if not any(marker in stripped for marker in SKIP_MARKERS):
                    continue
                if name == "test_sandbox.py" and GROUND_SKIP_PHRASE in stripped:
                    continue
                offenders.append(f"{name}:{number}: {stripped[:90]}")
    assert not offenders, "extra skip paths:\n" + "\n".join(offenders)
