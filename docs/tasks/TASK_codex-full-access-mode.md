---
title: "TASK — add explicit full-access Codex bridge mode"
date: 2026-11-16
for: Codex (Chat bridge)
status: done
---

# Goal

Update the Codex bridge permissions so explicitly authorized Codex jobs can perform the repository workflow blocked by `workspace-write` (Git metadata writes, local loopback test servers, and network operations such as pushing). All implementation work must be done by Codex.

# Requirements

1. Inspect repository guidance and the current Codex CLI's actual `exec` / `resume` help before choosing flags. Do not guess unsupported command syntax.
2. Preserve `codex_run` as read-only and `codex_write_run` as workspace-write. Do not silently widen either existing mode.
3. Add a separately named, clearly opt-in full-access Codex execution path with matching run and resume tools. It must use the CLI's supported unrestricted filesystem/network sandbox mode, retain Codex automatic review where supported, preserve repository trust checks, and must not use any approval/sandbox bypass flag.
4. Keep existing `cwd` and task-file validation. Explicitly document that the full-access mode can read/write outside `cwd` and access the network, and should only be called for tasks the user explicitly authorized. The repository restriction is input validation, not a sandbox boundary.
5. Update canonical README docs and any central tool descriptions so the permission difference is unmistakable. Preserve Claude tools and the separate sanctioned `cc_push` contract unless the CLI permission mode makes that impossible.
6. Add focused offline coverage for command construction, backend lookup/dispatch, and tool descriptions if the project conventions permit. Do not run the test suite in this task.
7. Keep changes scoped to this feature. Do not commit, push, publish, or modify other repositories.
8. Update this task's status and append a dated report with checked items for every requirement, files changed, documentation, and checks run or not run. Do not claim tests were run.

# Done when

A separate full-access Codex path is available and clearly gated/documented, current safer paths remain unchanged, focused tests describe the command contract, and this task file records the implementation accurately.

## Report — 2026-11-16

- [x] Requirement 1 — Read this brief, the canonical README, applicable ancestor `CLAUDE.md`, and the existing backend/runner/server and test conventions. Checked for applicable `AGENTS.md` files. Inspected the installed `codex --version` (`0.164.0`), `codex exec --help`, and `codex exec resume --help` before choosing flags. Consulted the OpenAI Docs skill and [official OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) for the automatic-review settings.
- [x] Requirement 2 — Preserved `codex_run` / `codex_ask` as read-only and `codex_write_run` / `codex_write_ask` as workspace-write with `--approve-for-me`. Their existing command construction and prompts remain unchanged in behavior.
- [x] Requirement 3 — Added the `codex-full-access` backend and explicit `codex_full_access_run` / `codex_full_access_ask` MCP tools. Starts use `--sandbox danger-full-access`; resumes use the supported `-c 'sandbox_mode="danger-full-access"'` override because resume does not expose `--sandbox`. Both configure `approval_policy="on-request"` and `approvals_reviewer="auto_review"` separately from the workspace-write convenience flag. Eligible approval requests retain automatic review; actions already allowed by full access do not require review. No approval/sandbox/hook-trust bypass, repository-check skip, or execpolicy-rule skip is introduced. Full-access follow-ups accept only full-access metadata; `cc_ask` and the safer resume tools refuse those jobs, requiring the explicitly named full-access tool. Shared status/result/wait/kill/list handling recognizes the new backend.
- [x] Requirement 4 — Kept existing cwd and task-file validation. The full-access prompt, README, central MCP instructions, tool descriptions, and config policy comment state that full access can read/write outside cwd and access the network. Repository restrictions validate inputs rather than sandboxing execution. Calls require explicit user authorization as a caller obligation; selecting a tool does not authorize committing, pushing, or publishing. No external MCP client configuration was changed.
- [x] Requirement 5 — Updated canonical README permission comparisons, command details, authorization expectations, automatic-review limitations, and publishing/containment language. Updated central MCP instructions and descriptions. Claude command flags, allow/deny lists, and `cc_push` implementation/signature remain unchanged; publishing descriptions distinguish Claude's sanctioned `cc_push` workflow from explicitly authorized full-access Codex pushes.
- [x] Requirement 6 — Added focused offline pytest coverage for full-access start/resume argv, unchanged safer-mode argv, backend lookup/classification, persisted metadata and resume dispatch, refusal of permission-mode upgrades and implicit full-access continuation, cwd/task validation (including symlinks), MCP wrapper forwarding, and tool descriptions. **Tests were not run**, as requested.
- [x] Requirement 7 — All implementation was performed by Codex and changes stayed in this project and feature scope. Did not commit, push, publish, edit other repositories, change client settings, or launch a full-access child session.
- [x] Requirement 8 — Set this task's status to `done` and appended this dated report with every requirement, files changed, documentation, and checks performed or omitted.

Files changed:

- `src/claude_code_bridge/backends.py` — full-access adapter, automatic-review overrides, and backend lookup/classification.
- `src/claude_code_bridge/runner.py` — explicit full-access run/resume entry points, task prompt, and refusal of implicit continuation through `cc_ask`.
- `src/claude_code_bridge/server.py` — two MCP tools, descriptions, and central instructions.
- `src/claude_code_bridge/config.py` — clarify that cwd validation is not a filesystem sandbox; no policy values changed.
- `tests/test_codex_full_access.py` — new offline regression coverage, intentionally unrun.
- `README.md` — canonical permission, authorization, CLI, review, and publishing documentation.
- `docs/tasks/TASK_codex-full-access-mode.md` — status and execution record (this file was already untracked at session start).

Checks performed: inspected installed CLI version and both help surfaces;
parsed all five changed/new Python files with `ast.parse`; reviewed the full
diff and added tests; ran `git diff --check`; inspected final Git status.
No test suite, individual pytest tests, smoke scripts, live Codex jobs,
loopback servers, or network publishing operations were run. Runtime permission
behavior and automatic review were not exercised in this workspace-write
session. Managed CLI/host restrictions may reject full access; the bridge does
not attempt to bypass them.

At the final read-only check, this repository had zero commits ahead of its
configured upstream. This session's changes remain uncommitted and local,
as requested.
