#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Aggregate per-PR cache artifacts into a structured signal bundle.

Pure functions: no subprocess, no network, only filesystem reads from the
per-PR cache directory. Missing files are normal (e.g. no fix-plan if no fix
was needed) and are surfaced as `None` in the bundle, not raised.

The bundle's job is to reduce free-form Markdown into a small set of typed
signals the rubric can evaluate deterministically:

  - blocker_count, major_count   (parsed from review.md frontmatter or body)
  - tests_status                  ("pass" | "fail" | "unknown")
  - regressions                   ("none" | "present" | "unknown")
  - overlap_verdict               ("independent" | "complementary" |
                                   "conflicting" | "redundant" | None)
  - triage_class                  ("actionable" | "definite-no" |
                                   "duplicate-candidate" | "needs-review" | None)
  - adversarial_verdict           ("pass" | "reject" | "escalate" | None)
  - adversarial_escalation_count  (count from decisions.log)
  - days_since_maintainer         (int, computed from PR state entry)
  - has_implementation            (bool)
  - cache_paths                   (list[str] of existing artifacts)

Parsing strategy: each artifact's top-of-file frontmatter (YAML-lite key: value)
is read first; if absent, fall back to simple heuristics on the body. Heuristics
are documented inline. Tests cover both paths.

Importable: aggregate, Signals, parse_*, load_decisions_log.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Cache artifact filenames the decision skill consumes.
ARTIFACT_NAMES = (
    "review.md",
    "verification.md",
    "fix-plan.md",
    "adversarial.md",
    "implementation.md",
    "overlap.md",
    "triage.md",
)
DECISIONS_LOG = "decisions.log"


@dataclass
class Signals:
    """Typed bundle of signals derived from the per-PR cache."""

    pr_number: int
    blocker_count: int = 0
    major_count: int = 0
    minor_count: int = 0
    tests_status: str = "unknown"   # pass | fail | unknown
    regressions: str = "unknown"    # none | present | unknown
    overlap_verdict: str | None = None
    triage_class: str | None = None
    adversarial_verdict: str | None = None
    adversarial_escalation_count: int = 0
    days_since_maintainer: int | None = None
    has_implementation: bool = False
    has_review: bool = False
    has_verification: bool = False
    has_fix_plan: bool = False
    verification_verdict: str | None = None
    ambiguity_signals: list[str] = field(default_factory=list)
    cache_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- frontmatter & key parsers ----------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, str]:
    """Return a flat dict of top-level frontmatter keys. Empty dict if absent.

    Only handles the simple `key: value` form, one per line. Values are stripped
    and unquoted at the surface level only (no nested structures, no lists).
    """
    if not text:
        return {}
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    block = match.group(1)
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        if value.startswith("'") and value.endswith("'") and len(value) >= 2:
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def parse_review(text: str) -> tuple[int, int, int]:
    """Parse blocker/major/minor finding counts from a review.md.

    Preference order:
      1. Frontmatter keys `blockers`, `majors`, `minors` (digits).
      2. Frontmatter key `findings` of the form "blocker=2, major=1, minor=0".
      3. Body counts of literal `severity: blocker` / `severity: major` /
         `severity: minor` substrings (case-insensitive).
    """
    fm = parse_frontmatter(text)
    if fm:
        if "blockers" in fm or "majors" in fm or "minors" in fm:
            return (
                _int(fm.get("blockers")),
                _int(fm.get("majors")),
                _int(fm.get("minors")),
            )
        if "findings" in fm:
            return _parse_findings_summary(fm["findings"])
    body = text.lower()
    return (
        len(re.findall(r"severity:\s*blocker", body)),
        len(re.findall(r"severity:\s*major", body)),
        len(re.findall(r"severity:\s*minor", body)),
    )


def _parse_findings_summary(summary: str) -> tuple[int, int, int]:
    """Parse 'blocker=2, major=1, minor=0' style summaries."""
    out = {"blocker": 0, "major": 0, "minor": 0}
    for part in summary.split(","):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip().lower()
        if k in out:
            out[k] = _int(v)
    return out["blocker"], out["major"], out["minor"]


def _int(value: str | None) -> int:
    if value is None:
        return 0
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return 0


def parse_implementation(text: str) -> tuple[str, str]:
    """Return (tests_status, regressions). Defaults to ('unknown','unknown')."""
    fm = parse_frontmatter(text)
    tests = (fm.get("tests") or "").lower().strip()
    regressions = (fm.get("regressions") or "").lower().strip()
    if tests not in {"pass", "fail"}:
        tests = "unknown"
    if regressions not in {"none", "present"}:
        regressions = "unknown"
    return tests, regressions


def parse_overlap(text: str) -> str | None:
    """Return the overlap verdict from frontmatter (independent | complementary
    | conflicting | redundant) or None if absent."""
    fm = parse_frontmatter(text)
    verdict = (fm.get("verdict") or "").lower().strip()
    if verdict in {"independent", "complementary", "conflicting", "redundant"}:
        return verdict
    return None


