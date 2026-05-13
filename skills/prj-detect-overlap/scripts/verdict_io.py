#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Verdict persistence for prj-detect-overlap.

Translates a per-pair verdict map into:
  - GitHub labels (via `gh pr edit --add-label`)
  - The overlap report at prs/{n}/overlap.md (markdown table)
  - An overlap-notes append for complementary pairs
  - State transitions on the target PR (and optionally the counterpart for
    conflicting pairs, which sets `blocking_on` on the newer side)

Pure functions where possible:
  - aggregate_verdict(pairs_with_verdicts) -> strongest single verdict
  - resolve_phase_transition(verdict, target_phase_entered_at, other_phase_entered_at, target_number, other_number)
    -> a PhaseTransition descriptor.
  - render_overlap_markdown(...) -> str

Subprocess + filesystem writes live behind discrete functions tests can patch.

Importable: VERDICTS, SEVERITY_ORDER, PhaseTransition, VerdictDecision,
PairVerdict, aggregate_strongest_verdict, resolve_phase_transition,
build_labels, render_overlap_markdown, apply_labels, write_overlap_report,
append_overlap_note, persist.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling import: state_io is owned by prj-orchestrator. Add its scripts dir
# to sys.path so we can `import state_io` cleanly. Walks from this file:
# scripts/ -> prj-detect-overlap/ -> skills/ -> prj-orchestrator/scripts/.
_SIBLING = Path(__file__).resolve().parent.parent.parent / "prj-orchestrator" / "scripts"
if _SIBLING.exists() and str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

import state_io  # noqa: E402

VERDICTS = ("independent", "complementary", "conflicting", "redundant")

# Higher index = stronger / more-severe verdict
SEVERITY_ORDER = {
    "independent": 0,
    "complementary": 1,
    "conflicting": 2,
    "redundant": 3,
}


class VerdictIoError(RuntimeError):
    """Raised on invalid verdicts or unparseable inputs."""


@dataclass(frozen=True)
class PairVerdict:
    """One pair's verdict + optional rationale."""

    target_pr: int
    other_pr: int
    shared_files: tuple[str, ...]
    overlap_count: int
    verdict: str
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_pr": self.target_pr,
            "other_pr": self.other_pr,
            "shared_files": list(self.shared_files),
            "overlap_count": self.overlap_count,
            "verdict": self.verdict,
            "rationale": self.rationale,
        }


@dataclass
class PhaseTransition:
    """How the persist step should mutate state for the target PR (and maybe a counterpart)."""

    target_phase: str
    target_next_action: dict[str, Any] | None
    target_blocking_on: int | None = None
    other_phase: str | None = None
    other_pr: int | None = None
    other_blocking_on: int | None = None


@dataclass
class VerdictDecision:
    """Full decision derived from the per-pair verdicts."""

    strongest_verdict: str
    transition: PhaseTransition
    labels_for_target: list[str] = field(default_factory=list)


# ---------- Pure functions ----------

def _validate_verdict(verdict: str) -> None:
    if verdict not in VERDICTS:
        raise VerdictIoError(
            f"unknown verdict {verdict!r}; expected one of {', '.join(VERDICTS)}"
        )


def aggregate_strongest_verdict(pairs: list[PairVerdict]) -> str:
    """Pick the most-severe verdict from a non-empty list. Empty -> 'independent'."""
    if not pairs:
        return "independent"
    return max(pairs, key=lambda p: SEVERITY_ORDER[p.verdict]).verdict


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _older(
    target_pr: int,
    target_phase_entered_at: str,
    other_pr: int,
    other_phase_entered_at: str,
) -> int:
    """Return the PR number considered 'older'. Earlier phase_entered_at wins;
    fallback to lower PR number for stability.
    """
    target_dt = _parse_iso(target_phase_entered_at)
    other_dt = _parse_iso(other_phase_entered_at)
    if target_dt is not None and other_dt is not None:
        if target_dt < other_dt:
            return target_pr
        if other_dt < target_dt:
            return other_pr
    # Tie or missing timestamps: lower PR number is older.
    return target_pr if target_pr < other_pr else other_pr


