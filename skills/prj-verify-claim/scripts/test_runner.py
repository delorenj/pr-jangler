#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Test-runner detection and execution for prj-verify-claim.

Repos in the wild use different test runners. We auto-detect by sniffing
characteristic files in the repo root, then execute the selected runner inside
the verification worktree. Config key `prj_test_runner` overrides the
auto-detection unconditionally.

Importable:
  - DETECTION_ORDER, RUNNERS
  - RunnerError
  - DetectionResult, RunResult (dataclasses)
  - detect_runner
  - build_command
  - run_in_worktree

CLI subcommands: detect, run.

The subprocess seam is a single `_run` function so tests can patch it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class RunnerError(RuntimeError):
    """Raised when detection or execution fails fatally."""


# Detection order: longest-lockfile-first wins so that, e.g., a repo with
# BOTH a package.json and a Cargo.toml is classified by the more specific
# marker. The order below reflects what the PR Jangler actually encounters
# in the wild.
DETECTION_ORDER: tuple[str, ...] = ("bun", "pnpm", "npm", "pytest", "cargo")

# Each runner: marker files (any-of), default test invocation as argv list.
RUNNERS: dict[str, dict[str, Any]] = {
    "bun": {
        "markers": ["bun.lockb", "bun.lock", "bunfig.toml"],
        "default_command": ["bun", "test"],
    },
    "pnpm": {
        "markers": ["pnpm-lock.yaml"],
        "default_command": ["pnpm", "test"],
    },
    "npm": {
        # package.json is the fallback marker for the JS family. Bun and pnpm
        # win over npm because their lockfiles are more specific.
        "markers": ["package-lock.json", "package.json"],
        "default_command": ["npm", "test"],
    },
    "pytest": {
        "markers": ["pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"],
        "default_command": ["pytest"],
    },
    "cargo": {
        "markers": ["Cargo.toml"],
        "default_command": ["cargo", "test"],
    },
}


@dataclass(frozen=True)
class DetectionResult:
    """Outcome of test-runner auto-detection."""

    runner: str | None
    source: str  # "override" | "auto" | "none"
    markers_seen: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runner": self.runner,
            "source": self.source,
            "markers_seen": list(self.markers_seen),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class RunResult:
    """Outcome of a test-runner invocation."""

    runner: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "runner": self.runner,
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout_tail": self.stdout[-2000:],
            "stderr_tail": self.stderr[-2000:],
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
        }


def detect_runner(worktree: Path, override: str | None = None) -> DetectionResult:
    """Choose a test runner for the worktree.

    Precedence:
      1. If `override` is set and non-empty, use it verbatim.
         Override accepts either a known runner key (e.g. "bun") or a raw
         command string (e.g. "make test"). The string form is signaled by
         setting `runner = "custom"` so the caller knows to skip the
         RUNNERS lookup when building the command.
      2. Otherwise iterate DETECTION_ORDER; first runner whose marker files
         exist wins.
      3. If no markers match, return runner=None and source="none". The
         caller decides whether that is an `ambiguous` verdict.
    """
    if override:
        cleaned = override.strip()
        if cleaned in RUNNERS:
            return DetectionResult(
                runner=cleaned,
                source="override",
                notes=[f"prj_test_runner override = {cleaned!r}"],
            )
        return DetectionResult(
            runner="custom",
            source="override",
            notes=[f"prj_test_runner override is raw command {cleaned!r}"],
        )

    if not worktree.exists():
        return DetectionResult(
            runner=None,
            source="none",
            notes=[f"worktree path does not exist: {worktree}"],
        )

    markers_seen: list[str] = []
    for runner in DETECTION_ORDER:
        for marker in RUNNERS[runner]["markers"]:
            if (worktree / marker).exists():
                markers_seen.append(marker)
                return DetectionResult(
                    runner=runner,
                    source="auto",
                    markers_seen=markers_seen,
                    notes=[f"matched marker {marker!r} -> runner {runner!r}"],
                )

    return DetectionResult(
        runner=None,
        source="none",
        markers_seen=markers_seen,
        notes=["no known test-runner markers found in worktree root"],
    )


