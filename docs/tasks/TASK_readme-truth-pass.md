---
title: "TASK — make the README true: record the deny-list decision, fix the containment claim, audit every other statement"
date: 2026-09-03
for: Claude Code (headless, via the bridge)
status: done
protocol: ../../../../CLAUDE.md
---

# Why this exists

Round 1 shipped and is verified working through the tool interface. In the
close-out you surfaced a real finding: `Bash(rm:*)` is denied but `Bash(python3:*)`
is allowed, so `os.unlink` reaches the same syscall. You wrote it up honestly and
gave Chris three options.

Chris has decided: **option 1 — document it, change nothing about the allow or
deny lists.** This file records that decision and fixes the README so it stops
claiming more than it can deliver.

But the reasoning in the README is currently the weak version, and one sentence
next to it is simply wrong. Both are below.

---

## Item 1 — the deny-list paragraph argues the wrong point

**What it says now.** That the limitation is acceptable because the caller is
trusted — "this is your machine, running your code, on your subscription" — and
that locking it down further is a decision for Chris.

**Why that is the weak version.** It implies options 2 and 3 would work and were
declined on cost. They would not work. Removing `python`/`python3` leaves `make`,
`npm run`, `pnpm run`, `yarn test`, `uv`, `pytest` and `ruff` on the allow list,
and CC can write files. So it writes a test file, a Makefile target or a package
script, runs it through an allowed runner, and executes arbitrary code anyway.
Option 3 has the same hole in it: `python -m pytest` pointed at a test file CC
wrote thirty seconds earlier is the identical escape, on the allowlist you just
built.

Which means: **a deny list cannot bound a session that can write a file and run a
test.** To make it a real boundary you would have to remove every runner, and
then the bridge cannot run tests, which is most of the reason it exists.

**What to do.** Rewrite that paragraph to make that argument instead. Keep it
short and keep the concrete `.git/index.lock` story — it is what makes it
believable. Then record the decision explicitly, with a date and Chris's name, in
one line: *option 1, chosen 2026-09-03 — documented, lists unchanged.* The point
of writing the reasoning down is that nobody re-opens this in six months and
re-derives the weak answer.

---

## Item 2 — one containment claim is false

Two paragraphs below, the README lists what the real containment is:

> Real containment is the cwd bound (`~/Donkey_Betz/`), the one-run-per-cwd lock,
> the missing `WebFetch`/`WebSearch`, and the budget cap — not the shell denies.

`WebFetch`/`WebSearch` does not belong in that list. `urllib.request` defeats it
in exactly the way `os.unlink` defeats the `rm` deny — same mechanism, same
paragraph, two sentences apart. Cutting the web tools is still worth doing, but
what it buys is different: it stops reaching for the network being a *normal
move* for a headless session, and it means any network access is a deliberate act
rather than a default one. It is not a wall.

**What to do.** Take it out of the containment list, and say plainly what cutting
it actually buys. The genuine containment is the cwd bound, the one-run-per-cwd
lock and the budget cap — three things, not four.

---

## Item 3 — audit every other statement in the README

Both of the above were true when written and became false, or were written
slightly stronger than the code supports. Assume there are others.

Go through `README.md` claim by claim and check each one against the code, not
against your memory of the code. For each claim, note the file and line that
proves or disproves it. Cover at least:

- the tool table — five tools, their arguments, what each refuses
- the contract-check section against `contract.py`
- the allow list and deny list sections against `config.py`, item by item, including
  anything the round-1 web-tools cut left stale
- the budget statements — default, ceiling, what happens at the cap
- "Enforced by the bridge, not by the CLI" — every bullet against `runner.py`
- the notes on the CLI (`--max-turns` does not exist, session persistence in
  `--print` mode) — mark these as **dated claims about version 2.1.114** rather
  than standing facts, since you cannot verify them from here (`Bash(claude:*)` is
  not on the allow list, which is correct)
- the logs/metadata paths against `config.RUNS_DIR`

Fix what is wrong. Where a claim is true but stronger than the evidence, soften it
to what you can actually show. Where a claim cannot be checked from inside this
repo, say so in the README rather than deleting it — an unverifiable claim marked
as unverifiable is more useful than a missing one.

Do the same quick pass over the module docstrings in `src/claude_code_bridge/`,
which also describe the surface.

**Report the audit as a table** — claim, verdict (true / false / softened /
unverifiable), and the evidence. That table is the deliverable as much as the
edits are.

---

## Non-goals

