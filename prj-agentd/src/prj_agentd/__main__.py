"""prj-agentd CLI entrypoint.

Usage:
    prj-agentd once [--offline] [--project-root PATH] [--socket PATH]
    prj-agentd run [--offline] [--project-root PATH] [--socket PATH]
    prj-agentd status [--project-root PATH]
    prj-agentd approve <approval_id> [--decision approve|deny] [--reason TEXT]

Modes:
    once      Run exactly one tick, print TickResult JSON, exit.
    run       Loop ticks at config.tick_seconds until SIGINT.
    status    Print pending approvals and recent runs.
    approve   Record a human decision on a pending approval.

`--offline` swaps in the in-process OfflineAppServer. Use it for smoke tests
and Milestone-A cache-only runs without a live `codex app-server` socket.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import load_config
from .daemon import build_daemon
from .store import AgentdStore


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument("--socket", help="Override app-server socket path", default=None)
    parser.add_argument("--offline", action="store_true",
                        help="Use in-process OfflineAppServer (no codex app-server required)")


async def _cmd_once(args: argparse.Namespace) -> int:
    config = load_config(
        project_root=Path(args.project_root) if args.project_root else None,
        offline=args.offline,
        override_socket=Path(args.socket) if args.socket else None,
    )
    daemon = await build_daemon(config)
    try:
        result = await daemon.run_once()
        print(json.dumps(result.to_dict(), sort_keys=True, indent=2))
        return 0
    finally:
        await daemon.appserver.close()


async def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(
        project_root=Path(args.project_root) if args.project_root else None,
        offline=args.offline,
        override_socket=Path(args.socket) if args.socket else None,
    )
    daemon = await build_daemon(config)
    try:
        await daemon.run_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        await daemon.appserver.close()


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(project_root=Path(args.project_root) if args.project_root else None)
    store = AgentdStore(config.store_path)
    pending = store.pending_approvals()
    print(json.dumps({
        "project_root": str(config.project_root),
        "prj_repo": config.prj_repo,
        "pending_approvals": pending,
        "approval_count": len(pending),
    }, sort_keys=True, indent=2))
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    config = load_config(project_root=Path(args.project_root) if args.project_root else None)
    store = AgentdStore(config.store_path)
    store.record_approval_decision(
        approval_id=args.approval_id,
        decision=args.decision,
        reason=args.reason or "human decision",
        decided_by="cli",
    )
    print(json.dumps({"approval_id": args.approval_id, "decision": args.decision}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prj-agentd", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_once = sub.add_parser("once", help="Run exactly one tick")
    _add_common_args(p_once)

    p_run = sub.add_parser("run", help="Run the daemon loop until SIGINT")
    _add_common_args(p_run)

    p_status = sub.add_parser("status", help="Print pending approvals + summary")
    p_status.add_argument("--project-root", default=None)

    p_appr = sub.add_parser("approve", help="Record a human decision for a pending approval")
    p_appr.add_argument("approval_id")
    p_appr.add_argument("--decision", choices=("approve", "deny"), default="approve")
    p_appr.add_argument("--reason", default=None)
    p_appr.add_argument("--project-root", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "once":
        return asyncio.run(_cmd_once(args))
    if args.cmd == "run":
        return asyncio.run(_cmd_run(args))
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "approve":
        return _cmd_approve(args)
    parser.error(f"unknown cmd: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
