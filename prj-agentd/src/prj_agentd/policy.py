"""Approval policy engine — a firewall, not a prompt.

Implements the matrix from WHERE_WE_WANT_TO_BE.md:

    Action                                Default
    ----------------------------------    --------
    Read state.json, per-PR cache, logs   Auto-approve
    gh pr view, gh pr diff, gh api ratelimit
                                          Auto-approve
    python3 scripts/run.py --dry-run      Auto-approve
    --select-only                         Auto-approve
    Write PR Jangler cache files via approved scripts
                                          Auto-approve (workspace)
    Modify state.json directly            DENY
    Post GitHub PR comment/review         Human approval
    Apply labels                          Human approval
    Open fix PR                           Human approval
    Send email digest                     Human approval (until trusted)
    thread/shellCommand                   DENY in daemon mode (unsandboxed)

The policy applies in two places:
    1. Before invoking a phase skill — coarse pre-flight check.
    2. On server-initiated approval requests — fine-grained per-command gate.

`approval_mode` (from config):
    - "neverInteractive" : auto-decide everything, deny anything ambiguous
    - "unlessTrusted"    : default; ask for unlisted/risky operations
    - "always"           : ask for everything
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class Decision(str, Enum):
    AUTO_APPROVE = "auto_approve"
    AUTO_DENY = "auto_deny"
    REQUIRE_HUMAN = "require_human"


# Skill -> what kind of side-effects it produces.
# This drives the pre-flight decision before a turn starts.
SKILL_RISK: dict[str, Decision] = {
    # System / read-only-ish:
    "prj-orchestrator": Decision.AUTO_APPROVE,
    "prj-discover": Decision.AUTO_APPROVE,
    "prj-triage": Decision.AUTO_APPROVE,       # writes to per-PR cache only
    "prj-verify-claim": Decision.AUTO_APPROVE,  # cache only
    "prj-review": Decision.AUTO_APPROVE,       # cache-first; GitHub posting is a separate gate
    "prj-detect-overlap": Decision.AUTO_APPROVE,
    "prj-plan-fix": Decision.AUTO_APPROVE,
    "prj-validate-adversarial": Decision.AUTO_APPROVE,

    # GitHub-mutating / external:
    "prj-decision": Decision.REQUIRE_HUMAN,       # posts comments / applies labels
    "prj-implement-fix": Decision.REQUIRE_HUMAN,  # opens PR
    "prj-report-daily": Decision.REQUIRE_HUMAN,   # sends email
}


# Command patterns that are always safe to run, regardless of skill context.
SAFE_COMMAND_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p) for p in (
        r"^gh\s+(pr|api|repo)\s+(view|list|diff|status)\b",
        r"^gh\s+api\s+rate_limit\b",
        r"^git\s+(status|diff|log|rev-parse|branch|show)\b",
        r"^python3?\s+.*--(dry-run|select-only)\b",
        r"^cat\s+.*state\.json$",
        r"^ls\b",
    )
)

# Command patterns that the daemon will always block — these are the
# never-cross-the-line operations.
NEVER_ALLOW_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p) for p in (
        r".*\bstate\.json.*>\s*",            # any > redirection into state.json
        r"^echo\s+.+>\s*.*state\.json",
        r"^rm\s+-rf?\s+.*/_bmad-output\b",   # daemon must not wipe state
        r"^git\s+push\s+--force",
    )
)

# Command patterns that mutate GitHub from the agent's perspective.
GITHUB_MUTATION_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p) for p in (
        r"^gh\s+pr\s+(comment|review|edit|close|merge|ready)\b",
        r"^gh\s+pr\s+create\b",
        r"^gh\s+label\b",
        r"^gh\s+issue\s+(comment|create|close|edit|label)\b",
        r"^gh\s+api\s+repos/.+\s+-X\s+(POST|PUT|PATCH|DELETE)",
    )
)


@dataclass(frozen=True)
class ApprovalRequest:
    """One request flowing through the policy engine.

    `kind` shapes the policy:
        - 'skill'    : pre-flight check for an upcoming phase skill turn
        - 'command'  : a `command/exec` request from app-server
        - 'github'   : an explicit GitHub-mutation handle
        - 'state'    : an attempt to write PR Jangler state.json (always denied)
    """

    approval_id: str
    kind: str
    payload: dict
    run_id: str = ""

    @classmethod
    def for_skill(cls, run_id: str, skill: str, *, pr: int | None) -> "ApprovalRequest":
        return cls(
            approval_id=f"appr_{uuid.uuid4().hex[:12]}",
            kind="skill",
            run_id=run_id,
            payload={"skill": skill, "pr": pr},
        )

    @classmethod
    def for_command(cls, run_id: str, command: str) -> "ApprovalRequest":
        return cls(
            approval_id=f"appr_{uuid.uuid4().hex[:12]}",
            kind="command",
            run_id=run_id,
            payload={"command": command},
        )


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: str
    request: ApprovalRequest = field(default_factory=lambda: ApprovalRequest("", "", {}))


@dataclass(frozen=True)
class PolicyEngine:
    """Stateless decision-maker. Combine with the store for audit trails."""

    approval_mode: str = "unlessTrusted"
    extra_safe_commands: tuple[re.Pattern, ...] = ()
    session_approved_kinds: frozenset[str] = frozenset()

    def evaluate(self, request: ApprovalRequest) -> PolicyResult:
        """Return the policy decision for the given request."""
        if request.kind == "skill":
            return self._evaluate_skill(request)
        if request.kind == "command":
            return self._evaluate_command(request)
        if request.kind == "state":
            return PolicyResult(
                decision=Decision.AUTO_DENY,
                reason="direct state.json writes by the agent are never allowed",
                request=request,
            )
        if request.kind == "github":
            return self._evaluate_github(request)
        return PolicyResult(
            decision=Decision.REQUIRE_HUMAN,
            reason=f"unknown request kind: {request.kind!r}; defaulting to human gate",
            request=request,
        )

    def _evaluate_skill(self, request: ApprovalRequest) -> PolicyResult:
        skill = request.payload.get("skill")
        if skill is None:
            return PolicyResult(Decision.AUTO_DENY, "skill request missing skill name", request)
        risk = SKILL_RISK.get(skill)
        if risk is None:
            return PolicyResult(
                Decision.REQUIRE_HUMAN,
                f"unknown skill {skill!r}; defaulting to human gate",
                request,
            )
        if self.approval_mode == "always":
            return PolicyResult(Decision.REQUIRE_HUMAN, "approval_mode=always", request)
        if risk is Decision.AUTO_APPROVE and self.approval_mode == "neverInteractive":
            return PolicyResult(Decision.AUTO_APPROVE, f"skill {skill!r} is safe", request)
        return PolicyResult(risk, f"skill {skill!r} default policy", request)

    def _evaluate_command(self, request: ApprovalRequest) -> PolicyResult:
        cmd = (request.payload.get("command") or "").strip()
        if not cmd:
            return PolicyResult(Decision.AUTO_DENY, "empty command", request)
        for p in NEVER_ALLOW_PATTERNS:
            if p.search(cmd):
                return PolicyResult(
                    Decision.AUTO_DENY,
                    f"command matches never-allow pattern: {p.pattern}",
                    request,
                )
        for p in (*SAFE_COMMAND_PATTERNS, *self.extra_safe_commands):
            if p.search(cmd):
                return PolicyResult(
                    Decision.AUTO_APPROVE,
                    f"command matches safe pattern: {p.pattern}",
                    request,
                )
        for p in GITHUB_MUTATION_PATTERNS:
            if p.search(cmd):
                if "github_mutation" in self.session_approved_kinds:
                    return PolicyResult(
                        Decision.AUTO_APPROVE,
                        "github mutation approved for this session",
                        request,
                    )
                return PolicyResult(
                    Decision.REQUIRE_HUMAN,
                    "GitHub-mutating command needs human approval",
                    request,
                )
        if self.approval_mode == "neverInteractive":
            return PolicyResult(
                Decision.AUTO_DENY,
                "unlisted command and approval_mode=neverInteractive",
                request,
            )
        return PolicyResult(Decision.REQUIRE_HUMAN, "unlisted command", request)

    def _evaluate_github(self, request: ApprovalRequest) -> PolicyResult:
        kind = request.payload.get("operation", "")
        if "github_mutation" in self.session_approved_kinds:
            return PolicyResult(Decision.AUTO_APPROVE, f"session-approved github op {kind!r}", request)
        return PolicyResult(Decision.REQUIRE_HUMAN, f"github op {kind!r}", request)

    def with_session_approvals(self, kinds: Iterable[str]) -> "PolicyEngine":
        return PolicyEngine(
            approval_mode=self.approval_mode,
            extra_safe_commands=self.extra_safe_commands,
            session_approved_kinds=frozenset({*self.session_approved_kinds, *kinds}),
        )
