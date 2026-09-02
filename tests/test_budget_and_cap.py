"""Tests for the round-1 follow-ups: budget-as-argument and cap-visibility.

Covers the six cases named in TASK_bridge-followups-round-1.md Item 5.
No `claude` CLI required — same as test_runner_liveness.py, everything
that would touch a real CC session is either a pure-function check
(_validate_budget, _cc_command, cap_hit) or a start_run/start_ask
call with _spawn monkeypatched to a stub.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from claude_code_bridge import runner
from claude_code_bridge.config import MAX_BUDGET_CEILING_USD, MAX_BUDGET_USD


@pytest.fixture(autouse=True)
def _isolated_runs_dir(monkeypatch, tmp_path):
    runs = tmp_path / "cc-runs"
    runs.mkdir()
    monkeypatch.setattr(runner, "RUNS_DIR", runs)
    monkeypatch.setattr(runner, "WORKSPACE_ROOT", tmp_path)
    runner._LIVE_POPENS.clear()
    yield
    runner._LIVE_POPENS.clear()


@pytest.fixture
def stub_spawn(monkeypatch):
    """Replace _spawn with a no-op that returns a fake pid.

    start_run/start_ask both call _spawn; we do not want a real bash
    child (and definitely not a real `claude` process) in tests. The
    fake pid points at the test process itself, which is guaranteed
    alive — meaning _finalize_if_dead will leave the job as "running",
    which is fine because no test here asserts on job liveness.
    """
    def _fake(cmd, cwd, run_dir, prompt, job_id):
        (run_dir / "stdout.log").write_bytes(b"")
        (run_dir / "stderr.log").write_bytes(b"")
        return 1  # pid 1 is always alive
    monkeypatch.setattr(runner, "_spawn", _fake)


# ------- _validate_budget -------------------------------------------------

def test_budget_above_ceiling_raises_and_names_ceiling():
    ceiling = float(MAX_BUDGET_CEILING_USD)
    with pytest.raises(runner.BridgeError) as ei:
        runner._validate_budget(ceiling + 0.01)
    msg = str(ei.value)
    assert f"{ceiling:.2f}" in msg, f"error must name the ceiling, got: {msg}"


@pytest.mark.parametrize("bad", [0, 0.0, -1, -0.01, "nope", "", "abc", "NaN"])
def test_budget_zero_negative_or_non_numeric_raises(bad):
    with pytest.raises(runner.BridgeError):
        runner._validate_budget(bad)


# ------- _cc_command carries the budget ----------------------------------

def test_valid_budget_lands_after_max_budget_usd_flag():
    argv = runner._cc_command("session-x", resume=False, budget_usd="3.50")
    i = argv.index("--max-budget-usd")
    assert argv[i + 1] == "3.50"


def test_default_budget_is_two_dollars_when_none_given(tmp_path, stub_spawn):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    tf = cwd / "TASK_x.md"
    tf.write_text("---\nstatus: not started\n---\n")

    job_id = runner.start_run(str(tf), str(cwd))
    meta = json.loads((runner.RUNS_DIR / job_id / "meta.json").read_text())

    assert meta["budget_usd"] == MAX_BUDGET_USD == "2.00"
    argv = meta["cmd"]
    i = argv.index("--max-budget-usd")
    assert argv[i + 1] == "2.00"


# ------- start_ask inheritance -------------------------------------------

def _fabricate_prev_job(cwd: Path, budget: str | None) -> str:
    """Write a meta.json for a prior job. budget=None emulates a pre-change job."""
    job_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    run_dir = runner.RUNS_DIR / job_id
    run_dir.mkdir()
    tf = cwd / "TASK_prev.md"
    tf.write_text("---\nstatus: not started\n---\n")
    meta = {
        "job_id": job_id,
        "kind": "run",
        "cwd": str(cwd),
        "task_file": str(tf),
        "task_file_rel": "TASK_prev.md",
        "session_id": str(uuid.uuid4()),
        "cmd": ["fake"],
        "prompt": "fake",
        "started_at": runner._now_iso(),
        "started_ts": time.time(),
        "ended_at": runner._now_iso(),
        "ended_ts": time.time(),
        "exit_code": 0,
        "pid": 1,
        "task_file_size_before": 0,
        "task_file_sha256_before": runner._sha256(b""),
    }
    if budget is not None:
        meta["budget_usd"] = budget
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    (run_dir / "pre.txt").write_bytes(b"")
    (run_dir / "stdout.log").write_bytes(b"")
    (run_dir / "stderr.log").write_bytes(b"")
    return job_id


def test_start_ask_inherits_prior_budget(tmp_path, stub_spawn):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    prev = _fabricate_prev_job(cwd, budget="5.00")
    new_id = runner.start_ask(prev, "carry on")
    meta = json.loads((runner.RUNS_DIR / new_id / "meta.json").read_text())
    assert meta["budget_usd"] == "5.00"


def test_start_ask_falls_back_to_default_when_prior_lacks_budget(tmp_path, stub_spawn):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    prev = _fabricate_prev_job(cwd, budget=None)
    new_id = runner.start_ask(prev, "carry on")
    meta = json.loads((runner.RUNS_DIR / new_id / "meta.json").read_text())
    assert meta["budget_usd"] == MAX_BUDGET_USD == "2.00"


def test_start_ask_explicit_budget_overrides_inheritance(tmp_path, stub_spawn):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    prev = _fabricate_prev_job(cwd, budget="5.00")
    new_id = runner.start_ask(prev, "carry on", budget_usd=7.5)
    meta = json.loads((runner.RUNS_DIR / new_id / "meta.json").read_text())
    assert meta["budget_usd"] == "7.50"


# ------- cap_hit detector -------------------------------------------------

def _fake_view(run_dir: Path, stdout: bytes = b"", stderr: bytes = b"") -> runner.JobView:
    (run_dir / "stdout.log").write_bytes(stdout)
    (run_dir / "stderr.log").write_bytes(stderr)
    return runner.JobView(
        job_id="fake",
        meta={},
        stdout_path=run_dir / "stdout.log",
        stderr_path=run_dir / "stderr.log",
        pre_path=run_dir / "pre.txt",
        run_dir=run_dir,
    )


def test_cap_hit_matches_verbatim_cli_line(tmp_path):
    rd = tmp_path / "job"
    rd.mkdir()
    view = _fake_view(rd, stderr=b"Error: Exceeded USD budget (2)\n")
    assert runner.cap_hit(view) is True


def test_cap_hit_matches_in_stdout_too(tmp_path):
    rd = tmp_path / "job"
    rd.mkdir()
    view = _fake_view(rd, stdout=b"...\nError: Exceeded USD budget (20)\n...\n")
    assert runner.cap_hit(view) is True


def test_cap_hit_does_not_fire_on_ordinary_budget_text(tmp_path):
    rd = tmp_path / "job"
    rd.mkdir()
    # These strings all contain "budget" but are not the CLI's cap message.
    view = _fake_view(
        rd,
        stdout=b"Discussed the budget for next quarter.\n",
        stderr=b"budget_usd: 2.00\nEstimated budget spent so far.\n",
    )
    assert runner.cap_hit(view) is False