def resolve_phase_transition(
    strongest_verdict: str,
    target_pr: int,
    target_phase_entered_at: str,
    pairs: list[PairVerdict],
    other_phase_entered_at_by_pr: dict[int, str] | None = None,
) -> PhaseTransition:
    """Translate the strongest verdict into a state mutation."""
    _validate_verdict(strongest_verdict)
    other_ts_map = other_phase_entered_at_by_pr or {}

    if strongest_verdict == "redundant":
        return PhaseTransition(
            target_phase="Rejected",
            target_next_action=None,
        )

    if strongest_verdict == "conflicting":
        # Identify the highest-severity conflicting pair to attach blocking_on
        conflicting = [p for p in pairs if p.verdict == "conflicting"]
        # Pick the older counterpart for blocking_on if target is newer.
        first = conflicting[0] if conflicting else None
        if first is None:
            return PhaseTransition(
                target_phase="Blocked",
                target_next_action=None,
            )
        other_ts = other_ts_map.get(first.other_pr, "")
        older_pr = _older(
            target_pr, target_phase_entered_at,
            first.other_pr, other_ts,
        )
        if older_pr == target_pr:
            # Target is older; block the OTHER PR instead.
            return PhaseTransition(
                target_phase="ReviewPending",
                target_next_action={"skill": "prj-review", "mode": None},
                other_phase="Blocked",
                other_pr=first.other_pr,
                other_blocking_on=target_pr,
            )
        # Target is newer; target is blocked on the older counterpart.
        return PhaseTransition(
            target_phase="Blocked",
            target_next_action=None,
            target_blocking_on=first.other_pr,
        )

    # complementary or independent
    return PhaseTransition(
        target_phase="ReviewPending",
        target_next_action={"skill": "prj-review", "mode": None},
    )


def build_labels(
    target_pr: int,
    target_phase_entered_at: str,
    pairs: list[PairVerdict],
    other_phase_entered_at_by_pr: dict[int, str] | None = None,
) -> list[str]:
    """Build the GitHub label list for the target PR based on per-pair verdicts.

    Labels emitted (per pair):
      - redundant     -> prj/overlap:duplicate, prj/triage:definite-no
      - conflicting   -> prj/overlap:conflicts-with-{other_pr} (only on the blocked side)
      - complementary -> prj/overlap:depends-on-{older_pr} (only on the newer side)
      - independent   -> no label

    Labels are deduplicated and returned sorted for determinism.
    """
    other_ts_map = other_phase_entered_at_by_pr or {}
    labels: set[str] = set()
    for pair in pairs:
        if pair.verdict == "redundant":
            labels.add("prj/overlap:duplicate")
            labels.add("prj/triage:definite-no")
        elif pair.verdict == "conflicting":
            other_ts = other_ts_map.get(pair.other_pr, "")
            older_pr = _older(
                target_pr, target_phase_entered_at,
                pair.other_pr, other_ts,
            )
            if older_pr != target_pr:
                # Target is newer -> target gets the conflicts-with label.
                labels.add(f"prj/overlap:conflicts-with-{pair.other_pr}")
        elif pair.verdict == "complementary":
            other_ts = other_ts_map.get(pair.other_pr, "")
            older_pr = _older(
                target_pr, target_phase_entered_at,
                pair.other_pr, other_ts,
            )
            if older_pr != target_pr:
                labels.add(f"prj/overlap:depends-on-{older_pr}")
    return sorted(labels)


