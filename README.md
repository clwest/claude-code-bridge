# claude-code-bridge

An MCP server that lets a caller (Cowork Claude, in practice) start a
headless Claude Code session on a `TASK_*.md` file, poll it, read the
result, and send follow-up questions into the same session — without a
human relaying messages between them.

The point is not automation for its own sake. The point is that the
task-file protocol is already how work is scoped, executed and recorded
here; the bridge is a **transport** for that protocol, not a replacement
for it. The task file stays the contract. The report is mandatory. The
reviewer still reads to the end. What goes away is the human relay.

## What it exposes

Seven tools. That's the whole surface.

| Tool | What it does |
|---|---|
| `cc_run(task_file, cwd, budget_usd=None, timeout_s=None)` | Start a headless CC session in `cwd`, telling it to do the work in `task_file` now (not to ask for confirmation). Refuses non-`TASK_*.md` inputs, refuses cwds outside `~/Donkey_Betz/`, refuses if another job is already running in that cwd — a stale job past its wall-clock deadline is reaped first, and the reap is announced in the return. Optional `budget_usd` overrides the default $2.00 per-run cap, up to a ceiling of $50.00. Optional `timeout_s` overrides the default 45-minute wall-clock ceiling, up to 8 hours. Returns a `job_id`. |
| `cc_status(job_id)` | running / finished / killed / timeout, elapsed seconds, budget, timeout, and the last few lines of stdout+stderr. If the budget cap was hit, a `BUDGET: CAP HIT` line makes that unmissable. If the job was killed or timed out, `ended_reason:` names it. |
| `cc_result(job_id)` | The full final output once finished, plus a **CONTRACT CHECK** (see below). If the job is still running, returns a `still running` message instead of the final output. A killed or timed-out job carries `ended_reason:` at the top so a stopped run is never indistinguishable from one that ran to completion. |
| `cc_ask(job_id, prompt, budget_usd=None, timeout_s=None)` | A follow-up question **into the same CC session** (resumed by `session_id`). Optional `budget_usd` overrides the inherited budget from the prior job (default $2.00, ceiling $50.00). Optional `timeout_s` overrides the inherited wall-clock ceiling (default 45 min, ceiling 8 h). Returns a new `job_id`. Use this to say "you skipped the ask list, please add it." |
| `cc_kill(job_id)` | Stop a running job on purpose. Only kills the pid recorded in that job's `meta.json` — never a pid, name or pattern from the caller. Sends SIGTERM, waits 5 seconds, sends SIGKILL if still alive. Records `ended_reason: killed` and which signal actually ended it, and says whether the cwd is free afterwards. On an already-ended job, says so and does nothing. |
| `cc_list()` | Every job this bridge has started, with cwd and current state. |
| `cc_push(cwd, remote="origin")` | Publish the current branch's commits from `cwd` on `remote`. The only sanctioned way through — `Bash(git push:*)` in the deny list stays as-is. Deliberately no argument passthrough: `--force`, `--force-with-lease`, `--delete`, `--tags` and refspecs are unreachable from outside this tool. Refuses a detached HEAD (names the SHA), reports a non-fast-forward with git's own reason on one line and says the branch was not published, treats "already up to date" as a normal result, and pushes with `--set-upstream` when no upstream is set (the return says a new remote branch was created). Returns branch, remote name and URL, remote SHA before and after, and how many commits were published. 120s wall clock. |

## The contract check

After a run finishes, `cc_result` reads the task file and reports, at the
top of the result:

- `REPORT: present` / `REPORT: MISSING` — a `## Report — YYYY-MM-DD`
  section anywhere in the file. If a report heading was already there
  before this run, "(but not added by this run — check date)" is appended
  so a stale report doesn't count as work done.
- `ASK LIST: present` / `ASK LIST: MISSING` — a bulleted checklist
  (`- [ ]` / `- [x]`) inside the added report, matching CLAUDE.md's
  "before you report done, list every ask and mark each one" rule.
- `STATUS: <value>` — the current frontmatter `status:` line, so a stale
  "not started" header is visible immediately.
- `BYTES ADDED THIS RUN` — a rough sanity check on how much the file
  grew.
- `BUDGET: CAP HIT — output is truncated, the work is probably incomplete`
  — printed above the report line when the CLI wrote its budget-exceeded
  message. This is deliberately separate from the report-contract lines
  above: a cap-hit run and a botched run need different responses (rerun
  with a bigger `budget_usd`, versus rebrief the task), so the reviewer
  must be able to tell them apart without guessing. `cc_status` prints
  the same line when the cap was hit.

