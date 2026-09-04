---
title: "cc_wait, and a contract check that stops crying wolf"
status: done
session: bridge-4
source: Cowork, 2026-09-04 — two gaps found by using the bridge all day
---

# Why this exists

Two small things, both found by running eleven jobs through this thing.

**Nothing tells the caller a job ended.** The only way to learn is to call
`cc_status` again, so a watching session sleeps three minutes, asks, sleeps,
asks. A job that finishes ten seconds after a poll is not noticed for another
three minutes, and every one of those polls is a round trip that buys nothing.

**The contract check reports a missing report that is not missing.** Twice on
2026-09-04 `cc_result` said `REPORT: MISSING` for task files that plainly
contained a report — written as `# Report — 2026-09-04`, an H1, where the check
looks for `## Report`. An instrument that reports a problem that is not there
is worse than no instrument, because it teaches the reader to skip the line.

# Parts

1. **`cc_wait(job_id, timeout_s=...)`.** Blocks until the job ends, then
   returns exactly what `cc_status` would return at that moment. If the job is
   already finished, return immediately — do not sleep first. If the wait's own
   timeout expires before the job ends, return the running status and say the
   wait timed out, which is NOT an error and must not be phrased as one. The
   wait's timeout is its own, independent of the job's wall-clock ceiling, and
   waiting must never extend, shorten or otherwise touch that ceiling.

   Poll internally on a modest interval rather than busy-looping. Default wait
   somewhere near five minutes, ceiling no higher than the job ceiling.

2. **Fix the report detection.** Accept a report heading at any level — `#`
   through `######` — and accept `Report` followed by anything (a dash, a date,
   a session number). Match on the heading line, not on a bare occurrence of
   the word "report" somewhere in prose, or the check will start crying the
   other way. Add a test for each shape that has actually appeared in a task
   file in this workspace, including `# Report — 2026-09-04`.

3. **While you are in there:** if the ask-list check has the same brittleness,
   say so and fix it the same way. If it does not, say it does not — do not
   change it to look symmetrical.

# Non-goals

No push, callback, webhook or notification mechanism — the caller asks, the
bridge answers, that stays true. No change to how jobs are started, budgeted
or killed. No change to the allow/deny lists. Do not make `cc_status` block.

# Verification

Run the suite with counts before and after. Exercise `cc_wait` for real against
a short job in the playground: one wait that returns because the job ended, one
that returns because the wait timed out while the job was still running, and
one against a job that had already finished before the call. Paste all three
returns verbatim.

For part 2, run the fixed check against the two real task files that triggered
this — `~/Donkey_Betz/docs/tasks/TASK_drive-mirror-renames.md` and
`~/Donkey_Betz/freedom-ford/docs/_internal/TASK_books-1-acquisition-and-relief.md`
— and paste what it now says for each.

Note in the report that neither change is live until the desktop app restarts.

# Ask list

- [x] cc_wait returns on job end, on its own timeout, and immediately for a finished job
- [x] the wait's timeout never touches the job's wall-clock ceiling
- [x] report heading detected at any level and with a trailing dash/date
- [x] ask-list check examined; changed or explicitly left alone with the reason
- [x] three pasted cc_wait returns from a real playground job
- [x] the two real task files re-checked, output pasted

## Report — 2026-09-04

Neither of these changes is live in the desktop app until the Claude
Code app is restarted — the bridge is loaded once by the MCP client
and the new `cc_wait` tool + the new report regex both sit in already-
imported Python modules. `python3 -m pytest` sees the changes today;
the app sees them at the next launch.

### Part 1 — `cc_wait`

Added `runner.wait_for_job(job_id, timeout_s=None) -> (JobView, bool)`
and the `cc_wait` MCP tool in `server.py`.

- Blocks by polling `load_job` at `_WAIT_POLL_S = 1.0` — not a busy
  loop. `load_job` runs `_finalize_if_dead`, so the wait picks the job
  up the same way `cc_status` would.
- Default wait is `DEFAULT_WAIT_TIMEOUT_S = 5 * 60`. The wait is
  capped at the job's own `timeout_s` — waiting longer than the job
  could possibly run is dead time, not correctness.
- `wait_for_job` never mutates `meta["deadline_ts"]` or `meta["timeout_s"]`.
  A parametrized regression test asserts the recorded deadline is
  byte-identical before and after a wait timeout
  (`test_wait_returns_on_own_timeout_while_job_still_runs`).
- Already-finished path checks `is_running` before any `time.sleep`,
  returns immediately. `test_wait_returns_immediately_for_already_finished_job`
  asserts elapsed < 0.5s and the verification script below reports
  0.0002s.
