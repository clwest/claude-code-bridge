"""Local CLI harness for smoke-testing the bridge without MCP transport.

Usage:
    claude-code-bridge-cli run <task_file> <cwd>
    claude-code-bridge-cli status <job_id>
    claude-code-bridge-cli result <job_id>
    claude-code-bridge-cli ask <job_id> <prompt>
    claude-code-bridge-cli kill <job_id>
    claude-code-bridge-cli list
    claude-code-bridge-cli wait <job_id> [--timeout SECS]
    claude-code-bridge-cli push <cwd> [--remote origin]
"""
from __future__ import annotations

import argparse
import sys

from .runner import BridgeError
from .server import cc_ask, cc_kill, cc_list, cc_push, cc_result, cc_run, cc_status, cc_wait


def _cmd_run(args: argparse.Namespace) -> int:
    print(cc_run(args.task_file, args.cwd))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    print(cc_status(args.job_id))
    return 0


def _cmd_result(args: argparse.Namespace) -> int:
    print(cc_result(args.job_id))
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    print(cc_ask(args.job_id, args.prompt))
    return 0


def _cmd_kill(args: argparse.Namespace) -> int:
    print(cc_kill(args.job_id))
    return 0


def _cmd_list(_: argparse.Namespace) -> int:
    print(cc_list())
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    print(cc_push(args.cwd, args.remote))
    return 0


def _cmd_wait(args: argparse.Namespace) -> int:
    out = cc_wait(args.job_id, timeout_s=args.timeout)
    print(out)
    return 1 if out.startswith("wait: timed out") else 0


def main() -> None:
    p = argparse.ArgumentParser(prog="claude-code-bridge-cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="start a headless CC run on a task file")
    r.add_argument("task_file")
    r.add_argument("cwd")
    r.set_defaults(func=_cmd_run)

    s = sub.add_parser("status", help="poll a job")
    s.add_argument("job_id")
    s.set_defaults(func=_cmd_status)

    x = sub.add_parser("result", help="final output + contract check")
    x.add_argument("job_id")
    x.set_defaults(func=_cmd_result)

    a = sub.add_parser("ask", help="follow-up in the same session")
    a.add_argument("job_id")
    a.add_argument("prompt")
    a.set_defaults(func=_cmd_ask)

    k = sub.add_parser("kill", help="stop a running job on purpose")
    k.add_argument("job_id")
    k.set_defaults(func=_cmd_kill)

    lst = sub.add_parser("list", help="list all jobs")
    lst.set_defaults(func=_cmd_list)

    ph = sub.add_parser("push", help="publish current branch (git push)")
    ph.add_argument("cwd")
    ph.add_argument("--remote", default="origin")
    ph.set_defaults(func=_cmd_push)

    w = sub.add_parser("wait", help="block until finished (uses cc_wait)")
    w.add_argument("job_id")
    w.add_argument("--timeout", type=float, default=300.0)
    w.set_defaults(func=_cmd_wait)

    args = p.parse_args()
    try:
        raise SystemExit(args.func(args))
    except BridgeError as exc:
        print(f"Bridge error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
