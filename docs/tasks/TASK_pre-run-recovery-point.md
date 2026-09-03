---
title: "TASK — record a recovery point before every run, and print it in the result"
date: 2026-09-03
for: Claude Code (headless, via the bridge)
status: done
protocol: ../../../../CLAUDE.md
---

# Why this exists

The README now says the honest thing: the shell deny list is not a boundary,
and the real containment is the cwd bound, the one-run-per-cwd lock and the
budget cap. Chris chose that (option 1, 2026-09-03) on the argument that no
deny list can bound a session that can write a file and run a test.

Which leaves the question the deny list was never answering: **when a run
damages something, how do we get it back?**

Today the answer is "hope it was committed." That is not good enough for the
thing the bridge is actually for — a headless session, unattended, editing a
repo with uncommitted work in it. The failure that happens here is not a rogue
session; it is a normal session doing something destructive inside a cwd it was
legitimately given.

This adds the cheapest real fix: take a recovery point before the run starts,
anchor it so git will not collect it, and print it where the reviewer already
looks.

Evidence it is needed, from tonight, in this workspace: a git write left
`.git/index.lock`, `.git/HEAD.lock` and orphaned `tmp_obj_*` files behind and
blocked every later git command until a human cleared them. Five of those
orphans were already there from an earlier episode nobody had noticed. Written
up as L-038 in `~/Donkey_Betz/docs/LESSONS.md`.

---

## Item 1 — capture the recovery point at spawn

In `runner.py`, before `_spawn` is called, in both `start_run` and `start_ask`,
collect the state of the run's `cwd`. Put it in one helper — do not write this
twice — and have it return a dict that goes straight into `meta.json` under a
`pre_run_git` key.

What to capture:

- `head_sha` — `git rev-parse HEAD`
- `branch` — `git rev-parse --abbrev-ref HEAD`
- `dirty` — true if `git status --porcelain` is non-empty
- `snapshot_ref` — see Item 2
- `status_porcelain` — the verbatim output of `git status --porcelain`, written
  to `pre_git_status.txt` in the run directory alongside `pre.txt`, not inlined
  into `meta.json` (it can be long)

All of these are the **bridge's own** subprocesses, not CC's, so no allowlist
question arises. Requirements on how you run them:

- pass `cwd=` explicitly, never rely on the process's working directory
- put a short timeout on every call (5 seconds is plenty) so a hung or
  network-backed git can never block a spawn
- **never let a git failure fail the run.** A cwd that is not a git repository
  is a normal case, not an error: record `{"git": "not a repository"}` and
  carry on. Same for any unexpected git error — record what happened, spawn
  anyway. The recovery point is a convenience; refusing to work without one
  would be worse than not having it.

## Item 2 — the snapshot has to survive garbage collection

`git stash create` writes a commit object capturing the working tree without
touching the working tree, the index or the stash list. That is exactly what we
want — except the object it returns is **unreferenced**, so a later `git gc`
can delete it, and this workspace runs git maintenance.

So: when the tree is dirty and `git stash create` returns a sha, immediately
anchor it:

```
git update-ref refs/cc-bridge/<job_id> <sha>
```

Record that ref name in `meta.json` as `snapshot_ref`. When the tree is clean,
there is nothing to snapshot — record `snapshot_ref: null` and say "clean tree"
rather than inventing an empty one.

**State the limitation in the code comment and in the README, because it is a
real one:** `git stash create` does **not** capture untracked files. A file CC
has never seen committed is not in the snapshot. That is why
`status_porcelain` is captured too — it at least tells the reviewer which
untracked paths existed before the run, so a missing one is visible rather than
silent. Do not try to fix this by stashing untracked files; that changes the
working tree, and a transport layer must not do that to a repo it was only
asked to run a session in.

## Item 3 — print it where the reviewer looks

`cc_result` already prints the contract check and the budget. Add the recovery
point to that block:

```
pre-run HEAD: 380e98b (main), tree dirty
pre-run snapshot: refs/cc-bridge/20260903T001809Z-69a69826
recover with: git diff 380e98b..HEAD    |    git stash apply refs/cc-bridge/20260903T001809Z-69a69826
```

On a clean tree, say `tree clean — no snapshot needed` and print only the diff
command. If git was unavailable, print one line saying so. Keep it three lines
at most; this sits above the CC output and must not push the actual result off
the screen.

