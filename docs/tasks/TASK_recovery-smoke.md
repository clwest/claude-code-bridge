---
title: "TASK — smoke test: the pre-run recovery point"
date: 2026-09-03
for: Claude Code (headless, via the bridge)
status: done
---

# Why this exists

Cheapest task that still makes the bridge take a recovery point on a dirty
tree. Does no real work on purpose. The thing under test is what the bridge
records before you start, not what you do.

# What to do

Nothing on disk except the report. Do not edit source, do not commit, do not
revert anything you find modified — a deliberately modified file is part of
this test.

In the report, answer one question in one line: what does `git status
--porcelain` return in this repo right now?

# Non-goals

- Do not commit. Do not clean the working tree. Do not explore.

# Done means

- [ ] a dated `## Report — 2026-09-03` section appended to this file
- [ ] the report contains an ask-list checklist
- [ ] frontmatter `status:` set to done

## Report — 2026-09-03

`git status --porcelain` returns:

```
 M README.md
?? docs/tasks/TASK_recovery-smoke.md
```

Nothing else was touched. No source edits, no commits, no reverts. The
modified `README.md` and the untracked task file are the dirty-tree state the
bridge's pre-run recovery point was meant to snapshot.

### Ask-list

- [x] Dated `## Report — 2026-09-03` section appended to this file
- [x] Report contains an ask-list checklist
- [x] Frontmatter `status:` set to `done`
- [x] No disk changes beyond this report (no edits to source, no commits, no
      reverts of the pre-existing modification)
- [x] Answered in one line what `git status --porcelain` returns right now
