"""Offline contracts for explicit full access; no real CLI jobs or network calls."""
from __future__ import annotations

import asyncio
import json

import pytest

from claude_code_bridge import runner, server
from claude_code_bridge.backends import get_backend, is_codex_backend


@pytest.mark.parametrize("resume", [False, True])
def test_full_access_command_keeps_review_separate_from_sandbox(tmp_path, resume):
    backend = get_backend("codex-full-access")
    output = tmp_path / "last message.txt"
    options = {"model": "gpt-6-luna", "reasoning_effort": "low"}
    if resume:
        cmd = backend.resume_command("thread-original", output, **options)
        expected = [
            "codex", "exec", "resume", "--json", "--ignore-user-config",
            "-c", 'sandbox_mode="danger-full-access"', "thread-original",
        ]
    else:
        cmd = backend.start_command(None, output, **options)
        expected = [
            "codex", "exec", "--ignore-user-config", "--json",
            "--sandbox", "danger-full-access",
        ]
    assert cmd == expected + [
        "-c", 'approval_policy="on-request"',
        "-c", 'approvals_reviewer="auto_review"',
        "--model", "gpt-6-luna", "-c", 'model_reasoning_effort="low"',
        "--output-last-message", str(output), "-",
    ]
    assert not any(arg.startswith("--dangerously-") for arg in cmd)
    assert "--approve-for-me" not in cmd
    assert "--skip-git-repo-check" not in cmd
    assert "--ignore-rules" not in cmd


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("name", ["codex", "codex-write"])
def test_safer_command_contracts_remain_unchanged(tmp_path, name, resume):
    backend = get_backend(name)
    output = tmp_path / "last.txt"
    expected = ["codex", "exec"]
    if name == "codex-write":
        expected += ["--approve-for-me"]
    if resume:
        expected += ["resume", "--json", "--ignore-user-config"]
        if name == "codex":
            expected += ["-c", 'sandbox_mode="read-only"']
        expected += ["thread-original"]
        cmd = backend.resume_command("thread-original", output)
    else:
        expected += ["--ignore-user-config", "--json"]
        if name == "codex":
            expected += ["--sandbox", "read-only"]
        cmd = backend.start_command(None, output)
    assert cmd == expected + ["--output-last-message", str(output), "-"]
    assert backend.sandbox_mode == ("read-only" if name == "codex" else "workspace-write")


def test_backend_lookup_classifies_full_access_without_changing_legacy_default():
    assert get_backend("codex-full-access").name == "codex-full-access"
    for name in ("codex", "codex-write", "codex-full-access"):
        assert is_codex_backend(name)
    assert not is_codex_backend("claude")
    assert not is_codex_backend(None)
    assert get_backend(None).name == "claude"
    with pytest.raises(ValueError, match="unknown execution backend"):
        get_backend("full-access-typo")


@pytest.fixture
def offline_job(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    task = cwd / "TASK_full_access.md"
    task.write_text("Do explicitly authorized work.\n")
    monkeypatch.setattr(runner, "WORKSPACE_ROOT", cwd)
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runner, "_pre_run_git_snapshot", lambda *args: {})
    calls = []

    def fake_spawn(cmd, spawn_cwd, run_dir, prompt, job_id):
        calls.append({"cmd": cmd, "cwd": spawn_cwd, "prompt": prompt})
        (run_dir / "stdout.log").write_text(
            json.dumps({"type": "thread.started", "thread_id": "thread-original"})
            + "\n" + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7}})
            + "\n"
        )
        (run_dir / "stderr.log").write_text("")
        (run_dir / "last_message.txt").write_text("Full-access result")
        (run_dir / "exit_code").write_text("0")
        return 1

    monkeypatch.setattr(runner, "_spawn", fake_spawn)
    return cwd, task, calls


