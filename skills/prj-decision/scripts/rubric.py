#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic decision rubric for prj-decision.

Input: aggregate.Signals (a typed bundle of per-PR cache signals).
Output: RubricResult with decision class, confidence band, gate explanations.

Three classes are evaluated:
  1. close-as-not-now (CONSERVATIVE triple gate — all three must hold)
  2. ready-to-merge   (four structural gates — all must hold)
  3. request-changes  (default fallback when neither of the above qualifies)

Returns a structured object with reasoning hints the comment template renders.

Importable: evaluate, RubricResult, CLOSE_AS_NOT_NOW_DAYS,
            DECISION_CLASSES, CONFIDENCE_THRESHOLD_LOW.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Sibling-import aggregate so this module can be exercised standalone.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from aggregate import Signals  # noqa: E402

CLOSE_AS_NOT_NOW_DAYS = 30
CONFIDENCE_THRESHOLD_LOW = "low"
DECISION_CLASSES = ("ready-to-merge", "request-changes", "close-as-not-now")


@dataclass
class RubricResult:
    decision: str
    confidence: str   # high | medium | low
    gates_passed: list[str] = field(default_factory=list)
    gates_failed: list[str] = field(default_factory=list)
    reasoning_hints: list[str] = field(default_factory=list)
    ambiguity_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- gate helpers ----------

def _close_as_not_now_gates(s: Signals) -> tuple[bool, list[str], list[str]]:
    """Triple gate for close-as-not-now.

    Returns (qualifies, gates_passed, gates_failed).
    """
    passed: list[str] = []
    failed: list[str] = []

    # Gate (a): no maintainer activity in >30 days
    if s.days_since_maintainer is not None and s.days_since_maintainer > CLOSE_AS_NOT_NOW_DAYS:
        passed.append(
            f"maintainer-inactive>{CLOSE_AS_NOT_NOW_DAYS}d "
            f"(days_since_maintainer={s.days_since_maintainer})"
        )
    else:
        failed.append(
            f"maintainer-inactive>{CLOSE_AS_NOT_NOW_DAYS}d "
            f"(days_since_maintainer={s.days_since_maintainer})"
        )

    # Gate (b): one of definite-no / overlap-redundant / ≥2 adversarial escalations
    one_of_passed = False
    if s.triage_class == "definite-no":
        one_of_passed = True
        passed.append("triage:definite-no")
    elif s.overlap_verdict == "redundant":
        one_of_passed = True
        passed.append("overlap:redundant")
    elif s.adversarial_escalation_count >= 2:
        one_of_passed = True
        passed.append(f"adversarial-escalations>=2 (count={s.adversarial_escalation_count})")
    else:
        failed.append(
            "one-of(definite-no | overlap-redundant | >=2 adversarial escalations)"
        )

    # Gate (c): some review evidence exists
    has_evidence = s.has_review or s.triage_class is not None
    if has_evidence:
        passed.append("review-evidence-present")
    else:
        failed.append("review-evidence-present")

    qualifies = (
        s.days_since_maintainer is not None
        and s.days_since_maintainer > CLOSE_AS_NOT_NOW_DAYS
        and one_of_passed
        and has_evidence
    )
    return qualifies, passed, failed


def _ready_to_merge_gates(s: Signals) -> tuple[bool, list[str], list[str]]:
    passed: list[str] = []
    failed: list[str] = []

    # Gate 1: review findings resolved (no blockers, or implementation present)
    blockers_clean = s.blocker_count == 0 or (s.has_implementation and s.tests_status == "pass")
    if blockers_clean:
        passed.append(f"blockers-resolved (blocker_count={s.blocker_count})")
    else:
        failed.append(f"blockers-resolved (blocker_count={s.blocker_count})")

    # Gate 2: tests pass with no regressions (if implementation exists)
    if s.has_implementation:
        if s.tests_status == "pass" and s.regressions == "none":
            passed.append("implementation-tests-clean")
        else:
            failed.append(
                f"implementation-tests-clean (tests={s.tests_status}, "
                f"regressions={s.regressions})"
            )
        tests_clean = s.tests_status == "pass" and s.regressions == "none"
    else:
        # No fix was needed; treat as clean.
        passed.append("no-implementation-needed")
        tests_clean = True

    # Gate 3: no overlap conflicts
    overlap_ok = s.overlap_verdict in (None, "independent", "complementary")
    if overlap_ok:
        passed.append(f"overlap-ok (verdict={s.overlap_verdict})")
    else:
        failed.append(f"overlap-ok (verdict={s.overlap_verdict})")

    # Gate 4: adversarial verdict clean (if present)
    adv_ok = s.adversarial_verdict in (None, "pass")
    if adv_ok:
        passed.append(f"adversarial-ok (verdict={s.adversarial_verdict})")
    else:
        failed.append(f"adversarial-ok (verdict={s.adversarial_verdict})")

    qualifies = blockers_clean and tests_clean and overlap_ok and adv_ok
    return qualifies, passed, failed


