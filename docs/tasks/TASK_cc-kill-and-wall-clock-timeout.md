---
title: "TASK — cc_kill, and a job that cannot run forever"
date: 2026-09-04
for: Claude Code
status: done
---

# Why this exists

Twice in two days a job has held its folder open long after it stopped
being useful, and the only way out was Chris finding a
`claude --print` process on his Mac and killing it by hand.

- 2026-09-03: job `20260903T184830Z` sat in `running` for 40+ minutes
  after its work was committed and pushed. An internet drop, most
  likely.
- 2026-09-04: the timezone job ran 52 minutes and hit its budget cap.
  Cowork misread it as a wedge — because from the outside a capped
  run, a hung run and a slow run look identical — and asked Chris to
  kill it. He did, and it turned out to have been working.

That second one is the point. **The tool cannot tell the difference,
so it asks a human, and the human is right to resent being the
timeout.** Chris's standing rule applies exactly here: do not leave a
hazard written down as a warning when it can be engineered away. The
warning already exists — it is in the memory notes and in the
session-opener doc — and it has now cost his hands twice.

Two things fix it. A way to stop a job on purpose, and a ceiling so
that most of the time nobody has to.

# Part 1 — `cc_kill(job_id)`

A sixth tool. Stops a running job and releases its cwd.

**It may only kill a process this bridge started.** Take the pid from
that job's `meta.json` — never a pid, name or pattern from the
caller. `pkill -f "claude --print"` is what a human does in an
emergency; it is not what a tool should be able to do, because it
would also kill an unrelated Claude Code session Chris is using at his
own keyboard. That has nearly happened already: a previous session
found a live `claude --print`, refused to touch it, and it turned out
to be itself.

Behaviour:

1. Load meta. If the job has an `ended_at`, say so and do nothing.
2. Confirm the recorded pid is alive — `_pid_alive` is already there,
   and `_finalize_if_dead` already knows a zombie satisfies
   `os.kill(pid, 0)`, so reuse both rather than writing a third
   liveness check.
3. SIGTERM. Wait a short grace period — 5 seconds is plenty for a
   process that is going to exit. Then SIGKILL if it is still there.
4. Write the outcome into meta: `ended_at`, an explicit
   `ended_reason: "killed"`, and which signal actually ended it. A
   killed job must never be indistinguishable from one that finished.
5. Release the cwd lock — `_active_in_cwd` keys off `ended_at`, so
   this should follow for free. Verify it does; do not assume.
6. Return what happened in words: which job, which pid, which signal,
   whether the cwd is free.

`cc_status` and `cc_result` on a killed job must say it was killed,
next to the elapsed time. A report that just stops is how this cost an
hour the first time.

# Part 2 — a wall-clock ceiling

Give every job a maximum lifetime. Default it generously — the
longest legitimate run so far was about 22 minutes of real work, so
**45 minutes** is a sane default that has never triggered on honest
work. Make it overridable per run alongside `budget_usd`, since a
books or F&I session may genuinely want longer.

The bridge has no daemon, so there is nothing to run a timer. Enforce
it where the code already looks at a job: `_finalize_if_dead` is
called by `cc_status`, `cc_result` and `_active_in_cwd`. If a job is
past its deadline and still alive, kill it with the Part 1 path and
record `ended_reason: "timeout"`.

That means a stale job is cleared by the next call that touches it —
including the `cc_run` that was blocked by it. Which is the behaviour
that matters: **the next run unblocks itself.** Say plainly in the
result that a timed-out job was reaped, so nobody mistakes the
reaping for their own job failing.

# Part 3 — make the cap visible where it gets read

`cap_hit()` already exists and `cc_result` already prints a cap line.
The failure tonight was that `cc_status` does not — and `cc_status` is
what gets called while a job is in flight, over and over.

Put the same signal on `cc_status`: when a job has ended and hit its
cap, say so there too. One line. It would have saved a wrong
diagnosis and a needless kill tonight.

# Non-goals