def parse_triage(text: str) -> str | None:
    fm = parse_frontmatter(text)
    cls = (fm.get("classification") or fm.get("class") or "").lower().strip()
    if cls in {"actionable", "definite-no", "duplicate-candidate", "needs-review"}:
        return cls
    return None


def parse_adversarial(text: str) -> str | None:
    fm = parse_frontmatter(text)
    verdict = (fm.get("verdict") or "").lower().strip()
    if verdict in {"pass", "reject", "escalate"}:
        return verdict
    return None


def parse_verification(text: str) -> str | None:
    fm = parse_frontmatter(text)
    verdict = (fm.get("verdict") or "").lower().strip()
    if verdict in {"verified", "not-verified", "ambiguous"}:
        return verdict
    return None


# ---------- decisions.log ----------

def load_decisions_log(path: Path) -> list[dict[str, Any]]:
    """Read JSONL decisions log; return [] if absent. Malformed lines skipped."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def count_adversarial_escalations(entries: list[dict[str, Any]]) -> int:
    return sum(1 for e in entries if e.get("kind") == "adversarial-escalation")


# ---------- maintainer activity ----------

def days_since(iso_ts: str | None, now: datetime) -> int | None:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = now - ts
    return max(0, delta.days)


# ---------- top-level aggregator ----------

def aggregate(
    cache_dir: Path,
    pr_number: int,
    pr_state_entry: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Signals:
    """Read every cache artifact under cache_dir and produce a Signals bundle.

    cache_dir is `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/`.
    Missing artifacts are tolerated; the bundle simply records their absence.
    """
    now = now or datetime.now(timezone.utc)
    signals = Signals(pr_number=pr_number)

    review_text = _read_text(cache_dir / "review.md")
    if review_text:
        signals.has_review = True
        signals.cache_paths.append("review.md")
        b, m, mi = parse_review(review_text)
        signals.blocker_count = b
        signals.major_count = m
        signals.minor_count = mi

    impl_text = _read_text(cache_dir / "implementation.md")
    if impl_text:
        signals.has_implementation = True
        signals.cache_paths.append("implementation.md")
        tests, regressions = parse_implementation(impl_text)
        signals.tests_status = tests
        signals.regressions = regressions

    overlap_text = _read_text(cache_dir / "overlap.md")
    if overlap_text:
        signals.cache_paths.append("overlap.md")
        signals.overlap_verdict = parse_overlap(overlap_text)

    triage_text = _read_text(cache_dir / "triage.md")
    if triage_text:
        signals.cache_paths.append("triage.md")
        signals.triage_class = parse_triage(triage_text)

    adv_text = _read_text(cache_dir / "adversarial.md")
    if adv_text:
        signals.cache_paths.append("adversarial.md")
        signals.adversarial_verdict = parse_adversarial(adv_text)

    ver_text = _read_text(cache_dir / "verification.md")
    if ver_text:
        signals.has_verification = True
        signals.cache_paths.append("verification.md")
        signals.verification_verdict = parse_verification(ver_text)

    fix_text = _read_text(cache_dir / "fix-plan.md")
    if fix_text:
        signals.has_fix_plan = True
        signals.cache_paths.append("fix-plan.md")

    log_entries = load_decisions_log(cache_dir / DECISIONS_LOG)
    if log_entries:
        signals.cache_paths.append(DECISIONS_LOG)
    signals.adversarial_escalation_count = count_adversarial_escalations(log_entries)

    if pr_state_entry:
        signals.days_since_maintainer = days_since(
            pr_state_entry.get("last_maintainer_activity_at"), now,
        )

    # Ambiguity signals are passed forward to the rubric for downstream LLM
    # judgement when the deterministic gates do not yield a clear class.
    if signals.has_review and signals.blocker_count == 0 and signals.major_count > 0 \
            and signals.has_fix_plan and not signals.has_implementation:
        signals.ambiguity_signals.append(
            "majors-without-implementation: review has major findings but fix-plan "
            "exists without implementation; may be out-of-scope or pending"
        )
    if signals.verification_verdict == "ambiguous":
        signals.ambiguity_signals.append("verification-ambiguous")
    if signals.adversarial_verdict == "reject" and signals.has_fix_plan \
            and signals.adversarial_escalation_count < 2:
        signals.ambiguity_signals.append(
            "single-adversarial-reject: one rejection short of the close-as-not-now gate"
        )

    return signals


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True, help="Per-PR cache directory")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument(
        "--state-entry",
        help="Optional JSON string for the PR's state entry (passes last_maintainer_activity_at)",
    )
    args = parser.parse_args()

    cache = Path(args.cache_dir)
    pr_entry = json.loads(args.state_entry) if args.state_entry else None
    sigs = aggregate(cache, args.pr, pr_entry)
    print(json.dumps(sigs.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