- **Do not change `ALLOWED_TOOLS` or `DISALLOWED_TOOLS`.** That is the whole
  content of the decision. Documentation only.
- **Do not add features.** No new tools, no new arguments, no pre-run git
  snapshot — that idea exists and is a separate brief if Chris wants it.
- **Do not rewrite the round-1 reports** in `docs/tasks/TASK_bridge-followups-round-1.md`.
  They are the record of what happened. Append if you must, never edit.
- **Do not try to push.** `git push` is denied for you, deliberately. Chris is
  pushing this one himself once it lands.
- **Do not touch `playground/cc-bridge-test/`.** Those smoke files are the
  regression pair for round 1 and are outside this repo anyway.

## Done means

- [ ] the deny-list paragraph argues that a deny list cannot bound a session with
      a runner and write access, and names the specific runners that make it so
- [ ] the option-1 decision is recorded in the README with date and decider
- [ ] `WebFetch`/`WebSearch` is out of the containment list, with what it actually
      buys stated in its place
- [ ] every README claim checked against code, with the claim-by-claim table in
      the report
- [ ] module docstrings in `src/claude_code_bridge/` checked the same way
- [ ] `pytest` still passes — give the real count
- [ ] one commit, not pushed
- [ ] dated `## Report — 2026-09-03` section appended to THIS file with the
      ask-list checklist, and frontmatter `status:` updated

---

## Report — 2026-09-03

### What I did

1. Rewrote the deny-list paragraph to make the *right* argument — that a
   deny list cannot bound a session with a runner and write access — and
   named the specific runners on the allow list that make it so (`pytest`,
   `make`, `npm run`, `pnpm run`, `yarn test`, `uv`, `ruff`, plus the
   `python`/`python3` pair). Kept the `.git/index.lock` story because it
   is what makes it believable. Recorded Chris's option-1 decision in one
   line at the end of the paragraph, dated 2026-09-03.
2. Took `WebFetch`/`WebSearch` out of the containment list. The genuine
   containment is now stated as three things — cwd bound, one-run-per-cwd
   lock, budget cap. Rewrote the `WebFetch`/`WebSearch` bullet in "Not
   passed to CC at all" to say what cutting them actually buys (reaching
   for the network stops being a *normal* move) and to be explicit that
   `urllib.request` defeats it the same way `os.unlink` defeats the `rm`
   deny.
3. Audited every other claim in `README.md` against the code. Table below.
   Fixed the ones that were wrong or overstated; softened the ones that
   were true but stronger than the evidence; marked the 2.1.114 CLI
   claims as dated rather than standing (since `Bash(claude:*)` is not on
   the allow list, this session cannot re-verify them).
4. Read every module docstring in `src/claude_code_bridge/`. The only
   drift found was that `runner.py`'s module docstring listed the on-disk
   layout but omitted the `exit_code` file that `_spawn` writes; added it.
5. Softened one `cc_status` tool description in `server.py` — it claimed
   the numeric exit code appears in status output, but the code only puts
   `(exit unknown)` into the state string and reserves the numeric exit
   code for `cc_result`.
6. Ran `pytest -q`: **22 passed in 2.54s**. No test changes were needed.

### Claim-by-claim audit

Legend: **T** = true (kept as-is), **F** = false (fixed), **S** = softened
(true but overstated), **U** = unverifiable from here (marked as dated).

