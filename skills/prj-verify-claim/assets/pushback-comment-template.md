<!--
Pushback comment template for prj-verify-claim.

Rendered by scripts/comment_post.py via str.Template (`$variable` substitution).
Required variables:
  $opening       - one-line greeting; varies by commenter role (first-timer / maintainer / contributor)
  $commenter     - GitHub login of the comment author (without leading @)
  $claim_excerpt - up to ~280 chars of the original claim, quoted
  $strategy      - one of: failing-test, existing-test, manual-exercise
  $worktree      - relative worktree path (e.g. _bmad-output/pr-workflow/worktrees/101/)
  $command       - exact command that was run inside the worktree
  $observation   - what the reproduction actually showed (one paragraph)
  $next_step     - what would unblock us (specific ask)
  $bot_signature - footer line identifying this as an automated reproduction attempt

Tone rules (do not break these):
  - Polite. No accusatory language.
  - Curious, not declarative. We are asking, not concluding.
  - Specific. Name the command, the worktree, the observed output.
  - Reversible. Make it easy for the commenter to send a counter-example.
-->

$opening

I attempted to reproduce the issue you raised:

> $claim_excerpt

Here is what I tried:

- **Strategy:** $strategy
- **Worktree:** `$worktree`
- **Command:** `$command`

**What I observed:** $observation

It is very possible I am running this in a different environment than you, or that I misread which behavior you meant. Before this PR moves forward I would love your help with the next step:

$next_step

Thanks for the report either way — even a "yeah that is what I meant, here is one more detail" is hugely useful.

---

$bot_signature
