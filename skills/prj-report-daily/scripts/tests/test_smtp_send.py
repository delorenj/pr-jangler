#!/usr/bin/env python3
"""Unit tests for smtp_send.py."""

from __future__ import annotations

import smtplib
import sys
import unittest
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import smtp_send  # noqa: E402


class _FakeSMTP:
    """Records starttls, login, sendmail, quit invocations."""

    instances: list = []

    def __init__(self, host, port, timeout=30):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[tuple] = []
        _FakeSMTP.instances.append(self)

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def sendmail(self, sender, recipients, message):
        self.calls.append(("sendmail", sender, tuple(recipients), len(message)))

    def quit(self):
        self.calls.append(("quit",))


class _BoomSMTP(_FakeSMTP):
    def __init__(self, host, port, timeout=30, fail_on="login"):
        super().__init__(host, port, timeout)
        self.fail_on = fail_on

    def starttls(self):
        if self.fail_on == "starttls":
            raise smtplib.SMTPException("starttls failed")
        super().starttls()

    def login(self, user, password):
        if self.fail_on == "login":
            raise smtplib.SMTPAuthenticationError(535, b"bad creds")
        super().login(user, password)

    def sendmail(self, sender, recipients, message):
        if self.fail_on == "sendmail":
            raise smtplib.SMTPRecipientsRefused({"foo@x": (550, b"no")})
        super().sendmail(sender, recipients, message)


class TestBuildMessage(unittest.TestCase):
    def test_multipart_alternative_with_plain_first(self):
        msg = smtp_send.build_message(
            subject="hi", sender="from@x.com", recipient="to@y.com",
            plain_body="plain text", html_body="<p>html</p>",
        )
        self.assertEqual(msg["Subject"], "hi")
        self.assertEqual(msg["From"], "from@x.com")
        self.assertEqual(msg["To"], "to@y.com")
        parts = msg.get_payload()
        self.assertEqual(len(parts), 2)
        # RFC 2046: plain part should come first.
        self.assertEqual(parts[0].get_content_subtype(), "plain")
        self.assertEqual(parts[1].get_content_subtype(), "html")


class TestSendReport(unittest.TestCase):
    def setUp(self):
        _FakeSMTP.instances.clear()

    def test_happy_path_calls_starttls_login_sendmail_quit(self):
        result = smtp_send.send_report(
            host="smtp.example.com", port=587,
            username="u", password="p",
            sender="from@x", recipient="to@y",
            subject="s", plain_body="P", html_body="<p>H</p>",
            smtp_factory=_FakeSMTP,
        )
        self.assertEqual(len(_FakeSMTP.instances), 1)
        smtp = _FakeSMTP.instances[0]
        seq = [c[0] for c in smtp.calls]
        self.assertEqual(seq, ["starttls", "login", "sendmail", "quit"])
        # Login carried both creds.
        login_call = next(c for c in smtp.calls if c[0] == "login")
        self.assertEqual(login_call[1], "u")
        self.assertEqual(login_call[2], "p")
        # sendmail used the right sender and recipient.
        send_call = next(c for c in smtp.calls if c[0] == "sendmail")
        self.assertEqual(send_call[1], "from@x")
        self.assertEqual(send_call[2], ("to@y",))
        self.assertGreater(result["message_bytes"], 0)
        self.assertEqual(result["host"], "smtp.example.com")

    def test_starttls_failure_raises_send_error(self):
        def factory(host, port, timeout=30):
            return _BoomSMTP(host, port, timeout, fail_on="starttls")
        with self.assertRaises(smtp_send.SmtpSendError) as ctx:
            smtp_send.send_report(
                host="s", port=587, username="u", password="p",
                sender="f@x", recipient="t@y", subject="x",
                plain_body="P", html_body="<p>H</p>",
                smtp_factory=factory,
            )
        self.assertIn("STARTTLS", str(ctx.exception))

    def test_login_failure_raises_send_error(self):
        def factory(host, port, timeout=30):
            return _BoomSMTP(host, port, timeout, fail_on="login")
        with self.assertRaises(smtp_send.SmtpSendError) as ctx:
            smtp_send.send_report(
                host="s", port=587, username="u", password="p",
                sender="f@x", recipient="t@y", subject="x",
                plain_body="P", html_body="<p>H</p>",
                smtp_factory=factory,
            )
        self.assertIn("login", str(ctx.exception))

    def test_sendmail_failure_raises_send_error(self):
        def factory(host, port, timeout=30):
            return _BoomSMTP(host, port, timeout, fail_on="sendmail")
        with self.assertRaises(smtp_send.SmtpSendError):
            smtp_send.send_report(
                host="s", port=587, username="u", password="p",
                sender="f@x", recipient="t@y", subject="x",
                plain_body="P", html_body="<p>H</p>",
                smtp_factory=factory,
            )

    def test_connect_failure_raises_send_error(self):
        def factory(host, port, timeout=30):
            raise OSError("no route to host")
        with self.assertRaises(smtp_send.SmtpSendError):
            smtp_send.send_report(
                host="s", port=587, username="u", password="p",
                sender="f@x", recipient="t@y", subject="x",
                plain_body="P", html_body="<p>H</p>",
                smtp_factory=factory,
            )

    def test_invalid_host_raises_send_error(self):
        with self.assertRaises(smtp_send.SmtpSendError):
            smtp_send.send_report(
                host="", port=587, username="u", password="p",
                sender="f@x", recipient="t@y", subject="x",
                plain_body="P", html_body="<p>H</p>",
                smtp_factory=_FakeSMTP,
            )

    def test_invalid_port_raises_send_error(self):
        with self.assertRaises(smtp_send.SmtpSendError):
            smtp_send.send_report(
                host="s", port=0, username="u", password="p",
                sender="f@x", recipient="t@y", subject="x",
                plain_body="P", html_body="<p>H</p>",
                smtp_factory=_FakeSMTP,
            )


if __name__ == "__main__":
    unittest.main()
