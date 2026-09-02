"""MCP server: five tools that drive Claude Code headless.

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
    list_jobs,
    load_job,
    start_ask,
    start_run,
    tail,
)


_CAP_LINE = (
    "BUDGET: CAP HIT \u2014 output is truncated, the work is probably incomplete"
)


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


def _format_job_line(view) -> str:
    m = view.meta
    running = is_running(view)
    status = "running" if running else "finished"
    if not running and m.get("exit_code") == "unknown":
        status = "finished (exit unknown)"
    return (
        f"{view.job_id}  [{status}]  cwd={m['cwd']}  "
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
        "running in that cwd. Optional `budget_usd` overrides the default "
        "$2.00 per-run cap, up to a ceiling of $20.00; this is Claude "
        "Code's own cost-estimate cap on a subscription, not a bill. "
        "Returns a job_id immediately; the CC session runs in the background."
    ),
    structured_output=False,  # see module docstring
)
def cc_run(task_file: str, cwd: str, budget_usd: float | None = None) -> str:
    """Start a headless CC run on a task file."""
    try:
        job_id = start_run(task_file, cwd, budget_usd=budget_usd)
        return f"Started job {job_id}"
    except Exception as exc:
        return _friendly_error(exc)


@server.tool(
    name="cc_status",
    description=(
        "Report on a job started with cc_run or cc_ask: running/finished, "
        "elapsed seconds, exit code if finished, and the last ~20 lines "
        "of stdout+stderr. Does not block."
    ),
    structured_output=False,
)
def cc_status(job_id: str) -> str:
    """Poll a job."""
    try:
        view = load_job(job_id)
        m = view.meta
        running = is_running(view)
        state = "running" if running else "finished"
        if not running and m.get("exit_code") == "unknown":
            state = "finished (exit unknown)"
        parts = [
            f"job_id: {view.job_id}",
            f"state: {state}",
            f"elapsed: {elapsed(view):.1f}s",
            f"cwd: {m['cwd']}",
            f"session_id: {m['session_id']}",
            f"task_file: {m['task_file']}",
            f"kind: {m.get('kind', 'run')}",
            f"budget: ${m.get('budget_usd', '2.00')}",
        ]
        if m.get("ended_at"):
            parts.append(f"ended_at: {m['ended_at']}")
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
        header = (
            f"--- CONTRACT CHECK ({view.meta['task_file_rel']}) ---\n"
            + (f"{_CAP_LINE}\n" if cap else "")
            + result.render()
            + f"session_id: {view.meta['session_id']}\n"
            + f"exit_code: {view.meta.get('exit_code', 'unknown')}  "
            + f"budget: ${view.meta.get('budget_usd', '2.00')}\n"
            + f"cwd: {view.meta['cwd']}\n"
            + f"elapsed: {elapsed(view):.1f}s\n"
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
        "$2.00, ceiling $20.00; Claude Code cost-estimate cap, not a "
        "bill). Returns a new job_id for the follow-up run."
    ),
    structured_output=False,
)
def cc_ask(job_id: str, prompt: str, budget_usd: float | None = None) -> str:
    """Ask a follow-up in the same session."""
    try:
        new_job_id = start_ask(job_id, prompt, budget_usd=budget_usd)
        return f"Started follow-up job {new_job_id}"
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
