#!/usr/bin/env python3
"""Unit tests for checklist_io.validate_response."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import checklist_io  # noqa: E402


def _all_pass_findings() -> list[dict]:
    return [
        {"item": item, "passes": True, "finding": f"{item} ok per evidence."}
        for item in checklist_io.REQUIRED_ITEMS
    ]


def _mixed_findings(failed_item: str) -> list[dict]:
    out: list[dict] = []
    for item in checklist_io.REQUIRED_ITEMS:
        out.append({
            "item": item,
            "passes": item != failed_item,
            "finding": f"{item} ok" if item != failed_item else f"{item} fails because reasons.",
        })
    return out


class TestValidateResponse(unittest.TestCase):
    def test_valid_pass_response(self):
        raw = {"verdict": "pass", "summary": "looks good", "findings": _all_pass_findings()}
        out = checklist_io.validate_response(raw)
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(len(out["findings"]), 6)
        items = [f["item"] for f in out["findings"]]
        self.assertEqual(items, list(checklist_io.REQUIRED_ITEMS))

    def test_valid_reject_response_with_concerns(self):
        raw = {
            "verdict": "reject",
            "summary": "scope creep",
            "findings": _mixed_findings("scope"),
            "concerns": ["touches unrelated files", " "],
        }
        out = checklist_io.validate_response(raw)
        self.assertEqual(out["verdict"], "reject")
        self.assertEqual(out["concerns"], ["touches unrelated files"])

    def test_missing_items_raises(self):
        raw = {
            "verdict": "pass",
            "findings": [
                {"item": "scope", "passes": True, "finding": "x"},
            ],
        }
        with self.assertRaises(checklist_io.ValidationError) as ctx:
            checklist_io.validate_response(raw)
        self.assertTrue(
            any("missing required checklist items" in r for r in ctx.exception.reasons)
        )

    def test_invalid_verdict_raises(self):
        raw = {"verdict": "maybe", "findings": _all_pass_findings()}
        with self.assertRaises(checklist_io.ValidationError):
            checklist_io.validate_response(raw)

    def test_unknown_item_in_findings(self):
        findings = _all_pass_findings()
        findings.append({"item": "made_up", "passes": True, "finding": "x"})
        raw = {"verdict": "pass", "findings": findings}
        with self.assertRaises(checklist_io.ValidationError) as ctx:
            checklist_io.validate_response(raw)
        self.assertTrue(any("findings[6].item" in r for r in ctx.exception.reasons))

    def test_duplicate_items_rejected(self):
        findings = _all_pass_findings()
        findings.append({"item": "scope", "passes": True, "finding": "again"})
        raw = {"verdict": "pass", "findings": findings}
        with self.assertRaises(checklist_io.ValidationError) as ctx:
            checklist_io.validate_response(raw)
        self.assertTrue(any("duplicate" in r for r in ctx.exception.reasons))

    def test_passes_must_be_bool(self):
        findings = _all_pass_findings()
        findings[0]["passes"] = "yes"
        raw = {"verdict": "pass", "findings": findings}
        with self.assertRaises(checklist_io.ValidationError) as ctx:
            checklist_io.validate_response(raw)
        self.assertTrue(any("passes must be a bool" in r for r in ctx.exception.reasons))

    def test_finding_must_be_nonempty(self):
        findings = _all_pass_findings()
        findings[0]["finding"] = "   "
        raw = {"verdict": "pass", "findings": findings}
        with self.assertRaises(checklist_io.ValidationError) as ctx:
            checklist_io.validate_response(raw)
        self.assertTrue(any("non-empty string" in r for r in ctx.exception.reasons))

    def test_verdict_pass_inconsistent_with_failed_item(self):
        raw = {"verdict": "pass", "findings": _mixed_findings("scope")}
        with self.assertRaises(checklist_io.ValidationError) as ctx:
            checklist_io.validate_response(raw)
        self.assertTrue(
            any("inconsistent" in r for r in ctx.exception.reasons)
        )

    def test_json_string_input_accepted(self):
        import json as _j
        raw = _j.dumps({"verdict": "pass", "findings": _all_pass_findings()})
        out = checklist_io.validate_response(raw)
        self.assertEqual(out["verdict"], "pass")

    def test_non_object_input_rejected(self):
        with self.assertRaises(checklist_io.ValidationError):
            checklist_io.validate_response(["not", "an", "object"])


if __name__ == "__main__":
    unittest.main()
