---
title: "TASK — remove WebFetch and WebSearch from the unattended allowlist"
date: 2026-09-02
written_by: Claude (Cowork)
for: Claude Code, via claude-code-bridge (this is the bridge modifying itself)
status: done
---

# TASK — remove WebFetch and WebSearch from the allowlist

## Why

Chris's decision, 2026-09-02, in his words: "there's no need for web fetch and web search since you can do it. When I was using CC for it we needed it then." Cowork has its own web tools; a headless CC session does not need network beyond git. The brief that built this server said no network except git; these two slipped in.

## What to do

1. In `src/claude_code_bridge/config.py`, remove `WebFetch` and `WebSearch` from the allowed-tools list. Leave a one-line comment where they were: `# WebFetch/WebSearch deliberately absent — Cowork does web; headless CC gets no network beyond git (Chris, 2026-09-02).`
2. In `README.md`, section "What this server can do to this machine unattended", remove the mention of the built-in web tools from the Allowed list and add them to the "Not passed to CC at all" list with the same one-line reason.
3. Run the test suite. It must pass.
4. Commit with message `Cut WebFetch/WebSearch from the unattended allowlist (Chris, 2026-09-02)`. Do not push — the bridge denies it anyway; Chris or the next terminal session pushes.

## Non-goals

Do not touch any other tool in the allowlist or denylist. Do not change anything in `runner.py`.

## Done means

- [x] `grep -c WebFetch src/claude_code_bridge/config.py` returns 0 apart from the comment line
- [x] README updated as above
- [x] Tests pass
- [x] Committed, not pushed
- [x] Report appended with the ask list; status `done`

## Report — 2026-09-02

Cut `WebFetch` and `WebSearch` from `ALLOWED_TOOLS` in `src/claude_code_bridge/config.py`, replacing the two list entries with the one-line placeholder comment from the brief. Also rewrote the block comment sitting above the list — it had previously said "network fetches beyond WebFetch/WebSearch (which are Claude's own audited tools)" — so the file no longer contradicts itself. Updated the README's Allowed list (dropped the "built-in web tools" clause) and added a bullet at the top of the "Not passed to CC at all" list with the same reason and dated attribution. Tests pass (`pytest -q` → 5 passed). `grep -c WebFetch src/claude_code_bridge/config.py` returns 1 — the single placeholder comment line, matching the "0 apart from the comment line" criterion.

Two commits: the code+README change with the task's specified message, then the task file itself (report + status flip). Neither pushed. This repo will be one commit ahead of `origin/main` after the code commit and two after the task-file commit; no risk — the change is local-only and reversible, and Chris pushes when he's ready.

### Ask list

- [x] Remove `WebFetch`/`WebSearch` from `ALLOWED_TOOLS` in `config.py`, leaving the one-line placeholder comment.
- [x] README: remove the built-in web tools mention from the Allowed list.
- [x] README: add `WebFetch`/`WebSearch` to the "Not passed to CC at all" list with the same one-line reason.
- [x] Tests pass.
- [x] Commit with the specified message; do not push.
- [x] Do not touch any other tool in the allowlist or denylist.
- [x] Do not change anything in `runner.py`.

### Note on the brief

The brief's done-check `grep -c WebFetch src/claude_code_bridge/config.py` is worded as "returns 0 apart from the comment line". `grep -c` counts matching lines, so with the required placeholder comment present the literal count is 1, not 0. Read as intent — "no other occurrences beyond the placeholder" — the check passes. Also: the block comment above the list previously named `WebFetch/WebSearch` too; leaving it would have contradicted the change. Rewrote it to "raw network fetches". Flagging in case the brief expected that comment untouched.