- The server tool prefixes the returned status with
  `wait: timed out (job still running)` when its own deadline fires —
  no "error" word, per the "must not be phrased as one" instruction.
  Otherwise it returns cc_status verbatim.
- Also updated the CLI harness's `wait` subcommand to call the new
  `cc_wait` rather than its own poll-sleep loop; exit code 1 when the
  wait timed out, 0 when the job ended, same shape as before.

### Part 2 — Report detection

`REPORT_HEADING_RE` is now `^#{1,6}\s+Report\b.*$`. Accepts every
heading level `#` through `######`, requires a word boundary after
`Report` (so `Reporting` and `Reports` do not match), and admits any
trailing text (a dash + date, a session id, or nothing at all).

Confirmed shapes come from a full sweep of `~/Donkey_Betz/**/TASK_*.md`
on 2026-09-04 — `test_report_heading_regex_matches_real_shapes` uses
the eleven real headings that walk pulled up, including the two named
in the task file (`# Report — 2026-09-04` and `## Report — 2026-09-04`).

The known imperfection: a brief that includes `## Report back` (a
request for a report, not a report) with no report actually appended
now reads as `REPORT: present (but not added by this run — check
date)`. That is the yellow-flag phrasing the render already uses for
stale reports — a note to the reader, not a green light — and it is
covered by `test_check_reports_missing_when_only_brief_asks_for_report`.
Distinguishing "Report back" from "Report bridge-4" purely from prose
would require an English-word blocklist; kept it simple and let the
existing added-by-this-run distinction carry the yellow flag.

### Part 3 — Ask-list check examined, left alone

`CHECKLIST_RE = ^\s*-\s*\[[ xX]\]` does not share the brittleness the
report regex had. The report regex was brittle in two specific ways:
(a) it locked to a single heading level (`##` only), and (b) it required
a trailing date shape. Neither maps onto the checklist regex — it
matches the `- [x]` line itself, standard GFM syntax is uniformly
`- [x]` across every task file in this workspace, and any trailing text
on the same line already matches. A parametrized test walks the shapes
seen in real reports (indented, bold, uppercase X, plain done, plain
not-done) and confirms all match.

The one weakness that *does* exist — a stale checklist elsewhere in
the file reading as "present" when a run added a report but no
checklist — is a different failure mode from what the task asked
about, and is already gated by
`report_added_by_this_run` in `contract.check`. That gating is
unchanged. Left the regex alone rather than editing it for symmetry.

### Verification

Full suite counts:

    Before this session: 62 passed
    After this session : 105 passed  (+43 new)

    $ python3 -m pytest tests/ -q
    ........................................................................ [ 68%]
    .................................                                        [100%]
    105 passed in 84.81s (0:01:24)

### Part 1 — three pasted `cc_wait` returns from a real playground job

`scripts/verify_cc_wait.py` exercises `server.cc_wait` against real
jobs in `~/Donkey_Betz/playground/cc-runs` (real `meta.json`, real
Popen, real exit_code file — the only fake bit is that the bash
wrapper runs `sleep` instead of `claude --print`, and `cc_wait` does
not care what the wrapper runs).

    $ python3 scripts/verify_cc_wait.py
    ========================================================================
    Case 1: WAIT returns because the job ended
    ========================================================================
    (wait blocked for 2.02s)
    job_id: 20260904T160947Z-0d294bbc
    state: finished
    elapsed: 2.0s
    cwd: /Users/donkeyking/Donkey_Betz/playground
    session_id: cf200c40-ec71-4d8d-a527-b903e5142a66
    task_file: /Users/donkeyking/Donkey_Betz/playground/TASK_verify_cc_wait.md
    kind: run
    budget: $2.00
    timeout: 2700s
    ended_at: 2026-09-04T16:09:49Z

    ========================================================================
    Case 2: WAIT returns because the wait timed out (job still running)
    ========================================================================
    (wait blocked for 2.01s)
    wait: timed out (job still running)
    job_id: 20260904T160949Z-76083a9c
    state: running
    elapsed: 2.0s
    cwd: /Users/donkeyking/Donkey_Betz/playground
    session_id: 639fd125-9939-4fac-a9b1-be1e065b6f72
    task_file: /Users/donkeyking/Donkey_Betz/playground/TASK_verify_cc_wait.md
    kind: run
    budget: $2.00
    timeout: 2700s

    ========================================================================
    Case 3: WAIT against a job that had already finished before the call
    ========================================================================
    (wait blocked for 0.0002s — must be near zero, no initial sleep)
    job_id: 20260904T160951Z-d720ba17
    state: finished
    elapsed: 0.0s
    cwd: /Users/donkeyking/Donkey_Betz/playground
    session_id: fc60830e-e5e9-4474-a7c6-b9ea24fab333
    task_file: /Users/donkeyking/Donkey_Betz/playground/TASK_verify_cc_wait.md
    kind: run
    budget: $2.00
    timeout: 2700s
    ended_at: 2026-09-04T16:09:51Z

