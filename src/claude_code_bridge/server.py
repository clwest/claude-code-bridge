"""MCP server: six tools that drive Claude Code headless.

Notes for future readers:

  1. This uses the mcp>=2 MCPServer API (`mcp.server.mcpserver`). FastMCP
     was removed at 2.0.

  2. Every tool is declared with `structured_output=False`. That is not a
     style choice — the Claude desktop app on macOS (bug
     anthropics/claude-code#80094, filed 2026-07-22) will not dispatch
     tool calls for tools that publish an outputSchema. Once that bug is
     fixed, structured_output can be removed here. Same pattern as
     google-docs-mcp; same reason.

  3. Return values are strings. Callers get plain text; there is no
     schema for the SDK to infer.

  4. The permission surface is written down in config.py and in the
     README. The server enforces it by passing the CLI's own allow/deny
     flags on every spawn. There is no code path here that grants more
     than that.
"""
from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import __version__
from .contract import check as contract_check
from .runner import (
    BridgeError,
    cap_hit,
    elapsed,
    is_running,
    kill_job,
    list_jobs,
    load_job,
    start_ask,
    start_run,
    tail,
)


_CAP_LINE = (
    "BUDGET: CAP HIT \u2014 output is truncated, the work is probably incomplete"
)


def _short(sha: str | None) -> str:
    return sha[:7] if sha else "?"


def _recovery_block(pre_run_git: dict | None) -> str:
    """Render the recovery point for cc_result. At most three lines.

    Sits above the CC output; must not push the actual result off the
    screen. Non-repo / missing snapshot cases collapse to one line.
    """
    if not pre_run_git:
        return "pre-run git: not captured"
    if "git" in pre_run_git:
        # Non-repo, git-not-on-path, timeout — one line, no commands.
        return f"pre-run git: {pre_run_git['git']}"

    head = pre_run_git.get("head_sha")
    branch = pre_run_git.get("branch") or "?"
    dirty = pre_run_git.get("dirty")
    snap = pre_run_git.get("snapshot_ref")

    if not head:
        return "pre-run git: HEAD unavailable"

    short = _short(head)
    if dirty and snap:
        return (
            f"pre-run HEAD: {short} ({branch}), tree dirty\n"
            f"pre-run snapshot: {snap}\n"
            f"recover with: git diff {short}..HEAD    |    "
            f"git stash apply {snap}"
        )
    if dirty and not snap:
        # Dirty but the snapshot step failed — say so, still give the diff.
        return (
            f"pre-run HEAD: {short} ({branch}), tree dirty (snapshot failed)\n"
            f"recover with: git diff {short}..HEAD"
        )
    # Clean tree.
    return (
        f"pre-run HEAD: {short} ({branch}), tree clean \u2014 no snapshot needed\n"
        f"recover with: git diff {short}..HEAD"
    )


def _status_pre_run_line(pre_run_git: dict | None) -> str | None:
    """One-line pre-run HEAD summary for cc_status. No recovery commands."""
    if not pre_run_git:
        return None
    if "git" in pre_run_git:
        return f"pre-run git: {pre_run_git['git']}"
    head = pre_run_git.get("head_sha")
    branch = pre_run_git.get("branch") or "?"
    if not head:
        return "pre-run HEAD: unavailable"
    return f"pre-run HEAD: {_short(head)} ({branch})"


server: MCPServer = MCPServer(
    name="claude-code-bridge",
    version=__version__,
    instructions=(
        "Drive a headless Claude Code session on a TASK_*.md file. Every "
        "run has a fixed allow/deny tool list (see README) and a per-run "
        "cost cap. Follow-ups on the same session use cc_ask. The bridge "
        "does not fix the task file — it reports what CC did and whether "
        "the report contract (dated report section, ask-list checklist, "
        "status update) was met."
    ),
)


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, BridgeError):
        return f"Bridge error: {exc}"
    return f"{exc.__class__.__name__}: {exc}"


def _state_for(view) -> str:
    """Human-readable state including ended_reason.

    "running" while alive; "finished", "killed", "timeout" once done.
    A wrapper that died before writing exit_code shows as
    "finished (exit unknown)" so a lost-child case is visible.
    """
    m = view.meta
    if is_running(view):
        return "running"
    reason = m.get("ended_reason", "finished")
    if reason == "killed":
        return "killed"
    if reason == "timeout":
        return "timeout"
    if m.get("exit_code") == "unknown":
        return "finished (exit unknown)"
    return "finished"


def _format_reaped(reaped: list[dict]) -> str:
    """Render the reap list for the top of a cc_run / cc_ask return.

    Empty list → empty string. Otherwise one line per reaped job so the
    caller sees "reaped stale job X (timeout)" and does not mistake it
    for their own job failing.
    """
    if not reaped:
        return ""
    lines = [
        f"Reaped stale job {r['job_id']} ({r.get('reason', 'finished')})."
        for r in reaped
    ]
    return "\n".join(lines) + "\n"


