#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Check PR Jangler's external CLI dependencies.

Reports presence + version for: gh, op, git. For gh and op, also checks
authentication status. Returns structured JSON; never raises.

Exit code is always 0 (warnings only). Consumers parse the JSON to decide
whether to surface issues to the user.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from typing import Any


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _run(args: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


def check_gh(expected_user: str | None) -> dict[str, Any]:
    info: dict[str, Any] = {"name": "gh", "path": _which("gh")}
    if not info["path"]:
        info["status"] = "missing"
        return info
    code, out, _ = _run(["gh", "--version"])
    info["version"] = out.splitlines()[0] if out else "unknown"
    code, out, err = _run(["gh", "auth", "status"])
    info["authenticated"] = code == 0
    info["auth_detail"] = (out or err).splitlines()[:6]
    if expected_user and info["authenticated"]:
        info["expected_user"] = expected_user
        info["matches_expected_user"] = expected_user.lower() in (out + err).lower()
    info["status"] = "ok" if info["authenticated"] else "unauthenticated"
    return info


def check_op() -> dict[str, Any]:
    info: dict[str, Any] = {"name": "op", "path": _which("op")}
    if not info["path"]:
        info["status"] = "missing"
        return info
    code, out, _ = _run(["op", "--version"])
    info["version"] = out
    code, _, err = _run(["op", "whoami"])
    info["signed_in"] = code == 0
    info["status"] = "ok" if info["signed_in"] else "not-signed-in"
    return info


def check_git() -> dict[str, Any]:
    info: dict[str, Any] = {"name": "git", "path": _which("git")}
    if not info["path"]:
        info["status"] = "missing"
        return info
    code, out, _ = _run(["git", "--version"])
    info["version"] = out
    info["status"] = "ok"
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-bot-user", help="Compare gh auth status against this login", default=None)
    args = parser.parse_args()

    checks = [
        check_gh(args.expected_bot_user),
        check_op(),
        check_git(),
    ]
    summary = {
        "checks": checks,
        "ok": all(c.get("status") == "ok" for c in checks),
        "missing": [c["name"] for c in checks if c.get("status") == "missing"],
        "unauthenticated": [c["name"] for c in checks if c.get("status") in {"unauthenticated", "not-signed-in"}],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
