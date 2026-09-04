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
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import re

from .config import (
    ALLOWED_TOOLS,
    DEFAULT_JOB_TIMEOUT_S,
    DISALLOWED_TOOLS,
    MAX_BUDGET_CEILING_USD,
    MAX_BUDGET_USD,
    MAX_JOB_TIMEOUT_S,
    PERMISSION_MODE,
    RUNS_DIR,
    WORKSPACE_ROOT,
)


# Grace period between SIGTERM and SIGKILL when the bridge kills a job.
# 5 seconds is plenty for a process that is going to exit; anything
# longer is a process that will not go quietly, and SIGKILL is the
# right next move. Same value for explicit cc_kill and wall-clock reap.
_KILL_GRACE_S = 5.0

# Poll interval while waiting for a signalled process to exit. 0.1s is
# fine — the grace period is measured in seconds, not milliseconds.
_KILL_POLL_S = 0.1

# Default and poll interval for cc_wait. The default (five minutes) is
# a compromise: long enough that a caller does not sit in a tight
# poll-sleep-poll ladder for a job that finishes on human timescales,
# short enough that a stuck wait does not sit forever unattended.
# The wait ceiling is always the job's own timeout — see wait_for_job.
DEFAULT_WAIT_TIMEOUT_S = 5 * 60
_WAIT_POLL_S = 1.0


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


def _signal_pgid(pid: int, sig: int) -> None:
    """Send `sig` to the process group led by `pid`.

    `_spawn` launches every job with `start_new_session=True`, which makes
    the bash wrapper a session (and process-group) leader. Signalling the
    group instead of just the wrapper pid means the claude subprocess and
    any Bash tool it started are all included — without this, killing the
    wrapper leaves orphaned children behind.

    Fails silently on ProcessLookupError (the group is already gone) and
    PermissionError (caller cannot signal it — nothing to do here).
    """
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_dead(pid: int, timeout_s: float) -> bool:
    """Poll until pid is dead or the timeout expires. Returns True if dead."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(_KILL_POLL_S)
    return not _pid_alive(pid)


def _terminate_pid(pid: int) -> str:
    """SIGTERM the pid's group, wait, SIGKILL if still alive. Returns signal used.

    Used by both `kill_job` (caller-initiated) and `_finalize_if_dead`'s
    timeout branch (bridge-initiated). Same behaviour either way so a
    killed-by-user job and a timed-out job look the same from the
    OS's perspective — only the reason recorded in meta differs.
    """
    _signal_pgid(pid, signal.SIGTERM)
    if _wait_dead(pid, _KILL_GRACE_S):
        return "SIGTERM"
    _signal_pgid(pid, signal.SIGKILL)
    # A short second wait so meta records reflect a truly-dead pid; if
    # the process still isn't gone after this, it is stuck in D-state
    # and there is nothing further a signal can do.
    _wait_dead(pid, 2.0)
    return "SIGKILL"


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


def _validate_timeout(timeout_s: float | int | str | None) -> float:
    """Return a per-run timeout in seconds, or raise BridgeError.

    Zero, negative, non-numeric and above-ceiling values are rejected —
    same shape as `_validate_budget`. The default is DEFAULT_JOB_TIMEOUT_S
    (45 minutes); the ceiling is MAX_JOB_TIMEOUT_S. A caller who genuinely
    needs a longer session (a books or F&I sweep, say) sets it per run.
    """
    if timeout_s is None:
        return float(DEFAULT_JOB_TIMEOUT_S)
    ceiling = float(MAX_JOB_TIMEOUT_S)
    try:
        value = float(timeout_s)
    except (TypeError, ValueError) as exc:
        raise BridgeError(
            f"timeout_s must be a number (got {timeout_s!r}); "
            f"ceiling is {ceiling:.0f}s"
        ) from exc
    if value != value or value <= 0:  # NaN or non-positive
        raise BridgeError(
            f"timeout_s must be greater than 0 (got {value}); "
            f"ceiling is {ceiling:.0f}s"
        )
    if value > ceiling:
        raise BridgeError(
            f"timeout_s {value} exceeds ceiling {ceiling:.0f}s; "
            "raise MAX_JOB_TIMEOUT_S in config.py deliberately"
        )
    return value


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


# Every git subprocess in the recovery-point helper runs with this timeout.
# 5 seconds is plenty for local git; anything longer is a hung or
# network-backed repo and we would rather skip the snapshot than block a
# spawn. The helper never lets a git failure fail the run.
_GIT_TIMEOUT_S = 5.0

# Wall clock on cc_push. A push that hangs on the network must not hang the
# tool; 120s is generous for a real push and firm enough to bound the wait.
_PUSH_TIMEOUT_S = 120.0


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git subprocess with an explicit cwd and a short timeout.

    Kept in one place so the timeout, cwd and text-mode decisions are the
    same everywhere. Callers handle CalledProcessError / TimeoutExpired /
    FileNotFoundError — nothing here raises out of the caller's try/except.
    """
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=True,
    )


