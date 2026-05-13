#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""GitHub label application for prj-triage.

Thin wrapper around `gh pr edit --add-label`. Single seam (`_run_gh`) keeps the
module unit-testable without touching the network: tests patch _run_gh to
inject fake stdout/stderr/returncode.

Importable: apply_triage_label, GhOpsError, TRIAGE_LABELS.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys


class GhOpsError(RuntimeError):
    """Raised when the gh CLI fails or the inputs are invalid."""


TRIAGE_LABELS = {
    "actionable": "prj/triage:actionable",
    "definite-no": "prj/triage:definite-no",
    "possible-duplicate": "prj/triage:duplicate-candidate",
    "needs-review": "prj/triage:needs-review",
}


def _run_gh(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Execute `gh` with the given args. Returns (returncode, stdout, stderr).

    This is the single seam tests patch.
    """
    binary = shutil.which("gh") or "gh"
    cmd = [binary, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise GhOpsError(f"gh CLI not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhOpsError(f"gh CLI timed out after {timeout}s: {' '.join(cmd)}") from exc
    return proc.returncode, proc.stdout, proc.stderr


def label_for(classification: str) -> str:
    """Return the GitHub label string for a triage classification.

    Raises GhOpsError for unknown classification.
    """
    if classification not in TRIAGE_LABELS:
        raise GhOpsError(
            f"unknown triage classification: {classification!r}; "
            f"expected one of {sorted(TRIAGE_LABELS)}"
        )
    return TRIAGE_LABELS[classification]


def apply_triage_label(
    repo: str,
    pr_number: int,
    classification: str,
) -> dict:
    """Apply the `prj/triage:{class}` label to a PR.

    Returns a dict suitable for runlog inclusion. Raises GhOpsError on failure.
    """
    if not repo:
        raise GhOpsError("repo must be non-empty (owner/name)")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise GhOpsError(f"pr_number must be a positive int, got {pr_number!r}")
    label = label_for(classification)
    args = [
        "pr", "edit", str(pr_number),
        "--repo", repo,
        "--add-label", label,
    ]
    rc, stdout, stderr = _run_gh(args)
    if rc != 0:
        tail = (stderr or stdout or "").strip()[-500:]
        raise GhOpsError(f"gh exited {rc} applying label {label!r}: {tail}")
    return {
        "label": label,
        "pr_number": pr_number,
        "repo": repo,
        "status": "applied",
    }


# ---------- CLI surface (debugging aid) ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Repo in owner/name format")
    parser.add_argument("--pr-number", required=True, type=int, help="Target PR number")
    parser.add_argument(
        "--classification",
        required=True,
        choices=sorted(TRIAGE_LABELS),
        help="Triage classification",
    )
    args = parser.parse_args()
    try:
        result = apply_triage_label(args.repo, args.pr_number, args.classification)
    except GhOpsError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
