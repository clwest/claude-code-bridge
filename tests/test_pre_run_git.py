"""Tests for the pre-run git recovery point.

See TASK_pre-run-recovery-point.md Item 5. All six cases are covered
against a real temporary git repository — git is available on any
machine that can run the bridge, but `claude` is not, so nothing here
spawns a real CC session (start_run/start_ask paths monkeypatch
`_spawn`, matching the pattern in test_budget_and_cap.py).

`user.name` and `user.email` are set per test repo via `git config` so
`git commit` works on a machine with no global git identity (CI).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claude_code_bridge import runner


# ------- fixtures ---------------------------------------------------------


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
    """No real bash/claude — same pattern as test_budget_and_cap.py."""
    def _fake(cmd, cwd, run_dir, prompt, job_id):
        (run_dir / "stdout.log").write_bytes(b"")
        (run_dir / "stderr.log").write_bytes(b"")
        return 1  # pid 1 is always alive
    monkeypatch.setattr(runner, "_spawn", _fake)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )


def _init_repo(cwd: Path) -> None:
    cwd.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], cwd)
    _git(["config", "user.email", "test@example.invalid"], cwd)
    _git(["config", "user.name", "Test User"], cwd)
    # gc.auto=0 keeps the "survives gc" test from having concurrent
    # maintenance run and confuse the assertion.
    _git(["config", "gc.auto", "0"], cwd)
    (cwd / "README.md").write_text("seed\n")
    _git(["add", "README.md"], cwd)
    _git(["commit", "-m", "seed"], cwd)


# ------- Case 1: clean tree ----------------------------------------------


def test_clean_tree_records_head_and_branch_and_null_snapshot(tmp_path):
    cwd = tmp_path / "repo"
    _init_repo(cwd)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    info = runner._pre_run_git_snapshot(cwd, "job-clean", run_dir)

    assert info["head_sha"] and len(info["head_sha"]) == 40
    assert info["branch"] == "main"
    assert info["dirty"] is False
    assert info["snapshot_ref"] is None
    # No ref should have been created on a clean tree.
    refs = _git(["for-each-ref", "refs/cc-bridge/"], cwd).stdout
    assert refs == ""


# ------- Case 2: dirty tree → snapshot ref written -----------------------


def test_dirty_tree_writes_anchored_snapshot_with_modified_content(tmp_path):
    cwd = tmp_path / "repo"
    _init_repo(cwd)
    (cwd / "README.md").write_text("seed\nlocal edit\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    info = runner._pre_run_git_snapshot(cwd, "job-dirty", run_dir)

    assert info["dirty"] is True
    assert info["snapshot_ref"] == "refs/cc-bridge/job-dirty"
    # The ref exists and points at a real commit object.
    sha = _git(["rev-parse", info["snapshot_ref"]], cwd).stdout.strip()
    assert len(sha) == 40
    # The commit the ref points at contains the modified content.
    blob = _git(["show", f"{sha}:README.md"], cwd).stdout
    assert "local edit" in blob


# ------- Case 3: snapshot survives `git gc --prune=now` ------------------


def test_snapshot_survives_git_gc_prune_now(tmp_path):
    """The whole point of Item 2. Without update-ref the sha is unreferenced."""
    cwd = tmp_path / "repo"
    _init_repo(cwd)
    (cwd / "README.md").write_text("seed\ngc-target\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    info = runner._pre_run_git_snapshot(cwd, "job-gc", run_dir)
    ref = info["snapshot_ref"]
    assert ref is not None
    sha = _git(["rev-parse", ref], cwd).stdout.strip()

    # Force aggressive gc: prune unreferenced objects immediately, expire
    # reflog entries. If the ref weren't there, the stash-create sha would
    # be unreachable and collected.
    _git(["reflog", "expire", "--expire=now", "--all"], cwd)
    _git(["gc", "--prune=now"], cwd)

    # Ref still resolves and the object is still present.
    still = _git(["rev-parse", ref], cwd).stdout.strip()
    assert still == sha
    # And the content is still there.
    blob = _git(["show", f"{sha}:README.md"], cwd).stdout
    assert "gc-target" in blob


# ------- Case 4: non-git cwd → no exception, meta records it -------------


def test_non_git_cwd_records_fact_and_still_spawns(tmp_path, stub_spawn):
    cwd = tmp_path / "not-a-repo"
    cwd.mkdir()
    tf = cwd / "TASK_x.md"
    tf.write_text("---\nstatus: not started\n---\n")

    # Must not raise.
    job_id = runner.start_run(str(tf), str(cwd))["job_id"]
    meta = json.loads((runner.RUNS_DIR / job_id / "meta.json").read_text())

    assert meta["pre_run_git"] == {"git": "not a repository"}
    # Spawn still happened — pid is recorded.
    assert meta.get("pid") == 1


# ------- Case 5: status_porcelain written and names untracked file -------


def test_status_porcelain_written_and_lists_untracked(tmp_path):
    cwd = tmp_path / "repo"
    _init_repo(cwd)
    (cwd / "new_untracked.txt").write_text("hi\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    runner._pre_run_git_snapshot(cwd, "job-status", run_dir)

    status_path = run_dir / "pre_git_status.txt"
    assert status_path.is_file()
    body = status_path.read_text()
    assert "new_untracked.txt" in body
    # Porcelain marks untracked with `??`.
    assert "??" in body


# ------- Case 6: failing / timing-out git does not raise -----------------


def test_git_failure_or_timeout_does_not_raise(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    _init_repo(cwd)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def _boom(args, cwd):  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=5.0)

    monkeypatch.setattr(runner, "_git", _boom)
    # Must not raise: the first probe times out and the helper returns.
    info = runner._pre_run_git_snapshot(cwd, "job-timeout", run_dir)
    assert info == {"git": "git timed out"}


def test_git_not_on_path_does_not_raise(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    _init_repo(cwd)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def _missing(args, cwd):  # noqa: ARG001
        raise FileNotFoundError("git")

    monkeypatch.setattr(runner, "_git", _missing)
    info = runner._pre_run_git_snapshot(cwd, "job-missing", run_dir)
    assert info == {"git": "git not on PATH"}


# ------- start_run wires the snapshot into meta.json --------------------


def test_start_run_records_pre_run_git_in_meta(tmp_path, stub_spawn):
    cwd = tmp_path / "repo"
    _init_repo(cwd)
    (cwd / "TASK_x.md").write_text("---\nstatus: not started\n---\n")

    job_id = runner.start_run(str(cwd / "TASK_x.md"), str(cwd))["job_id"]
    meta = json.loads((runner.RUNS_DIR / job_id / "meta.json").read_text())

    pre = meta["pre_run_git"]
    assert pre["branch"] == "main"
    assert pre["head_sha"]
    # Task file itself is an untracked change so the tree is dirty and
    # the snapshot ref is anchored under refs/cc-bridge/<job_id>.
    # (Actually — TASK_x.md is untracked; git status --porcelain will mark
    # it '?? TASK_x.md' so the tree is dirty; but `git stash create` on a
    # working tree with ONLY untracked files returns empty. That is the
    # documented limitation.)
    # So: dirty is True, snapshot_ref may be None here — the important
    # thing is neither field raised.
    assert pre["dirty"] is True
    # And pre_git_status.txt was written into the run directory.
    assert (runner.RUNS_DIR / job_id / "pre_git_status.txt").is_file()