| Claim | Verdict | Evidence |
|---|---|---|
| `cc_run(task_file, cwd, budget_usd=None)` signature | T | `server.py:96` |
| "Refuses non-`TASK_*.md` inputs" | T | `runner.py:302–305` (`_validate_task_file`) |
| "Refuses cwds outside `~/Donkey_Betz/`" | T | `runner.py:317–321` + `_resolve_under:89–98`; root at `config.py:13` |
| "Refuses if another job is already running in that cwd" | T | `runner.py:371–376` via `_active_in_cwd:277–289` |
| "Default $2.00 per-run cap, ceiling $20.00" | T | `config.py:23` and `config.py:30` |
| `cc_status` "running / finished, elapsed seconds, budget, last few lines" | T | `server.py:123–142` |
| `cc_status` shows exit code (tool description) | F → fixed | Description said "exit code if finished" but code only emits `(exit unknown)` state; softened description at `server.py:107–113` |
| `cc_result` "Errors if the job is still running" | S → softened | `server.py:163–164` returns a text `still running` line, does not raise; README table softened |
| `cc_ask(job_id, prompt, budget_usd=None)` — inherited budget default | T | `runner.py:441–445` |
| `cc_list()` — every job with cwd and state | T | `server.py:221–229`, `runner.py:508–524` |
| `REPORT: present` == "wasn't there before" | S → softened | `contract.py:57` marks present for *any* Report heading; the "not added by this run" is a separate parenthetical (`contract.py:36–37`). README softened to match. |
| `ASK LIST: present` / `MISSING` behaviour | T | `contract.py:22, 77–82` |
| `STATUS:` line from frontmatter | T | `contract.py:23, 84–85` |
| `BYTES ADDED THIS RUN` | T | `contract.py:67, 43` |
| `BUDGET: CAP HIT` line printed above the contract lines | T | `server.py:44–46, 172–176` (result) and `server.py:135–136` (status); regex at `runner.py:134` |
| Allow list — `Read` etc. under cwd | S → softened | Was "Read any file under cwd" without naming the CC tools; expanded to name `Read`, `Glob`, `Grep`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit`. |
| Allow list — runners listed as `python` only | F → fixed | `config.py:83–84` allows both `Bash(python:*)` **and** `Bash(python3:*)`; added `python3` to the README bullet. |
| Allow list — git subcommands | T | `config.py:69–80` matches README bullet exactly |
| Allow list — filesystem inspection commands | T | `config.py:99–108` matches |
| Deny list — `git push`, `rm`, `sudo`, `brew`, `pip`, `pip3`, installs | T | `config.py:115–121` |
| Deny list — `curl`, `wget`, `ssh`, `scp`, `rsync` | T | `config.py:124–128` |
| Deny list — `defaults`, `security` | T | `config.py:129–130` |
| Deny-list paragraph — "acceptable because caller trusted, locking down is Chris's call" | F → fixed | The right argument is that no deny list can bound a session that can write files and run a runner. Rewrote around the specific runners on the allow list that make an escape trivial; recorded the option-1 decision inline (2026-09-03, Chris). |
| Containment includes `WebFetch`/`WebSearch` | F → fixed | `urllib.request` on the allowed `python3` defeats it identically to `os.unlink` vs `rm`. Removed from containment list; explained what cutting them actually buys in the "Not passed to CC at all" bullet. |
| `WebFetch`/`WebSearch` deliberately absent from allow list | T | `config.py:56` comment + absence in list |
| `--dangerously-skip-permissions` never used | T | `runner.py:168–186` — argv never includes it |
| `--add-dir` never used | T | Same — no `--add-dir` in `_cc_command` |
| cwd required under `~/Donkey_Betz/`, resolved & symlinks followed | T | `runner.py:89–98` uses `.expanduser().resolve()` |
| `task_file` must be `TASK_*.md`, exist, under cwd | T | `runner.py:296–314` |
| One run per cwd — via on-disk meta | T | `runner.py:277–289` |
| Dated CLI notes about 2.1.114 (`--max-turns`, session persistence) | U → marked dated | Cannot re-verify from this session (`Bash(claude:*)` not on allow list). Header rewritten to make the "dated claim" nature explicit and to instruct future readers to re-verify with `claude --help`. |
| Logs/metadata at `~/Donkey_Betz/playground/cc-runs/<job_id>/` | T | `config.py:17` |
| `mcp>=2,<3`, `MCPServer` | T | `pyproject.toml:15`, `server.py:27` |
| `structured_output=False` on every tool | T | `server.py:94, 112, 157, 201, 219` |
| "No shell interpolation ... `subprocess.Popen(cmd, shell=False)`; prompt is a positional string" | F → fixed | `runner.py:347–354` launches through `["/bin/bash", "-c", shell_line]` (not a direct argv spawn) and the prompt is piped via stdin (`runner.py:359–361`, `_cc_command:156–186` comment). Rewritten to describe what is actually true — a `shlex.quote`'d wrapper line, prompt on stdin — and to say why (exit-code capture; commander.js variadic-option collision on 2.1.114). |
| State on disk, restart-safe | T | `runner.py:11–13` docstring; `_load_meta`/`_save_meta` and per-job files at `runner.py:195–204` |
| `cc_ask` resumes by session_id | T | `runner.py:428–491` — `_cc_command(session_id, resume=True, ...)` at `runner.py:464` |
| CLI flag table (`--print`, `--output-format text`, `--session-id`, `-r`, `--permission-mode`, `--allowedTools`, `--disallowedTools`, `--max-budget-usd`) | T | `runner.py:168–186` |
| Only dependency is `mcp>=2,<3` | T | `pyproject.toml:14–16` (pytest is a `dev` extra only) |
| CLI harness commands (`run`, `status`, `result`, `ask`, `list`, `wait`) | T | `cli.py:63–88` |
| `wait` "polls at a fixed interval" | S → softened | `cli.py:87` exposes `--poll SECS` (default 2s) and `--timeout` (default 600s); README updated to say so. |
| CLI harness supports `budget_usd` | F → added caveat | `cli.py:_cmd_run` / `_cmd_ask` do not pass `budget_usd`. README now says the harness is the minimal smoke-test surface and callers who need the override use the MCP tools. |
| Budget cap phrased as "per-session" | S → softened | Cap is per invocation. A session with N asks can spend up to N × cap; README updated to say so. |
| Bridge cleans stale locks on next look | T | `_active_in_cwd:277–289` calls `_finalize_if_dead` before returning, so a zombie wrapper with an `exit_code` file gets reaped on the next check |

### Module docstrings in `src/claude_code_bridge/`

- `__init__.py` — only `__version__`; nothing to audit.
- `__main__.py` — one-line entry-point docstring; accurate.
- `config.py` — describes the file's purpose and references the README
  section it mirrors; accurate. Individual constants (`WORKSPACE_ROOT`,
  `RUNS_DIR`, `MAX_BUDGET_USD`, `MAX_BUDGET_CEILING_USD`,
  `PERMISSION_MODE`, `ALLOWED_TOOLS`, `DISALLOWED_TOOLS`) each have
  comment blocks that match their values and use.
- `contract.py` — describes the report/ask-list/status contract per
  CLAUDE.md; accurate; regexes at `contract.py:21–23` match what the
  docstring describes.
- `cli.py` — describes the six subcommands (`run`, `status`, `result`,
  `ask`, `list`, `wait`); accurate. Does not claim `budget_usd` support,
  which is correct because it doesn't offer one.
- `runner.py` — described the on-disk layout but omitted `exit_code`;
  **fixed**. The rest (`_LIVE_POPENS` note, finalize-if-dead order of
  evidence, cap-hit regex, `_spawn` wrapper explanation, prompt-via-stdin
  rationale, `is_running` note about zombies) is accurate.
- `server.py` — docstring notes the `mcp>=2` API move, the
  `structured_output=False` bug reference (`claude-code#80094`,
  2026-07-22 — dated claim), the string return type, and that the
  permission surface is enforced by passing flags. All accurate.
