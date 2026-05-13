#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Resolve SMTP credentials from a 1Password reference via `op read`.

A `prj_smtp_creds_ref` like `op://Vault/item` is appended with `/username`
and `/password` fields. Each is fetched via a separate `op read` subprocess
call. Failures raise `CredsError` with a clear message; the caller logs.

Importable: CredsError, SmtpCreds, resolve_smtp_creds, _run_op.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass


class CredsError(RuntimeError):
    """Raised when `op` is missing, fails, or returns empty output."""


@dataclass(frozen=True)
class SmtpCreds:
    username: str
    password: str


def _run_op(ref: str, timeout: int = 15) -> str:
    """Run `op read <ref>` and return stripped stdout. Raises CredsError on failure."""
    try:
        result = subprocess.run(
            ["op", "read", ref],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CredsError(
            "1Password CLI `op` not found on PATH. Install it: https://developer.1password.com/docs/cli/"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CredsError(f"`op read {ref}` timed out after {timeout}s") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise CredsError(
            f"`op read {ref}` exited {result.returncode}: {stderr or 'no stderr'}"
        )
    value = (result.stdout or "").strip()
    if not value:
        raise CredsError(f"`op read {ref}` returned empty value")
    return value


def resolve_smtp_creds(
    base_ref: str,
    *,
    runner=None,
) -> SmtpCreds:
    """Resolve SMTP user/pass from `op://...`-style base reference.

    `runner` is the subprocess wrapper; tests inject a fake to avoid `op`.
    When None, looks up the current module-level `_run_op` so that
    `patch.object(creds, "_run_op", ...)` intercepts correctly.
    Appends `/username` and `/password` fields to the base ref.
    """
    if not base_ref or not isinstance(base_ref, str):
        raise CredsError("prj_smtp_creds_ref is empty or not a string")
    if runner is None:
        # Late binding so patching this module's _run_op takes effect.
        runner = globals()["_run_op"]
    base = base_ref.rstrip("/")
    user_ref = f"{base}/username"
    pass_ref = f"{base}/password"
    username = runner(user_ref)
    password = runner(pass_ref)
    return SmtpCreds(username=username, password=password)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True, help="1Password base reference (op://...)")
    args = parser.parse_args()
    try:
        creds = resolve_smtp_creds(args.ref)
    except CredsError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 4
    # NEVER log the password. Only confirm shape.
    print(json.dumps({
        "status": "ok",
        "username_present": bool(creds.username),
        "password_present": bool(creds.password),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