def _format_job_line(view) -> str:
    m = view.meta
    return (
        f"{view.job_id}  [{_state_for(view)}]  cwd={m['cwd']}  "
        f"task={m.get('task_file_rel', m.get('task_file', '-'))}  "
        f"session={m['session_id']}  kind={m.get('kind', 'run')}"
    )


@server.tool(
    name="cc_run",
    description=(
        "Start a headless Claude Code session in `cwd` on a TASK_*.md file, "
        "telling it to do the work now (not to ask for confirmation). "
        "`task_file` must be an existing TASK_*.md under `cwd`, and `cwd` "
        "must be under ~/Donkey_Betz/. Refuses if another job is already "
        "running in that cwd (a stale job past its wall-clock deadline is "
        "reaped first, and the reap is announced in the return). Optional "
        "`budget_usd` overrides the default $2.00 per-run cap, up to a "
        "ceiling of $50.00; this is Claude Code's own cost-estimate cap on "
        "a subscription, not a bill. Optional `timeout_s` overrides the "
        "default 45-minute wall-clock ceiling, up to 8 hours. Returns a "
        "job_id immediately; the CC session runs in the background."
    ),
    structured_output=False,  # see module docstring
)
def cc_run(
    task_file: str,
    cwd: str,
    budget_usd: float | None = None,
    timeout_s: float | None = None,
) -> str:
    """Start a headless CC run on a task file."""
    try:
        result = start_run(task_file, cwd, budget_usd=budget_usd, timeout_s=timeout_s)
        prefix = _format_reaped(result.get("reaped", []))
        return f"{prefix}Started job {result['job_id']}"
    except Exception as exc:
        return _friendly_error(exc)


@server.tool(
    name="cc_status",
    description=(
        "Report on a job started with cc_run or cc_ask: running/finished "
        "(or 'finished (exit unknown)' if the wrapper died before writing "
        "the exit code), elapsed seconds, budget, and the last ~20 lines "
        "of stdout+stderr. Does not block. The numeric exit code is on "
        "cc_result, not here."
    ),
    structured_output=False,
)
def cc_status(job_id: str) -> str:
    """Poll a job."""
    try:
        view = load_job(job_id)
        m = view.meta
        state = _state_for(view)
        timeout_val = m.get("timeout_s")
        timeout_str = f"{float(timeout_val):.0f}s" if timeout_val is not None else "?"
        parts = [
            f"job_id: {view.job_id}",
            f"state: {state}",
            f"elapsed: {elapsed(view):.1f}s",
            f"cwd: {m['cwd']}",
            f"session_id: {m['session_id']}",
            f"task_file: {m['task_file']}",
            f"kind: {m.get('kind', 'run')}",
            f"budget: ${m.get('budget_usd', '2.00')}",
            f"timeout: {timeout_str}",
        ]
        if m.get("ended_at"):
            parts.append(f"ended_at: {m['ended_at']}")
            reason = m.get("ended_reason")
            if reason and reason != "finished":
                sig = m.get("killed_signal")
                if sig:
                    parts.append(f"ended_reason: {reason} ({sig})")
                else:
                    parts.append(f"ended_reason: {reason}")
        pre_line = _status_pre_run_line(m.get("pre_run_git"))
        if pre_line:
            parts.append(pre_line)
        if cap_hit(view):
            parts.append(_CAP_LINE)
        stdout_tail = tail(view.stdout_path)
        stderr_tail = tail(view.stderr_path)
        if stdout_tail:
            parts.append("\n--- stdout (tail) ---\n" + stdout_tail)
        if stderr_tail:
            parts.append("\n--- stderr (tail) ---\n" + stderr_tail)
        return "\n".join(parts)
    except Exception as exc:
        return _friendly_error(exc)


