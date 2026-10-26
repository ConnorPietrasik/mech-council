"""The only family that spends tokens."""

from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys

import pytest

from tests.support.ground import FIXTURE_ROOT, GROUND_SKIP_PHRASE, SKIP_MARKERS, fstype_under

TOY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata", "toy-project")
RESULTS = ("consensus", "needs_human", "stalemate", "round_limit_reached")

#: only the token-spending case carries `slow`; every other check in this file is free.


def _run(argv: list[str], cwd: str, env: dict, timeout: int) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        [sys.executable, "-m", "mech_council", *argv],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        raise AssertionError("the real-pi smoke hung; the watchdog should have ended it")
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


@pytest.mark.slow
@pytest.mark.timeout(0)
def test_a_real_council_reviews_the_toy_project_and_exits_zero(tmp_path):
    """Planted bugs, 2 seats + a chair, 2 rounds, transcripts off → exit 0."""
    runs = str(tmp_path / "runs")
    env = dict(os.environ, COUNCIL_RUNS_DIR=runs)
    result = _run(["review", "--project", TOY, "--allow-nested", "--rounds", "2"], cwd=TOY, env=env, timeout=2400)
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stderr[-2000:]}"

    run_dirs = [name for name in os.listdir(runs) if name != "sandbox"]
    assert len(run_dirs) == 1, run_dirs
    run_dir = os.path.join(runs, run_dirs[0])
    with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest["outcome"] in RESULTS, manifest["outcome"]
    assert manifest["rounds"] >= 1
    with open(os.path.join(run_dir, "report.md"), encoding="utf-8") as handle:
        report = handle.read()
    for heading in ("## Consensus findings", "## Never vetted", "## Round-by-round verdicts", "## Artifacts"):
        assert heading in report, f"{heading} missing from a real report"
    assert "src/cache.py" in report, "a real council read the real files"
    for base, record in manifest["members"].items():
        assert record["namespaces"], f"{base} recorded no namespace"
        assert record["prompts"] >= 1, f"{base} spent no prompts"
    assert manifest["survivor_pids"] == [], "a real run left a survivor behind"


def test_the_smoke_needs_the_nested_flag_when_it_runs_inside_an_agent():
    """The smoke test's own method claim: without `--allow-nested` the nested guard refuses, and says so."""
    marker = {"COUNCIL_RUN_DIR": "/tmp/not-a-real-run-dir", "COUNCIL_RUN_PATH": "/tmp/not-a-real-run-dir"}
    env = dict(os.environ, **marker, COUNCIL_RUNS_DIR="/tmp/council-smoke-guard")
    result = _run(["review", "--project", TOY], cwd=TOY, env=env, timeout=120)
    # Well-formed flags refused at startup: a setup failure (1), not a usage error (2).
    assert result.returncode == 1, result.stderr[-400:]
    assert "--allow-nested" in result.stderr, "the refusal must name the way out"
    assert not os.path.exists("/tmp/council-smoke-guard"), "a refusal creates nothing"


def test_the_sandbox_family_is_never_silently_empty():
    """The harness rule is that the sandbox family *ran*, not that it exited 0 — and the ground skip is the one
    sanctioned way it may not have, so this says nothing about a run on ground that cannot hold a lower."""
    here = os.path.dirname(os.path.abspath(__file__))
    listing = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_sandbox.py", "--collect-only", "-q", "-rs", "-p", "no:cacheprovider"],
        cwd=os.path.dirname(here),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    collected = [line for line in listing.stdout.splitlines() if "::" in line]
    if len(collected) >= 12:
        return
    ground = fstype_under(FIXTURE_ROOT)
    said_the_ground = GROUND_SKIP_PHRASE in listing.stdout + listing.stderr
    assert said_the_ground and ground == "overlay", (
        f"the sandbox family collected {len(collected)} cases on {ground} ground ({FIXTURE_ROOT}): "
        f"only {GROUND_SKIP_PHRASE} excuses it, and the family said otherwise\n{(listing.stdout + listing.stderr)[-600:]}"
    )


def _skip_candidates(path: str) -> list[tuple[int, str]]:
    """Every call and decorator in a file, with the source text it spans."""
    with open(path, encoding="utf-8") as handle:
        src = handle.read()
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(src)):
        candidates = [node] if isinstance(node, ast.Call) else []
        candidates += list(getattr(node, "decorator_list", []))
        found.extend((getattr(c, "lineno", 0), ast.get_source_segment(src, c) or "") for c in candidates)
    return found


def test_the_ground_skip_is_the_only_way_this_suite_proves_nothing():
    """A test skipped for any other reason fails the suite."""
    here = os.path.dirname(os.path.abspath(__file__))
    offenders = []
    for name in sorted(os.listdir(here)):
        if not name.startswith("test_"):
            continue
        for number, span in _skip_candidates(os.path.join(here, name)):
            if not any(marker in span for marker in SKIP_MARKERS):
                continue
            if name == "test_sandbox.py" and GROUND_SKIP_PHRASE in span:
                continue
            offenders.append(f"{name}:{number}: {' '.join(span.split())[:90]}")
    assert not offenders, "extra skip paths:\n" + "\n".join(offenders)
