#!/usr/bin/env python3
"""Unit tests for render.py."""

from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import render  # noqa: E402


def _aggregate(needs=None, in_prog=None, resolved=None, no_change=None,
               shortlist_overflow=0, all_quiet=False):
    needs = needs or []
    in_prog = in_prog or []
    resolved = resolved or []
    no_change = no_change or []
    return {
        "groups": {
            "needs_attention": needs,
            "in_progress": in_prog,
            "resolved_today": resolved,
            "no_change": no_change,
        },
        "shortlist": {"items": needs[:5], "overflow": shortlist_overflow},
        "all_quiet": all_quiet,
        "enrichment": {},
    }


PR = {
    "pr_number": 42,
    "phase": "PleaseAdvise",
    "phase_entered_at": "2026-05-10T10:00:00+00:00",
    "last_action_at": "2026-05-11T08:00:00+00:00",
    "contributor_login": "alice",
    "title": "Fix off-by-one in cache",
}


class TestRenderSubject(unittest.TestCase):
    def test_all_quiet_subject(self):
        agg = _aggregate(all_quiet=True)
        now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
        subject = render.render_subject(agg, "owner/name", now)
        self.assertIn("all quiet", subject)
        self.assertIn("2026-05-11", subject)
        self.assertTrue(subject.startswith("[PR Jangler]"))

    def test_active_subject_counts(self):
        agg = _aggregate(needs=[PR, PR], in_prog=[PR])
        now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
        subject = render.render_subject(agg, "owner/name", now)
        self.assertIn("2 need attention", subject)
        self.assertIn("1 in progress", subject)


class TestRenderHTML(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 11, 12, 30, tzinfo=timezone.utc)

    def test_no_unresolved_placeholders_active(self):
        agg = _aggregate(needs=[PR], in_prog=[dict(PR, pr_number=43, phase="ReviewPending")])
        out = render.render_html(agg, "octocat/x", self.now, "/tmp/archive.md")
        # No leftover $var or ${var} placeholders.
        leftover = re.findall(r"(?<!\$)\$(?:\w+|\{\w+\})", out)
        self.assertEqual(leftover, [])
        self.assertIn("<html", out.lower())
        self.assertIn("Needs your eyes", out)

    def test_no_unresolved_placeholders_all_quiet(self):
        agg = _aggregate(all_quiet=True)
        out = render.render_html(agg, "octocat/x", self.now, "/tmp/archive.md")
        leftover = re.findall(r"(?<!\$)\$(?:\w+|\{\w+\})", out)
        self.assertEqual(leftover, [])
        # No shortlist block when there's nothing to advise on.
        self.assertNotIn("Needs your eyes", out)

    def test_pr_link_embedded(self):
        agg = _aggregate(needs=[PR])
        out = render.render_html(agg, "octocat/repo", self.now, "/x")
        self.assertIn("github.com/octocat/repo/pull/42", out)

    def test_overflow_note_present(self):
        prs = [dict(PR, pr_number=n) for n in range(100, 108)]
        agg = _aggregate(needs=prs, shortlist_overflow=3)
        out = render.render_html(agg, "octocat/x", self.now, "/tmp")
        self.assertIn("+ 3 more", out)

    def test_html_escaping_of_title(self):
        bad = dict(PR, title="<script>x</script> & co")
        agg = _aggregate(needs=[bad])
        out = render.render_html(agg, "octocat/x", self.now, "/tmp")
        self.assertNotIn("<script>x</script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_render_error_on_unresolved_after_substitution(self):
        # Inject a placeholder the renderer doesn't know about.
        broken = "<html>$mystery_field</html>"
        agg = _aggregate(all_quiet=True)
        with self.assertRaises(render.RenderError):
            render.render_html(agg, "x/y", self.now, "/tmp", template_text=broken)


class TestRenderPlain(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 11, 12, 30, tzinfo=timezone.utc)

    def test_plain_has_no_html_tags(self):
        agg = _aggregate(needs=[PR], in_prog=[dict(PR, pr_number=43, phase="ReviewPending")])
        out = render.render_plain(agg, "octocat/x", self.now, "/tmp")
        # No HTML tag-like substring (any `<` followed by a word char would be a tag).
        import re as _re
        self.assertIsNone(_re.search(r"<\w", out))
        self.assertNotIn("</", out)
        self.assertIn("#42", out)
        self.assertIn("NEEDS YOUR EYES", out)

    def test_plain_covers_same_info_as_html(self):
        agg = _aggregate(needs=[PR], in_prog=[dict(PR, pr_number=43, phase="ReviewPending")])
        out = render.render_plain(agg, "octocat/x", self.now, "/tmp")
        # Same PRs covered
        self.assertIn("#42", out)
        self.assertIn("#43", out)
        # Same headings/sections
        self.assertIn("Needs attention", out)
        self.assertIn("In progress", out)

    def test_all_quiet_plain(self):
        agg = _aggregate(all_quiet=True)
        out = render.render_plain(agg, "owner/r", self.now, "/tmp")
        self.assertIn("All quiet", out)


class TestRenderReport(unittest.TestCase):
    def test_returns_subject_html_plain(self):
        now = datetime(2026, 5, 11, 12, 30, tzinfo=timezone.utc)
        agg = _aggregate(needs=[PR])
        result = render.render_report(agg, "octocat/x", now, "/tmp")
        self.assertIn("subject", result)
        self.assertIn("html", result)
        self.assertIn("plain", result)
        self.assertTrue(result["subject"].startswith("[PR Jangler]"))


if __name__ == "__main__":
    unittest.main()
