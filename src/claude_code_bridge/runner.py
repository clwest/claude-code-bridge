"""Start, poll and resume headless Claude Code subprocesses.

State per job lives on disk under `RUNS_DIR/<job_id>/`:

    meta.json     — parameters, session id, pid, timings, pre-run task-file digest
    pre.txt       — task file contents before the run (for the contract check)
    stdout.log    — stdout captured verbatim
    stderr.log    — stderr captured verbatim
    exit_code     — written by the bash wrapper after CC exits (see `_spawn`)

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

import re

from .config import (
    ALLOWED_TOOLS,
    DISALLOWED_TOOLS,
    MAX_BUDGET_CEILING_USD,
    MAX_BUDGET_USD,
    PERMISSION_MODE,
    RUNS_DIR,
    WORKSPACE_ROOT,
)


class BridgeError(Exception):
    """Raised for policy violations and validation failures."""


# Held between spawn and job termination so we can `.poll()` the child
# and reap the zombie the bash wrapper leaves behind when it exits.
# Without this the wrapper stays in the MCP server's process table as
# <defunct> and `os.kill(pid, 0)` on it succeeds — which is what
# Cowork's first MCP-side call to Test 1 tripped on: the job never
# transitioned out of "running", the exit_code file sat on disk
# unread, and the one-run-per-cwd lock never cleared.
#
# The dict is populated on spawn and drained by `_reap_popen`. Only
# the process that spawned the child can reap it, so this is only
# useful within a single MCP server run — but that is exactly the
# case where the zombie problem showed up. A restart of the MCP
# server hands the wrapper (or its remains) to launchd, which does
# the reap on its own.
_LIVE_POPENS: dict[str, "subprocess.Popen[bytes]"] = {}


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


def _validate_budget(budget_usd: float | int | str | None) -> str:
    """Return a `--max-budget-usd` string, or raise BridgeError.

    Callers may pass a float, int, numeric string, or None (default).
    Zero, negative, non-numeric and above-ceiling values are rejected
    with a message that names the ceiling — do not silently clamp.
    """
    if budget_usd is None:
        return MAX_BUDGET_USD
    ceiling = float(MAX_BUDGET_CEILING_USD)
    try:
        value = float(budget_usd)
    except (TypeError, ValueError) as exc:
        raise BridgeError(
            f"budget_usd must be a number (got {budget_usd!r}); "
            f"ceiling is ${ceiling:.2f}"
        ) from exc
    if value != value or value <= 0:  # NaN or non-positive
        raise BridgeError(
            f"budget_usd must be greater than 0 (got {value}); "
            f"ceiling is ${ceiling:.2f}"
        )
    if value > ceiling:
        raise BridgeError(
            f"budget_usd {value} exceeds ceiling ${ceiling:.2f}; "
            "raise MAX_BUDGET_CEILING_USD in config.py deliberately"
        )
    return f"{value:.2f}"


# The exact CLI message on 2.1.114 is `Error: Exceeded USD budget (2)`.
# We match "exceeded" near "budget" case-insensitively so a different
# number or minor rewording still fires. Do not try to parse the amount.
_CAP_HIT_RE = re.compile(r"exceeded[^\n]{0,40}budget", re.IGNORECASE)


def cap_hit(view: "JobView") -> bool:
    """True iff the CLI wrote its budget-exceeded message to stdout or stderr.

    Item 3 of TASK_bridge-followups-round-1: a cap-hit run and a botched
    run need different responses (rerun with bigger cap vs. rebrief), so
    the caller must be able to tell them apart without guessing.
    """
    for path in (view.stdout_path, view.stderr_path):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if _CAP_HIT_RE.search(data.decode("utf-8", errors="replace")):
            return True
    return False


def _cc_command(session_id: str, resume: bool, budget_usd: str) -> list[str]:
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
        budget_usd,
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


def _reap_popen(job_id: str) -> int | None:
    """Poll our recorded Popen (if any) and drop it once it has exited.

    Returns the exit code if the process has now terminated (which also
    reaps the zombie), None if it is still running or we don't hold a
    Popen for this job.
    """
    proc = _LIVE_POPENS.get(job_id)
    if proc is None:
        return None
    code = proc.poll()
    if code is not None:
        _LIVE_POPENS.pop(job_id, None)
    return code


def _finalize_if_dead(meta_path: Path, meta: dict) -> dict:
    """Mark a job finished if the evidence on disk says it is.

    Order of evidence, most to least authoritative:
      1. `ended_at` already recorded → nothing to do.
      2. `exit_code` file exists → wrapper reached its final `echo $?`,
         so CC exited cleanly and the job is done. This is checked
         BEFORE the pid because a zombie satisfies `os.kill(pid, 0)`
         (Cowork's 2026-09-02 bug report), and a pid check first would
         report "running" forever with the exit_code file sitting on
         disk unread.
      3. Our Popen recorded on spawn returns from `.poll()` with a
         non-None code → wrapper has exited but for some reason (kill,
         write failure) never wrote `exit_code`. This also reaps the
         zombie.
      4. Neither file nor Popen, and the pid is dead → wrapper was
         killed and cleaned up out from under us. Mark done, exit
         code unknown.

    Anything else → still running.
    """
    if meta.get("ended_at"):
        return meta

    job_id = meta.get("job_id", "")
    exit_path = meta_path.parent / "exit_code"

    exit_code: int | str | None = None
    if exit_path.is_file():
        raw = exit_path.read_text().strip()
        try:
            exit_code = int(raw)
        except ValueError:
            exit_code = raw or "unknown"
        # A file on disk means done; opportunistically reap our Popen
        # so it doesn't linger as a zombie.
        _reap_popen(job_id)
    else:
        popen_code = _reap_popen(job_id)
        if popen_code is not None:
            exit_code = popen_code
        else:
            pid = meta.get("pid")
            if pid and _pid_alive(pid):
                return meta  # still running
            exit_code = "unknown"

    meta["ended_at"] = _now_iso()
    meta["ended_ts"] = time.time()
    meta["exit_code"] = exit_code
    _save_meta(meta_path, meta)
    return meta


def _active_in_cwd(cwd: Path) -> str | None:
    cwd_str = str(cwd)
    for meta_path in _iter_jobs():
        meta = json.loads(meta_path.read_text())
        if meta.get("cwd") != cwd_str:
            continue
        # Finalize first, then check ended_at — a zombie wrapper with
        # an exit_code file on disk would otherwise show as running
        # forever and hold the cwd lock indefinitely.
        meta = _finalize_if_dead(meta_path, meta)
        if not meta.get("ended_at"):
            return meta["job_id"]
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


def _spawn(cmd: list[str], cwd: Path, run_dir: Path, prompt: str, job_id: str) -> int:
    """Spawn CC in the background and capture its eventual exit code.

    Uses a `bash -c '<cmd>; echo $? > exit_code'` wrapper so the exit
    code survives after the process exits — Popen's exit code is only
    available if the launcher process waits, and this launcher returns
    immediately. The recorded pid is the wrapper bash pid, which lives
    exactly as long as the CC session it wraps.

    Also records the Popen in `_LIVE_POPENS` so the status-path can
    `.poll()` and reap the wrapper zombie. Without that, a long-running
    MCP server accumulates <defunct> processes and `os.kill(pid, 0)`
    keeps returning success — the bug Cowork's first MCP-side call to
    Test 1 hit.

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
    _LIVE_POPENS[job_id] = proc
    # Write the prompt and close stdin so CC sees EOF and starts working.
    # Popen won't block on this write for prompts under the pipe buffer size
    # (64KB on macOS); larger prompts would need a background writer.
    if proc.stdin:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
    return proc.pid


def start_run(task_file: str, cwd: str, budget_usd: float | int | str | None = None) -> str:
    """Start a fresh headless CC run. Returns job_id."""
    cwd_p = _validate_cwd(cwd)
    tf = _validate_task_file(task_file, cwd_p)
    budget_str = _validate_budget(budget_usd)

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
    # The prompt tells CC to DO the work, not consider it. Written this way
    # because on 2026-09-02 a bare "Read <file>" prompt made a real headless
    # run stop to ask whether it should proceed — with nobody on the other
    # end of --print to answer. Do not "simplify" this back to `Read X`:
    # a --print session that asks a question is a dead run that still costs
    # money. `start_ask` passes the caller's prompt through unchanged (those
    # come from Cowork and are already imperative).
    prompt = (
        f"Read {task_rel} and do the work in it now. This is a headless "
        "session — there is no one to answer questions, so do not stop to "
        "ask for confirmation. If you hit a decision you cannot make, "
        "write the options, the costs and your recommendation into the "
        "task file, then keep going with everything that is not blocked "
        "by it. Finish by appending the dated report section and the "
        "ask-list checklist and updating the frontmatter status."
    )
    cmd = _cc_command(session_id, resume=False, budget_usd=budget_str)

    pre_bytes = tf.read_bytes()
    (run_dir / "pre.txt").write_bytes(pre_bytes)

    pid = _spawn(cmd, cwd_p, run_dir, prompt, job_id)

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
        "budget_usd": budget_str,
    }
    _save_meta(run_dir / "meta.json", meta)
    return job_id