def _request_changes_signals(s: Signals) -> list[str]:
    """Return non-empty list when there is an actionable signal pointing to
    request-changes. Used for confidence scoring; absence of any signal lowers
    confidence (the rubric still returns request-changes as the fallback).
    """
    out: list[str] = []
    if s.blocker_count > 0:
        out.append(f"blocker-findings (count={s.blocker_count})")
    if s.major_count > 0:
        out.append(f"major-findings (count={s.major_count})")
    if s.adversarial_verdict == "reject":
        out.append("adversarial-reject")
    if s.verification_verdict == "verified" and not s.has_fix_plan:
        out.append("verified-claim-no-fix-plan")
    if s.has_implementation and (s.tests_status == "fail" or s.regressions == "present"):
        out.append("implementation-tests-failing")
    if s.overlap_verdict == "conflicting":
        out.append("overlap-conflicting")
    return out


# ---------- main evaluator ----------

def evaluate(signals: Signals) -> RubricResult:
    """Apply the rubric. Order matters: close-as-not-now > ready-to-merge >
    request-changes. The first class whose gates hold wins.
    """
    # 1. close-as-not-now (conservative triple gate)
    close_ok, close_passed, close_failed = _close_as_not_now_gates(signals)
    if close_ok:
        return RubricResult(
            decision="close-as-not-now",
            confidence="high",
            gates_passed=close_passed,
            gates_failed=[],
            reasoning_hints=[
                "Triple gate satisfied: maintainer inactive >30d AND "
                "(definite-no | overlap-redundant | >=2 adversarial escalations).",
                "Label only — PR is NOT auto-closed on GitHub. Maintainer closes manually.",
            ],
            ambiguity_signals=list(signals.ambiguity_signals),
        )

    # 2. ready-to-merge
    ready_ok, ready_passed, ready_failed = _ready_to_merge_gates(signals)
    if ready_ok:
        confidence = "high"
        if signals.ambiguity_signals:
            confidence = "medium"
        return RubricResult(
            decision="ready-to-merge",
            confidence=confidence,
            gates_passed=ready_passed,
            gates_failed=[],
            reasoning_hints=[
                "All structural gates clean. Maintainer should merge.",
            ],
            ambiguity_signals=list(signals.ambiguity_signals),
        )

    # 3. request-changes (default fallback)
    rc_signals = _request_changes_signals(signals)
    confidence: str
    hints: list[str] = []
    # Hard signals (blockers, failing tests, adversarial reject) → high confidence
    # even when standalone. Softer signals (single major, conflicting overlap) → medium
    # unless paired with another.
    hard_signals = {
        sig for sig in rc_signals
        if sig.startswith("blocker-findings")
        or sig.startswith("adversarial-reject")
        or sig.startswith("implementation-tests-failing")
    }
    if rc_signals:
        if hard_signals or len(rc_signals) >= 2:
            confidence = "high"
        else:
            confidence = "medium"
        hints.append(
            "Actionable signals present: " + ", ".join(rc_signals)
        )
    else:
        confidence = CONFIDENCE_THRESHOLD_LOW
        hints.append(
            "No definitive structural signal. Falling back to request-changes as a "
            "safety net; LLM may reroute to please-advise."
        )

    failed = ready_failed + ([] if close_ok else close_failed)
    return RubricResult(
        decision="request-changes",
        confidence=confidence,
        gates_passed=rc_signals,
        gates_failed=failed,
        reasoning_hints=hints,
        ambiguity_signals=list(signals.ambiguity_signals),
    )


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--signals", required=True,
        help="JSON-encoded Signals object (e.g. piped from aggregate.py)",
    )
    args = parser.parse_args()
    raw = json.loads(args.signals)
    # Construct Signals from dict, tolerating extras.
    fields = {f for f in Signals.__dataclass_fields__}
    cleaned = {k: v for k, v in raw.items() if k in fields}
    signals = Signals(**cleaned)
    result = evaluate(signals)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
