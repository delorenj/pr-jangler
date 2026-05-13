#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Send the daily report through SMTP with STARTTLS.

Builds a `MIMEMultipart('alternative')` with plain + HTML parts, opens a
single `smtplib.SMTP` connection, runs `starttls` then `login`, calls
`sendmail`, and `quit`s. SMTP failures bubble up as `SmtpSendError` so the
orchestration layer can advance the retry counter.

Importable: SmtpSendError, build_message, send_report.
"""

from __future__ import annotations

import argparse
import json
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any


class SmtpSendError(RuntimeError):
    """Raised when SMTP transport fails (connect, STARTTLS, login, send)."""


def build_message(
    *,
    subject: str,
    sender: str,
    recipient: str,
    plain_body: str,
    html_body: str,
) -> MIMEMultipart:
    """Construct a multipart/alternative message. RFC 2046 recommends the
    plain-text part appear FIRST so older clients fall back gracefully."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def send_report(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    sender: str,
    recipient: str,
    subject: str,
    plain_body: str,
    html_body: str,
    timeout: int = 30,
    smtp_factory: Any = None,
) -> dict[str, Any]:
    """Send the report. `smtp_factory` is injectable for tests.

    When `smtp_factory` is None, looks up `smtplib.SMTP` at call time so
    test patches against `smtplib.SMTP` (or this module's `smtplib`) work.
    Returns a dict summarizing the send (no secrets). Raises `SmtpSendError`
    on any transport failure.
    """
    if smtp_factory is None:
        smtp_factory = smtplib.SMTP
    if not host:
        raise SmtpSendError("prj_smtp_host is empty")
    if not isinstance(port, int) or port <= 0:
        raise SmtpSendError(f"prj_smtp_port is invalid: {port!r}")
    if not recipient:
        raise SmtpSendError("prj_email_to is empty")
    if not sender:
        raise SmtpSendError("prj_email_from is empty")

    message = build_message(
        subject=subject,
        sender=sender,
        recipient=recipient,
        plain_body=plain_body,
        html_body=html_body,
    )
    raw = message.as_string()

    try:
        smtp = smtp_factory(host, port, timeout=timeout)
    except Exception as exc:
        raise SmtpSendError(f"SMTP connect to {host}:{port} failed: {exc}") from exc

    try:
        try:
            smtp.starttls()
        except Exception as exc:
            raise SmtpSendError(f"STARTTLS failed against {host}:{port}: {exc}") from exc
        try:
            smtp.login(username, password)
        except Exception as exc:
            raise SmtpSendError(f"SMTP login as {username} failed: {exc}") from exc
        try:
            smtp.sendmail(sender, [recipient], raw)
        except Exception as exc:
            raise SmtpSendError(f"SMTP sendmail failed: {exc}") from exc
    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    return {
        "host": host,
        "port": port,
        "sender": sender,
        "recipient": recipient,
        "subject": subject,
        "message_bytes": len(raw),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--sender", required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--plain", required=True, help="Path to plain-text body file")
    parser.add_argument("--html", required=True, help="Path to HTML body file")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True, help="Plaintext password (testing only)")
    args = parser.parse_args()

    with open(args.plain, encoding="utf-8") as fh:
        plain_body = fh.read()
    with open(args.html, encoding="utf-8") as fh:
        html_body = fh.read()

    try:
        result = send_report(
            host=args.host,
            port=args.port,
            username=args.username,
            password=args.password,
            sender=args.sender,
            recipient=args.recipient,
            subject=args.subject,
            plain_body=plain_body,
            html_body=html_body,
        )
    except SmtpSendError as exc:
        print(json.dumps({"status": "smtp-error", "error": str(exc)}), file=sys.stderr)
        return 5
    print(json.dumps({"status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
