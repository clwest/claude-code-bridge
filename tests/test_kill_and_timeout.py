"""Tests for cc_kill and the wall-clock timeout.

Covers everything in TASK_cc-kill-and-wall-clock-timeout.md's "Done means"
checklist that can be exercised without a real `claude` CLI. All fake
subprocesses are spawned with `start_new_session=True` — same as
`runner._spawn` — so `os.killpg` signalling never reaches the pytest
process. Any test that forgot that flag would kill the whole test run;
if a new test here is doing more than a couple of things, hold that flag
in your head.
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
    # Best-effort cleanup: any fake children still alive get SIGKILL'd so
    # a failing test doesn't leak processes or wedge the runner.
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
    task_file: Path | None = None,
) -> None:
    """Write a minimal meta.json for a job spawned outside start_run.

    Mirrors the shape start_run produces closely enough that
    _finalize_if_dead, _active_in_cwd and kill_job all work on it.
    """
    if started_ts is None:
        started_ts = time.time()
    if task_file is None:
        task_file = cwd / "TASK_fake.md"
    meta = {
        "job_id": job_id,
        "kind": "run",
        "cwd": str(cwd),
        "task_file": str(task_file),
        "task_file_rel": task_file.name,
        "session_id": str(uuid.uuid4()),
        "cmd": ["fake"],
        "prompt": "fake",
        "started_at": runner._now_iso(),
        "started_ts": started_ts,
        "pid": pid,
        "task_file_size_before": 0,
        "task_file_sha256_before": runner._sha256(b""),
        "budget_usd": "2.00",
        "timeout_s": float(DEFAULT_JOB_TIMEOUT_S),
        "deadline_ts": deadline_ts if deadline_ts is not None else started_ts + DEFAULT_JOB_TIMEOUT_S,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    (run_dir / "pre.txt").write_bytes(b"")


def _spawn_registered(
    cwd: Path,
    script: str,
    *,
    started_ts: float | None = None,
    deadline_ts: float | None = None,
    task_file: Path | None = None,
) -> tuple[str, subprocess.Popen]:
    """Spawn a bash child in a new session and register it as a job.

    `start_new_session=True` is critical — every fake child in this file
    must have it, or `os.killpg` on that pid would reach pytest itself.
    """
    job_id = _new_job_id()
    run_dir = runner.RUNS_DIR / job_id
    run_dir.mkdir()
    stdout_f = (run_dir / "stdout.log").open("wb")
    stderr_f = (run_dir / "stderr.log").open("wb")
    proc = subprocess.Popen(
        ["/bin/bash", "-c", script],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=stdout_f,
        stderr=stderr_f,
        start_new_session=True,
    )
    runner._LIVE_POPENS[job_id] = proc
    _write_meta(
        job_id,
        cwd,
        run_dir,
        proc.pid,
        started_ts=started_ts,
        deadline_ts=deadline_ts,
        task_file=task_file,
    )
    return job_id, proc


def _stub_spawn(monkeypatch):
    def _fake(cmd, cwd, run_dir, prompt, job_id):
        (run_dir / "stdout.log").write_bytes(b"")
        (run_dir / "stderr.log").write_bytes(b"")
        return 1  # pid 1 is always alive
    monkeypatch.setattr(runner, "_spawn", _fake)


def _make_task_file(cwd: Path) -> Path:
    tf = cwd / "TASK_x.md"
    tf.write_text("---\nstatus: not started\n---\n")
    return tf


# ------- kill_job: kills only the recorded pid, plain-words return -------


def test_kill_running_job_marks_it_killed_and_frees_cwd(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    job_id, proc = _spawn_registered(cwd, "sleep 60")

    result = runner.kill_job(job_id)

    assert result["job_id"] == job_id
    assert result["pid"] == proc.pid
    assert result["signal"] in ("SIGTERM", "SIGKILL")
    assert result["cwd_free"] is True

    meta = json.loads((runner.RUNS_DIR / job_id / "meta.json").read_text())
    assert meta["ended_reason"] == "killed"
    assert meta["killed_signal"] == result["signal"]
    assert meta.get("ended_at")

    proc.wait(timeout=5)
    assert not runner._pid_alive(proc.pid)


def test_kill_refuses_already_ended_job(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    job_id, proc = _spawn_registered(cwd, "sleep 60")
    runner.kill_job(job_id)  # ends it
    proc.wait(timeout=5)

    result = runner.kill_job(job_id)
    assert result.get("already_ended") is True, (
        f"second kill_job on an ended job should short-circuit, got: {result}"
    )
    assert result.get("ended_reason") == "killed"


def test_kill_on_already_dead_pid_finalizes_and_reports(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # A wrapper that exits immediately, WITHOUT writing an exit_code file
    # or having its Popen recorded — mimics the "wrapper cleaned up out
    # from under us" case _finalize_if_dead handles.
    job_id, proc = _spawn_registered(cwd, "true")
    proc.wait(timeout=5)
    runner._LIVE_POPENS.pop(job_id, None)  # simulate cross-process forget

    result = runner.kill_job(job_id)
    assert result.get("already_dead") is True
    # The job has been finalized either as "finished" (via the exit_code
    # file, if the bash wrapper wrote one — but we spawned it without
    # that wrapper here so it did not) or as "finished" with exit
    # code "unknown". Either way ended_at is set now.
    meta = json.loads((runner.RUNS_DIR / job_id / "meta.json").read_text())
    assert meta.get("ended_at")


def test_cc_run_in_same_cwd_succeeds_after_kill(tmp_path, monkeypatch):
    """Done-means item: assert cc_run succeeds, not merely that the lock cleared."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    tf = _make_task_file(cwd)

    victim_id, victim_proc = _spawn_registered(cwd, "sleep 60")
    runner.kill_job(victim_id)
    victim_proc.wait(timeout=5)

    _stub_spawn(monkeypatch)
    result = runner.start_run(str(tf), str(cwd))
    assert result["job_id"] != victim_id
    # kill_job already flipped the victim to ended, so no reap here.
    assert result["reaped"] == []


