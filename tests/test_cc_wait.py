"""Tests for cc_wait — the blocking-until-end tool added 2026-09-04.

Every fake child in this file is spawned with `start_new_session=True`
so signal cleanup from a leaked test cannot reach the pytest process
(same shape as `test_kill_and_timeout.py`). If a new test does more
than a couple of things, hold that flag in your head.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from claude_code_bridge import runner, server
from claude_code_bridge.config import DEFAULT_JOB_TIMEOUT_S


@pytest.fixture(autouse=True)
def _isolated_runs_dir(monkeypatch, tmp_path):
    runs = tmp_path / "cc-runs"
    runs.mkdir()
    monkeypatch.setattr(runner, "RUNS_DIR", runs)
    monkeypatch.setattr(runner, "WORKSPACE_ROOT", tmp_path)
    runner._LIVE_POPENS.clear()
    yield
    for _job_id, proc in list(runner._LIVE_POPENS.items()):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    runner._LIVE_POPENS.clear()


def _new_job_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def _write_meta(
    job_id: str,
    cwd: Path,
    run_dir: Path,
    pid: int,
    *,
    started_ts: float | None = None,
    deadline_ts: float | None = None,
    timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
) -> None:
    if started_ts is None:
        started_ts = time.time()
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
        "started_ts": started_ts,
        "pid": pid,
        "task_file_size_before": 0,
        "task_file_sha256_before": runner._sha256(b""),
        "budget_usd": "2.00",
        "timeout_s": float(timeout_s),
        "deadline_ts": (
            deadline_ts
            if deadline_ts is not None
            else started_ts + float(timeout_s)
        ),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    (run_dir / "pre.txt").write_bytes(b"")


def _spawn_fake_job(
    cwd: Path,
    script: str,
    *,
    timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
) -> tuple[str, subprocess.Popen]:
    """Spawn a bash child in a new session and register it as a job.

    Mirrors the runner's own wrapper: the script writes `exit_code`
    before it exits, so `_finalize_if_dead` can pick the job up cleanly.
    """
    job_id = _new_job_id()
    run_dir = runner.RUNS_DIR / job_id
    run_dir.mkdir()
    exit_path = run_dir / "exit_code"
    wrapped = f"{script}; echo $? > {exit_path}"
    stdout_f = (run_dir / "stdout.log").open("wb")
    stderr_f = (run_dir / "stderr.log").open("wb")
    proc = subprocess.Popen(
        ["/bin/bash", "-c", wrapped],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=stdout_f,
        stderr=stderr_f,
        start_new_session=True,
    )
    runner._LIVE_POPENS[job_id] = proc
    _write_meta(job_id, cwd, run_dir, proc.pid, timeout_s=timeout_s)
    return job_id, proc


def _make_task_file(cwd: Path) -> Path:
    (cwd / "TASK_fake.md").write_text("---\nstatus: not started\n---\n")
    return cwd / "TASK_fake.md"


# ------- Part 1: cc_wait returns on job end, timeout, and already-done ---


def test_wait_returns_when_job_ends(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _make_task_file(cwd)
    # A short job — sleep 0.3s then exit — so the wait actually
    # observes an end, not a pre-check.
    job_id, proc = _spawn_fake_job(cwd, "sleep 0.3; true")

    started = time.time()
    view, timed_out = runner.wait_for_job(job_id, timeout_s=10)
    elapsed = time.time() - started

    assert timed_out is False, "the wait must not report timeout when the job ended"
    assert view.meta.get("ended_at"), "the returned view must show the job ended"
    assert not runner.is_running(view)
    assert elapsed < 5.0, "wait must return promptly after the job ends"
    proc.wait(timeout=5)


def test_wait_returns_immediately_for_already_finished_job(tmp_path):
    """No initial sleep — the returned view must be prompt.

    Task file: "If the job is already finished, return immediately —
    do not sleep first."
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _make_task_file(cwd)
    job_id, proc = _spawn_fake_job(cwd, "true")
    proc.wait(timeout=5)
    # Let _finalize_if_dead see the exit_code file.
    runner.load_job(job_id)

    started = time.time()
    view, timed_out = runner.wait_for_job(job_id, timeout_s=60)
    elapsed = time.time() - started

    assert timed_out is False
    assert view.meta.get("ended_at")
    assert elapsed < 0.5, (
        f"already-finished job returned in {elapsed:.3f}s; "
        "cc_wait must not sleep before checking"
    )