The bridge does **not** fix any of these. It makes them impossible to
miss. Cowork then uses `cc_ask` to send it back — which is exactly what
Chris did by hand on 2026-07-22 and what Cowork did by hand on 2026-09-02.

## Stopping a run: `cc_kill` and the wall-clock ceiling

Two failures in two days on 2026-09-03 / 2026-09-04 held a cwd open long
after the work was done — an internet drop that left a session hanging,
and a cap-hit run that looked like a wedge from the outside. Both times
Chris had to find a `claude --print` on his Mac and kill it by hand.
The bridge now handles that itself, two ways:

- **`cc_kill(job_id)`** — an explicit stop. Takes only a `job_id`, looks
  up the pid from that job's `meta.json`, and signals only that process
  group. No pattern-based killing (no `pkill`, no name matching), because
  a tool that could `pkill -f "claude --print"` could also kill a
  session Chris is running at his own keyboard. SIGTERM first, five-
  second grace, SIGKILL if the process ignored SIGTERM. The outcome is
  recorded as `ended_reason: killed` alongside the signal that actually
  ended it, so `cc_status` and `cc_result` never make a killed job look
  like one that finished.

- **A 45-minute wall-clock ceiling** on every job. The default is
  generous — the longest legitimate run so far was about 22 minutes —
  but it exists so most jobs never need a human timeout. `cc_run` and
  `cc_ask` both accept an optional `timeout_s` override, up to an
  8-hour ceiling in `config.py`. Enforcement is lazy: there is no
  daemon and no background thread. When the next call touches a job
  past its deadline (`cc_status`, `cc_result`, or a blocked `cc_run`
  trying to start in the same cwd), the bridge kills it and records
  `ended_reason: timeout`. That means **the next `cc_run` unblocks
  itself**, and it says so plainly in the return — "Reaped stale job
  X (timeout). Started job Y." — so nobody mistakes the reaping for
  their own job failing.

## Publishing commits: `cc_push` and why the Bash deny stays

`Bash(git push:*)` is in the deny list and stays there. The only sanctioned
way to publish a commit through this bridge is `cc_push`.

Two things fall out of that:

- **No credential lives in the workspace.** A stored token would have to sit
  in a file readable by every later Claude Code run and every future session,
  which is the same shape as the incident that put a PyPI token into a
  permission file. `cc_push` runs on the Mac and inherits the user's
  environment, so the macOS keychain credential helper answers on its own
  — no new secret anywhere.
- **The deny is a design surface, not a hurdle to be routed around.** On
  earlier runs a push occasionally came out through `Bash(python:*)`, which
  is allowlisted and reaches `subprocess`. Making that the routine path
  would turn a known hole in the boundary (see "Known limitation" further
  down) into the process. `cc_push` gives CC a first-class way through
  that goes only where the deny already permits by other means.

`cc_push` has no argument passthrough. `--force`, `--force-with-lease`,
`--delete`, `--tags` and refspecs are unreachable from outside the tool
because there is no parameter that would carry them. A detached HEAD is
refused (with the SHA in the message); a non-fast-forward push is refused
with git's own reason on one line and a plain "branch was not published";
"already up to date" is a normal return; a branch with no upstream is
pushed with `--set-upstream` and the return says a new remote branch was
created. 120s wall clock so a hung network cannot hang the tool.

## Recovering from a run

The deny list is not a boundary (see the containment note below); the
containment that actually holds is the cwd bound, the one-run-per-cwd
lock and the budget cap. That leaves the question a deny list was never
going to answer: **when a run damages something inside the cwd it was
legitimately given, how do we get it back?** This section is the answer.

Before every `cc_run` and `cc_ask`, the bridge captures a recovery point
from the run's `cwd` and records it in the job's `meta.json` under
`pre_run_git`:

- `head_sha`, `branch`, `dirty` — the commit the run started from.
- `snapshot_ref` — on a dirty tree, `git stash create` produces a commit
  object capturing the working tree without touching the working tree,
  the index or the stash list. The bridge immediately anchors that
  object under `refs/cc-bridge/<job_id>` with `git update-ref`, so a
  later `git gc` cannot delete it. On a clean tree there is nothing to
  snapshot and `snapshot_ref` is `null`.
- `pre_git_status.txt` — verbatim `git status --porcelain`, written into
  the run directory next to `pre.txt`.

`cc_result` prints the recovery point above the CC output, at most three
lines, e.g.:

