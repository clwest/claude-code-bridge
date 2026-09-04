"""Run the fixed contract check against the two real task files that
triggered the 2026-09-04 REPORT: MISSING cry-wolf.

Both files contain a real appended report (`## Report — 2026-09-04` and
`# Report — 2026-09-04` respectively). The task file asked for the
output pasted verbatim.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from claude_code_bridge.contract import check  # noqa: E402


TARGETS = [
    Path.home() / "Donkey_Betz/docs/tasks/TASK_drive-mirror-renames.md",
    Path.home() / "Donkey_Betz/freedom-ford/docs/_internal/TASK_books-1-acquisition-and-relief.md",
]


def main() -> int:
    for path in TARGETS:
        print("=" * 72)
        print("CHECK:", path)
        print("=" * 72)
        # pre_text="" is the worst case for the "added by this run"
        # heuristic — it forces the whole file into `added_text`, so if
        # the report heading is genuinely there we should still detect it.
        result = check(path, "")
        print(result.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