`cc_status` gets one line — `pre-run HEAD: <short sha> (<branch>)` — and no
recovery commands. Status is for "is it done yet", not for recovery.

## Item 4 — README

Add a short section, **Recovering from a run**, after the contract-check
section. It should say what is captured, where it lives, the two recovery
commands, and the untracked-files limitation stated plainly. Also mention that
snapshot refs accumulate under `refs/cc-bridge/` and can be listed with
`git for-each-ref refs/cc-bridge/` and deleted by hand — the bridge does not
delete them, because a transport layer that prunes its own recovery points is
not a recovery mechanism.

## Item 5 — tests

Add tests that build a real temporary git repo (git is available; `claude` is
not, and the existing tests avoid needing it — keep that property). Set
`user.name` and `user.email` per test repo with `-c` flags or `git config` so
commit works on any machine.

Cover:

- clean tree → `head_sha` and `branch` recorded, `snapshot_ref` is null
- dirty tree → `snapshot_ref` is set, the ref exists under `refs/cc-bridge/`,
  and the commit it points at contains the modified content
- the snapshot survives `git gc --prune=now` (this is the whole point of Item 2
  — if this test does not exist, the feature is not tested)
- a cwd that is not a git repository → no exception, meta records it, spawn
  still happens
- `status_porcelain` is written to the run directory and names an untracked file
- a git call that fails or times out does not raise out of the helper

Run `pytest` and give the real counts.

---

## Non-goals

- **Do not refuse to start on a dirty tree.** That was considered and rejected:
  most real work here starts from a dirty tree, and a transport layer that
  blocks on it would just get worked around. Record, do not gate.
- **Do not commit, stash-push, checkout, reset or clean anything.** The only
  writes to the repo are `git stash create` (which writes an object, not the
  tree) and `git update-ref` (which writes a ref). Nothing else.
- **Do not delete old snapshot refs.**
- **Do not change `ALLOWED_TOOLS` or `DISALLOWED_TOOLS`.** Settled 2026-09-03.
- **Do not add a sixth tool.** Five is the surface.
- **Do not try to push.** Chris pushes.

## Done means

- [ ] one helper captures the pre-run git state, is called from both
      `start_run` and `start_ask`, and cannot raise out
- [ ] every git subprocess has an explicit `cwd` and a timeout
- [ ] a dirty tree produces a `git stash create` object anchored at
      `refs/cc-bridge/<job_id>`, recorded in `meta.json`
- [ ] a clean tree records `snapshot_ref: null` and no ref is created
- [ ] a non-git cwd records the fact and the run still spawns
- [ ] `pre_git_status.txt` written to the run directory
- [ ] `cc_result` prints HEAD, branch, dirty flag, snapshot ref and the recovery
      commands, in at most three lines
- [ ] `cc_status` prints the one-line version
- [ ] README has a **Recovering from a run** section, including the
      untracked-files limitation and the accumulating-refs note
- [ ] tests cover all six cases in Item 5, including survival of
      `git gc --prune=now`; report the real pytest count
- [ ] one commit, not pushed
- [ ] dated `## Report — 2026-09-03` section appended to THIS file with the
      ask-list checklist, and frontmatter `status:` updated

---

## Report — 2026-09-03

Implemented the pre-run recovery point end-to-end. All work stays inside the
bridge repo (`~/Donkey_Betz/mcp-servers/claude-code-bridge/`).

### What changed

- **`runner.py`** — added `_pre_run_git_snapshot(cwd, job_id, run_dir)` and a
  `_git(args, cwd)` helper. Every git subprocess passes `cwd=` explicitly and
  uses a 5-second timeout (`_GIT_TIMEOUT_S`). The helper never raises out:
  `FileNotFoundError`, `TimeoutExpired`, `CalledProcessError` and `OSError`
  each land in a targeted `except` and return a dict describing the situation
  (`{"git": "not a repository"}`, `{"git": "git timed out"}`, etc.). On a
  dirty tree it runs `git stash create` and, when a sha comes back,
  immediately anchors it with `git update-ref refs/cc-bridge/<job_id> <sha>`
  so a later `git gc` cannot collect it. `git status --porcelain` is written
  verbatim to `pre_git_status.txt` in the run directory. `snapshot_ref` is
  recorded in `meta.json` under a new `pre_run_git` key.