```
pre-run HEAD: 380e98b (main), tree dirty
pre-run snapshot: refs/cc-bridge/20260903T001809Z-69a69826
recover with: git diff 380e98b..HEAD    |    git stash apply refs/cc-bridge/20260903T001809Z-69a69826
```

On a clean tree it prints `tree clean — no snapshot needed` and only the
diff command. If git is unavailable or the cwd is not a git repository,
it prints one line saying so and no commands. `cc_status` prints a
one-line `pre-run HEAD: <short sha> (<branch>)` — status is for "is it
done yet", not recovery.

**Real limitation, stated plainly:** `git stash create` does **not**
capture untracked files. A file CC has never seen committed is not in
the snapshot. `pre_git_status.txt` at least tells the reviewer which
untracked paths existed before the run, so a missing one is visible
rather than silent. Stashing untracked files was considered and rejected
because it would change the working tree, and a transport layer must not
do that to a repo it was only asked to run a session in.

Snapshot refs accumulate under `refs/cc-bridge/`. List them with:

```
git for-each-ref refs/cc-bridge/
```

and delete stale ones by hand (`git update-ref -d refs/cc-bridge/<job_id>`).
The bridge does not delete them, because a transport layer that prunes
its own recovery points is not a recovery mechanism.

## What this server can do to this machine unattended

Written before the first non-test run, and it is the thing to say yes to
before Chris restarts the desktop app.

