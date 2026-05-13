#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Test runner autodetect + execution for prj-plan-fix.

NOTE: duplicated for v1 from prj-verify-claim (which builds in parallel).
Consolidate once both skills land.

Autodetects a project's test runner by looking for:
  - bun.lockb / package.json with "test" script -> `bun run test`
  - pyproject.toml or setup.py with pytest config -> `pytest`
  - Cargo.toml -> `cargo test`
  - Otherwise: fall back to `prj_test_runner` from config or `pytest`.

CLI:
  python3 test_runner.py detect [--project-root <path>]
  python3 test_runner.py run --command "<cmd>" [--cwd <path>] [--test-file <path>]

Importable:
  detect_runner(project_root, config_override=None) -> str
  run_tests(command, cwd) -> RunResult
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RunResult:
    command: str
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @property
    def failed(self) -> bool:
        return self.exit_code != 0

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "stdout_tail": self.stdout[-2000:] if self.stdout else "",
            "stderr_tail": self.stderr[-2000:] if self.stderr else "",
            "duration_ms": self.duration_ms,
        }


def detect_runner(project_root: Path, config_override: str | None = None) -> str:
    """Pick a test command for the project.

    Order:
      1. `config_override` (from `prj_test_runner` in `[modules.prj]`)
      2. `bun run test` if bun + package.json with "test" script
      3. `pytest` if pyproject.toml/setup.py present
      4. `cargo test` if Cargo.toml present
      5. fallback: `pytest`
    """
    if config_override and config_override.strip():
        return config_override.strip()

    pkg_json = project_root / "package.json"
    bun_lock = project_root / "bun.lockb"
    if pkg_json.exists() and bun_lock.exists():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            if isinstance(data.get("scripts"), dict) and "test" in data["scripts"]:
                return "bun run test"
        except (json.JSONDecodeError, OSError):
            pass

    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            if isinstance(data.get("scripts"), dict) and "test" in data["scripts"]:
                return "npm test"
        except (json.JSONDecodeError, OSError):
            pass

    if (project_root / "pyproject.toml").exists() or (project_root / "setup.py").exists():
        return "pytest"

    if (project_root / "Cargo.toml").exists():
        return "cargo test"

    return "pytest"


def run_tests(
    command: str,
    cwd: Path,
    timeout: int = 300,
    _runner=subprocess.run,
) -> RunResult:
    """Execute a shell test command in cwd. Returns RunResult.

    `_runner` parameter exists for test injection.
    """
    import time

    start = time.monotonic()
    try:
        completed = _runner(
            shlex.split(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        rc = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, (bytes, bytearray)) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, (bytes, bytearray)) else (exc.stderr or "")
        rc = 124  # convention: timed out
    duration_ms = int((time.monotonic() - start) * 1000)
    return RunResult(
        command=command,
        cwd=str(cwd),
        exit_code=rc,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
    )


# ---------- CLI surface ----------

def _cmd_detect(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    cmd = detect_runner(root, config_override=args.override)
    print(json.dumps({"status": "ok", "command": cmd, "project_root": str(root)}))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    result = run_tests(args.command, cwd, timeout=args.timeout)
    print(json.dumps({"status": "ok", **result.to_dict()}))
    return 0 if result.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_detect = sub.add_parser("detect", help="Print the autodetected test command")
    p_detect.add_argument("--project-root", help="Override project root", default=None)
    p_detect.add_argument("--override", help="Force a specific test command", default=None)
    p_detect.set_defaults(func=_cmd_detect)

    p_run = sub.add_parser("run", help="Run a test command in cwd")
    p_run.add_argument("--command", required=True, help="Test command to run")
    p_run.add_argument("--cwd", help="Working directory", default=None)
    p_run.add_argument("--timeout", type=int, default=300, help="Timeout in seconds")
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
