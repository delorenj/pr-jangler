# Heads up: this repo runs an automated PR concierge

This repository uses **PR Jangler**, an automated workflow that watches the PR
backlog and assists with triage, review, and fix proposals. If you open a PR
here, you may notice some or all of the following:

- **Labels prefixed `prj/`** appearing on your PR after a triage sweep. They
  reflect the bot's classification of your PR or its comments. They do not
  prevent a human from reviewing or merging your work; they are advisory.

- **A "Code Review" comment** authored by the bot, posted as a PR Review of
  type `Comment` (never `Approve` or `Request Changes`). Findings cite specific
  files and line numbers. Treat them as one reviewer's read, not a verdict.

- **A separate fix-PR opened against your branch** if the bot believes it can
  resolve a flagged issue with a minimal diff. The fix-PR includes a failing
  test that demonstrates the bug, the proposed fix, and an "if you don't want
  this, just close the PR" note. **You retain full control over your branch.**
  Merging the fix-PR is opt-in.

- **Polite clarifying questions** from the bot if reviewer comments reference
  behavior the bot cannot reproduce. These are not gotchas; they are the bot
  trying not to break working code based on a misread.

## What the bot will NOT do

- Push directly to your branch.
- Force-push anything.
- Close your PR on its own.
- Apply `Approve` or `Request Changes` reviews (only `Comment`-type reviews).
- Act on a fix request without first reproducing the alleged bug.

## Disagree with the bot?

Apply one of these labels and the bot will defer to you on the next sweep:

- `prj/override:actionable`
- `prj/override:definite-no`
- `prj/override:duplicate`
- `prj/override:needs-review`

For deeper disagreements (the bot keeps misjudging a class of PRs),
open an issue tagged `prj-rubric` so the underlying classification rubric
can be updated.

## Source

PR Jangler module source and architecture lives at:
`<link to your fork or the upstream repo here>`

The full plan and design rationale is at `skills/reports/module-plan-pr-backlog-workflow.md`.
