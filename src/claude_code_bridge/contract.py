"""Contract check on a TASK_*.md file after a headless CC run.

The workspace CLAUDE.md requires that a completed task get:
  1. A dated `## Report — YYYY-MM-DD` section appended.
  2. An ask-list checklist in that report — every ask marked done / not
     done / not possible.
  3. A frontmatter `status:` line updated (done, blocked, or similar).

The bridge does not fix any of this. It reads the file after the run and
reports what is or isn't there, so Cowork can see immediately without
having to open the file. The "was this added by THIS run" check compares
against the pre-run content that `runner.start_run` cached.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


REPORT_HEADING_RE = re.compile(r"^##\s+Report\s*[—-]\s*(\d{4}-\d{2}-\d{2})", re.M)
CHECKLIST_RE = re.compile(r"^\s*-\s*\[[ xX]\]", re.M)
STATUS_LINE_RE = re.compile(r"^status:\s*(\S.*?)\s*$", re.M)


@dataclass
class ContractResult:
    report_present: bool
    report_added_by_this_run: bool
    ask_list_present: bool
    status: str
    added_bytes: int

    def render(self) -> str:
        rep = "present" if self.report_present else "MISSING"
        if self.report_present and not self.report_added_by_this_run:
            rep += " (but not added by this run — check date)"
        ask = "present" if self.ask_list_present else "MISSING"
        return (
            f"REPORT: {rep}\n"
            f"ASK LIST: {ask}\n"
            f"STATUS: {self.status}\n"
            f"BYTES ADDED THIS RUN: {self.added_bytes}\n"
        )


def check(task_file: Path, pre_text: str) -> ContractResult:
    """Report on the task file's post-run state.

    `pre_text` is the file's content before the run started. The "added by
    this run" heuristic looks at the tail of the file that is new — reports
    are appended, so this is the simplest reliable signal.
    """
    post_text = task_file.read_text()

    # Report heading anywhere.
    report_present = bool(REPORT_HEADING_RE.search(post_text))

    # Was a report added by THIS run? Compute the tail added after the
    # pre-run text (if the pre-text is a strict prefix of post-text, use
    # the tail; otherwise fall back to "did any new report heading appear
    # that wasn't there before").
    if post_text.startswith(pre_text):
        added_text = post_text[len(pre_text):]
    else:
        added_text = post_text  # fallback — CC rewrote the file
    added_bytes = len(added_text.encode())

    pre_headings = set(REPORT_HEADING_RE.findall(pre_text))
    post_headings = set(REPORT_HEADING_RE.findall(post_text))
    new_report_headings = post_headings - pre_headings
    report_added_by_this_run = bool(new_report_headings) or bool(
        REPORT_HEADING_RE.search(added_text)
    )

    # Ask list — check the added portion first, then fall back to whole file.
    if CHECKLIST_RE.search(added_text):
        ask_list_present = True
    else:
        # Whole-file fallback still yields "present", but a stale checklist
        # elsewhere in the file is not conclusive. Note in tests.
        ask_list_present = bool(CHECKLIST_RE.search(post_text)) and report_added_by_this_run

    m = STATUS_LINE_RE.search(post_text)
    status = m.group(1) if m else "(none)"

    return ContractResult(
        report_present=report_present,
        report_added_by_this_run=report_added_by_this_run,
        ask_list_present=ask_list_present,
        status=status,
        added_bytes=added_bytes,
    )