def test_full_access_dispatch_persists_and_resumes_its_mode(offline_job):
    cwd, task, calls = offline_job
    first = runner.start_codex_full_access_run(
        str(task), str(cwd), model="gpt-6-luna", reasoning_effort="low", timeout_s=120
    )["job_id"]
    view = runner.load_job(first)
    assert view.meta["backend"] == "codex-full-access"
    assert view.meta["session_ref"] == "thread-original"
    assert view.meta["usage"] == {"input_tokens": 7}
    assert "budget_usd" not in view.meta
    assert calls[0]["cwd"] == cwd
    assert "unrestricted filesystem and network access" in calls[0]["prompt"]
    assert "input validation, not a sandbox boundary" in calls[0]["prompt"]
    assert "user explicitly authorized" in calls[0]["prompt"]
    assert "Do not commit, push, or publish changes unless the task" in calls[0]["prompt"]
    assert "canonical documentation" in calls[0]["prompt"]
    assert "checked checklist item for every request" in calls[0]["prompt"]

    second = runner.start_codex_full_access_ask(first, "Authorized follow-up")["job_id"]
    resumed = runner.load_job(second)
    assert resumed.meta["backend"] == "codex-full-access"
    assert resumed.meta["session_ref"] == "thread-original"
    assert resumed.meta["prev_job_id"] == first
    assert resumed.meta["timeout_s"] == 120
    assert resumed.meta["model"] == "gpt-6-luna"
    assert resumed.meta["reasoning_effort"] == "low"
    assert 'sandbox_mode="danger-full-access"' in calls[1]["cmd"]
    assert "resume" in calls[1]["cmd"]
    assert calls[1]["prompt"] == "Authorized follow-up"
    assert "Full-access result" in server.cc_result(second)
    assert "failed" not in server.cc_status(second)


@pytest.mark.parametrize("backend", ["claude", "codex", "codex-write"])
def test_full_access_resume_cannot_upgrade_other_jobs(monkeypatch, backend):
    monkeypatch.setattr(runner, "_load_meta", lambda job: (None, {"backend": backend}))
    with pytest.raises(runner.BridgeError, match="can only continue a full-access"):
        runner.start_codex_full_access_ask("prior-job", "follow-up")


@pytest.mark.parametrize("resume", [runner.start_ask, runner.start_codex_ask, runner.start_codex_write_ask])
def test_other_resume_tools_refuse_full_access_jobs(monkeypatch, resume):
    monkeypatch.setattr(runner, "_load_meta", lambda job: (None, {"backend": "codex-full-access"}))
    with pytest.raises(runner.BridgeError):
        resume("prior-job", "follow-up")


def test_full_access_keeps_cwd_and_task_validation(offline_job, tmp_path):
    cwd, task, calls = offline_job
    with pytest.raises(runner.BridgeError, match="not under"):
        runner.start_codex_full_access_run(str(task), str(tmp_path))
    with pytest.raises(runner.BridgeError, match="TASK_.*md"):
        runner.start_codex_full_access_run("README.md", str(cwd))
    with pytest.raises(runner.BridgeError, match="not found"):
        runner.start_codex_full_access_run("TASK_missing.md", str(cwd))
    outside = tmp_path / "TASK_outside.md"
    outside.write_text("Outside cwd.\n")
    with pytest.raises(runner.BridgeError, match="not under cwd"):
        runner.start_codex_full_access_run(str(outside), str(cwd))
    link = cwd / "TASK_link.md"
    link.symlink_to(outside)
    with pytest.raises(runner.BridgeError, match="not under cwd"):
        runner.start_codex_full_access_run(str(link), str(cwd))
    assert calls == []


def test_full_access_tool_wrappers_dispatch_parameters(monkeypatch):
    captured = {}

    def start(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return {"job_id": "full-job", "reaped": []}

    monkeypatch.setattr(server, "start_codex_full_access_run", start)
    out = server.codex_full_access_run("TASK_work.md", "/repo", 120, "gpt-6-luna", "low")
    assert "full-job (danger-full-access)" in out
    assert captured == {
        "args": ("TASK_work.md", "/repo"),
        "kwargs": {"timeout_s": 120, "model": "gpt-6-luna", "reasoning_effort": "low"},
    }
    monkeypatch.setattr(server, "start_codex_full_access_ask", start)
    out = server.codex_full_access_ask("prior-job", "Authorized follow-up", 180)
    assert "full-job (danger-full-access)" in out
    assert captured == {
        "args": ("prior-job", "Authorized follow-up"),
        "kwargs": {"timeout_s": 180},
    }


def test_tool_descriptions_explain_full_access_and_authorization():
    tools = {tool.name: tool for tool in asyncio.run(server.server.list_tools())}
    for name in ("codex_full_access_run", "codex_full_access_ask"):
        description = tools[name].description
        assert "danger-full-access" in description
        assert "unrestricted filesystem and network" in description
        assert "read/write outside cwd" in description
        assert "user explicitly authorized" in description
        assert "input validation" in description
        assert "not a sandbox boundary" in description
        assert "automatic review" in description
        assert "already allowed by full access need no review" in description
        assert tools[name].output_schema is None
    assert "read-only" in tools["codex_run"].description
    assert "workspace-write sandbox" in tools["codex_write_run"].description
    assert "codex_full_access_ask" in tools["cc_ask"].description
    assert "codex_full_access_run" in server.server.instructions
    assert "not a sandbox boundary" in server.server.instructions
    assert "Bash(git push:*)" in tools["cc_push"].description