def test_kill_uses_sigkill_when_process_ignores_sigterm(tmp_path, monkeypatch):
    """SIGTERM first, SIGKILL after grace — the escalation path for a stubborn child.

    The trap on SIGTERM in the child means the SIGTERM the runner sends
    first is caught and ignored; the runner has to escalate to SIGKILL
    for the pid to actually die. Grace period is monkeypatched down to
    keep the test under a second.
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr(runner, "_KILL_GRACE_S", 0.3)

    stubborn = "trap '' TERM; while true; do sleep 0.1; done"
    job_id, proc = _spawn_registered(cwd, stubborn)
    # Give the trap a moment to install before we signal.
    time.sleep(0.1)

    result = runner.kill_job(job_id)
    assert result["signal"] == "SIGKILL", (
        "a SIGTERM-trapping child must be escalated to SIGKILL after the grace period"
    )
    proc.wait(timeout=5)
    assert not runner._pid_alive(proc.pid)


# ------- ended_reason surfaces in cc_status and cc_result ----------------


def test_cc_status_shows_ended_reason_killed(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "TASK_fake.md").write_text("---\nstatus: not started\n---\n")
    job_id, proc = _spawn_registered(cwd, "sleep 60")
    runner.kill_job(job_id)
    proc.wait(timeout=5)

    out = server.cc_status(job_id)
    assert "state: killed" in out
    assert "ended_reason: killed" in out


def test_cc_result_shows_ended_reason_killed(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "TASK_fake.md").write_text("---\nstatus: not started\n---\n")
    job_id, proc = _spawn_registered(cwd, "sleep 60")
    runner.kill_job(job_id)
    proc.wait(timeout=5)

    out = server.cc_result(job_id)
    assert "ended_reason: killed" in out


# ------- wall-clock timeout: lazy reap on next touch ---------------------


def test_past_deadline_running_job_is_reaped_on_load(tmp_path):
    """A long sleeper whose deadline has already passed gets killed on load_job."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    past = time.time() - 60
    job_id, proc = _spawn_registered(
        cwd,
        "sleep 60",
        started_ts=past - 3600,
        deadline_ts=past,
    )

    view = runner.load_job(job_id)
    assert view.meta.get("ended_at"), (
        "load_job → _finalize_if_dead should have reaped the deadline-past job"
    )
    assert view.meta["ended_reason"] == "timeout"
    proc.wait(timeout=5)
    assert not runner._pid_alive(proc.pid)