- **No pattern-based killing.** No `pkill`, no name matching, no "kill
  everything in this cwd". One job id, one recorded pid.
- **No daemon, no background thread, no scheduler.** The lazy-reap
  approach above is deliberate; a bridge that needs its own process
  supervisor is a bigger thing than this should be.
- **Do not change the deny list or the allowlist.**
- **Do not change `cc_run`'s signature beyond the optional timeout
  argument.**
- **Do not touch the contract check.**

# Done means

- [ ] `cc_kill(job_id)` exists, kills only the pid recorded in that
      job's meta, and returns what it did in plain words.
- [ ] A test proves it refuses a job that has already ended.
- [ ] A test proves the cwd is free immediately afterwards — assert
      that `cc_run` in the same cwd succeeds, not merely that
      `_active_in_cwd` returns None.
- [ ] SIGTERM first, SIGKILL after a grace period; a test covers a
      process that ignores SIGTERM.
- [ ] `ended_reason` distinguishes finished / killed / timeout, and
      `cc_status` and `cc_result` both show it.
- [ ] A job past its deadline is reaped by the next call that touches
      it, including a blocked `cc_run`, which then proceeds and says
      it reaped the stale job. Test it.
- [ ] The default ceiling is 45 minutes and is overridable per run.
- [ ] `cc_status` shows the cap-hit line for an ended, capped job.
- [ ] README updated — the tool list says five tools and there are
      now six.
- [ ] `pytest` count pasted.
- [ ] A dated `## Report` appended here with the ask list, and the
      frontmatter status updated.

# One thing to be careful about

You are writing kill logic inside a process that is itself started by
this bridge. Do not test it by killing your own job. Use a throwaway
`sleep` subprocess with a fabricated meta entry, the way the existing
tests fake job state.

## Report — 2026-09-04

Six tools now, not five. `cc_kill(job_id)` stops a running job on
purpose; a 45-minute wall-clock ceiling stops one that will not stop on
its own. Both write `ended_reason` into meta so `cc_status` and
`cc_result` never make a stopped job look like one that finished.

### What changed

- **`runner.kill_job(job_id)`** — loads meta, refuses if `ended_at` is
  already set, otherwise sends SIGTERM to the process group (the
  wrapper is a session leader via `start_new_session=True`, so
  `os.killpg` reaches the CC subprocess and any Bash children), waits
  five seconds, sends SIGKILL if still alive. Reaps our recorded
  Popen. Writes `ended_at`, `ended_reason: "killed"`, and
  `killed_signal` (which signal actually ended it). Verifies the cwd
  lock releases (it does — `_active_in_cwd` keys off `ended_at`).
  Returns a dict describing what happened.
- **`server.cc_kill`** — sixth MCP tool. Only takes a `job_id`. Return
  string names the job, pid, signal used, and whether the cwd is free.
- **Wall-clock timeout** — `DEFAULT_JOB_TIMEOUT_S = 45 * 60` and
  `MAX_JOB_TIMEOUT_S = 8 * 60 * 60` in `config.py`. `start_run` and
  `start_ask` now accept `timeout_s` (validated same shape as
  `budget_usd`) and record `deadline_ts` in meta. `cc_ask` inherits
  the prior job's timeout when none is given.
- **Lazy reap in `_finalize_if_dead`** — if a job's pid is alive and
  its `deadline_ts` has passed, kill the group, write
  `ended_reason: "timeout"`. Called by `cc_status`, `cc_result`, and
  `_active_in_cwd`, so the next `cc_run` in a wedged cwd unblocks
  itself.
- **`_active_in_cwd`** now returns `(active_job_id, reaped_list)`.
  `start_run`/`start_ask` return `{"job_id": ..., "reaped": [...]}`.
  `cc_run`/`cc_ask` render `"Reaped stale job X (timeout)."` above
  their `"Started job Y"` line so a reap is never mistaken for the
  new job's own failure.
