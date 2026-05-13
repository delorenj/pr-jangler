#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Structural validator for the adversarial-checklist response.

The validator's LLM step must produce JSON with exactly six findings — one per
checklist item — each carrying `passes: bool` and `finding: str`. This module
enforces that contract before any verdict is persisted.

It does NOT make a verdict judgement of its own. It only certifies that the
response has enough structure that downstream code (`adversarial_io.py`) can
trust the verdict field.

CLI: `python3 checklist_io.py --check '<json>'` prints a validation report and
exits 0 (valid) or 2 (invalid). Importable: `validate_response`,
`ValidationError`, `REQUIRED_ITEMS`.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


# The canonical six items, in canonical order. Order is not enforced on input
# (findings are matched by item name) but is preserved on output.
REQUIRED_ITEMS: tuple[str, ...] = (
    "test_validity",
    "scope",
    "side_effects",
    "regressions",
    "ac_alignment",
    "worst_case_probe",
)

VALID_VERDICTS: frozenset[str] = frozenset({"pass", "reject", "escalate"})


class ValidationError(Exception):
    """Raised when an adversarial response fails structural validation."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _coerce_response(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError([f"response is not valid JSON: {exc.msg}"]) from exc
    if not isinstance(raw, dict):
        raise ValidationError([f"response must be a JSON object, got {type(raw).__name__}"])
    return raw


def validate_response(raw: Any) -> dict[str, Any]:
    """Validate a candidate adversarial response.

    Returns a normalized response: verdict, summary (optional), findings sorted
    in canonical item order, concerns (optional list of strings).

    Raises ValidationError with a complete list of reasons on failure.
    """
    response = _coerce_response(raw)
    reasons: list[str] = []

    # Verdict
    verdict = response.get("verdict")
    if not isinstance(verdict, str) or verdict not in VALID_VERDICTS:
        reasons.append(
            f"`verdict` must be one of {sorted(VALID_VERDICTS)}, got {verdict!r}"
        )

    # Findings: must be a list, must cover all six items exactly once
    findings_raw = response.get("findings")
    if not isinstance(findings_raw, list):
        reasons.append("`findings` must be a list of six checklist entries")
        findings_raw = []

    seen_items: dict[str, dict[str, Any]] = {}
    for i, entry in enumerate(findings_raw):
        if not isinstance(entry, dict):
            reasons.append(f"findings[{i}] must be an object")
            continue
        item = entry.get("item")
        if not isinstance(item, str) or item not in REQUIRED_ITEMS:
            reasons.append(
                f"findings[{i}].item must be one of {list(REQUIRED_ITEMS)}, got {item!r}"
            )
            continue
        if item in seen_items:
            reasons.append(f"findings[{i}].item={item!r} duplicates an earlier finding")
            continue
        passes = entry.get("passes")
        if not isinstance(passes, bool):
            reasons.append(f"findings[{i}].passes must be a bool, got {type(passes).__name__}")
        finding_text = entry.get("finding")
        if not isinstance(finding_text, str) or not finding_text.strip():
            reasons.append(f"findings[{i}].finding must be a non-empty string")
        seen_items[item] = {
            "item": item,
            "passes": bool(passes) if isinstance(passes, bool) else False,
            "finding": finding_text.strip() if isinstance(finding_text, str) else "",
        }

    missing = [item for item in REQUIRED_ITEMS if item not in seen_items]
    if missing:
        reasons.append(f"missing required checklist items: {missing}")

    # Concerns (optional). If present, must be list[str].
    concerns_raw = response.get("concerns", [])
    if concerns_raw is None:
        concerns: list[str] = []
    elif isinstance(concerns_raw, list):
        concerns = []
        for i, c in enumerate(concerns_raw):
            if not isinstance(c, str):
                reasons.append(f"concerns[{i}] must be a string, got {type(c).__name__}")
            else:
                stripped = c.strip()
                if stripped:
                    concerns.append(stripped)
    else:
        reasons.append("`concerns` must be a list of strings if provided")
        concerns = []

    summary = response.get("summary", "")
    if summary is None:
        summary = ""
    if not isinstance(summary, str):
        reasons.append("`summary` must be a string if provided")
        summary = ""

    if reasons:
        raise ValidationError(reasons)

    ordered_findings = [seen_items[item] for item in REQUIRED_ITEMS]

    # Cross-check: if any item failed, verdict cannot be `pass`.
    any_failed = any(not f["passes"] for f in ordered_findings)
    if verdict == "pass" and any_failed:
        raise ValidationError(
            [
                "verdict=pass is inconsistent with at least one finding having passes=false; "
                "the response failed structural cross-check",
            ]
        )

    return {
        "verdict": verdict,
        "summary": summary.strip(),
        "findings": ordered_findings,
        "concerns": concerns,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        help="JSON string to validate. If omitted, read JSON from stdin.",
        default=None,
    )
    args = parser.parse_args()

    raw = args.check if args.check is not None else sys.stdin.read()
    try:
        normalized = validate_response(raw)
    except ValidationError as exc:
        print(
            json.dumps(
                {"status": "invalid", "reasons": exc.reasons},
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(json.dumps({"status": "ok", "response": normalized}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
