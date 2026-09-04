"""Regression test for the 2026-09-02 zombie-liveness bug.

Cowork's first MCP-side call to Test 1 found: the bash wrapper exited,
wrote its `exit_code` file, and became a zombie in the MCP server's
process table. `os.kill(pid, 0)` returns success on a zombie, so the
old `_finalize_if_dead` — which checked the pid before the file — kept
reporting `state: running` forever, held the one-run-per-cwd lock open
indefinitely, and made `cc_result` unreachable.

This test spawns a bash wrapper that mimics `_spawn` (writes an
`exit_code` file, exits immediately) directly, without calling `claude`,
and asserts:

  1. Within one poll, `_finalize_if_dead` marks the job ended.
  2. The recorded exit_code matches the file's contents.
  3. `is_running` on the returned view is False.
  4. `_active_in_cwd` no longer thinks the cwd is locked.
  5. `_LIVE_POPENS` is empty (zombie was reaped, not left <defunct>).

Runs on any machine with `bash` — no `claude` CLI required.
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from claude_code_bridge import runner


@pytest.fixture(autouse=True)
def _isolated_runs_dir(monkeypatch, tmp_path):
    """Point RUNS_DIR at a per-test tmp dir so tests don't collide."""
    runs = tmp_path / "cc-runs"
    runs.mkdir()
    monkeypatch.setattr(runner, "RUNS_DIR", runs)
    # Fresh popen dict so a previous test's entries don't leak in.
    runner._LIVE_POPENS.clear()
    yield
    runner._LIVE_POPENS.clear()


def _spawn_fake_job(cwd: Path, exit_code: int = 0, delay: float = 0.05) -> tuple[str, Path]:
    """Create a fake job dir with a bash wrapper that exits fast.

    Mirrors the shape of `runner._spawn` closely enough to exercise
    the same `_finalize_if_dead` path — a `bash -c` child that writes
    `exit_code` and returns immediately.
    """
    job_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    run_dir = runner.RUNS_DIR / job_id
    run_dir.mkdir()
    exit_path = run_dir / "exit_code"
    shell_line = f"sleep {delay}; echo {exit_code} > {exit_path}; exit {exit_code}"

    stdout_f = (run_dir / "stdout.log").open("wb")
    stderr_f = (run_dir / "stderr.log").open("wb")
    proc = subprocess.Popen(
        ["/bin/bash", "-c", shell_line],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=stdout_f,
        stderr=stderr_f,
        start_new_session=True,
    )
    runner._LIVE_POPENS[job_id] = proc

    meta = {
        "job_id": job_id,
        "kind": "run",
        "cwd": str(cwd),
        "task_file": str(cwd / "TASK_fake.md"),
        "task_file_rel": "TASK_fake.md",
        "session_id": str(uuid.uuid4()),
        "cmd": ["fake"],
        "prompt": "fake",
        "started_at": runner._now_iso(),
        "started_ts": time.time(),
        "pid": proc.pid,
        "task_file_size_before": 0,
        "task_file_sha256_before": runner._sha256(b""),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    (run_dir / "pre.txt").write_bytes(b"")
    return job_id, run_dir


def _wait_for_exit_file(run_dir: Path, timeout: float = 5.0) -> None:
    exit_path = run_dir / "exit_code"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if exit_path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"exit_code file never appeared under {run_dir}")


def test_finished_job_reports_finished_within_one_poll(tmp_path):
    """The bug: even after exit_code was written, cc_status kept saying running."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    job_id, run_dir = _spawn_fake_job(cwd, exit_code=0)
    _wait_for_exit_file(run_dir)

    # One poll — no busy-loop, no sleep. If `_finalize_if_dead` still
    # short-circuits on the zombie pid, this fails: view.meta.ended_at
    # is missing and is_running is True.
    view = runner.load_job(job_id)

    assert view.meta.get("ended_at"), (
        "job with exit_code file on disk was still reported as running "
        "(the 2026-09-02 zombie-liveness regression)"
    )
    assert view.meta["exit_code"] == 0
    assert runner.is_running(view) is False


def test_nonzero_exit_captured(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    job_id, run_dir = _spawn_fake_job(cwd, exit_code=7)
    _wait_for_exit_file(run_dir)
    view = runner.load_job(job_id)
    assert view.meta["exit_code"] == 7


def test_active_cwd_lock_releases(tmp_path):
    """The concurrency guard used to hold the cwd forever after a job died."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    job_id, run_dir = _spawn_fake_job(cwd, exit_code=0)
    _wait_for_exit_file(run_dir)

    # First call reaps and finalizes.
    active, _reaped = runner._active_in_cwd(cwd)
    assert active is None, (
        f"cwd {cwd} still locked after job {job_id} finished — "
        "one-run-per-cwd would refuse subsequent runs forever"
    )


def test_popen_dict_drained(tmp_path):
    """Zombie hygiene: the Popen we recorded gets reaped, not left <defunct>."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    job_id, run_dir = _spawn_fake_job(cwd, exit_code=0)
    _wait_for_exit_file(run_dir)
    runner.load_job(job_id)
    assert job_id not in runner._LIVE_POPENS


def test_running_job_still_reports_running(tmp_path):
    """Sanity check the other direction: don't call a live process finished."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # A slow "job" — 2 seconds — that hasn't written exit_code yet.
    job_id, run_dir = _spawn_fake_job(cwd, exit_code=0, delay=2.0)
    # Poll immediately, before the sleep completes.
    view = runner.load_job(job_id)
    assert runner.is_running(view), (
        "a job whose wrapper is still sleeping was reported as finished"
    )
    # Cleanup — wait for it to complete so subprocess reaper doesn't yell.
    _wait_for_exit_file(run_dir, timeout=5.0)
    runner.load_job(job_id)  # reap