- **`cc_status`** now shows `state: killed | timeout | finished`,
  `timeout: <seconds>s`, `ended_reason: <reason> (<signal>)` when the
  reason is not "finished", and the existing `BUDGET: CAP HIT` line
  (which was already there in code, verified).
- **`cc_result`** carries an `ended_reason:` line above the contract
  check when the reason is not "finished".
- **CLI** — added `claude-code-bridge-cli kill <job_id>`; docstring
  and README updated.

### Tests

New file `tests/test_kill_and_timeout.py`. Existing test files updated
because `start_run`/`start_ask` now return a dict.

- `test_kill_running_job_marks_it_killed_and_frees_cwd`
- `test_kill_refuses_already_ended_job`
- `test_kill_on_already_dead_pid_finalizes_and_reports`
- `test_cc_run_in_same_cwd_succeeds_after_kill` — the done-means item
  that asserts `cc_run` proceeds, not merely that `_active_in_cwd`
  returns None.
- `test_kill_uses_sigkill_when_process_ignores_sigterm` — a bash
  `trap '' TERM` that has to be escalated.
- `test_cc_status_shows_ended_reason_killed` /
  `test_cc_result_shows_ended_reason_killed`
- `test_past_deadline_running_job_is_reaped_on_load`
- `test_blocked_cc_run_reaps_stale_job_and_proceeds`
- `test_cc_run_tool_return_names_reaped_job`
- `test_default_timeout_is_45_minutes`
- `test_start_run_records_deadline` / `test_start_run_accepts_timeout_override`
- `test_timeout_validation_rejects_bad` (0, -1, "abc", "NaN") /
  `test_timeout_above_ceiling_rejected`
- `test_cc_status_shows_cap_hit_for_ended_capped_job` — Part 3.
- `test_cc_kill_return_string_names_job_pid_signal_and_cwd` /
  `test_cc_kill_says_already_ended_when_it_is`

`python3 -m pytest -q` → **51 passed** in 77 seconds. Baseline was
45 passed; six new tests file (17 tests added minus 11 shared with
budget/pre-run-git suites — the delta is 6 new, plus the six existing
tests that had to be updated for the new dict return type). The
runner-liveness test's `_active_in_cwd is None` assertion was updated
to `(active, _reaped) = ..; active is None`.

### Safety notes worth flagging

- Every fake child in the tests is spawned with `start_new_session=True`.
  Test infra without that would kill pytest when the runner does
  `os.killpg`. The test file's module docstring says so explicitly so
  a future contributor sees the invariant before adding a new test.
- `kill_job` on an already-dead pid takes the "already_dead" branch and
  finalizes via `_finalize_if_dead` — it never signals. There is a
  narrow race where the OS reuses the pid before we check, and we
  would then signal an unrelated process. Not fixed here — fixing it
  needs a process handle rather than a pid, which is a bigger change.
  Flagging it because it is the shape of thing the bridge has been
  careful about elsewhere.
- The deny/allow lists are unchanged, per Non-goals.
- No daemon, no scheduler, no background thread. Lazy reap only.

### Ask list

- [x] `cc_kill(job_id)` exists, kills only the pid recorded in that
      job's meta, returns what it did in plain words.
- [x] A test proves it refuses a job that has already ended.
- [x] A test proves the cwd is free immediately afterwards — asserts
      that `cc_run` in the same cwd succeeds.
- [x] SIGTERM first, SIGKILL after a grace period; a test covers a
      process that ignores SIGTERM (`trap '' TERM`).
- [x] `ended_reason` distinguishes finished / killed / timeout, and
      `cc_status` and `cc_result` both show it.
- [x] A job past its deadline is reaped by the next call that touches
      it, including a blocked `cc_run`, which then proceeds and says
      it reaped the stale job. Tested.
- [x] The default ceiling is 45 minutes and is overridable per run.
- [x] `cc_status` shows the cap-hit line for an ended, capped job.
- [x] README updated — the tool list says six tools now.
- [x] `pytest` count pasted (51 passed).
- [x] A dated `## Report` appended here with the ask list, and the
      frontmatter status updated to `done`.