def start_ask(
    prev_job_id: str,
    prompt: str,
    budget_usd: float | int | str | None = None,
) -> str:
    """Resume an existing CC session with a follow-up prompt. Returns new job_id."""
    _, prev_meta = _load_meta(prev_job_id)
    cwd_p = Path(prev_meta["cwd"])
    session_id = prev_meta["session_id"]
    task_file = Path(prev_meta["task_file"])
    task_rel = prev_meta.get("task_file_rel", str(task_file))
    # cc_ask with no budget inherits the prior job's cap, falling back
    # to the default for jobs recorded before budget_usd was stored.
    if budget_usd is None:
        inherited = prev_meta.get("budget_usd")
        budget_str = _validate_budget(inherited) if inherited is not None else MAX_BUDGET_USD
    else:
        budget_str = _validate_budget(budget_usd)

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

    cmd = _cc_command(session_id, resume=True, budget_usd=budget_str)

    # Cache the task file contents so a follow-up ask's contract check
    # measures what THIS ask added.
    pre_bytes = task_file.read_bytes() if task_file.is_file() else b""
    (run_dir / "pre.txt").write_bytes(pre_bytes)

    pid = _spawn(cmd, cwd_p, run_dir, prompt, job_id)

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
        "budget_usd": budget_str,
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
    # `load_job` runs `_finalize_if_dead` before handing back the view,
    # so `ended_at` is the authoritative signal. Do NOT re-check the
    # pid here — a zombie wrapper satisfies `os.kill(pid, 0)` and would
    # flip a job that already finished back to "running" (the
    # regression Cowork reported 2026-09-02).
    return not view.meta.get("ended_at")


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
