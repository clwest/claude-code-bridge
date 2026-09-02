"""Start, poll and resume headless Claude Code subprocesses.

State per job lives on disk under `RUNS_DIR/<job_id>/`:

    meta.json     — parameters, session id, pid, timings, pre-run task-file digest
    pre.txt       — task file contents before the run (for the contract check)
    stdout.log    — stdout captured verbatim
    stderr.log    — stderr captured verbatim

The record on disk is the source of truth. The MCP server process may be
restarted between `cc_run` and `cc_status`; the state is read from disk on
every call.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    ALLOWED_TOOLS,
    DISALLOWED_TOOLS,
    MAX_BUDGET_USD,
    PERMISSION_MODE,
    RUNS_DIR,
    WORKSPACE_ROOT,
)


class BridgeError(Exception):
    """Raised for policy violations and validation failures."""


@dataclass
class JobView:
    job_id: str
    meta: dict
    stdout_path: Path
    stderr_path: Path
    pre_path: Path
    run_dir: Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _resolve_under(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    root_resolved = root.expanduser().resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise BridgeError(
            f"path {resolved} is not under {root_resolved}"
        ) from exc
    return resolved


def _cc_command(session_id: str, resume: bool) -> list[str]:
    """Build the claude CLI argv (no prompt — prompt goes on stdin).

    The prompt is sent via stdin rather than as a positional argument
    because commander.js variadic options (`--allowedTools <tools...>`,
    `--disallowedTools <tools...>`) consume every following non-flag
    token, including a trailing positional. Empirically verified on
    2.1.114: `claude --print --disallowedTools "X" "prompt"` fails with
    "Input must be provided either through stdin or as a prompt
    argument" because "prompt" is grabbed as another disallowed tool.
    Stdin sidesteps the issue entirely.
    """
    cmd = [
        "claude",
        "--print",
        "--output-format",
        "text",
        "--permission-mode",
        PERMISSION_MODE,
        "--max-budget-usd",
        MAX_BUDGET_USD,
        "--allowedTools",
        " ".join(ALLOWED_TOOLS),
        "--disallowedTools",
        " ".join(DISALLOWED_TOOLS),
    ]
    if resume:
        cmd += ["-r", session_id]
    else:
        cmd += ["--session-id", session_id]
    return cmd


def _iter_jobs() -> list[Path]:
    if not RUNS_DIR.exists():
        return []
    return sorted(p for p in RUNS_DIR.glob("*/meta.json"))


def _load_meta(job_id: str) -> tuple[Path, dict]:
    run_dir = RUNS_DIR / job_id
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        raise BridgeError(f"unknown job_id: {job_id}")
    return meta_path, json.loads(meta_path.read_text())


def _save_meta(meta_path: Path, meta: dict) -> None:
    meta_path.write_text(json.dumps(meta, indent=2))


def _finalize_if_dead(meta_path: Path, meta: dict) -> dict:
    if meta.get("ended_at"):
        return meta
    pid = meta.get("pid")
    if pid and _pid_alive(pid):
        return meta
    meta["ended_at"] = _now_iso()
    meta["ended_ts"] = time.time()
    # Read exit code the bash wrapper wrote when CC exited.
    exit_path = meta_path.parent / "exit_code"
    if exit_path.is_file():
        try:
            meta["exit_code"] = int(exit_path.read_text().strip())
        except ValueError:
            meta["exit_code"] = exit_path.read_text().strip() or "unknown"
    else:
        meta["exit_code"] = meta.get("exit_code", "unknown")
    _save_meta(meta_path, meta)
    return meta


def _active_in_cwd(cwd: Path) -> str | None:
    cwd_str = str(cwd)
    for meta_path in _iter_jobs():
        meta = json.loads(meta_path.read_text())
        if meta.get("cwd") != cwd_str:
            continue
        if meta.get("ended_at"):
            continue
        pid = meta.get("pid")
        if pid and _pid_alive(pid):
            return meta["job_id"]
        _finalize_if_dead(meta_path, meta)
    return None


def _new_job_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def _validate_task_file(task_file: str, cwd: Path) -> Path:
    tf = Path(task_file).expanduser()
    if not tf.is_absolute():
        tf = (cwd / tf).resolve()
    else:
        tf = tf.resolve()
    if not tf.name.startswith("TASK_") or tf.suffix != ".md":
        raise BridgeError(
            f"task_file must be a TASK_*.md file (got {tf.name!r})"
        )
    if not tf.is_file():
        raise BridgeError(f"task_file not found: {tf}")
    try:
        tf.relative_to(cwd)
    except ValueError as exc:
        raise BridgeError(
            f"task_file {tf} is not under cwd {cwd}"
        ) from exc
    return tf


def _validate_cwd(cwd: str) -> Path:
    cwd_p = _resolve_under(Path(cwd), WORKSPACE_ROOT)
    if not cwd_p.is_dir():
        raise BridgeError(f"cwd is not a directory: {cwd_p}")
    return cwd_p


def _spawn(cmd: list[str], cwd: Path, run_dir: Path, prompt: str) -> int:
    """Spawn CC in the background and capture its eventual exit code.

    Uses a `bash -c '<cmd>; echo $? > exit_code'` wrapper so the exit
    code survives after the process exits — Popen's exit code is only
    available if the launcher process waits, and this launcher returns
    immediately. The recorded pid is the wrapper bash pid, which lives
    exactly as long as the CC session it wraps.

    The prompt is piped to stdin (see `_cc_command` for why not argv).
    """
    exit_path = run_dir / "exit_code"
    quoted = " ".join(shlex.quote(a) for a in cmd)
    shell_line = f"{quoted}; echo $? > {shlex.quote(str(exit_path))}"

    stdout_f = (run_dir / "stdout.log").open("wb")
    stderr_f = (run_dir / "stderr.log").open("wb")
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, bash is the launcher
        ["/bin/bash", "-c", shell_line],
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=stdout_f,
        stderr=stderr_f,
        start_new_session=True,
    )
    # Write the prompt and close stdin so CC sees EOF and starts working.
    # Popen won't block on this write for prompts under the pipe buffer size
    # (64KB on macOS); larger prompts would need a background writer.
    if proc.stdin:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
    return proc.pid


def start_run(task_file: str, cwd: str) -> str:
    """Start a fresh headless CC run. Returns job_id."""
    cwd_p = _validate_cwd(cwd)
    tf = _validate_task_file(task_file, cwd_p)

    active = _active_in_cwd(cwd_p)
    if active:
        raise BridgeError(
            f"another job ({active}) is already running in {cwd_p}; "
            "one run per cwd at a time"
        )

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = _new_job_id()
    run_dir = RUNS_DIR / job_id
    run_dir.mkdir()

    session_id = str(uuid.uuid4())
    task_rel = tf.relative_to(cwd_p)
    prompt = f"Read {task_rel}"
    cmd = _cc_command(session_id, resume=False)

    pre_bytes = tf.read_bytes()
    (run_dir / "pre.txt").write_bytes(pre_bytes)

    pid = _spawn(cmd, cwd_p, run_dir, prompt)

    meta = {
        "job_id": job_id,
        "kind": "run",
        "cwd": str(cwd_p),
        "task_file": str(tf),
        "task_file_rel": str(task_rel),
        "session_id": session_id,
        "cmd": cmd,
        "prompt": prompt,
        "started_at": _now_iso(),
        "started_ts": time.time(),
        "pid": pid,
        "task_file_size_before": len(pre_bytes),
        "task_file_sha256_before": _sha256(pre_bytes),
    }
    _save_meta(run_dir / "meta.json", meta)
    return job_id


def start_ask(prev_job_id: str, prompt: str) -> str:
    """Resume an existing CC session with a follow-up prompt. Returns new job_id."""
    _, prev_meta = _load_meta(prev_job_id)
    cwd_p = Path(prev_meta["cwd"])
    session_id = prev_meta["session_id"]
    task_file = Path(prev_meta["task_file"])
    task_rel = prev_meta.get("task_file_rel", str(task_file))

    if not cwd_p.is_dir():
        raise BridgeError(f"prior cwd no longer exists: {cwd_p}")

    active = _active_in_cwd(cwd_p)
    if active:
        raise BridgeError(
            f"another job ({active}) is already running in {cwd_p}; "
            "one run per cwd at a time"
        )
    if not prompt or not prompt.strip():
        raise BridgeError("prompt must be non-empty for cc_ask")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = _new_job_id()
    run_dir = RUNS_DIR / job_id
    run_dir.mkdir()

    cmd = _cc_command(session_id, resume=True)

    # Cache the task file contents so a follow-up ask's contract check
    # measures what THIS ask added.
    pre_bytes = task_file.read_bytes() if task_file.is_file() else b""
    (run_dir / "pre.txt").write_bytes(pre_bytes)

    pid = _spawn(cmd, cwd_p, run_dir, prompt)

    meta = {
        "job_id": job_id,
        "kind": "ask",
        "prev_job_id": prev_job_id,
        "cwd": str(cwd_p),
        "task_file": str(task_file),
        "task_file_rel": task_rel,
        "session_id": session_id,
        "cmd": cmd,
        "prompt": prompt,
        "started_at": _now_iso(),
        "started_ts": time.time(),
        "pid": pid,
        "task_file_size_before": len(pre_bytes),
        "task_file_sha256_before": _sha256(pre_bytes),
    }
    _save_meta(run_dir / "meta.json", meta)
    return job_id


def load_job(job_id: str) -> JobView:
    meta_path, meta = _load_meta(job_id)
    meta = _finalize_if_dead(meta_path, meta)
    run_dir = meta_path.parent
    return JobView(
        job_id=job_id,
        meta=meta,
        stdout_path=run_dir / "stdout.log",
        stderr_path=run_dir / "stderr.log",
        pre_path=run_dir / "pre.txt",
        run_dir=run_dir,
    )


def list_jobs() -> list[JobView]:
    out: list[JobView] = []
    for meta_path in _iter_jobs():
        meta = json.loads(meta_path.read_text())
        meta = _finalize_if_dead(meta_path, meta)
        run_dir = meta_path.parent
        out.append(
            JobView(
                job_id=meta["job_id"],
                meta=meta,
                stdout_path=run_dir / "stdout.log",
                stderr_path=run_dir / "stderr.log",
                pre_path=run_dir / "pre.txt",
                run_dir=run_dir,
            )
        )
    return out


def is_running(view: JobView) -> bool:
    pid = view.meta.get("pid")
    if not pid:
        return False
    if view.meta.get("ended_at"):
        return False
    return _pid_alive(pid)


def elapsed(view: JobView) -> float:
    started = view.meta.get("started_ts", time.time())
    ended = view.meta.get("ended_ts")
    if ended:
        return ended - started
    return time.time() - started


def tail(path: Path, n_lines: int = 20) -> str:
    if not path.is_file():
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-n_lines:])