- **`start_run` and `start_ask`** both call the helper before `_spawn`, so the
  recovery point exists before any child process runs. On start_ask this
  captures the state at the moment the follow-up begins, which is what
  matters — the previous ask may have moved HEAD.
- **`server.py`** — `cc_result` now prints a `_recovery_block(...)` at the
  bottom of the header, at most three lines: `pre-run HEAD: <short> (<branch>),
  tree dirty|clean` + `pre-run snapshot: refs/cc-bridge/<job_id>` (only when
  dirty and anchored) + `recover with: git diff <short>..HEAD [| git stash
  apply <ref>]`. Non-repo / no-HEAD cases collapse to one line and no commands.
  `cc_status` gets a one-line `pre-run HEAD: <short> (<branch>)` via
  `_status_pre_run_line(...)`, per the Item 3 rule ("status is for 'is it
  done yet', not recovery").
- **README.md** — added a "Recovering from a run" section between the contract
  check and the containment section. Includes: what is captured, where it
  lives, the two recovery commands, the plain statement that
  `git stash create` does not capture untracked files (with the reason we did
  not try to fix that), and the accumulating-refs note (`git for-each-ref
  refs/cc-bridge/`, delete by hand — the bridge does not delete its own
  recovery points).
- **`tests/test_pre_run_git.py`** — new file, 8 tests, covering all six cases
  from Item 5 against a real temporary git repo. `_init_repo` sets
  `user.name`/`user.email`/`gc.auto=0` per-repo so commit works on any machine
  and the gc test is not raced by ambient maintenance. `_spawn` is stubbed the
  same way `test_budget_and_cap.py` does it — no real `claude`, no real bash
  child — so the suite still runs on machines without the CLI.

### Test count

Real `python -m pytest` output, no fudging:

```
collected 30 items

tests/test_budget_and_cap.py .................                           [ 56%]
tests/test_pre_run_git.py ........                                       [ 83%]
tests/test_runner_liveness.py .....                                      [100%]

============================== 30 passed in 3.47s ==============================
```

Eight new (all in `test_pre_run_git.py`), zero regressions on the previously
green 22.

### Things I noticed while doing this, worth writing down

- `git stash create` on a working tree that has **only** untracked changes
  returns an empty string, not a stash object. That is fine — the helper
  treats an empty return as "nothing anchored" and leaves `snapshot_ref: null`
  — but it means the wired-in `test_start_run_records_pre_run_git_in_meta`
  test asserts `dirty=True` and does not assert on `snapshot_ref`, because
  `TASK_x.md` is untracked and there is nothing else to snapshot. Modified
  tracked files do produce a stash object; that path is covered by the
  dedicated `test_dirty_tree_writes_anchored_snapshot_with_modified_content`
  and `test_snapshot_survives_git_gc_prune_now` tests. The README already
  states this limitation. This is a real "untracked files are outside the
  recovery point" corner, worth flagging even though it is documented.
- Nothing in the deny list was touched, no sixth tool added, no push
  attempted. Non-goals held.

### Ask list

- [x] one helper captures the pre-run git state, is called from both
      `start_run` and `start_ask`, and cannot raise out
- [x] every git subprocess has an explicit `cwd` and a timeout (5s)
- [x] a dirty tree produces a `git stash create` object anchored at
      `refs/cc-bridge/<job_id>`, recorded in `meta.json`
- [x] a clean tree records `snapshot_ref: null` and no ref is created
- [x] a non-git cwd records the fact and the run still spawns
- [x] `pre_git_status.txt` written to the run directory
- [x] `cc_result` prints HEAD, branch, dirty flag, snapshot ref and the
      recovery commands, in at most three lines
- [x] `cc_status` prints the one-line version
- [x] README has a **Recovering from a run** section, including the
      untracked-files limitation and the accumulating-refs note
- [x] tests cover all six cases in Item 5, including survival of
      `git gc --prune=now`; real count reported (30 passed, 8 new)
- [x] one commit, not pushed (see below)
- [x] dated `## Report — 2026-09-03` section appended with the ask-list
      checklist, and frontmatter `status:` updated to `done`

### Unpushed commits

One commit on `main` in `claude-code-bridge`, not pushed. Not a real risk:
the change lives in a git repo with a remote and is one push away from
backup; nothing in this session is the only copy of anything. Chris pushes.
