#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Send a one-time "PR Jangler online" test email.

Reads SMTP config from `[modules.prj]` in the project's `_bmad/config.toml`.
Resolves the SMTP creds via `op read` if `prj_smtp_creds_ref` looks like a
1Password reference (`op://...`); otherwise expects `prj_smtp_user` and
`prj_smtp_password` to be set inline (not recommended for production).

Reports JSON to stdout. Exit code 0 on success, 1 on send failure, 2 on
config error.
"""

from __future__ import annotations

import argparse
import json
import smtplib
import ssl
import subprocess
import sys
import tomllib
from email.message import EmailMessage
from pathlib import Path
from typing import Any


def _load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"config not found: {config_path}")
    with config_path.open("rb") as fh:
        data = tomllib.load(fh)
    return data.get("modules", {}).get("prj", {}) or {}


def _op_read(ref: str, field: str) -> str:
    """Resolve op://vault/item/field. The ref passed in is the item-level base."""
    full_ref = f"{ref.rstrip('/')}/{field}"
    result = subprocess.run(["op", "read", full_ref], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"op read {full_ref} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-path", required=True, help="Path to _bmad/config.toml")
    args = parser.parse_args()

    try:
        cfg = _load_config(Path(args.config_path))
    except (FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        print(json.dumps({"status": "config-error", "error": str(exc)}), file=sys.stderr)
        return 2

    required = ["prj_email_to", "prj_email_from", "prj_smtp_host", "prj_smtp_port"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        print(json.dumps({"status": "config-error", "missing": missing}), file=sys.stderr)
        return 2

    creds_ref = cfg.get("prj_smtp_creds_ref", "")
    try:
        if creds_ref.startswith("op://"):
            user = _op_read(creds_ref, "username")
            password = _op_read(creds_ref, "password")
        else:
            user = cfg.get("prj_smtp_user") or ""
            password = cfg.get("prj_smtp_password") or ""
            if not user or not password:
                raise RuntimeError("no SMTP user/pass; set prj_smtp_creds_ref to an op:// reference or inline prj_smtp_user/prj_smtp_password")
    except Exception as exc:
        print(json.dumps({"status": "creds-error", "error": str(exc)}), file=sys.stderr)
        return 2

    msg = EmailMessage()
    msg["Subject"] = "[PR Jangler] online"
    msg["From"] = cfg["prj_email_from"]
    msg["To"] = cfg["prj_email_to"]
    msg.set_content(
        "PR Jangler setup test email.\n\n"
        f"Repo: {cfg.get('prj_repo','(not set)')}\n"
        f"Bot user: {cfg.get('prj_bot_user','(not set)')}\n"
        "If you received this, the SMTP path is working.\n"
    )

    try:
        with smtplib.SMTP(cfg["prj_smtp_host"], int(cfg["prj_smtp_port"]), timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as exc:
        print(json.dumps({"status": "send-failed", "error": str(exc), "host": cfg["prj_smtp_host"]}))
        return 1

    print(json.dumps({
        "status": "sent",
        "to": cfg["prj_email_to"],
        "from": cfg["prj_email_from"],
        "host": cfg["prj_smtp_host"],
        "port": int(cfg["prj_smtp_port"]),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
