---
title: "TASK — make documentation closure part of Codex write jobs"
date: 2026-11-16
for: Codex (Chat bridge)
status: done
---

# Goal

Make every Codex write task preserve project knowledge so future work does not depend on someone remembering undocumented decisions.

# Requirements

1. Update the workspace-write Codex prompt in `src/claude_code_bridge/runner.py` so it tells Codex to:
   - Read the task file and applicable repository guidance before editing. Check `AGENTS.md`, `CLAUDE.md` when present, and relevant existing documentation.
   - Treat documentation as part of completing implementation work. When a change affects user-visible behavior, setup/configuration, interfaces, architecture, or workflows, update the existing canonical documentation in the same task. Prefer maintaining existing docs over creating duplicates.
   - If no product-documentation change is warranted, explain why in the task report rather than adding a needless document.
   - Keep the `TASK_*.md` file as the durable work record: set its frontmatter status to `done` when complete, append a dated `## Report — YYYY-MM-DD` section, and include a checked checklist item for every request plus files changed, documentation updated (or why none was needed), and checks run or not run. Do not claim checks that were not performed.
   - Preserve the existing workspace boundary and no-commit/no-push defaults. Do not alter the read-only Codex path or Claude execution behavior.
2. Update the Codex section in `README.md` to document the write-task documentation and report expectations, keeping the existing safety and no-budget notes accurate.
3. Make no unrelated changes. Do not run the test suite for this prompt/documentation change. Do not commit, push, or publish.

# Done when

The default Codex write prompt, README, and this task file all describe the same documentation-closure contract, and only those three files are changed.

## Report — 2026-11-16

- [x] Read the task, applicable parent `CLAUDE.md`, and relevant README documentation; checked for repository `AGENTS.md` and `CLAUDE.md` guidance.
- [x] Updated the workspace-write prompt to require reading guidance and existing docs before editing.
- [x] Made canonical documentation updates part of implementation completion for behavior, setup/configuration, interfaces, architecture, and workflow changes; prefer existing docs and explain in the report when no product-documentation update is warranted.
- [x] Required the task file as the durable record: `status: done` on completion, a dated report, a checked item for every request, files changed, documentation updated or why none was needed, and checks run or not run without claiming unperformed checks.
- [x] Preserved the workspace boundary, scope limits, blocker handling, and no-commit/no-push/no-publish defaults. Left the read-only Codex prompt and Claude execution behavior unchanged.
- [x] Updated the README Codex section with the same documentation and report contract, preserving safety and no-budget notes.
- [x] Changed only the three requested files during this session. Preserved pre-existing changes, including those in `backends.py` and `server.py`.
- [x] Did not run the test suite, commit, push, or publish, as requested.

Files changed: `src/claude_code_bridge/runner.py` (write prompt only),
`README.md` (canonical Codex documentation), and this task file (status and report).
The README update documents the changed write-task workflow; no duplicate
documentation was created.

Checks performed: parsed `runner.py` with Python `ast.parse`; compared the
session-start snapshot to confirm the Claude and read-only prompts were unchanged,
the runner diff was confined to the write prompt, and existing backend/server
changes were preserved; reviewed the prompt and README against every requirement;
ran `git diff --check`. The test suite and live Codex execution were not run
because this task is limited to prompt/documentation changes and prohibits the suite.

## Report — 2026-11-16 — Follow-up: project continuity

User steering: context-kit exists because handoffs and documentation were missed
or overwritten. Closure must preserve persistent, trustworthy project continuity.

- [x] Extended the workspace-write prompt and README contract: when a project includes context-kit or its docs designate it as the continuity system, follow its existing handoff, inventory, and verification workflow where applicable.
- [x] Prohibited inventing a parallel documentation system; projects without context-kit use their existing canonical docs.
- [x] Clarified that the task-file report is a concise execution record, not a replacement for context-kit's handoff.
- [x] Modified only `src/claude_code_bridge/runner.py`, `README.md`, and this task file; preserved the original report as history and retained `status: done`.
- [x] Preserved workspace/scope boundaries, safety and no-budget notes, and the Claude/read-only paths.
- [x] Did not run tests, commit, push, or publish.

Documentation updated: the README Codex contract now records the continuity
workflow requirements alongside the write prompt. No parallel documentation or
handoff system was created.

Checks performed: Python syntax parsing with `ast.parse`, comparison against the
follow-up baseline confirming runner edits are confined to the workspace-write
prompt, `git diff --check`, and a final file-scope/history-preservation comparison.
Tests and live Codex execution were not run, as requested for this prompt/docs work.
