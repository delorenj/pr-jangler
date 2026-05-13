#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Regression suite runner for the adversarial validator.

Executes the configured `prj_test_runner` command (or an auto-detected default
such as `bun test`, `pytest`, `npm test`, `cargo test`) inside the project root
and reports a structural summary suitable for the validator's hard gate:

    {
      "status": "ok | runner-failed",
      "command": "...",
      "exit_code": int,
      "returncode_indicates_pass": bool,
      "stdout_tail": "...",
      "stderr_tail": "...",
      "regressions": int,    # non-negative integer; 0 = no regressions
      "duration_ms": int
    }

The `regressions` count is the structural gate: validator auto-rejects when > 0.
Today's heuristic: if the runner's exit code != 0, treat as `regressions == 1`
unless `stdout` contains the new-failing-test marker (signalled by the caller).
A richer parse is possible later; this skill keeps the gate strict and simple.

CLI: `python3 regression_run.py --project-root <p>` runs the suite and prints
JSON. Importable: `run_regression`, `RegressionResult`, `detect_runner`.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path


TAIL_BYTES = 4000
DEFAULT_TIMEOUT_S = 600


@dataclass
class RegressionResult:
    """Structured summary of a regression-suite run."""

    status: str
    command: str
    exit_code: int
    returncode_indicates_pass: bool
    stdout_tail: str
    stderr_tail: str
    regressions: int
    duration_ms: int

    def to_dict(self) -> dict:
        return asdict(self)


def _tail(text: str, limit: int = TAIL_BYTES) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def detect_runner(project_root: Path) -> str:
    """Auto-detect a sensible default test command based on repo markers.

    Order: package.json with `test` script -> `bun test` (when bun.lockb or
    package.json declares bun) else `npm test`; pyproject.toml -> `pytest`;
    Cargo.toml -> `cargo test`. Falls back to `pytest` as a final default.
    """
    if (project_root / "bun.lockb").exists() or (project_root / "bun.lock").exists():
        return "bun test"
    if (project_root / "package.json").exists():
        return "npm test"
    if (project_root / "pyproject.toml").exists() or (project_root / "setup.py").exists():
        return "pytest"
    if (project_root / "Cargo.toml").exists():
        return "cargo test"
    return "pytest"


def load_runner_command(project_root: Path) -> str:
    """Read `prj_test_runner` from config; fall back to detection."""
    cfg_path = project_root / "_bmad" / "config.toml"
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
        runner = data.get("modules", {}).get("prj", {}).get("prj_test_runner")
        if isinstance(runner, str) and runner.strip():
            return runner.strip()
    return detect_runner(project_root)


def run_regression(
    project_root: Path,
    runner_command: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    runner: object | None = None,
) -> RegressionResult:
    """Execute the regression suite and return a structured result.

    `runner` is a callable hook for tests: it receives (cmd_list, project_root,
    timeout_s) and must return an object exposing .returncode, .stdout, .stderr.
    By default subprocess.run is used.
    """
    command = runner_command or load_runner_command(project_root)
    cmd_list = shlex.split(command)
    started = time.monotonic()

    if runner is None:
        runner_call = lambda: subprocess.run(
            cmd_list,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    else:
        runner_call = lambda: runner(cmd_list, project_root, timeout_s)

    try:
        result = runner_call()
        exit_code = int(getattr(result, "returncode", 1))
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        passed = exit_code == 0
        # Structural gate: any non-zero exit is treated as one or more
        # regressions for the purposes of the hard auto-reject. A richer parse
        # of test-runner output (counting individual failures) can refine this
        # later without changing the contract.
        regressions = 0 if passed else 1
        status = "ok"
    except subprocess.TimeoutExpired as exc:
        exit_code = -1
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        passed = False
        regressions = 1
        status = "runner-failed"
    except FileNotFoundError as exc:
        exit_code = -1
        stdout = ""
        stderr = f"test runner not found: {exc}"
        passed = False
        regressions = 1
        status = "runner-failed"

    duration_ms = int((time.monotonic() - started) * 1000)
    return RegressionResult(
        status=status,
        command=command,
        exit_code=exit_code,
        returncode_indicates_pass=passed,
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
        regressions=int(regressions),
        duration_ms=duration_ms,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, help="Project root path")
    parser.add_argument(
        "--runner",
        help="Override the test-runner command (otherwise read from config or detected)",
        default=None,
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_S,
        help=f"Subprocess timeout in seconds (default {DEFAULT_TIMEOUT_S})",
    )
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    if not project_root.is_dir():
        print(json.dumps({"status": "error", "error": f"project root not found: {project_root}"}), file=sys.stderr)
        return 2

    result = run_regression(project_root, runner_command=args.runner, timeout_s=args.timeout)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    # Exit codes: 0 = no regressions, 1 = regressions detected, 2 = runner failed
    if result.status == "runner-failed":
        return 2
    return 0 if result.regressions == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
