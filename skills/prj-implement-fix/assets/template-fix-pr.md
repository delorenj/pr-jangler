# [prj] Auto-fix for #{pr_number}: {summary}

This PR is opened by **PR Jangler** (the autonomous PR backlog workflow) against the contributor's branch.

It proposes the smallest patch that resolves a verified claim raised on #{pr_number}. The fix-plan was adversarially validated before this PR was opened.

## What this PR does

{summary}

## Why this fix is correct

{rationale}

## What could go wrong

{risks}

## Failing-test-now-passing proof

The new test exercises the failing path before the fix is applied. With the fix applied, the new test passes and the full test suite shows no regressions.

```text
{test_summary}
```

## Provenance

- Original PR: #{pr_number}
- Claim source: {claim_source}
- Fix-plan: `prs/{pr_number}/fix-plan.md`
- Adversarial verdict: `prs/{pr_number}/adversarial.md` (pass)
- Commit author: {bot_user}
- Generated: {generated_at}

## How to respond

- **Accept:** merge this PR into your branch. Your original PR will then carry the fix.
- **Decline:** close this PR. PR Jangler will record the close reason and learn from it.
- **Adjust:** push changes on top of `prj/auto-fix/{pr_number}-{slug}`; PR Jangler will not force-push or amend.

The original PR has been labelled `prj/fix-proposed`. Maintainers can override at any time with the `prj/override:*` labels.