def build_command(
    detection: DetectionResult,
    override: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """Build the final argv for the runner.

    `override` here is the raw command (not the runner key); set it when the
    DetectionResult is `runner="custom"` (raw override) or when the caller
    wants to inject a specific test target.
    """
    extra = extra or []
    if detection.runner is None:
        raise RunnerError("cannot build command: no runner detected")

    if detection.runner == "custom":
        if not override:
            raise RunnerError(
                "runner='custom' requires an override command string"
            )
        # Split lightly on whitespace. Quoting is not supported; callers that
        # need complex commands should write a wrapper script.
        parts = override.strip().split()
        return [*parts, *extra]

    base = list(RUNNERS[detection.runner]["default_command"])
    return [*base, *extra]


def _run(
    cmd: list[str],
    cwd: Path,
    timeout: int,
) -> tuple[int, str, str, bool]:
    """Single subprocess seam. Returns (rc, stdout, stderr, timed_out).

    Tests patch this directly.
    """
    binary = shutil.which(cmd[0]) or cmd[0]
    timed_out = False
    try:
        proc = subprocess.run(
            [binary, *cmd[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr, timed_out
    except FileNotFoundError as exc:
        raise RunnerError(f"{cmd[0]} not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        return 124, stdout, stderr, timed_out


def run_in_worktree(
    worktree: Path,
    detection: DetectionResult,
    override: str | None = None,
    extra: list[str] | None = None,
    timeout: int = 600,
) -> RunResult:
    """Execute the detected runner inside the worktree. Returns a RunResult.

    `override` here mirrors `build_command`: pass the raw command string when
    DetectionResult.runner == 'custom'.
    """
    if not worktree.exists():
        raise RunnerError(f"worktree path does not exist: {worktree}")
    command = build_command(detection, override=override, extra=extra)
    started = time.monotonic()
    rc, stdout, stderr, timed_out = _run(command, cwd=worktree, timeout=timeout)
    duration_ms = int((time.monotonic() - started) * 1000)
    runner_label = detection.runner or "unknown"
    return RunResult(
        runner=runner_label,
        command=command,
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        timed_out=timed_out,
    )


# ---------- CLI surface ----------

def _cmd_detect(args: argparse.Namespace) -> int:
    detection = detect_runner(Path(args.worktree), override=args.override)
    print(json.dumps(detection.to_dict(), indent=2, sort_keys=True))
    return 0 if detection.runner else 1


def _cmd_run(args: argparse.Namespace) -> int:
    worktree = Path(args.worktree)
    detection = detect_runner(worktree, override=args.override)
    if detection.runner is None:
        print(
            json.dumps({"status": "no-runner", "detection": detection.to_dict()}, indent=2),
            file=sys.stderr,
        )
        return 2
    try:
        result = run_in_worktree(
            worktree,
            detection,
            override=args.override if detection.runner == "custom" else None,
            extra=args.extra,
            timeout=args.timeout,
        )
    except RunnerError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_det = sub.add_parser("detect", help="Auto-detect test runner for a worktree")
    p_det.add_argument("--worktree", required=True, help="Path to the worktree root")
    p_det.add_argument("--override", default=None, help="prj_test_runner value to honor")
    p_det.set_defaults(func=_cmd_detect)

    p_run = sub.add_parser("run", help="Execute the detected runner in a worktree")
    p_run.add_argument("--worktree", required=True, help="Path to the worktree root")
    p_run.add_argument("--override", default=None, help="prj_test_runner override (runner key or raw command)")
    p_run.add_argument(
        "--extra", nargs="*", default=None,
        help="Extra args appended to the runner command",
    )
    p_run.add_argument(
        "--timeout", type=int, default=600,
        help="Hard timeout in seconds (default 600)",
    )
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args()
    rc = args.func(args)
    sys.exit(rc)


if __name__ == "__main__":
    main()
