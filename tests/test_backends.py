"""Focused, offline tests for backend command and event contracts."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from claude_code_bridge import runner
from claude_code_bridge.backends import CodexBackend, get_backend
from claude_code_bridge.config import ALLOWED_TOOLS, DISALLOWED_TOOLS, PERMISSION_MODE


@pytest.fixture(autouse=True)
def _isolated_runs_dir(monkeypatch, tmp_path):
    runs = tmp_path / "cc-runs"
    runs.mkdir()
    monkeypatch.setattr(runner, "RUNS_DIR", runs)
    monkeypatch.setattr(runner, "WORKSPACE_ROOT", tmp_path)
    runner._LIVE_POPENS.clear()
    yield
    runner._LIVE_POPENS.clear()


def test_codex_start_and_resume_commands_are_read_only(tmp_path):
    backend = CodexBackend()
    output = tmp_path / "last-message.txt"

    start = backend.start_command(
        None, output, model="gpt-6-luna", reasoning_effort="low"
    )
    assert start[:6] == [
        "codex", "exec", "--ignore-user-config", "--json", "--sandbox", "read-only"
    ]
    assert start[-3:] == ["--output-last-message", str(output), "-"]
    assert "gpt-6-luna" in start
    assert 'model_reasoning_effort="low"' in start
    assert "--dangerously-bypass-approvals-and-sandbox" not in start
    assert "--ignore-rules" not in start
    assert "--skip-git-repo-check" not in start

    resume = backend.resume_command("thread-123", output)
    assert resume[:6] == [
        "codex", "exec", "resume", "--json", "--ignore-user-config", "-c"
    ]
    assert 'sandbox_mode="read-only"' in resume
    assert "thread-123" in resume
    assert resume[-3:] == ["--output-last-message", str(output), "-"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in resume
    assert "--ignore-rules" not in resume
    assert "--skip-git-repo-check" not in resume


def test_codex_event_parser_extracts_thread_final_usage_and_failure():
    events = [
        {"type": "thread.started", "thread_id": "thread-123"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}},
        {"type": "turn.failed", "error": {"message": "sample failure"}},
    ]
    parsed = CodexBackend.parse_output("\n".join(json.dumps(e) for e in events))

    assert parsed.session_ref == "thread-123"
    assert parsed.usage == {"input_tokens": 12, "output_tokens": 3}
    assert parsed.turn_failed == "sample failure"
    assert parsed.session_ref_mismatch is False
    assert parsed.parse_errors == 0


def test_codex_resume_parser_detects_thread_id_mismatch():
    parsed = CodexBackend.parse_output(
        '{"type":"thread.started","thread_id":"other-thread"}',
        expected_session_ref="expected-thread",
    )
    assert parsed.session_ref == "other-thread"
    assert parsed.session_ref_mismatch is True


def test_codex_turn_failed_event_and_exit_code_are_persisted(tmp_path):
    run_dir = tmp_path / "job"
    run_dir.mkdir()
    meta_path = run_dir / "meta.json"
    (run_dir / "stdout.log").write_text(json.dumps({
        "type": "turn.failed",
        "error": {"message": "safe test failure"},
    }) + "\n")
    (run_dir / "exit_code").write_text("1")
    meta = {
        "job_id": "codex-failed",
        "kind": "run",
        "backend": "codex",
        "started_ts": time.time(),
        "pid": 1,
    }

    finalized = runner._finalize_if_dead(meta_path, meta)

    assert finalized["exit_code"] == 1
    assert finalized["backend_turn_failed"] == "safe test failure"
    assert finalized["ended_reason"] == "failed"


def test_codex_backend_rejects_unverified_reasoning_values(tmp_path):
    with pytest.raises(ValueError, match="unsupported Codex reasoning effort"):
        CodexBackend().start_command(None, tmp_path / "last.txt", reasoning_effort="wild")
    with pytest.raises(ValueError, match="no verified per-run dollar budget"):
        CodexBackend().start_command(None, tmp_path / "last.txt", budget_usd="2.00")


def test_old_metadata_without_backend_resolves_to_claude():
    assert get_backend(None).name == "claude"
    # The established Claude command is still available through the adapter.
    cmd = get_backend(None).start_command("legacy-session", Path("unused"), budget_usd="2.00")
    assert cmd[0:2] == ["claude", "--print"]
    assert "--max-budget-usd" in cmd


def test_claude_adapter_keeps_legacy_start_and_resume_argv():
    backend = get_backend("claude")
    start = backend.start_command("session-x", Path("unused"), budget_usd="3.50")
    resume = backend.resume_command("session-x", Path("unused"), budget_usd="3.50")
    common = [
        "claude",
        "--print",
        "--output-format",
        "text",
        "--permission-mode",
        PERMISSION_MODE,
        "--max-budget-usd",
        "3.50",
        "--allowedTools",
        " ".join(ALLOWED_TOOLS),
        "--disallowedTools",
        " ".join(DISALLOWED_TOOLS),
    ]
    assert start == common + ["--session-id", "session-x"]
    assert resume == common + ["-r", "session-x"]


def test_new_claude_jobs_persist_backend_without_dropping_legacy_fields(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    task = cwd / "TASK_claude.md"
    task.write_text("Do the work.\n")

    def fake_spawn(cmd, spawn_cwd, run_dir, prompt, job_id):
        (run_dir / "stdout.log").write_text("")
        (run_dir / "stderr.log").write_text("")
        return 1

    monkeypatch.setattr(runner, "_spawn", fake_spawn)
    job_id = runner.start_run(str(task), str(cwd))["job_id"]
    meta = json.loads((runner.RUNS_DIR / job_id / "meta.json").read_text())

    assert meta["backend"] == "claude"
    assert meta["session_ref"] == meta["session_id"]
    assert meta["budget_usd"] == "2.00"
    assert "do the work in it now" in meta["prompt"]


def test_codex_start_persists_backend_session_and_usage(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    task = cwd / "TASK_review.md"
    task.write_text("Review this task.\n")
    session_ref = "01a16abb-a2b2-74b0-9a96-6f44e385d9ff"
    captured = {}

    def fake_spawn(cmd, spawn_cwd, run_dir, prompt, job_id):
        captured.update(cmd=cmd, cwd=spawn_cwd, prompt=prompt)
        (run_dir / "stdout.log").write_text(
            json.dumps({"type": "thread.started", "thread_id": session_ref}) + "\n"
            + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7}}) + "\n"
        )
        (run_dir / "stderr.log").write_text("")
        (run_dir / "last_message.txt").write_text("Read-only analysis")
        (run_dir / "exit_code").write_text("0")
        return 1

    monkeypatch.setattr(runner, "_spawn", fake_spawn)
    job_id = runner.start_codex_run(
        str(task), str(cwd), model="gpt-6-luna", reasoning_effort="low"
    )["job_id"]
    view = runner.load_job(job_id)

    assert view.meta["backend"] == "codex"
    assert view.meta["session_ref"] == session_ref
    assert view.meta["usage"] == {"input_tokens": 7}
    assert "budget_usd" not in view.meta
    assert "--sandbox" in captured["cmd"] and "read-only" in captured["cmd"]
    assert "do not attempt edits" in captured["prompt"]


def test_codex_ask_resumes_stored_session_and_rejects_mismatched_output(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    task = cwd / "TASK_review.md"
    task.write_text("Review this task.\n")
    session_ref = "thread-original"
    call_count = 0

    def fake_spawn(cmd, spawn_cwd, run_dir, prompt, job_id):
        nonlocal call_count
        call_count += 1
        observed = session_ref if call_count == 1 else "thread-wrong"
        (run_dir / "stdout.log").write_text(
            json.dumps({"type": "thread.started", "thread_id": observed}) + "\n"
        )
        (run_dir / "stderr.log").write_text("")
        (run_dir / "exit_code").write_text("0")
        return 1

    monkeypatch.setattr(runner, "_spawn", fake_spawn)
    first = runner.start_codex_run(str(task), str(cwd))["job_id"]
    runner.load_job(first)
    followed = runner.start_ask(first, "One short follow-up")
    second_id = followed["job_id"]
    second_meta_path = runner.RUNS_DIR / second_id / "meta.json"
    second_meta = json.loads(second_meta_path.read_text())

    assert second_meta["backend"] == "codex"
    assert second_meta["session_ref"] == session_ref
    assert "resume" in second_meta["cmd"]
    assert session_ref in second_meta["cmd"]

    view = runner.load_job(second_id)
    assert view.meta["backend_error"] == "Codex resume returned a different or missing thread ID"
    assert view.meta["ended_reason"] == "failed"


def test_legacy_claude_job_without_backend_remains_resumable(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    task = cwd / "TASK_old.md"
    task.write_text("Old task.\n")
    job_id = "legacy-" + uuid.uuid4().hex[:8]
    run_dir = runner.RUNS_DIR / job_id
    run_dir.mkdir()
    session_id = str(uuid.uuid4())
    (run_dir / "meta.json").write_text(json.dumps({
        "job_id": job_id,
        "kind": "run",
        "cwd": str(cwd),
        "task_file": str(task),
        "task_file_rel": task.name,
        "session_id": session_id,
        "cmd": ["claude"],
        "prompt": "old prompt",
        "started_at": runner._now_iso(),
        "started_ts": time.time(),
        "ended_at": runner._now_iso(),
        "ended_ts": time.time(),
        "exit_code": 0,
        "pid": 1,
    }))
    (run_dir / "pre.txt").write_text("")
    (run_dir / "stdout.log").write_text("")
    (run_dir / "stderr.log").write_text("")
    captured = {}

    def fake_spawn(cmd, spawn_cwd, new_run_dir, prompt, new_job_id):
        captured.update(cmd=cmd)
        (new_run_dir / "stdout.log").write_text("")
        (new_run_dir / "stderr.log").write_text("")
        return 1

    monkeypatch.setattr(runner, "_spawn", fake_spawn)
    followup_id = runner.start_ask(job_id, "carry on")["job_id"]
    followup = json.loads((runner.RUNS_DIR / followup_id / "meta.json").read_text())

    assert "backend" not in json.loads((run_dir / "meta.json").read_text())
    assert followup["backend"] == "claude"
    assert followup["session_ref"] == session_id
    assert followup["session_id"] == session_id
    assert captured["cmd"][0] == "claude"
    assert "-r" in captured["cmd"] and session_id in captured["cmd"]
