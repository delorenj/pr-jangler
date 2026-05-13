"""Config loader for prj-agentd.

Reads `[modules.prj.agentd]` (and falls back to `[modules.prj]`) from
`_bmad/config.toml`. Provides defaults for every key so a fresh project
boots without manual config beyond `prj_repo`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_SOCKET = "~/.codex/app-server.sock"
DEFAULT_TICK_SECONDS = 30
DEFAULT_APPROVAL_MODE = "unlessTrusted"
DEFAULT_SANDBOX = "workspaceWrite"


@dataclass(frozen=True)
class AgentdConfig:
    """Resolved configuration for one prj-agentd instance bound to one repo."""

    project_root: Path
    prj_repo: str
    socket_path: Path
    tick_seconds: int
    approval_mode: str
    sandbox: str
    offline: bool
    # The set of skills the daemon is allowed to dispatch through app-server.
    # Empty set means "all installed phase skills are eligible."
    skill_allowlist: frozenset[str] = field(default_factory=frozenset)
    # Optional explicit path to prj-orchestrator's run.py. When None, falls
    # back to <project_root>/skills/prj-orchestrator/scripts/run.py. Set this
    # when running the daemon against a target repo that doesn't have the
    # orchestrator skill copied in (e.g. testing against an arbitrary repo
    # while the canonical orchestrator lives elsewhere).
    orchestrator_run_py_override: Path | None = None

    @property
    def orchestrator_run_py(self) -> Path:
        if self.orchestrator_run_py_override is not None:
            return self.orchestrator_run_py_override
        return self.project_root / "skills" / "prj-orchestrator" / "scripts" / "run.py"

    @property
    def agentd_state_dir(self) -> Path:
        return self.project_root / "_bmad-output" / "pr-workflow" / "agentd"

    @property
    def store_path(self) -> Path:
        return self.agentd_state_dir / "agentd.sqlite"


def _find_project_root(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for parent in [here, *here.parents]:
        if (parent / "_bmad").is_dir():
            return parent
    return here


def load_config(
    project_root: Path | None = None,
    *,
    offline: bool = False,
    override_socket: Path | None = None,
) -> AgentdConfig:
    """Load AgentdConfig from `_bmad/config.toml`.

    Resolution order for each key:
        1. explicit override (function arg)
        2. [modules.prj.agentd] block
        3. [modules.prj] block (for shared keys like prj_repo)
        4. hardcoded default
    """
    project_root = (project_root or _find_project_root()).resolve()
    cfg_path = project_root / "_bmad" / "config.toml"
    raw: dict = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)
    prj_block = raw.get("modules", {}).get("prj", {})
    agentd_block = prj_block.get("agentd", {})

    prj_repo = (agentd_block.get("prj_repo") or prj_block.get("prj_repo") or "").strip()
    socket = override_socket
    if socket is None:
        socket = Path(agentd_block.get("socket_path", DEFAULT_SOCKET)).expanduser()
    tick = int(agentd_block.get("tick_seconds", DEFAULT_TICK_SECONDS))
    approval = str(agentd_block.get("approval_mode", DEFAULT_APPROVAL_MODE))
    sandbox = str(agentd_block.get("sandbox", DEFAULT_SANDBOX))
    allowlist = frozenset(agentd_block.get("skill_allowlist", []) or [])
    orch_override_raw = agentd_block.get("orchestrator_run_py")
    orch_override = (
        Path(orch_override_raw).expanduser().resolve()
        if orch_override_raw
        else None
    )

    return AgentdConfig(
        project_root=project_root,
        prj_repo=prj_repo,
        socket_path=Path(socket),
        tick_seconds=tick,
        approval_mode=approval,
        sandbox=sandbox,
        offline=offline,
        skill_allowlist=allowlist,
        orchestrator_run_py_override=orch_override,
    )