def test_wait_returns_on_own_timeout_while_job_still_runs(tmp_path):
    """A wait shorter than the job runtime returns with `timed_out=True`.

    The job is not touched — it keeps running under its own deadline.
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _make_task_file(cwd)
    # A 30s job; we wait only 1s, which must give us back
    # `timed_out=True` and leave the job alive.
    job_id, proc = _spawn_fake_job(cwd, "sleep 30")
    original_deadline = json.loads(
        (runner.RUNS_DIR / job_id / "meta.json").read_text()
    )["deadline_ts"]

    started = time.time()
    view, timed_out = runner.wait_for_job(job_id, timeout_s=1.0)
    elapsed = time.time() - started

    assert timed_out is True, "the wait must report its own timeout"
    assert runner.is_running(view), (
        "the job must still be running after a wait timeout"
    )
    assert 0.8 < elapsed < 3.5, f"wait honored roughly its 1s cap, took {elapsed:.2f}s"

    # The job's deadline_ts is untouched — the wait must not touch it.
    fresh_deadline = json.loads(
        (runner.RUNS_DIR / job_id / "meta.json").read_text()
    )["deadline_ts"]
    assert fresh_deadline == original_deadline, (
        "wait_for_job must never extend or shorten the job's wall-clock ceiling"
    )

    # Cleanup: kill the sleeper so the test does not leak the 30s child.
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)


# ------- Wait timeout is capped at the job timeout ----------------------


def test_wait_timeout_capped_by_job_timeout(tmp_path):
    """Requesting a longer wait than the job could possibly run gets capped."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _make_task_file(cwd)
    # Job's own timeout is 2s. Ask to wait 60s; the effective wait must
    # be at most the job's timeout — waiting longer than the job could
    # run is pointless.
    job_id, proc = _spawn_fake_job(cwd, "sleep 0.1; true", timeout_s=2)
    started = time.time()
    _view, timed_out = runner.wait_for_job(job_id, timeout_s=60)
    elapsed = time.time() - started
    assert timed_out is False
    assert elapsed < 5.0
    proc.wait(timeout=5)


# ------- Validation matches the shape of _validate_budget / _validate_timeout


@pytest.mark.parametrize("bad", [0, -1, "abc", "NaN"])
def test_wait_rejects_bad_timeout(bad, tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _make_task_file(cwd)
    job_id, proc = _spawn_fake_job(cwd, "true")
    proc.wait(timeout=5)

    with pytest.raises(runner.BridgeError):
        runner.wait_for_job(job_id, timeout_s=bad)


# ------- The server-tool wrapper phrases the timeout as not-an-error ----


def test_cc_wait_tool_wraps_status_when_job_ends(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _make_task_file(cwd)
    job_id, proc = _spawn_fake_job(cwd, "sleep 0.2; true")

    out = server.cc_wait(job_id, timeout_s=10)
    assert "wait: timed out" not in out, (
        f"job ended but cc_wait falsely reported a wait timeout: {out}"
    )
    assert "state: finished" in out
    assert f"job_id: {job_id}" in out
    proc.wait(timeout=5)


def test_cc_wait_tool_phrases_wait_timeout_as_not_error(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _make_task_file(cwd)
    job_id, proc = _spawn_fake_job(cwd, "sleep 30")

    out = server.cc_wait(job_id, timeout_s=1.0)
    # The prefix carries the "not an error" wording; the wrapper does
    # NOT say the word "error" here, per the task spec: "must not be
    # phrased as [an error]".
    assert "wait: timed out (job still running)" in out
    assert "state: running" in out
    lowered = out.lower()
    assert "error" not in lowered.split("bridge error:")[0], (
        "cc_wait timeout must not be phrased as an error"
    )

    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)


def test_cc_wait_tool_returns_immediately_for_finished_job(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _make_task_file(cwd)
    job_id, proc = _spawn_fake_job(cwd, "true")
    proc.wait(timeout=5)
    runner.load_job(job_id)  # finalize

    started = time.time()
    out = server.cc_wait(job_id, timeout_s=60)
    elapsed = time.time() - started
    assert "state: finished" in out
    assert "wait: timed out" not in out
    assert elapsed < 0.5, f"already-finished job took {elapsed:.3f}s"


def test_cc_wait_tool_rejects_unknown_job():
    out = server.cc_wait("no-such-job")
    assert "Bridge error" in out
    assert "unknown job_id" in out
