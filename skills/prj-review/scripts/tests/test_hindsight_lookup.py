#!/usr/bin/env python3
"""Unit tests for hindsight_lookup.py.

Best-effort semantics: every failure mode returns an empty list + warning.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hindsight_lookup  # noqa: E402


class TestLookupConventions(unittest.TestCase):
    def test_missing_binary_returns_unavailable(self):
        with patch.object(
            hindsight_lookup, "_run_hindsight",
            return_value=(-1, "", "hindsight CLI not found on PATH"),
        ):
            result = hindsight_lookup.lookup_conventions("style guide", bank="prj")
        self.assertFalse(result.available)
        self.assertEqual(result.excerpts, [])
        self.assertIn("not found", result.warning)

    def test_timeout_returns_unavailable(self):
        with patch.object(
            hindsight_lookup, "_run_hindsight",
            return_value=(-2, "", "hindsight timed out after 30s"),
        ):
            result = hindsight_lookup.lookup_conventions("q")
        self.assertFalse(result.available)
        self.assertIn("timed out", result.warning)

    def test_non_zero_exit_returns_unavailable(self):
        with patch.object(
            hindsight_lookup, "_run_hindsight",
            return_value=(5, "", "auth error"),
        ):
            result = hindsight_lookup.lookup_conventions("q")
        self.assertFalse(result.available)
        self.assertIn("exited 5", result.warning)
        self.assertIn("auth error", result.warning)

    def test_empty_query_skips(self):
        result = hindsight_lookup.lookup_conventions("   ", bank="prj")
        self.assertFalse(result.available)
        self.assertEqual(result.excerpts, [])

    def test_parses_json_list_of_strings(self):
        payload = json.dumps(["NO any types", "prefer Bun"])
        with patch.object(hindsight_lookup, "_run_hindsight", return_value=(0, payload, "")):
            result = hindsight_lookup.lookup_conventions("style")
        self.assertTrue(result.available)
        self.assertEqual(result.excerpts, ["NO any types", "prefer Bun"])

    def test_parses_json_dict_with_results(self):
        payload = json.dumps({"results": [{"content": "use .js extensions in imports"}]})
        with patch.object(hindsight_lookup, "_run_hindsight", return_value=(0, payload, "")):
            result = hindsight_lookup.lookup_conventions("imports")
        self.assertEqual(result.excerpts, ["use .js extensions in imports"])

    def test_parses_plain_text_fallback(self):
        with patch.object(hindsight_lookup, "_run_hindsight", return_value=(0, "line one\nline two\n", "")):
            result = hindsight_lookup.lookup_conventions("notes")
        self.assertEqual(result.excerpts, ["line one", "line two"])

    def test_to_dict_round_trips(self):
        result = hindsight_lookup.HindsightLookupResult(
            bank="prj", query="x", excerpts=["a"], available=True,
        )
        out = result.to_dict()
        self.assertEqual(out["bank"], "prj")
        self.assertEqual(out["excerpts"], ["a"])


if __name__ == "__main__":
    unittest.main()
