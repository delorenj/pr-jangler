# Seed Triage Rubric

Initial rubric for `mcp-server-trello`. Loaded as reference by `prj-triage`. The Hindsight bank `prj` accumulates refinements after observation. Keep this rubric short and pattern-based; complex judgment calls should produce `needs-review` (PR mode) or `advisory` (comment mode), never auto-decisions.

## PR-Mode Classification

### Definite-no (any one triggers)

- README typo or formatting changes without context, justification, or linked issue
- Pure formatting/whitespace changes without behavior implications
- Dependency bumps without changelog summary or stated reason
- Adds new external dependency without architectural justification
- Removes existing tool or feature without deprecation path
- Edits files under `skill/` (generated artifacts; should edit `src/` per CLAUDE.md)
- Edits files under `build/` (gitignored build output)

### Possible-duplicate (any one triggers)

- Touches files already touched by other open PRs
- Title or description shares strong keyword overlap with another open PR
- Adds a tool with a name similar to an existing tool or another open PR's proposed tool

### Actionable (substantive changes)

- Adds a new MCP tool with a clear use case
- Fixes a reproducible bug accompanied by a test
- Refactors with clear architectural justification
- Adds test coverage to existing functionality
- Documents existing behavior that lacked docs

### Needs-review (catch-all)

Anything substantive that doesn't match a definite-no or possible-duplicate pattern but isn't obviously actionable from the PR description alone.

## Comment-Mode Classification

### Actionable

- Maintainer says "this should X" or "X is broken when Y"
- Contributor says "I tested this and it fails when Z"
- Code-block in comment showing reproduction steps
- Linked issue referenced as bug

### Advisory

- Opinion or aesthetic preference without reproduction steps
- "Have you considered..." style suggestions
- Praise or general feedback

### Noise

- Status check ("any update?")
- Bot-generated comments (CI status, dependabot)
- Off-topic discussion

## Author Weighting

- Maintainer comments: highest priority for actionable classification.
- First-time contributors: extra patience and welcome tone in any pushback.
- Returning contributors: standard weight.
- Bot accounts: classified as noise unless explicitly whitelisted.

## Override Mechanism

Maintainer applies any of the following labels to override agent classification on the next discover sweep: `prj/override:actionable`, `prj/override:definite-no`, `prj/override:duplicate`, `prj/override:needs-review`. The agent records overrides in Hindsight bank `prj` for rubric refinement.

## Phase-Transition Table (canonical)

| Mode | Classification | Next phase | Next action |
| --- | --- | --- | --- |
| pr | actionable | ReviewPending | `{skill: "prj-review", mode: null}` |
| pr | possible-duplicate | OverlapCheck | `{skill: "prj-detect-overlap", mode: null}` |
| pr | definite-no | Rejected | `null` (terminal) |
| pr | needs-review | ReviewPending | `{skill: "prj-review", mode: null}` |
| comment | actionable | ClaimVerify | `{skill: "prj-verify-claim", mode: null}` |
| comment | advisory | Reviewed | `null` |
| comment | noise | Reviewed | `null` |