def _git_capture(
    args: list[str],
    cwd: Path,
    timeout: float = _GIT_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    """Run a git subprocess without check=True so the caller inspects returncode.

    Used by push_branch where non-zero exits are expected outcomes (no
    upstream yet, remote-tracking ref missing, rejection by remote) and
    should not raise a CalledProcessError we then have to catch.
    """
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _pre_run_git_snapshot(cwd: Path, job_id: str, run_dir: Path) -> dict:
    """Capture the state of `cwd` before the run and anchor a snapshot.

    Returns a dict suitable for `meta.json["pre_run_git"]`. Never raises:
    a cwd that is not a git repository, a hung git, a permission error —
    all record what happened and return, because refusing to spawn on a
    missing recovery point would be worse than not having one. See
    TASK_pre-run-recovery-point.md for the full rationale.

    Writes the verbatim `git status --porcelain` to `pre_git_status.txt`
    in the run directory when git is available (it can be long, so it does
    not go into meta.json).

    On a dirty tree, calls `git stash create` (which writes a commit object
    without touching the working tree, index or stash list) and immediately
    anchors it under `refs/cc-bridge/<job_id>` with `git update-ref`, so a
    later `git gc` cannot collect it. `git stash create` does NOT capture
    untracked files — this is a real limitation and is stated in the README
    alongside the recovery commands. `status_porcelain` at least records
    which untracked paths existed pre-run.
    """
    # First, is this a git repo at all? A non-repo cwd is a normal case,
    # not an error. `rev-parse --is-inside-work-tree` is the cheapest probe.
    try:
        _git(["rev-parse", "--is-inside-work-tree"], cwd)
    except FileNotFoundError:
        return {"git": "git not on PATH"}
    except subprocess.TimeoutExpired:
        return {"git": "git timed out"}
    except subprocess.CalledProcessError:
        return {"git": "not a repository"}

    info: dict = {
        "head_sha": None,
        "branch": None,
        "dirty": False,
        "snapshot_ref": None,
    }

    try:
        info["head_sha"] = _git(["rev-parse", "HEAD"], cwd).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Empty repo, detached weirdness, or hung git — record and press on.
        info["head_sha"] = None

    try:
        info["branch"] = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        info["branch"] = None

    try:
        status = _git(["status", "--porcelain"], cwd).stdout
        (run_dir / "pre_git_status.txt").write_text(status)
        info["dirty"] = bool(status.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        # If we cannot even ask for status, treat the tree as clean —
        # attempting a snapshot on an unknown-state repo is worse than not.
        info["dirty"] = False

    if info["dirty"]:
        try:
            sha = _git(["stash", "create"], cwd).stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            sha = ""
        if sha:
            ref = f"refs/cc-bridge/{job_id}"
            try:
                _git(["update-ref", ref, sha], cwd)
                info["snapshot_ref"] = ref
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                info["snapshot_ref"] = None

    return info


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
      4. Deadline exceeded and the wrapper is still alive → the
         wall-clock ceiling from `deadline_ts`. Kill the group, mark
         `ended_reason: "timeout"`. Lazy-reap by design (see
         TASK_cc-kill-and-wall-clock-timeout.md): the next call that
         touches the job clears it — including a blocked `cc_run`.
      5. Neither file nor Popen, and the pid is dead → wrapper was
         killed and cleaned up out from under us. Mark done, exit
         code unknown.

    Anything else → still running. `ended_reason` distinguishes a
    normal exit ("finished"), a caller-initiated `cc_kill` ("killed"),
    a wall-clock timeout ("timeout"), and an out-from-under-us death
    ("finished" with exit_code "unknown"). `cc_status` and `cc_result`
    surface this so a killed job is never indistinguishable from one
    that ran to completion.
    """
    if meta.get("ended_at"):
        return meta

    job_id = meta.get("job_id", "")
    exit_path = meta_path.parent / "exit_code"

    exit_code: int | str | None = None
    ended_reason: str = "finished"
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
            deadline_ts = meta.get("deadline_ts")
            if pid and _pid_alive(pid):
                if deadline_ts and time.time() > float(deadline_ts):
                    sig_used = _terminate_pid(pid)
                    _reap_popen(job_id)
                    meta["killed_signal"] = sig_used
                    ended_reason = "timeout"
                    exit_code = "timeout"
                else:
                    return meta  # still running
            else:
                exit_code = "unknown"

    meta["ended_at"] = _now_iso()
    meta["ended_ts"] = time.time()
    meta["exit_code"] = exit_code
    meta.setdefault("ended_reason", ended_reason)
    _save_meta(meta_path, meta)
    return meta


def _active_in_cwd(cwd: Path) -> tuple[str | None, list[dict]]:
    """Return (active_job_id_or_None, list_of_jobs_reaped_on_this_call).

    A "reaped" entry is a job whose `ended_at` was not set when we
    entered the loop but is set now — i.e. `_finalize_if_dead`
    finalized it here, which for the timeout branch means the bridge
    just killed a stale process. Callers use this list to say so
    plainly in their return, so nobody mistakes the reaping for their
    own job failing (see TASK_cc-kill-and-wall-clock-timeout.md Part 2:
    "the next run unblocks itself").
    """
    reaped: list[dict] = []
    cwd_str = str(cwd)
    active: str | None = None
    for meta_path in _iter_jobs():
        meta = json.loads(meta_path.read_text())
        if meta.get("cwd") != cwd_str:
            continue
        was_ended = bool(meta.get("ended_at"))
        # Finalize first, then check ended_at — a zombie wrapper with
        # an exit_code file on disk would otherwise show as running
        # forever and hold the cwd lock indefinitely.
        meta = _finalize_if_dead(meta_path, meta)
        if not was_ended and meta.get("ended_at"):
            reaped.append(
                {
                    "job_id": meta["job_id"],
                    "reason": meta.get("ended_reason", "finished"),
                }
            )
        if active is None and not meta.get("ended_at"):
            active = meta["job_id"]
    return active, reaped


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


def start_run(
    task_file: str,
    cwd: str,
    budget_usd: float | int | str | None = None,
    timeout_s: float | int | str | None = None,
) -> dict:
    """Start a fresh headless CC run.

    Returns a dict: {"job_id": <id>, "reaped": [{"job_id": ..., "reason": ...}, ...]}.
    The `reaped` list names any stale jobs finalized on this call —
    typically empty, but a timed-out prior job in the same cwd will
    show up here, and the caller should say so in its own return so
    nobody mistakes the reaping for their own job failing.
    """
    cwd_p = _validate_cwd(cwd)
    tf = _validate_task_file(task_file, cwd_p)
    budget_str = _validate_budget(budget_usd)
    timeout_val = _validate_timeout(timeout_s)

    active, reaped = _active_in_cwd(cwd_p)
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

    pre_run_git = _pre_run_git_snapshot(cwd_p, job_id, run_dir)

    started_ts = time.time()
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
        "started_ts": started_ts,
        "pid": pid,
        "task_file_size_before": len(pre_bytes),
        "task_file_sha256_before": _sha256(pre_bytes),
        "budget_usd": budget_str,
        "timeout_s": timeout_val,
        "deadline_ts": started_ts + timeout_val,
        "pre_run_git": pre_run_git,
    }
    _save_meta(run_dir / "meta.json", meta)
    return {"job_id": job_id, "reaped": reaped}


def start_ask(
    prev_job_id: str,
    prompt: str,
    budget_usd: float | int | str | None = None,
    timeout_s: float | int | str | None = None,
) -> dict:
    """Resume an existing CC session with a follow-up prompt.

    Returns {"job_id": <new_id>, "reaped": [...]}. Timeout inherits
    from the prior job when not specified, falling back to the default
    for pre-timeout jobs — same shape as budget inheritance.
    """
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
    if timeout_s is None:
        inherited_t = prev_meta.get("timeout_s")
        timeout_val = _validate_timeout(inherited_t) if inherited_t is not None else float(DEFAULT_JOB_TIMEOUT_S)
    else:
        timeout_val = _validate_timeout(timeout_s)

    if not cwd_p.is_dir():
        raise BridgeError(f"prior cwd no longer exists: {cwd_p}")

    active, reaped = _active_in_cwd(cwd_p)
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

    pre_run_git = _pre_run_git_snapshot(cwd_p, job_id, run_dir)

    started_ts = time.time()
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
        "started_ts": started_ts,
        "pid": pid,
        "task_file_size_before": len(pre_bytes),
        "task_file_sha256_before": _sha256(pre_bytes),
        "budget_usd": budget_str,
        "timeout_s": timeout_val,
        "deadline_ts": started_ts + timeout_val,
        "pre_run_git": pre_run_git,
    }
    _save_meta(run_dir / "meta.json", meta)
    return {"job_id": job_id, "reaped": reaped}


def kill_job(job_id: str) -> dict:
    """Stop a running job on purpose. Only kills the pid recorded in meta.

    Behaviour (Part 1 of TASK_cc-kill-and-wall-clock-timeout.md):

      1. Already-ended job → return unchanged, describe what ended it.
      2. Recorded pid not alive → let `_finalize_if_dead` catch up and
         return the resulting state.
      3. Otherwise SIGTERM the process group, wait `_KILL_GRACE_S`,
         SIGKILL if still there. Write `ended_at`, `ended_reason:
         "killed"`, and which signal actually ended it into meta.

    Never accepts a pid, name or pattern from the caller — the pid
    comes from the job's meta.json and nowhere else. `pkill -f` is
    what a human does in an emergency; a tool that can do it would
    also be able to kill an unrelated Claude Code session Chris is
    using at his own keyboard.
    """
    meta_path, meta = _load_meta(job_id)

    if meta.get("ended_at"):
        return {
            "job_id": job_id,
            "already_ended": True,
            "ended_at": meta["ended_at"],
            "ended_reason": meta.get("ended_reason", "finished"),
            "pid": meta.get("pid"),
        }

    pid = meta.get("pid")
    if not pid:
        raise BridgeError(f"job {job_id} has no recorded pid")

    cwd = Path(meta["cwd"])

    if not _pid_alive(pid):
        # Wrapper is already gone (or was cleaned up out from under us).
        # Let _finalize_if_dead put ended_at on it, then say so.
        meta = _finalize_if_dead(meta_path, meta)
        return {
            "job_id": job_id,
            "pid": pid,
            "already_dead": True,
            "ended_at": meta.get("ended_at"),
            "ended_reason": meta.get("ended_reason", "finished"),
            "cwd_free": _active_in_cwd(cwd)[0] is None,
        }

    sig_used = _terminate_pid(pid)
    _reap_popen(job_id)

    meta["ended_at"] = _now_iso()
    meta["ended_ts"] = time.time()
    meta["ended_reason"] = "killed"
    meta["killed_signal"] = sig_used
    meta.setdefault("exit_code", "killed")
    _save_meta(meta_path, meta)

    cwd_free = _active_in_cwd(cwd)[0] is None

    return {
        "job_id": job_id,
        "pid": pid,
        "signal": sig_used,
        "ended_at": meta["ended_at"],
        "ended_reason": "killed",
        "cwd_free": cwd_free,
    }


def _validate_wait_timeout(
    timeout_s: float | int | str | None,
    job_timeout_s: float | None,
) -> float:
    """Return a wait timeout in seconds, or raise BridgeError.

    The wait timeout is the caller's ceiling on how long to block; it
    has nothing to do with the job's own wall-clock deadline. Task
    spec: "The wait's timeout is its own, independent of the job's
    wall-clock ceiling, and waiting must never extend, shorten or
    otherwise touch that ceiling." The cap here is only there so a
    caller cannot wait longer than the job could conceivably run —
    that would be dead time, not correctness.
    """
    if timeout_s is None:
        value = float(DEFAULT_WAIT_TIMEOUT_S)
    else:
        try:
            value = float(timeout_s)
        except (TypeError, ValueError) as exc:
            raise BridgeError(
                f"timeout_s must be a number (got {timeout_s!r})"
            ) from exc
        if value != value or value <= 0:  # NaN or non-positive
            raise BridgeError(
                f"timeout_s must be greater than 0 (got {value})"
            )
    if job_timeout_s is not None and value > float(job_timeout_s):
        value = float(job_timeout_s)
    return value


def wait_for_job(
    job_id: str,
    timeout_s: float | int | str | None = None,
) -> tuple["JobView", bool]:
    """Block until the job ends, then return (view, wait_timed_out).

    `wait_timed_out=True` means our own wait deadline expired while the
    job was still running — the job keeps going, its own wall-clock
    ceiling is untouched, and the caller can wait again. This is NOT
    an error and callers must not phrase it as one.

    Returns immediately (no initial sleep) if the job is already
    finished when we look. Otherwise polls at `_WAIT_POLL_S` — a
    modest interval, not a busy loop — and re-reads job state via
    `load_job` on each pass, so `_finalize_if_dead` catches the
    exit_code file the wrapper drops.
    """
    view = load_job(job_id)
    wait_timeout = _validate_wait_timeout(
        timeout_s, view.meta.get("timeout_s")
    )
    if not is_running(view):
        return view, False

    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        time.sleep(min(_WAIT_POLL_S, max(remaining, 0.0)))
        view = load_job(job_id)
        if not is_running(view):
            return view, False

    view = load_job(job_id)
    return view, is_running(view)


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


def _summarize_push_failure(stderr: str) -> str:
    """One-line reason from git push stderr — the operative bit, not a wall.

    git puts the useful line first with '! [rejected]' or 'error:' /
    'fatal:'; we return the first such line, stripped. Falls back to the
    last non-empty line so a differently-shaped failure still says
    something meaningful.
    """
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    if not lines:
        return "git push failed with no message"
    for ln in lines:
        low = ln.lower()
        if ln.startswith("!") or "rejected" in low or low.startswith("error:") or low.startswith("fatal:"):
            return ln
    return lines[-1]


def push_branch(cwd: str, remote: str = "origin") -> dict:
    """Publish the current branch's commits on `remote` for `cwd`.

    Deliberately no argument passthrough beyond cwd and remote — there is
    no way to reach --force, --force-with-lease, --delete, --tags or a
    refspec from outside this function. Same-shape reasoning as cc_kill:
    the sanctioned way through is the tool, and the Bash(git push:*) deny
    in config.py stays as-is.

    Returns a dict describing what happened:
      branch, remote_name, remote_url, sha_before_local,
      sha_before_remote (None for a new branch), sha_after_remote,
      commits_pushed (0 for up_to_date), status
      ("pushed" | "up_to_date" | "new_branch"), message.

    Refuses (raises BridgeError) on:
      - cwd outside WORKSPACE_ROOT (reuses _validate_cwd, the same code
        path cc_run uses).
      - remote name starting with '-' (would otherwise be read as a git flag).
      - the named remote not configured.
      - detached HEAD (names the SHA in the message).
      - a non-fast-forward / otherwise rejected push (reports git's own
        reason on one line and says the branch was not published).
      - git push wall clock past _PUSH_TIMEOUT_S.
    """
    cwd_p = _validate_cwd(cwd)

    if not remote or remote.startswith("-"):
        raise BridgeError(f"remote name {remote!r} is not allowed")

    try:
        remote_url_proc = _git_capture(["remote", "get-url", remote], cwd_p)
    except FileNotFoundError as exc:
        raise BridgeError("git not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("git timed out resolving the remote URL") from exc
    if remote_url_proc.returncode != 0:
        raise BridgeError(
            f"remote {remote!r} is not configured in {cwd_p}"
        )
    remote_url = remote_url_proc.stdout.strip()

    branch_proc = _git_capture(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd_p)
    if branch_proc.returncode != 0:
        head_proc = _git_capture(["rev-parse", "HEAD"], cwd_p)
        head = head_proc.stdout.strip() or "unknown"
        raise BridgeError(
            f"detached HEAD at {head[:7]}; check out a branch before pushing"
        )
    branch = branch_proc.stdout.strip()

    local_sha = _git_capture(["rev-parse", "HEAD"], cwd_p).stdout.strip()

    remote_ref = f"refs/remotes/{remote}/{branch}"
    remote_before_proc = _git_capture(["rev-parse", "--verify", remote_ref], cwd_p)
    remote_sha_before: str | None
    if remote_before_proc.returncode == 0:
        remote_sha_before = remote_before_proc.stdout.strip()
    else:
        remote_sha_before = None

    upstream_proc = _git_capture(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd_p
    )
    has_upstream = upstream_proc.returncode == 0

    push_argv = ["push"]
    if not has_upstream:
        push_argv.append("--set-upstream")
    push_argv += [remote, branch]

    try:
        push_proc = _git_capture(push_argv, cwd_p, timeout=_PUSH_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(
            f"git push timed out after {_PUSH_TIMEOUT_S:.0f}s; "
            f"branch {branch} was not published"
        ) from exc

    if push_proc.returncode != 0:
        reason = _summarize_push_failure(push_proc.stderr or push_proc.stdout)
        raise BridgeError(
            f"push rejected: {reason}; branch {branch} was not published"
        )

    remote_after_proc = _git_capture(["rev-parse", "--verify", remote_ref], cwd_p)
    remote_sha_after = (
        remote_after_proc.stdout.strip()
        if remote_after_proc.returncode == 0
        else local_sha
    )

    new_branch = remote_sha_before is None
    if new_branch:
        # A brand-new branch published its full history. Count reachable
        # commits so the caller sees a real number, not just "created".
        count_proc = _git_capture(["rev-list", "--count", local_sha], cwd_p)
        commits_pushed = int(count_proc.stdout.strip() or "0")
        status = "new_branch"
        message = f"created remote branch {branch} with {commits_pushed} commits"
    elif remote_sha_before == local_sha:
        commits_pushed = 0
        status = "up_to_date"
        message = "already up to date"
    else:
        count_proc = _git_capture(
            ["rev-list", "--count", f"{remote_sha_before}..{local_sha}"], cwd_p
        )
        commits_pushed = int(count_proc.stdout.strip() or "0")
        status = "pushed"
        message = f"pushed {commits_pushed} commit{'s' if commits_pushed != 1 else ''}"

    return {
        "branch": branch,
        "remote_name": remote,
        "remote_url": remote_url,
        "sha_before_local": local_sha,
        "sha_before_remote": remote_sha_before,
        "sha_after_remote": remote_sha_after,
        "commits_pushed": commits_pushed,
        "status": status,
        "message": message,
    }
