"""Execution-specific command building and result parsing.

The runner owns job lifecycle and process management. Backends own only
their CLI invocation and any machine-readable output they expose.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import ALLOWED_TOOLS, DISALLOWED_TOOLS, PERMISSION_MODE


@dataclass(frozen=True)
class BackendOutput:
    session_ref: str | None = None
    turn_failed: str | None = None
    usage: dict | None = None
    session_ref_mismatch: bool = False
    parse_errors: int = 0


class ExecutionBackend(Protocol):
    name: str

    def start_command(
        self,
        session_ref: str | None,
        output_path: Path,
        budget_usd: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> list[str]: ...

    def resume_command(
        self,
        session_ref: str,
        output_path: Path,
        budget_usd: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> list[str]: ...

    def parse_output(self, stdout: str, expected_session_ref: str | None = None) -> BackendOutput: ...


class ClaudeBackend:
    """Claude CLI adapter; its invocation flags intentionally match the legacy runner."""

    name = "claude"

    def start_command(
        self,
        session_ref: str | None,
        output_path: Path,
        budget_usd: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> list[str]:
        if not session_ref or budget_usd is None:
            raise ValueError("Claude requires a session ID and per-run dollar budget")
        return self._command(session_ref, resume=False, budget_usd=budget_usd)

    def resume_command(
        self,
        session_ref: str,
        output_path: Path,
        budget_usd: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> list[str]:
        if not session_ref or budget_usd is None:
            raise ValueError("Claude requires a session ID and per-run dollar budget")
        return self._command(session_ref, resume=True, budget_usd=budget_usd)

    @staticmethod
    def _command(session_id: str, *, resume: bool, budget_usd: str) -> list[str]:
        # Keep in sync with the previous _cc_command implementation. Prompt
        # placement on stdin is handled by runner._spawn and remains unchanged.
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

    @staticmethod
    def parse_output(stdout: str, expected_session_ref: str | None = None) -> BackendOutput:
        return BackendOutput(session_ref=expected_session_ref)


class CodexBackend:
    """Opt-in Codex adapter. It is explicitly read-only until policy is verified."""

    name = "codex"
    # These levels were present across the bundled models inspected for
    # this CLI version; higher levels are not available on every model.
    _REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}

    def start_command(
        self,
        session_ref: str | None,
        output_path: Path,
        budget_usd: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> list[str]:
        if budget_usd is not None:
            raise ValueError("Codex has no verified per-run dollar budget option")
        if session_ref is not None:
            raise ValueError("Codex creates its own session reference")
        return self._command(resume_ref=None, output_path=output_path, model=model,
                             reasoning_effort=reasoning_effort)

    def resume_command(
        self,
        session_ref: str,
        output_path: Path,
        budget_usd: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> list[str]:
        if budget_usd is not None:
            raise ValueError("Codex has no verified per-run dollar budget option")
        if not session_ref:
            raise ValueError("Codex resume requires a thread ID")
        return self._command(resume_ref=session_ref, output_path=output_path, model=model,
                             reasoning_effort=reasoning_effort)

    @classmethod
    def _command(
        cls,
        *,
        resume_ref: str | None,
        output_path: Path,
        model: str | None,
        reasoning_effort: str | None,
    ) -> list[str]:
        if reasoning_effort is not None and reasoning_effort not in cls._REASONING_EFFORTS:
            raise ValueError(f"unsupported Codex reasoning effort: {reasoning_effort}")
        cmd = ["codex", "exec"]
        if resume_ref is not None:
            cmd += [
                "resume",
                "--json",
                "--ignore-user-config",
                "-c",
                'sandbox_mode="read-only"',
                resume_ref,
            ]
        else:
            cmd += ["--ignore-user-config", "--json", "--sandbox", "read-only"]
        if model:
            cmd += ["--model", model]
        if reasoning_effort:
            cmd += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        cmd += ["--output-last-message", str(output_path), "-"]
        return cmd

    @staticmethod
    def parse_output(stdout: str, expected_session_ref: str | None = None) -> BackendOutput:
        observed_ref = None
        turn_failed = None
        usage = None
        parse_errors = 0
        mismatch = False
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                parse_errors += 1
                continue
            if not isinstance(event, dict):
                parse_errors += 1
                continue
            event_type = event.get("type")
            if event_type == "thread.started":
                ref = event.get("thread_id")
                if isinstance(ref, str) and ref:
                    if observed_ref is None:
                        observed_ref = ref
                    elif observed_ref != ref:
                        mismatch = True
            elif event_type == "turn.completed":
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
            elif event_type == "turn.failed":
                error = event.get("error")
                turn_failed = (
                    error.get("message") if isinstance(error, dict) else str(error or "turn failed")
                )

        if expected_session_ref is not None and observed_ref != expected_session_ref:
            mismatch = True
        return BackendOutput(
            session_ref=observed_ref,
            turn_failed=turn_failed,
            usage=usage,
            session_ref_mismatch=mismatch,
            parse_errors=parse_errors,
        )


_BACKENDS: dict[str, ExecutionBackend] = {
    "claude": ClaudeBackend(),
    "codex": CodexBackend(),
}


def get_backend(name: str | None) -> ExecutionBackend:
    """Resolve persisted backend, treating metadata predating this seam as Claude."""
    backend_name = name or "claude"
    try:
        return _BACKENDS[backend_name]
    except KeyError as exc:
        raise ValueError(f"unknown execution backend: {backend_name}") from exc