def render_overlap_markdown(
    target_pr: int,
    target_files: list[str],
    pairs: list[PairVerdict],
    skipped: list[PairVerdict] | None = None,
    generated_at: str | None = None,
) -> str:
    """Render the overlap report Markdown."""
    ts = generated_at or datetime.now(timezone.utc).isoformat()
    lines: list[str] = []
    lines.append(f"# Overlap report — PR #{target_pr}")
    lines.append("")
    lines.append(f"Generated: {ts}")
    lines.append("")
    lines.append(f"Target PR files changed: {len(target_files)}")
    lines.append("")
    if not pairs:
        lines.append("No overlap pairs hit the 2-shared-file threshold.")
    else:
        lines.append("| Other PR | Overlap | Verdict | Shared files | Rationale |")
        lines.append("| --- | --- | --- | --- | --- |")
        for pair in pairs:
            shared = ", ".join(f"`{f}`" for f in pair.shared_files)
            rationale = (pair.rationale or "").replace("|", "\\|").replace("\n", " ").strip()
            lines.append(
                f"| #{pair.other_pr} | {pair.overlap_count} | `{pair.verdict}` | {shared} | {rationale} |"
            )
    if skipped:
        lines.append("")
        lines.append("## Sub-threshold pairs (informational)")
        lines.append("")
        lines.append("| Other PR | Overlap | Shared files |")
        lines.append("| --- | --- | --- |")
        for pair in skipped:
            shared = ", ".join(f"`{f}`" for f in pair.shared_files)
            lines.append(f"| #{pair.other_pr} | {pair.overlap_count} | {shared} |")
    lines.append("")
    return "\n".join(lines)


# ---------- I/O wrappers (each is a single test seam) ----------

def _pr_cache_dir(project_root: Path, pr_number: int) -> Path:
    return project_root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)


def write_overlap_report(project_root: Path, pr_number: int, body: str) -> Path:
    cache = _pr_cache_dir(project_root, pr_number)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "overlap.md"
    path.write_text(body, encoding="utf-8")
    return path


def append_overlap_note(project_root: Path, pr_number: int, note: str) -> Path:
    cache = _pr_cache_dir(project_root, pr_number)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "overlap-notes.md"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(note.rstrip())
        fh.write("\n")
    return path