The bridge starts `claude --print …` subprocesses. Each subprocess is a
full Claude Code session with the tool allow/deny lists below, pinned by
this server on every spawn. Every invocation gets its own
`--max-budget-usd` cap: the default is **$2.00** USD, and a caller may
raise it per-run up to a hard ceiling of **$50.00** — the bridge will
never pass more than the ceiling, and rejects out-of-range values rather
than silently clamping them. Note this is a *per-invocation* cap, not a
per-session cap: a `cc_ask` on the same session gets its own budget
argument (default: inherit the prior job's), so a long session with N
asks can spend up to N × the cap in total. Raising the ceiling itself is a
deliberate edit to `config.py`, not something a caller can do at
runtime. The bridge itself never elevates or loosens the tool
permissions; there is no code path that grants more.

Every invocation also gets a **wall-clock ceiling** — 45 minutes by
default, up to an 8-hour ceiling in `config.py`, overridable per run
with `timeout_s`. A job past its deadline is killed the next time
anything touches it (`cc_status`, `cc_result`, or a blocked `cc_run`
in the same cwd) and recorded as `ended_reason: timeout`. This is why
`cc_kill` exists at all — but the timeout is the reason `cc_kill` will
almost never need to be called by hand.

**Allowed, without asking:**

- `Read`, `Glob`, `Grep` — any file under the run's `cwd`, which is
  required to be under `~/Donkey_Betz/`.
- `Edit`, `Write`, `MultiEdit`, `NotebookEdit` — file edits under `cwd`.
- Task tracking (`TaskCreate`, `TaskUpdate`, `TaskGet`, `TaskList`,
  `TodoWrite`).
- Git operations that don't publish: `add`, `commit`, `status`, `log`,
  `diff`, `show`, `branch`, `checkout`, `restore`, `rm`, `mv`, and
  `config --get`.
- Common test / build / format runners: `pytest`, `make`, `uv`, `python`,
  `python3`, `npm test`, `npm run`, `pnpm test`, `pnpm run`, `yarn test`,
  `ruff`, `pyright`, `mypy`, `prettier`, `black`.
- Filesystem inspection commands: `ls`, `cat`, `wc`, `head`, `tail`,
  `find`, `grep`, `rg`, `echo`, `pwd`.

**Explicitly denied**, on top of the allowlist, so a category that ever
slips past the allowlist is still blocked:

- `git push` (the one exception, the Dealer AI internal-docs repo, is
  CC's own session-end rule from CLAUDE.md — the bridge does not add push
  permission anywhere).
- `rm`, `sudo`, `brew`, `pip`, `pip3`, `npm install`, `pnpm install`,
  `yarn install`.
- `curl`, `wget`, `ssh`, `scp`, `rsync` — no raw network fetches.
- `defaults`, `security` — no macOS preferences or keychain access.

**Known limitation — a deny list cannot bound a session with a runner and
write access.** `python3` (and `python`) are on the *allow* list because
tests, scripts and formatters need them. Anything a denied shell command
could do, a CC session can still do by calling it through Python — `import
os; os.unlink(path)` reaches the same syscall as `rm`, `urllib.request`
reaches the same one as `curl`. This was hit in practice on 2026-09-02
while cleaning up a stale `.git/index.lock`: `Bash(rm:*)` refused,
`Bash(find … -delete)` refused, `python3 -c "import os; os.unlink(...)"`
went through.

Removing `python`/`python3` does not fix this. The allow list still holds
`pytest`, `make`, `npm run`, `pnpm run`, `yarn test`, `uv` and `ruff`, and
CC can write files — so it can write a test file, a Makefile target or a
package script, run it through any allowed runner, and execute arbitrary
code that way. A minimal-python allowlist has the same hole:
`python -m pytest` pointed at a test file CC wrote thirty seconds earlier
is the identical escape, on the allowlist you just built. To make the deny
list a real boundary you would have to remove every runner, and then the
bridge cannot run tests, which is most of the reason it exists.

Read the deny list as *makes the wrong thing awkward to do by accident*,
not *makes it impossible to do on purpose*. Real containment is the cwd
bound (`~/Donkey_Betz/`), the one-run-per-cwd lock and the budget cap —
three things, not four.

**Decision — 2026-09-03, Chris:** option 1 — documented, allow and deny
lists unchanged. Written down so nobody re-opens this in six months and
re-derives the weak answer.

**Not passed to CC at all:**

- `WebFetch`, `WebSearch` — deliberately absent; Cowork does web, and a
  headless CC session should not reach the network by default (Chris,
  2026-09-02). This is not containment: `urllib.request` defeats it the
  same way `os.unlink` defeats the `rm` deny — same mechanism, one
  paragraph up. What cutting them buys is that reaching for the network
  stops being a *normal move* for a headless session; any network access
  is a deliberate act rather than a default one.
- `--dangerously-skip-permissions` / `--allow-dangerously-skip-permissions`.
  The whole point of this bridge is that the allowance is written down
  before the run; a flag that turns off permission checking wholesale
  would undo that.
- `--add-dir` outside `cwd`. The cwd bounds the read/edit surface;
  broadening it belongs in a deliberate task, not the transport layer.

**Enforced by the bridge, not by the CLI:**

- `cwd` must be under `~/Donkey_Betz/` (resolved, symlinks followed).
- `task_file` must be a file named `TASK_*.md`, must exist, and must be
  located under `cwd`.
- One run per `cwd` at a time. `cc_run` refuses to start if another
  job's process (as recorded on disk) is still alive in the same cwd —
  this is CLAUDE.md's "another Claude Code may be running" rule,
  enforced by the tool that would otherwise be the one violating it.

**Dated claims about the CLI at version 2.1.114** (`Bash(claude:*)` is
not on the allow list, so a headless run cannot re-verify these — check
by hand against `claude --help` when bumping):

- `--max-turns` did not exist on 2.1.114. The cost cap
  (`--max-budget-usd`) is what stops a runaway run.
- Session persistence was on by default in `--print` mode, which is what
  makes `cc_ask` able to resume a session by `session_id`.

Logs and metadata per run live in
`~/Donkey_Betz/playground/cc-runs/<job_id>/` (gitignored). The record
that matters is the report the CC session appended to the task file —
that gets committed.

## Design decisions worth knowing

- **`mcp>=2,<3`**, `MCPServer`. FastMCP was removed at 2.0. Same as
  google-docs-mcp; same reason.
- **`structured_output=False`** on every tool. There is an open bug in
  Claude Desktop (`anthropics/claude-code#80094`, 2026-07-22) that
  prevents tool dispatch when tools publish an outputSchema. Remove the
  flag once that bug ships a fix.
- **No shell interpolation of caller input.** The claude subprocess is
  launched via a `bash -c '<cmd>; echo $? > exit_code'` wrapper so the
  exit code survives after the wrapper returns; every CLI arg in `<cmd>`
  is `shlex.quote`'d and no caller input is concatenated into the shell
  line. The prompt is written to CC's stdin, not passed as argv, because
  commander.js variadic options (`--allowedTools`, `--disallowedTools`)
  would otherwise consume a trailing positional as another tool name
  (verified on 2.1.114 — see `_cc_command` in `runner.py`).
- **State on disk, not in memory.** The MCP server process can be
  restarted between `cc_run` and `cc_status`; the meta.json, stdout.log
  and stderr.log per job survive that.
- **`cc_ask` resumes by session_id, not by cwd or task_file.** So a
  correction after a bad run goes to the same CC session that produced
  the bad run — CC sees the follow-up as a continuation, not a fresh
  read of the task file.

## What the CLI surface looks like today (recorded)

`claude --version` at build time: **2.1.114 (Claude Code)**

The flags the bridge uses:

| Flag | What it does here |
|---|---|
| `--print` (`-p`) | Non-interactive: run the prompt, print the result, exit. |
| `--output-format text` | Plain text output. `json` is available if we later want structured metadata; today we don't need cost/turn counts. |
| `--session-id <uuid>` | Set a UUID at run-time so `cc_ask` can resume by that same UUID without parsing anything back from the CLI. |
| `-r <uuid>` (`--resume`) | Resume the session created above. |
| `--permission-mode default` | In `--print` mode, unresolvable permission prompts fail cleanly instead of hanging. |
| `--allowedTools <list>` | Space-separated allowlist; see above. |
| `--disallowedTools <list>` | Space-separated denylist; see above. |
| `--max-budget-usd 2.00` | Per-run cost cap. |

Flags the bridge deliberately does **not** use:

- `--add-dir` — see permissions section.
- `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`
  — see permissions section.
- `--no-session-persistence` — the opposite is what makes `cc_ask` work.
- `--include-hook-events`, `--include-partial-messages`,
  `--input-format stream-json` — the bridge takes one prompt in and one
  final result out; no streaming needs on either end.

If a future Claude Code release renames or removes any of these flags,
this README is where the mismatch will show up first. Re-run
`claude --help`, update `config.py` and this section, and bump the
version.

## Install

```bash
cd ~/Donkey_Betz/mcp-servers/claude-code-bridge
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -e .
```

Only dependency is `mcp>=2,<3`. The bridge shells out to the `claude`
CLI that Chris is already logged into — no auth flow, no client secret.

## Registering the server

Claude Desktop and Claude Code keep separate MCP configuration. This
bridge is primarily useful from **Claude Desktop** (where Cowork lives).
There is no reason to register it into Claude Code — a CC session
starting another CC session is possible but not what this is for.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
and add an entry under `mcpServers` (back the file up first):

```json
{
  "mcpServers": {
    "claude-code-bridge": {
      "command": "/Users/donkeyking/Donkey_Betz/mcp-servers/claude-code-bridge/.venv/bin/python",
      "args": ["-m", "claude_code_bridge"]
    }
  }
}
```

Absolute path to the venv's Python — the desktop app does not load your
shell profile. Then fully quit the app (Cmd+Q) and reopen. Logs are at
`~/Library/Logs/Claude/mcp-server-claude-code-bridge.log`.

## Local CLI harness

For smoke-testing without going through MCP transport:

```bash
claude-code-bridge-cli run <task_file> <cwd>
claude-code-bridge-cli status <job_id>
claude-code-bridge-cli result <job_id>
claude-code-bridge-cli ask <job_id> <prompt>
claude-code-bridge-cli kill <job_id>
claude-code-bridge-cli list
claude-code-bridge-cli wait <job_id> [--timeout SECS]
```

Or, once the venv is set up, use the wrapper: `./bin/ccbridge run …`.

`wait` polls `status` at a fixed interval (`--poll SECS`, default 2s) and
prints the final state when the job finishes, or times out after
`--timeout SECS` (default 600s). Useful for the test suite; not exposed
as an MCP tool because a caller can already build it out of `status`.

The CLI harness accepts no `budget_usd` argument on `run` or `ask` — it
is the minimal smoke-test surface. Callers that need to override the
budget go through the MCP tools (`cc_run`, `cc_ask`), where the argument
is exposed.

## When something breaks

- `Bridge error: cwd … is not under …/Donkey_Betz`
  → the cwd allowlist is doing its job; move the task file, or invoke
  the bridge from a cwd under the workspace root.
- `Bridge error: task_file must be a TASK_*.md file`
  → the free-form prompt refusal is doing its job.
- `Bridge error: another job (…) is already running in …`
  → the concurrency guard is doing its job. Check `cc_list` for the
  live job; wait for it, or check that it hasn't died and left a stale
  lock (the bridge cleans stale locks the next time it looks at them).
- CC subprocess exits immediately with nothing on stdout — check
  `stderr.log` under `~/Donkey_Betz/playground/cc-runs/<job_id>/`. The
  common cause is an `--allowedTools` / `--disallowedTools` syntax the
  CLI rejects; update `config.py` and re-run.

## What this depends on

- The `claude` CLI on `PATH`, logged into an account that has budget.
- Python 3.11+ and `mcp>=2,<3`.
- Write access to `~/Donkey_Betz/playground/cc-runs/`.

Nothing else. The bridge is deliberately small — everything real happens
in the CC subprocess it spawns.
