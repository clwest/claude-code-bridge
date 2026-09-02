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

Five tools. That's the whole surface.

| Tool | What it does |
|---|---|
| `cc_run(task_file, cwd)` | Start a headless CC session in `cwd` with the prompt `Read <task_file>`. Refuses non-`TASK_*.md` inputs, refuses cwds outside `~/Donkey_Betz/`, refuses if another job is already running in that cwd. Returns a `job_id`. |
| `cc_status(job_id)` | running / finished, elapsed seconds, and the last few lines of stdout+stderr. |
| `cc_result(job_id)` | The full final output once finished, plus a **CONTRACT CHECK** (see below). Errors if the job is still running. |
| `cc_ask(job_id, prompt)` | A follow-up question **into the same CC session** (resumed by `session_id`). Returns a new `job_id`. Use this to say "you skipped the ask list, please add it." |
| `cc_list()` | Every job this bridge has started, with cwd and current state. |

## The contract check

After a run finishes, `cc_result` reads the task file and reports, at the
top of the result:

- `REPORT: present` / `REPORT: MISSING` — a `## Report — YYYY-MM-DD`
  section that wasn't there before the run started.
- `ASK LIST: present` / `ASK LIST: MISSING` — a bulleted checklist
  (`- [ ]` / `- [x]`) inside the added report, matching CLAUDE.md's
  "before you report done, list every ask and mark each one" rule.
- `STATUS: <value>` — the current frontmatter `status:` line, so a stale
  "not started" header is visible immediately.
- `BYTES ADDED THIS RUN` — a rough sanity check on how much the file
  grew.

The bridge does **not** fix any of these. It makes them impossible to
miss. Cowork then uses `cc_ask` to send it back — which is exactly what
Chris did by hand on 2026-07-22 and what Cowork did by hand on 2026-09-02.

## What this server can do to this machine unattended

Written before the first non-test run, and it is the thing to say yes to
before Chris restarts the desktop app.

The bridge starts `claude --print …` subprocesses. Each subprocess is a
full Claude Code session with the tool allow/deny lists below, pinned by
this server on every spawn. Every run has a per-session budget cap of
**$2.00** USD (`--max-budget-usd`). The bridge itself never elevates or
loosens these; there is no code path that grants more.

**Allowed, without asking:**

- Read any file under the run's `cwd` — which is required to be under
  `~/Donkey_Betz/`.
- Edit and write files under `cwd`.
- Task tracking (`TaskCreate`, `TaskUpdate`, `TaskGet`, `TaskList`,
  `TodoWrite`) and the built-in web tools (`WebFetch`, `WebSearch`).
- Git operations that don't publish: `add`, `commit`, `status`, `log`,
  `diff`, `show`, `branch`, `checkout`, `restore`, `rm`, `mv`, and
  `config --get`.
- Common test / build / format runners: `pytest`, `make`, `uv`, `python`,
  `npm test`, `npm run`, `pnpm test`, `pnpm run`, `yarn test`, `ruff`,
  `pyright`, `mypy`, `prettier`, `black`.
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

**Not passed to CC at all:**

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

**Notes on the underlying CLI (2.1.114):**

- `--max-turns` does not exist on this version. The cost cap
  (`--max-budget-usd`) is what stops a runaway run.
- Session persistence is on by default in `--print` mode, which is what
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
- **No shell interpolation.** The claude subprocess is spawned with a
  fixed argv (`subprocess.Popen(cmd, shell=False)`); the prompt is a
  positional string that CC treats as user input, not as a shell command.
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
claude-code-bridge-cli list
claude-code-bridge-cli wait <job_id> [--timeout SECS]
```

Or, once the venv is set up, use the wrapper: `./bin/ccbridge run …`.

`wait` polls `status` at a fixed interval and prints the final state when
the job finishes. Useful for the test suite; not exposed as an MCP tool
because a caller can already build it out of `status`.

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
