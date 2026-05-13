# Semantic Comparison Prompt — prj-detect-overlap

You are reviewing two open pull requests against the same repository that touch overlapping files. Decide the relationship between them. Pick exactly one verdict and write one paragraph of rationale.

## Variables

The agent invoking this prompt fills these in before reading:

- `{{ target_pr_number }}` — number of the target PR (the one currently being checked)
- `{{ target_pr_title }}` — title of the target PR
- `{{ target_pr_body }}` — body text of the target PR (may be empty)
- `{{ target_pr_diff }}` — full diff of the target PR (or a `gh pr diff` truncation noted explicitly)
- `{{ other_pr_number }}` — number of the other PR being compared
- `{{ other_pr_title }}` — title of the other PR
- `{{ other_pr_body }}` — body of the other PR (may be empty)
- `{{ other_pr_diff }}` — full diff of the other PR
- `{{ shared_files }}` — comma-separated list of files both PRs touch
- `{{ overlap_count }}` — integer count of shared files

## Verdict Definitions

Pick exactly one. These are mutually exclusive.

- **`independent`** — the two diffs touch the same files but address unrelated concerns. They could land in either order without affecting each other's correctness. Default to this when the relationship is unclear.
- **`conflicting`** — the two diffs disagree on the same lines, the same contracts, or pursue mutually exclusive approaches to the same problem. Merging both as-is would either fail to merge or produce broken behavior.
- **`complementary`** — the two diffs build on each other; one logically lands first and the other depends on or extends it. They are not redundant, but they are not orthogonal either.
- **`redundant`** — the two diffs do substantively the same thing on substantively the same surface. One of them should be closed in favor of the other.

## Conservatism Rules

- When in doubt between `independent` and `complementary`, pick `independent`.
- When in doubt between `complementary` and `conflicting`, pick `complementary` (a dependency, not a contradiction).
- `redundant` requires near-identical intent (same feature, same fix) on near-identical surface (mostly the same files, mostly the same lines). If either side adds something the other lacks, pick `complementary` instead.
- A single shared file with a single overlapping line is rarely `conflicting` unless that line is a contract (interface signature, exported constant, schema field). Tiebreak: `independent`.

## Output Format

Respond with exactly two lines, no extra prose, no fenced block:

```
verdict: <independent|conflicting|complementary|redundant>
rationale: <one paragraph, 1-4 sentences, no line breaks>
```

The CLI parses this format. Anything else breaks the pipeline.

## What to Read First

1. The `{{ shared_files }}` list — these are the high-signal anchors.
2. The hunks in each diff that touch those files.
3. The PR titles and bodies — intent matters even when diffs look similar.
4. Non-shared file changes — they often reveal that the two PRs are doing different things despite overlap.

## What Not to Do

- Do not invent a fifth verdict. Pick one of the four.
- Do not split the verdict across multiple categories. The CLI parses exactly one.
- Do not infer maintainer preference. That is the decision skill's job, not yours.
- Do not include code blocks or markdown in your rationale. One plain paragraph.