@server.tool(
    name="cc_result",
    description=(
        "Return the final output of a finished job, prefixed with the "
        "CONTRACT CHECK on its task file: whether a dated ## Report "
        "section was appended, whether the report contains an ask-list "
        "checklist, and the current frontmatter status. Errors if the "
        "job is still running."
    ),
    structured_output=False,
)
def cc_result(job_id: str) -> str:
    """Get final output plus contract check."""
    try:
        view = load_job(job_id)
        if is_running(view):
            return f"job {view.job_id} still running (elapsed {elapsed(view):.1f}s)"
        task_file = Path(view.meta["task_file"])
        pre_text = ""
        if view.pre_path.is_file():
            pre_text = view.pre_path.read_text(errors="replace")
        result = contract_check(task_file, pre_text)
        stdout_text = view.stdout_path.read_text(errors="replace") if view.stdout_path.is_file() else ""
        stderr_text = view.stderr_path.read_text(errors="replace") if view.stderr_path.is_file() else ""
        cap = cap_hit(view)
        m = view.meta
        reason = m.get("ended_reason", "finished")
        reason_line = ""
        if reason != "finished":
            sig = m.get("killed_signal")
            reason_line = (
                f"ended_reason: {reason}"
                + (f" ({sig})" if sig else "")
                + "\n"
            )
        header = (
            f"--- CONTRACT CHECK ({m['task_file_rel']}) ---\n"
            + (f"{_CAP_LINE}\n" if cap else "")
            + reason_line
            + result.render()
            + f"session_id: {m['session_id']}\n"
            + f"exit_code: {m.get('exit_code', 'unknown')}  "
            + f"budget: ${m.get('budget_usd', '2.00')}\n"
            + f"cwd: {m['cwd']}\n"
            + f"elapsed: {elapsed(view):.1f}s\n"
            + _recovery_block(m.get("pre_run_git")) + "\n"
        )
        body = "\n--- CC OUTPUT ---\n" + (stdout_text or "(empty)")
        if stderr_text.strip():
            body += "\n\n--- CC STDERR ---\n" + stderr_text
        return header + body
    except Exception as exc:
        return _friendly_error(exc)


@server.tool(
    name="cc_ask",
    description=(
        "Send a follow-up prompt into the SAME CC session as a prior "
        "job (resumed by session_id). Use this to say 'you skipped the "
        "ask list, please add it' or 'clarify X'. Optional `budget_usd` "
        "overrides the inherited budget from the prior job (defaults to "
        "$2.00, ceiling $50.00; Claude Code cost-estimate cap, not a "
        "bill). Optional `timeout_s` overrides the inherited wall-clock "
        "ceiling from the prior job (default 45 minutes, ceiling 8 "
        "hours). Returns a new job_id for the follow-up run."
    ),
    structured_output=False,
)
def cc_ask(
    job_id: str,
    prompt: str,
    budget_usd: float | None = None,
    timeout_s: float | None = None,
) -> str:
    """Ask a follow-up in the same session."""
    try:
        result = start_ask(job_id, prompt, budget_usd=budget_usd, timeout_s=timeout_s)
        prefix = _format_reaped(result.get("reaped", []))
        return f"{prefix}Started follow-up job {result['job_id']}"
    except Exception as exc:
        return _friendly_error(exc)


@server.tool(
    name="cc_kill",
    description=(
        "Stop a running job on purpose. Only kills the pid recorded in "
        "the job's meta.json — never a pid, name or pattern from the "
        "caller. Sends SIGTERM, waits 5 seconds, sends SIGKILL if the "
        "process is still alive. Records `ended_reason: killed` and "
        "which signal actually ended it, so a killed job is never "
        "indistinguishable from one that finished. Says whether the "
        "cwd is free afterwards. On a job that has already ended, "
        "returns unchanged and says so."
    ),
    structured_output=False,
)
def cc_kill(job_id: str) -> str:
    """Stop a running job. Only the pid recorded in that job's meta."""
    try:
        result = kill_job(job_id)
        if result.get("already_ended"):
            reason = result.get("ended_reason", "finished")
            return (
                f"Job {job_id} was already ended ({reason}) at "
                f"{result.get('ended_at')} — nothing to kill."
            )
        if result.get("already_dead"):
            reason = result.get("ended_reason", "finished")
            cwd_note = (
                "cwd is free."
                if result.get("cwd_free")
                else "cwd is still held by another job."
            )
            return (
                f"Job {job_id} (pid {result.get('pid')}) was already dead; "
                f"finalized as {reason}. {cwd_note}"
            )
        cwd_note = (
            "cwd is free."
            if result.get("cwd_free")
            else "cwd is still held by another job."
        )
        return (
            f"Killed job {job_id} (pid {result['pid']}) with "
            f"{result['signal']}. {cwd_note}"
        )
    except Exception as exc:
        return _friendly_error(exc)


@server.tool(
    name="cc_list",
    description=(
        "List every job this bridge has started, oldest first, with cwd "
        "and current state. Useful to find the job_id of a run whose id "
        "was not written down."
    ),
    structured_output=False,
)
def cc_list() -> str:
    """List all jobs."""
    try:
        views = list_jobs()
        if not views:
            return "(no jobs yet)"
        return "\n".join(_format_job_line(v) for v in views)
    except Exception as exc:
        return _friendly_error(exc)


def run() -> None:
    """Entry point for the MCP server (stdio transport)."""
    server.run(transport="stdio")
