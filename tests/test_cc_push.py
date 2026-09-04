"""Tests for cc_push: the seventh tool that publishes commits.

Everything here runs against a real bare repository at `file://<tmp_path>/origin.git`
— no network, no credentials, real git. Same shape as
test_pre_run_git.py: git is on any machine that can run the bridge.

WORKSPACE_ROOT is monkeypatched to tmp_path so `_validate_cwd` (which
push_branch reuses from cc_run) accepts the temporary working copies.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claude_code_bridge import runner, server


@pytest.fixture(autouse=True)
def _isolated_workspace_root(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "WORKSPACE_ROOT", tmp_path)
    yield


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


def _init_bare_origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return bare


def _init_work_repo(tmp_path: Path, bare: Path, name: str = "work") -> Path:
    work = tmp_path / name
    work.mkdir()
    _git(["init", "-b", "main"], work)
    _git(["config", "user.email", "test@example.invalid"], work)
    _git(["config", "user.name", "Test User"], work)
    _git(["config", "gc.auto", "0"], work)
    _git(["remote", "add", "origin", f"file://{bare}"], work)
    return work


def _commit(work: Path, filename: str, content: str, msg: str) -> None:
    (work / filename).write_text(content)
    _git(["add", filename], work)
    _git(["commit", "-m", msg], work)


# ------- happy path: two commits pushed as a new branch ------------------


def test_push_two_commits_creates_remote_branch(tmp_path):
    bare = _init_bare_origin(tmp_path)
    work = _init_work_repo(tmp_path, bare)
    _commit(work, "README.md", "one\n", "one")
    _commit(work, "README.md", "one\ntwo\n", "two")

    result = runner.push_branch(str(work))

    assert result["branch"] == "main"
    assert result["remote_name"] == "origin"
    assert result["remote_url"] == f"file://{bare}"
    assert result["sha_before_remote"] is None
    assert result["sha_after_remote"] == result["sha_before_local"]
    assert result["commits_pushed"] == 2
    assert result["status"] == "new_branch"
    # And the remote actually has it.
    remote_head = _git(["rev-parse", "main"], bare).strip()
    assert remote_head == result["sha_after_remote"]


# ------- pushed vs. up_to_date is visible in the return dict --------------


def test_push_already_up_to_date_reports_zero_and_up_to_date(tmp_path):
    bare = _init_bare_origin(tmp_path)
    work = _init_work_repo(tmp_path, bare)
    _commit(work, "README.md", "seed\n", "seed")
    runner.push_branch(str(work))  # first push

    result = runner.push_branch(str(work))  # nothing new

    assert result["status"] == "up_to_date"
    assert result["commits_pushed"] == 0
    assert result["message"] == "already up to date"
    assert result["sha_before_remote"] == result["sha_after_remote"]


def test_push_return_distinguishes_pushed_from_nothing(tmp_path):
    """Task-file ask: 'pushed 2 commits' vs 'nothing to push' without reading stdout."""
    bare = _init_bare_origin(tmp_path)
    work = _init_work_repo(tmp_path, bare)
    _commit(work, "README.md", "a\n", "a")
    r1 = runner.push_branch(str(work))
    _commit(work, "README.md", "a\nb\n", "b")
    r2 = runner.push_branch(str(work))
    r3 = runner.push_branch(str(work))

    assert r1["commits_pushed"] == 1 and r1["status"] == "new_branch"
    assert r2["commits_pushed"] == 1 and r2["status"] == "pushed"
    assert r3["commits_pushed"] == 0 and r3["status"] == "up_to_date"


# ------- detached HEAD refuses, names the SHA -----------------------------


def test_push_refuses_detached_head_and_names_sha(tmp_path):
    bare = _init_bare_origin(tmp_path)
    work = _init_work_repo(tmp_path, bare)
    _commit(work, "README.md", "seed\n", "seed")
    _commit(work, "README.md", "seed\ntwo\n", "two")
    first_sha = _git(["rev-parse", "HEAD~1"], work).strip()
    _git(["checkout", "--detach", first_sha], work)

    with pytest.raises(runner.BridgeError) as exc_info:
        runner.push_branch(str(work))

    msg = str(exc_info.value)
    assert "detached HEAD" in msg
    assert first_sha[:7] in msg


# ------- non-fast-forward: reports git's own reason, one line -------------


def test_push_reports_non_fast_forward_rejection(tmp_path):
    """Diverge two clones of the same origin; the second push is rejected."""
    bare = _init_bare_origin(tmp_path)
    work_a = _init_work_repo(tmp_path, bare, name="work_a")
    _commit(work_a, "README.md", "seed\n", "seed")
    runner.push_branch(str(work_a))

    # Clone the origin so work_b starts with the same main tip.
    work_b = tmp_path / "work_b"
    subprocess.run(
        ["git", "clone", f"file://{bare}", str(work_b)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    _git(["config", "user.email", "test@example.invalid"], work_b)
    _git(["config", "user.name", "Test User"], work_b)

    # work_a moves the remote forward.
    _commit(work_a, "README.md", "seed\naaa\n", "aaa")
    runner.push_branch(str(work_a))

    # work_b commits on the stale base and tries to push.
    _commit(work_b, "OTHER.md", "bbb\n", "bbb")
    with pytest.raises(runner.BridgeError) as exc_info:
        runner.push_branch(str(work_b))

    msg = str(exc_info.value)
    assert "not published" in msg
    # git's rejection line contains either 'rejected' or 'fetch first'.
    assert "rejected" in msg.lower() or "fetch first" in msg.lower()
    # And the message stays on a single line.
    assert "\n" not in msg


# ------- refuses cwds outside WORKSPACE_ROOT and bad remote names --------


def test_push_refuses_cwd_outside_workspace_root(tmp_path, monkeypatch):
    # Move the workspace root elsewhere so tmp_path is now outside it.
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(runner, "WORKSPACE_ROOT", other)
    with pytest.raises(runner.BridgeError) as exc_info:
        runner.push_branch(str(tmp_path))
    assert "is not under" in str(exc_info.value)


def test_push_refuses_missing_remote(tmp_path):
    bare = _init_bare_origin(tmp_path)
    work = _init_work_repo(tmp_path, bare)
    _commit(work, "README.md", "seed\n", "seed")
    with pytest.raises(runner.BridgeError) as exc_info:
        runner.push_branch(str(work), remote="no-such-remote")
    assert "not configured" in str(exc_info.value)


def test_push_refuses_remote_that_looks_like_a_flag(tmp_path):
    """A dash-prefixed remote name would otherwise be read as a git flag."""
    bare = _init_bare_origin(tmp_path)
    work = _init_work_repo(tmp_path, bare)
    with pytest.raises(runner.BridgeError):
        runner.push_branch(str(work), remote="--force")


# ------- server layer: cc_push returns a plain-text summary ---------------


def test_cc_push_tool_return_names_status_and_url(tmp_path):
    bare = _init_bare_origin(tmp_path)
    work = _init_work_repo(tmp_path, bare)
    _commit(work, "README.md", "seed\n", "seed")

    out = server.cc_push(str(work))

    assert "created remote branch main" in out
    assert f"file://{bare}" in out
    assert "commits published: 1" in out
    assert "branch: main" in out


def test_cc_push_tool_return_reports_refusal_on_detached_head(tmp_path):
    bare = _init_bare_origin(tmp_path)
    work = _init_work_repo(tmp_path, bare)
    _commit(work, "README.md", "seed\n", "seed")
    _git(["checkout", "--detach", "HEAD"], work)

    out = server.cc_push(str(work))
    assert out.startswith("Bridge error:")
    assert "detached HEAD" in out


def test_cc_push_signature_has_no_argument_passthrough():
    """Signature contract: cc_push takes cwd and remote — nothing else.

    A caller cannot reach --force, --force-with-lease, --delete, --tags,
    or a refspec because there is no parameter that would carry them.
    The bridge builds the argv itself.
    """
    import inspect

    sig = inspect.signature(server.cc_push)
    assert list(sig.parameters) == ["cwd", "remote"]