def test_blocked_cc_run_reaps_stale_job_and_proceeds(tmp_path, monkeypatch):
    """Part 2 promise: 'the next run unblocks itself' and says so."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    tf = _make_task_file(cwd)

    past = time.time() - 60
    stale_id, stale_proc = _spawn_registered(
        cwd,
        "sleep 60",
        started_ts=past - 3600,
        deadline_ts=past,
    )

    _stub_spawn(monkeypatch)
    result = runner.start_run(str(tf), str(cwd))

    assert result["job_id"] != stale_id
    assert any(
        r["job_id"] == stale_id and r["reason"] == "timeout"
        for r in result["reaped"]
    ), f"stale job should appear in reaped list, got: {result['reaped']}"

    # The wrapper is actually dead now.
    stale_proc.wait(timeout=5)


def test_cc_run_tool_return_names_reaped_job(tmp_path, monkeypatch):
    """The server tool's return must say 'Reaped stale job X (timeout)'."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    tf = _make_task_file(cwd)

    past = time.time() - 60
    stale_id, stale_proc = _spawn_registered(
        cwd,
        "sleep 60",
        started_ts=past - 3600,
        deadline_ts=past,
    )

    _stub_spawn(monkeypatch)
    out = server.cc_run(str(tf), str(cwd))
    assert f"Reaped stale job {stale_id}" in out
    assert "timeout" in out
    assert "Started job" in out
    stale_proc.wait(timeout=5)


def test_default_timeout_is_45_minutes():
    assert DEFAULT_JOB_TIMEOUT_S == 45 * 60


def test_start_run_records_deadline(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    tf = _make_task_file(cwd)
    _stub_spawn(monkeypatch)

    result = runner.start_run(str(tf), str(cwd))
    meta = json.loads((runner.RUNS_DIR / result["job_id"] / "meta.json").read_text())
    assert meta["timeout_s"] == float(DEFAULT_JOB_TIMEOUT_S)
    assert meta["deadline_ts"] - meta["started_ts"] == pytest.approx(float(DEFAULT_JOB_TIMEOUT_S))


def test_start_run_accepts_timeout_override(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    tf = _make_task_file(cwd)
    _stub_spawn(monkeypatch)

    result = runner.start_run(str(tf), str(cwd), timeout_s=120)
    meta = json.loads((runner.RUNS_DIR / result["job_id"] / "meta.json").read_text())
    assert meta["timeout_s"] == 120.0


@pytest.mark.parametrize("bad", [0, -1, "abc", "NaN"])
def test_timeout_validation_rejects_bad(bad):
    with pytest.raises(runner.BridgeError):
        runner._validate_timeout(bad)


def test_timeout_above_ceiling_rejected():
    with pytest.raises(runner.BridgeError):
        runner._validate_timeout(9 * 60 * 60)  # above 8h ceiling


# ------- Part 3: cc_status shows cap-hit line for ended capped job -------


def test_cc_status_shows_cap_hit_for_ended_capped_job(tmp_path):
    """The gap on 2026-09-04: cc_status did not carry the cap signal."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "TASK_fake.md").write_text("---\nstatus: not started\n---\n")
    # A wrapper that exits fast; we then fake the cap-hit output.
    job_id, proc = _spawn_registered(cwd, "true")
    proc.wait(timeout=5)
    # Overwrite the empty stderr with the cap-hit message.
    (runner.RUNS_DIR / job_id / "stderr.log").write_bytes(
        b"Error: Exceeded USD budget (2)\n"
    )
    # Let load_job finalize it.
    runner._LIVE_POPENS.pop(job_id, None)

    out = server.cc_status(job_id)
    assert "BUDGET: CAP HIT" in out, (
        "cc_status must show the cap-hit line when the job hit its cap "
        "(TASK_cc-kill-and-wall-clock-timeout.md Part 3)"
    )


# ------- kill_job return format ------------------------------------------


def test_cc_kill_return_string_names_job_pid_signal_and_cwd(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "TASK_fake.md").write_text("---\nstatus: not started\n---\n")
    job_id, proc = _spawn_registered(cwd, "sleep 60")

    out = server.cc_kill(job_id)
    assert job_id in out
    assert str(proc.pid) in out
    assert "SIGTERM" in out or "SIGKILL" in out
    assert "cwd is free" in out
    proc.wait(timeout=5)


def test_cc_kill_says_already_ended_when_it_is(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "TASK_fake.md").write_text("---\nstatus: not started\n---\n")
    job_id, proc = _spawn_registered(cwd, "sleep 60")
    runner.kill_job(job_id)
    proc.wait(timeout=5)

    out = server.cc_kill(job_id)
    assert "already ended" in out.lower()
