"""Exercise cc_wait against real bridge jobs in the real playground RUNS_DIR.

Not a test — the tests already cover the code paths. This is the "paste
what it returned verbatim" verification asked for in
TASK_cc-wait-and-the-report-check.md. Uses the same bash-wrapper shape
`runner._spawn` uses, so the jobs are real from the bridge's point of
view (real meta.json in RUNS_DIR, real Popen in _LIVE_POPENS, real
exit_code file dropped by the wrapper) — the only fake bit is that the
wrapper runs `sleep` instead of `claude --print`, and cc_wait does not
care what the wrapper runs.

Prints three sections:
  1. WAIT returned because the job ended
  2. WAIT returned because the wait timed out (job still running)
  3. WAIT returned immediately for a job that had already finished

Run:
    python3 -m scripts.verify_cc_wait
    (or)  python3 scripts/verify_cc_wait.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

# Make the src/ package importable when running as a script.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from claude_code_bridge import runner, server  # noqa: E402
from claude_code_bridge.config import DEFAULT_JOB_TIMEOUT_S, RUNS_DIR  # noqa: E402


def _spawn_fake_playground_job(cwd: Path, script: str) -> tuple[str, subprocess.Popen]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    run_dir = RUNS_DIR / job_id
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
    started_ts = time.time()
    meta = {
        "job_id": job_id,
        "kind": "run",
        "cwd": str(cwd),
        "task_file": str(cwd / "TASK_verify_cc_wait.md"),
        "task_file_rel": "TASK_verify_cc_wait.md",
        "session_id": str(uuid.uuid4()),
        "cmd": ["fake-wrapper"],
        "prompt": "verify_cc_wait",
        "started_at": runner._now_iso(),
        "started_ts": started_ts,
        "pid": proc.pid,
        "task_file_size_before": 0,
        "task_file_sha256_before": runner._sha256(b""),
        "budget_usd": "2.00",
        "timeout_s": float(DEFAULT_JOB_TIMEOUT_S),
        "deadline_ts": started_ts + DEFAULT_JOB_TIMEOUT_S,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    (run_dir / "pre.txt").write_bytes(b"")
    return job_id, proc


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    playground = Path.home() / "Donkey_Betz" / "playground"
    playground.mkdir(parents=True, exist_ok=True)

    # ----- Case 1: wait returns because the job ended -----
    _section("Case 1: WAIT returns because the job ended")
    job1_id, job1_proc = _spawn_fake_playground_job(playground, "sleep 1; true")
    t0 = time.time()
    out1 = server.cc_wait(job1_id, timeout_s=15)
    t1 = time.time() - t0
    print(f"(wait blocked for {t1:.2f}s)")
    print(out1)
    job1_proc.wait(timeout=5)

    # ----- Case 2: wait returns because the wait timed out -----
    _section("Case 2: WAIT returns because the wait timed out (job still running)")
    job2_id, job2_proc = _spawn_fake_playground_job(playground, "sleep 30")
    t0 = time.time()
    out2 = server.cc_wait(job2_id, timeout_s=2.0)
    t2 = time.time() - t0
    print(f"(wait blocked for {t2:.2f}s)")
    print(out2)
    # Clean up: kill the sleeper so it doesn't sit around 30s.
    try:
        os.killpg(job2_proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    job2_proc.wait(timeout=5)

    # ----- Case 3: wait returns immediately for a job already done -----
    _section("Case 3: WAIT against a job that had already finished before the call")
    job3_id, job3_proc = _spawn_fake_playground_job(playground, "true")
    job3_proc.wait(timeout=5)
    # Force _finalize_if_dead to see the exit_code file.
    runner.load_job(job3_id)
    t0 = time.time()
    out3 = server.cc_wait(job3_id, timeout_s=60)
    t3 = time.time() - t0
    print(f"(wait blocked for {t3:.4f}s — must be near zero, no initial sleep)")
    print(out3)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