def _run_gh(args: list[str], timeout: int = 60) -> str:
    """Single subprocess seam. Tests patch this to inject fake gh output."""
    binary = shutil.which("gh") or "gh"
    cmd = [binary, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise VerdictIoError(f"gh CLI not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VerdictIoError(
            f"gh CLI timed out after {timeout}s: {' '.join(cmd)}"
        ) from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise VerdictIoError(f"gh exited {proc.returncode}: {tail}")
    return proc.stdout


def apply_labels(repo: str, pr_number: int, labels: list[str]) -> None:
    """Apply each label via `gh pr edit --add-label`. No-op when labels is empty."""
    if not labels:
        return
    if not repo:
        raise VerdictIoError("repo must be non-empty (owner/name) to apply labels")
    for label in labels:
        _run_gh([
            "pr", "edit", str(pr_number),
            "--repo", repo,
            "--add-label", label,
        ])


# ---------- Combined persistence ----------

def persist(
    project_root: Path,
    repo: str,
    pairs: list[PairVerdict],
    skipped: list[PairVerdict],
    target_pr: int,
    target_files: list[str],
    apply_gh_labels: bool = True,
) -> dict[str, Any]:
    """Apply every effect of a verdict set: state mutation, overlap report,
    optional notes append, optional label application.

    Returns a dict suitable for embedding in a runlog entry.
    """
    state = state_io.load_state(project_root)
    pr_entry = state["prs"].get(str(target_pr))
    if pr_entry is None:
        raise VerdictIoError(f"PR {target_pr} not found in state.json")

    target_ts = pr_entry.get("phase_entered_at", "")
    other_ts_map: dict[int, str] = {}
    for pair in pairs:
        other_entry = state["prs"].get(str(pair.other_pr))
        if other_entry is not None:
            other_ts_map[pair.other_pr] = other_entry.get("phase_entered_at", "")

    strongest = aggregate_strongest_verdict(pairs)
    transition = resolve_phase_transition(
        strongest, target_pr, target_ts, pairs, other_ts_map,
    )
    labels = build_labels(target_pr, target_ts, pairs, other_ts_map)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Mutate target PR
    pr_entry["phase"] = transition.target_phase
    pr_entry["phase_entered_at"] = now_iso
    pr_entry["last_action_at"] = now_iso
    pr_entry["next_action"] = transition.target_next_action
    if transition.target_blocking_on is not None:
        pr_entry["blocking_on"] = transition.target_blocking_on
    elif "blocking_on" in pr_entry and transition.target_phase != "Blocked":
        pr_entry.pop("blocking_on", None)

    # Mutate other PR if this is a target-older conflicting case
    if transition.other_pr is not None and transition.other_phase is not None:
        other_entry = state["prs"].get(str(transition.other_pr))
        if other_entry is not None:
            other_entry["phase"] = transition.other_phase
            other_entry["phase_entered_at"] = now_iso
            other_entry["last_action_at"] = now_iso
            other_entry["next_action"] = None
            if transition.other_blocking_on is not None:
                other_entry["blocking_on"] = transition.other_blocking_on

    # Write overlap report
    body = render_overlap_markdown(
        target_pr=target_pr,
        target_files=target_files,
        pairs=pairs,
        skipped=skipped,
        generated_at=now_iso,
    )
    report_path = write_overlap_report(project_root, target_pr, body)

    # Complementary notes
    note_paths: list[str] = []
    for pair in pairs:
        if pair.verdict == "complementary":
            other_ts = other_ts_map.get(pair.other_pr, "")
            older_pr = _older(target_pr, target_ts, pair.other_pr, other_ts)
            note = (
                f"- {now_iso} complementary with PR #{pair.other_pr}; "
                f"older=#{older_pr} should land first."
            )
            note_paths.append(str(append_overlap_note(project_root, target_pr, note)))

    state_io.save_state(project_root, state)

    if apply_gh_labels:
        apply_labels(repo, target_pr, labels)

    return {
        "strongest_verdict": strongest,
        "labels_applied": labels if apply_gh_labels else [],
        "labels_planned": labels,
        "phase_transition": {
            "target_phase": transition.target_phase,
            "target_next_action": transition.target_next_action,
            "target_blocking_on": transition.target_blocking_on,
            "other_pr": transition.other_pr,
            "other_phase": transition.other_phase,
            "other_blocking_on": transition.other_blocking_on,
        },
        "report_path": str(report_path),
        "note_paths": note_paths,
        "pairs": [p.to_dict() for p in pairs],
    }


# ---------- CLI surface (debugging aid) ----------

def _pair_from_dict(data: dict[str, Any]) -> PairVerdict:
    verdict = data.get("verdict", "")
    _validate_verdict(verdict)
    shared = tuple(data.get("shared_files") or [])
    return PairVerdict(
        target_pr=int(data["target_pr"]),
        other_pr=int(data["other_pr"]),
        shared_files=shared,
        overlap_count=int(data.get("overlap_count") or len(shared)),
        verdict=verdict,
        rationale=str(data.get("rationale") or ""),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, help="Path to project root")
    parser.add_argument("--repo", required=True, help="Repo in owner/name format")
    parser.add_argument("--pr-number", type=int, required=True, help="Target PR number")
    parser.add_argument(
        "--pairs-json",
        required=True,
        help="JSON array of pair dicts (target_pr, other_pr, shared_files, verdict, rationale)",
    )
    parser.add_argument(
        "--target-files-json",
        required=True,
        help="JSON array of files changed by the target PR",
    )
    parser.add_argument(
        "--skipped-json",
        default="[]",
        help="JSON array of sub-threshold pair dicts",
    )
    parser.add_argument(
        "--skip-label",
        action="store_true",
        help="Skip the `gh` label application step",
    )
    args = parser.parse_args()

    raw_pairs = json.loads(args.pairs_json)
    raw_skipped = json.loads(args.skipped_json)
    target_files = json.loads(args.target_files_json)
    pairs = [_pair_from_dict(p) for p in raw_pairs]
    skipped = [_pair_from_dict({**p, "verdict": "independent"}) for p in raw_skipped]

    result = persist(
        Path(args.project_root),
        args.repo,
        pairs,
        skipped,
        args.pr_number,
        target_files,
        apply_gh_labels=not args.skip_label,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
