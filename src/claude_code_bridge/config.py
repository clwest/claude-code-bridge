"""Fixed policy for the bridge: paths, permissions, caps.

Kept in one file so a reader can audit the machine's exposure without
grepping the whole package. If the exposure changes, it changes here and
the README's "What this server can do to this machine unattended" section
changes with it.
"""
from __future__ import annotations

from pathlib import Path

# Only cwds under this root are allowed. Refuse anything else.
WORKSPACE_ROOT = Path.home() / "Donkey_Betz"

# Where per-run logs and metadata live. Gitignored; the record that matters
# is the report Cowork or Chris reads in the task file itself.
RUNS_DIR = WORKSPACE_ROOT / "playground" / "cc-runs"

# Default cost cap per invocation, in USD. --max-turns does not exist on
# the claude CLI (2.1.114); --max-budget-usd does, and stops the run
# when hit. A caller may override this per-run via cc_run/cc_ask, up to
# MAX_BUDGET_CEILING_USD below.
MAX_BUDGET_USD = "2.00"

# The most the bridge will ever pass to --max-budget-usd, regardless of
# what a caller asks for. A bad or too-large value is rejected, not
# clamped — see runner._validate_budget. Raise this only after a
# deliberate conversation about blast radius; a runaway CC session that
# is allowed to spend more can also make more of a mess.
MAX_BUDGET_CEILING_USD = "20.00"

# --permission-mode default: in --print mode, an unresolvable permission
# prompt fails cleanly rather than hanging. Combined with the allow/deny
# lists below, this gives us a headless run that either does allowed work
# or refuses.
#
# NOT used: bypassPermissions ("--dangerously-skip-permissions"), even to
# make a test pass. The whole point of this bridge is that the allowance
# is written down before the run.
PERMISSION_MODE = "default"

# Allowlist for the headless CC session. Uses the CLI's tool-permission
# syntax from `claude --help`:
#
#   --allowedTools <tools...>  Comma or space-separated list of tool names
#                              to allow (e.g. "Bash(git *) Edit")
#
# The list covers reading, editing, task tracking, tests/builds, and the
# git operations that don't publish. It does NOT include git push, package
# installs, raw network fetches, or a general shell escape.
ALLOWED_TOOLS = [
    # Read-only tools
    "Read",
    "Glob",
    "Grep",
    # WebFetch/WebSearch deliberately absent — Cowork does web; headless CC gets no network beyond git (Chris, 2026-09-02).
    # File edits
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    # Task/plan tracking (harmless, useful)
    "TaskCreate",
    "TaskUpdate",
    "TaskGet",
    "TaskList",
    "TodoWrite",
    # Git — everything except push
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git status:*)",
    "Bash(git log:*)",
    "Bash(git diff:*)",
    "Bash(git show:*)",
    "Bash(git branch:*)",
    "Bash(git checkout:*)",
    "Bash(git restore:*)",
    "Bash(git rm:*)",
    "Bash(git mv:*)",
    "Bash(git config --get:*)",
    # Common test/build/format runners
    "Bash(pytest:*)",
    "Bash(python:*)",
    "Bash(python3:*)",
    "Bash(make:*)",
    "Bash(uv:*)",
    "Bash(npm test:*)",
    "Bash(npm run:*)",
    "Bash(pnpm test:*)",
    "Bash(pnpm run:*)",
    "Bash(yarn test:*)",
    "Bash(ruff:*)",
    "Bash(pyright:*)",
    "Bash(mypy:*)",
    "Bash(prettier:*)",
    "Bash(black:*)",
    # Filesystem inspection commands (dedicated CC tools cover most of this,
    # but scripts sometimes need them)
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(wc:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(find:*)",
    "Bash(grep:*)",
    "Bash(rg:*)",
    "Bash(echo:*)",
    "Bash(pwd:*)",
]

# Explicit deny. Belt to the allowlist's braces: if a category ever slipped
# through the allowlist by accident, these still stop the highest-risk
# actions cold.
DISALLOWED_TOOLS = [
    "Bash(git push:*)",
    "Bash(rm:*)",
    "Bash(sudo:*)",
    "Bash(brew:*)",
    "Bash(pip:*)",
    "Bash(pip3:*)",
    "Bash(npm install:*)",
    "Bash(pnpm install:*)",
    "Bash(yarn install:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(ssh:*)",
    "Bash(scp:*)",
    "Bash(rsync:*)",
    "Bash(defaults:*)",
    "Bash(security:*)",
]
