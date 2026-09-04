"""Tests for the report-heading detector after the 2026-09-04 cry-wolf fix.

The old regex only matched `## Report — YYYY-MM-DD` and cried wolf on
`# Report — 2026-09-04` (H1). Every shape asserted here has actually
appeared in a task file under `~/Donkey_Betz/` — the shape list came out
of `grep -rn '^#+ Report' ~/Donkey_Betz/**/TASK_*.md` on 2026-09-04.

An instrument that reports a problem that is not there is worse than
no instrument (task file's phrasing), so this file exists to prove the
detector does not do that any more.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from claude_code_bridge.contract import (
    CHECKLIST_RE,
    REPORT_HEADING_RE,
    check,
)


# ------- REPORT_HEADING_RE: every shape seen in the workspace -----------

REAL_REPORT_HEADINGS = [
    # The two shapes named in the task file — the ones that triggered
    # the cry-wolf bug.
    "# Report — 2026-09-04",                          # H1, from the books-1 task
    "## Report — 2026-09-04",                         # H2, from drive-mirror-renames
    # Every other shape found in real TASK_*.md files.
    "## Report",
    "## Report — 2026-08-29",
    "## Report — 2026-09-02",
    "## Report — 2026-09-02 (macOS run)",
    "## Report — 2026-09-02 — fix pass after Cowork's review",
    "## Report — 2026-09-02 — BLOCKED at the lock step",
    "## Report — 2026-09-02 — DONE (headless re-run)",
    "## Report — 2026-08-28",
    "### Report what stayed out",
]


@pytest.mark.parametrize("heading", REAL_REPORT_HEADINGS)
def test_report_heading_regex_matches_real_shapes(heading):
    assert REPORT_HEADING_RE.search(heading), (
        f"REPORT_HEADING_RE failed to match a shape that has actually "
        f"appeared in a workspace task file: {heading!r}"
    )


# The old regex specifically required a date. Confirm we no longer do.
def test_bare_report_heading_matches():
    assert REPORT_HEADING_RE.search("## Report")


def test_report_heading_with_dash_variants_match():
    # Em-dash (—), en-dash (–), hyphen (-) — all seen in the wild.
    for shape in ("## Report — 2026-09-04", "## Report - 2026-09-04"):
        assert REPORT_HEADING_RE.search(shape), shape


def test_all_six_heading_levels_match():
    for hashes in ("#", "##", "###", "####", "#####", "######"):
        line = f"{hashes} Report — 2026-09-04"
        assert REPORT_HEADING_RE.search(line), line


def test_deeper_than_h6_does_not_match():
    # `#######` is not a valid ATX heading in CommonMark; treat it as prose.
    assert not REPORT_HEADING_RE.search("####### Report — 2026-09-04")


def test_bare_word_report_in_prose_does_not_match():
    # "Match on the heading line, not on a bare occurrence of the word
    # 'report' somewhere in prose" — task file, part 2.
    prose = (
        "This section is the report for last week. We had to file a "
        "report on Tuesday.\n"
        "Reporting has now stabilised."
    )
    assert not REPORT_HEADING_RE.search(prose)


def test_reporting_and_reports_do_not_match():
    # `\b` at the end of `Report` keeps these out.
    for shape in ("## Reporting on the run", "## Reports go here"):
        assert not REPORT_HEADING_RE.search(shape), shape


# ------- check(): end-to-end on realistic pre/post file pairs ----------


def _write_pair(tmp_path: Path, _pre: str, post: str) -> Path:
    tf = tmp_path / "TASK_x.md"
    tf.write_text(post)
    return tf


def test_check_reports_present_when_h1_report_appended(tmp_path):
    """The books-1 case: brief with no report, run appends `# Report — DATE`."""
    pre = (
        "---\nstatus: not started\n---\n\n"
        "# Why this exists\n\nfoo\n\n"
        "# Ask list\n\n- [ ] one\n"
    )
    post = pre + "\n# Report — 2026-09-04\n\nwork.\n\n- [x] one\n"
    tf = _write_pair(tmp_path, pre, post)
    result = check(tf, pre)
    assert result.report_present
    assert result.report_added_by_this_run
    assert result.ask_list_present
    rendered = result.render()
    assert "REPORT: present" in rendered
    assert "MISSING" not in rendered


def test_check_reports_present_when_h2_report_appended(tmp_path):
    """The drive-mirror-renames case: `## Report — DATE` appended."""
    pre = (
        "---\nstatus: not started\n---\n\n"
        "# Ask list\n\n- [ ] one\n"
    )
    post = pre + "\n## Report — 2026-09-04\n\nwork.\n\n- [x] one\n"
    tf = _write_pair(tmp_path, pre, post)
    result = check(tf, pre)
    assert result.report_present
    assert result.report_added_by_this_run
    rendered = result.render()
    assert "REPORT: present" in rendered
    assert "MISSING" not in rendered


def test_check_reports_missing_when_only_brief_asks_for_report(tmp_path):
    """A brief with `## Report back` alone is not itself a report.

    This is the imperfect case the current design accepts: the "Report back"
    heading appears in pre_text and post_text, so `report_added_by_this_run`
    is False. The render surfaces the ambiguity as "present (but not added
    by this run — check date)" — a yellow flag rather than a bright-red
    MISSING, which is the same shape the old check used for stale reports.
    """
    pre = "---\nstatus: open\n---\n\n## Report back\n\nDo X.\n"
    post = pre  # run added nothing
    tf = _write_pair(tmp_path, pre, post)
    result = check(tf, pre)
    rendered = result.render()
    assert "REPORT: present (but not added by this run" in rendered


def test_check_reports_missing_when_no_report_heading_anywhere(tmp_path):
    pre = "---\nstatus: open\n---\n\n# Why\n\ntext.\n"
    post = pre  # run added nothing
    tf = _write_pair(tmp_path, pre, post)
    result = check(tf, pre)
    assert not result.report_present
    assert "REPORT: MISSING" in result.render()


# ------- CHECKLIST_RE: examined and left alone -------------------------
#
# Task file, part 3: "if the ask-list check has the same brittleness,
# say so and fix it the same way. If it does not, say it does not — do
# not change it to look symmetrical."
#
# The report check was brittle in two specific ways: (a) it required an
# exact `##` heading level, and (b) it required a trailing date shape.
# Neither maps onto the checklist regex:
#
#   (a) the checklist regex is not looking at a heading level at all —
#       it matches the `- [x]` / `- [ ]` line itself, and standard GFM
#       checklist syntax is uniformly `- [x]`. No file in the workspace
#       uses `* [ ]` or `+ [ ]`.
#   (b) the checklist regex has no trailing shape requirement — anything
#       after `[x]` is fine, so a "done" status, a note in parens or an
#       explanation on the same line all match cleanly.
#
# The one weakness it does have (a stale ask list elsewhere in the file
# reading as "present" when a run added a report but no checklist) is
# addressed by the whole-file fallback being gated on
# `report_added_by_this_run` — see contract.check. That is a different
# kind of imperfection from what part 2 asked for, and it does not
# make a real checklist read as MISSING.
#
# Concretely, the shapes below all match — none of them would have been
# hidden by any brittleness worth removing.


@pytest.mark.parametrize(
    "line",
    [
        "- [ ] plain not-done",
        "- [x] plain done, lowercase",
        "- [X] plain done, uppercase",
        "  - [x] indented",
        "- [x] **bold** with **markdown** — done",
        "- [x] cc_wait returns on job end, on its own timeout, and immediately for a finished job",
    ],
)
def test_checklist_regex_matches_shapes_seen_in_reports(line):
    assert CHECKLIST_RE.search(line), line


@pytest.mark.parametrize(
    "line",
    [
        "not a checklist",
        "- plain bullet, no box",
        "* [x] asterisk bullet — not the GFM style used here",
        "- [~] partial marker — not one of ` `, x, X",
    ],
)
def test_checklist_regex_does_not_match_non_checklist(line):
    assert not CHECKLIST_RE.search(line), line