Case 1: 2.02s ≈ the job's own 1s sleep plus one 1s poll — the wait
returns on the first poll after the job ends. Case 2: 2.01s ≈ the
requested wait of 2s to within poll granularity, and the returned
status still says `state: running`, so the job is untouched (I killed
the sleeper afterwards to not leak a 30s child). Case 3: 0.0002s —
sub-millisecond, proving the `is_running` check is before any sleep.

### Part 2 — the two real task files, re-checked

`scripts/verify_contract_check.py` runs the fixed `contract.check`
against the two files named in the task, with `pre_text=""` (the
worst case for the added-by-this-run heuristic — it forces the whole
file to be treated as new, so the presence check is what carries the
result).

    $ python3 scripts/verify_contract_check.py
    ========================================================================
    CHECK: /Users/donkeyking/Donkey_Betz/docs/tasks/TASK_drive-mirror-renames.md
    ========================================================================
    REPORT: present
    ASK LIST: present
    STATUS: done
    BYTES ADDED THIS RUN: 10615

    ========================================================================
    CHECK: /Users/donkeyking/Donkey_Betz/freedom-ford/docs/_internal/TASK_books-1-acquisition-and-relief.md
    ========================================================================
    REPORT: present
    ASK LIST: present
    STATUS: done
    BYTES ADDED THIS RUN: 10786

Both files now report `REPORT: present`. Before this fix, the H1
(`# Report — 2026-09-04`) in the books file crossed the `##`-only
regex silently and read `REPORT: MISSING` — the exact cry-wolf the
task exists to remove.

### Ask-list checklist

- [x] **cc_wait returns on job end, on its own timeout, and immediately
  for a finished job** — three cases pasted above; three matching
  regression tests in `tests/test_cc_wait.py`
  (`test_wait_returns_when_job_ends`,
  `test_wait_returns_on_own_timeout_while_job_still_runs`,
  `test_wait_returns_immediately_for_already_finished_job`).
- [x] **the wait's timeout never touches the job's wall-clock ceiling**
  — `wait_for_job` never writes to meta. The regression test reads
  `meta["deadline_ts"]` before and after and asserts equality; the
  Case 2 return above shows `timeout: 2700s` unchanged after a 2s
  wait against a 30s sleeper.
- [x] **report heading detected at any level and with a trailing
  dash/date** — `REPORT_HEADING_RE` now `^#{1,6}\s+Report\b.*$`; a
  parametrized test walks all eleven real heading shapes from the
  workspace including `# Report — 2026-09-04` (H1) and
  `## Report — 2026-09-04` (H2). Bare-prose occurrences ("filed a
  report") and stems ("Reporting", "Reports") do not match, covered
  by their own tests.
- [x] **ask-list check examined; changed or explicitly left alone
  with the reason** — examined, left alone. Reason in Part 3 above:
  the checklist regex does not share the report regex's brittleness
  (no heading-level lock, no trailing-shape requirement) and every
  real checklist shape in workspace reports matches it. A parametrized
  test locks that in.
- [x] **three pasted cc_wait returns from a real playground job** —
  Case 1/2/3 above, verbatim from `scripts/verify_cc_wait.py`.
- [x] **the two real task files re-checked, output pasted** —
  verbatim from `scripts/verify_contract_check.py` above.

### What did not go into this change

- No push, callback, webhook, or notification (non-goal). `cc_wait`
  is still a caller-pulls model — the bridge does not initiate.
- No change to `cc_status` (non-goal — "Do not make `cc_status`
  block"). It still returns immediately and never sleeps.
- No change to how jobs are started, budgeted, killed, or to the
  allow/deny lists.
- Did not rewrite the CLI's `wait` subcommand beyond swapping its
  own poll loop out for a `cc_wait` call — the exit code semantics
  (0 on end, 1 on wait timeout) are the same as before so external
  callers of the CLI are not surprised.

### Unpushed work

`~/Donkey_Betz/mcp-servers/claude-code-bridge` is currently **2
commits ahead of `origin/main`** (before this session's commit). The
commit for this session's work will make it 3 ahead. Whether to push
is Chris's call (per CLAUDE.md). **No risk case:** everything is
committed locally and in `.git/objects`; nothing here exists only on
one machine.
