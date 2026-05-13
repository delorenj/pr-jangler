#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Credential resolution for prj-implement-fix.

Reads a 1Password reference via `op read` and returns the secret as a string.
Never writes the secret to disk. Callers pass the resolved value to subprocess
env for the duration of the call, then let it fall out of scope.

NOTE: duplicated for v1; consolidate later with prj-report-daily/creds.py once
both skills land and we know the common surface.

CLI: `python3 creds.py resolve --ref op://Vault/Item/Field`.

Importable: resolve_reference, CredentialError.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys


class CredentialError(RuntimeError):
    """Raised when a credential cannot be resolved (bad ref, op missing, op failed)."""


def _resolve_via_op(ref: str, timeout: int = 30) -> str:
    """Invoke `op read <ref>` and return the trimmed stdout.

    Single subprocess seam so tests can patch via patch.object(creds, "_resolve_via_op").
    """
    binary = shutil.which("op") or "op"
    cmd = [binary, "read", "--no-newline", ref]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CredentialError(f"op CLI not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CredentialError(f"op read timed out after {timeout}s: {ref}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise CredentialError(f"op read exited {proc.returncode}: {tail}")
    value = proc.stdout.strip()
    if not value:
        raise CredentialError(f"op read returned empty value for {ref}")
    return value


def resolve_reference(ref: str) -> str:
    """Resolve a single 1Password reference. Raises CredentialError on bad inputs.

    Accepts `op://...` or `env://VAR_NAME` (for tests that want to bypass op).
    """
    if not isinstance(ref, str) or not ref:
        raise CredentialError("credential reference must be a non-empty string")
    if ref.startswith("env://"):
        import os

        var = ref[len("env://"):]
        value = os.environ.get(var)
        if not value:
            raise CredentialError(f"environment variable {var!r} is unset or empty")
        return value
    if not ref.startswith("op://"):
        raise CredentialError(
            f"unsupported credential scheme in {ref!r}; expected op:// or env://"
        )
    return _resolve_via_op(ref)


# ---------- CLI surface ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_resolve = sub.add_parser("resolve", help="Resolve a credential reference")
    p_resolve.add_argument("--ref", required=True, help="op:// or env:// reference")
    args = parser.parse_args()

    try:
        value = resolve_reference(args.ref)
    except CredentialError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1

    # By design, we do NOT echo the resolved secret on stdout. Emit length + a
    # confirmation so callers can verify resolution without leaking the value.
    print(json.dumps({"status": "ok", "length": len(value), "ref": args.ref}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