- Individual tool descriptions — `cc_run`, `cc_result`, `cc_ask`,
  `cc_list` accurate. `cc_status` overclaimed exit code; **fixed**.

### Non-goals honoured

- `ALLOWED_TOOLS` / `DISALLOWED_TOOLS` in `config.py` unchanged.
- No new tools, no new arguments, no pre-run snapshot.
- Round-1 reports in `TASK_bridge-followups-round-1.md` not touched.
- No `git push`.
- Nothing in `playground/cc-bridge-test/` touched.

### Tests

`pytest -q` → **22 passed in 2.54s**.

### Ask-list checklist

- [x] the deny-list paragraph argues that a deny list cannot bound a
      session with a runner and write access, and names the specific
      runners that make it so
- [x] the option-1 decision is recorded in the README with date and
      decider (2026-09-03, Chris)
- [x] `WebFetch`/`WebSearch` is out of the containment list, with what it
      actually buys stated in its place
- [x] every README claim checked against code, with the claim-by-claim
      table in the report (see above)
- [x] module docstrings in `src/claude_code_bridge/` checked the same way
- [x] `pytest` still passes — **22 passed**
- [x] one commit, not pushed
- [x] dated `## Report — 2026-09-03` section appended to THIS file with
      the ask-list checklist, and frontmatter `status:` updated to `done`

### Not-done items

None. Every ask in the brief was actionable from inside this repo and
none of them required a decision I could not make.

### Notes flagged to Chris

- Unpushed commits after this session: one on `main` in this repo
  (`claude-code-bridge`), plus whatever prior round-1 commits are ahead
  of the remote. Not a real risk — everything is local work Chris is
  pushing himself; the file record survives with or without the push.
- The 2.1.114 CLI notes are marked dated in the README. The next time
  `claude` on `PATH` is bumped, re-check `--max-turns` and default
  session persistence with `claude --help` and update if the CLI has
  moved. The bridge itself will not spot that regression because
  `Bash(claude:*)` is (correctly) off the allow list.
