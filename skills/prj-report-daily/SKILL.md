---
name: prj-report-daily
description: PR Jangler daily email reporter. Use when the orchestrator dispatches the daily report at the configured local hour or when the user runs 'prj-report-daily' for manual debug.
---

# prj-report-daily

## Overview

This skill is the daily reporter for the PR Jangler module. One invocation aggregates the persisted queue at `{project-root}/_bmad-output/pr-workflow/state.json`, every per-PR cache under `{project-root}/_bmad-output/pr-workflow/prs/`, and the last 24 hours of run-log lines; renders a multipart email (HTML with inline CSS plus a plain-text fallback); resolves SMTP credentials from 1Password via `op read`; sends through `prj_smtp_host` on `prj_smtp_port` with STARTTLS; archives the rendered body to `{project-root}/_bmad-output/pr-workflow/reports/{YYYY-MM-DD}.md`; and updates `state.last_report_sent` on success.

Act as a calm, no-bury operations summarizer. PleaseAdvise rises first. "Needs your eyes" is capped at 5 with an overflow pointer to the archive. If nothing happened, send the brief "all quiet" liveness email rather than skip; the user must hear from the bot every day.

## Conventions

- Bare paths (e.g. `scripts/run.py`) resolve from the skill root.
- `{project-root}/...` resolves from the project working directory.
- Configuration variables live under `[modules.prj]` in `{project-root}/_bmad/config.toml`.
- All state I/O goes through `prj-orchestrator`'s `state_io` module (imported via `sys.path`); this skill never touches `state.json` directly.

## On Activation

Load configuration from `{project-root}/_bmad/config.toml` (the `[modules.prj]` block). Required keys:

- `prj_repo` (used in the subject + header for context)
- `prj_email_to`, `prj_email_from`
- `prj_smtp_host`, `prj_smtp_port`
- `prj_smtp_creds_ref` (1Password reference, resolved via `op read` for `username` and `password` fields)

If any required key is missing, exit with status `misconfigured`, log it, and do not touch SMTP. The runlog entry names the missing key.

Execute the report:

```bash
python3 scripts/run.py
```

That single script handles the full pipeline: aggregate, render, resolve creds, send, archive, persist `last_report_sent`, append run-log. Flags:

- `--dry-run` render and archive locally but skip SMTP and do not update `last_report_sent`
- `--verbose` emit progress diagnostics to stderr
- `--project-root <path>` override autodetect
- `--now <iso>` override "now" for deterministic testing/replay

See `python3 scripts/run.py --help` for full detail.

## Architecture

Six Python modules, each with a single concern. The LLM activation context exists so the orchestrator's Skill-tool dispatch resolves cleanly when the daily-report cadence fires.

| Concern | Lives in |
| --- | --- |
| Shared state I/O (atomic load, validate, save, runlog) | `state_io` (imported from `prj-orchestrator/scripts`) |
| Aggregation: group PRs by status; build the shortlist; detect all-quiet | `scripts/aggregate.py` |
| Rendering: subject + HTML + plain-text from `assets/email-template.html` | `scripts/render.py` |
| Credential resolution: `op read` subprocess for `prj_smtp_creds_ref` | `scripts/creds.py` |
| SMTP send: stdlib `smtplib` with STARTTLS + retry counter | `scripts/smtp_send.py` |
| Archive write: markdown wrapper around HTML + plain-text under `reports/` | `scripts/archive.py` |
| Orchestration | `scripts/run.py` |

## State and Files Touched

This skill writes to:

- `{project-root}/_bmad-output/pr-workflow/state.json` (only `last_report_sent` and `report_send_failures`)
- `{project-root}/_bmad-output/pr-workflow/reports/{YYYY-MM-DD}.md` (frozen archive of the rendered report)
- `{project-root}/_bmad-output/pr-workflow/logs/{YYYY-MM-DD}.jsonl` (append-only run-log)

This skill reads from:

- `{project-root}/_bmad/config.toml`
- `{project-root}/_bmad-output/pr-workflow/state.json`
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/` (review.md, decisions.log, verification.md, fix-plan.md, adversarial.md, implementation.md, meta.json)
- `{project-root}/_bmad-output/pr-workflow/logs/*.jsonl` (last 24 hours)

## Aggregation Rules

Codified in `scripts/aggregate.py`; summarized here for activation review.

- `needs-attention`: PRs in `PleaseAdvise` plus PRs in `FixImpl` (an automated fix-PR was opened and is awaiting human eyes). Sorted by priority then by oldest `phase_entered_at`.
- `in-progress`: any PR in a non-terminal, non-`PleaseAdvise` phase (`Discovered`, `Triaged`, `OverlapCheck`, `ReviewPending`, `Reviewed`, `CommentsTriage`, `ClaimVerify`, `FixPlan`, `AdversarialCheck`).
- `resolved-today`: PRs whose phase transitioned to a terminal phase (`ReadyToMerge`, `Blocked`, `Rejected`, `Archived`) within the last 24 hours, sourced from `last_action_at`.
- `no-change`: PRs whose `last_action_at` is older than 24 hours and which are not already classified above. Count only (no per-PR detail in the email).
- "Needs your eyes" shortlist: top 5 across `needs-attention`. Overflow rendered as "+ N more, see archive."
- All-quiet: no `needs-attention`, no `resolved-today`, no `in-progress` activity in the last 24 hours (zero run-log entries with `action=dispatch` for per-PR skills). When all-quiet, the subject becomes `[PR Jangler] {date}: all quiet` and the body is a one-paragraph liveness confirmation.

## Rendering Rules

- Inline CSS only. No `<link>`, no `<script>`, no external assets.
- HTML body is built by `string.Template.substitute` against `assets/email-template.html`. No Jinja2.
- Plain-text fallback is built by the same renderer and attached to a `MIMEMultipart('alternative')` with the HTML.
- After substitution, the renderer asserts no `$` placeholder remains; an exception raises rather than send a broken email.
- Subject template (default): `[PR Jangler] {date}: {n} need attention, {m} in progress`. All-quiet override: `[PR Jangler] {date}: all quiet`.

## Sending Rules

- Credentials: `op read "{prj_smtp_creds_ref}/username"` and `op read "{prj_smtp_creds_ref}/password"`. Failures raise `CredsError` with a clear message; the runlog records `status: "creds-error"` and the script exits 4.
- Transport: `smtplib.SMTP(host, port)`, `starttls()`, `login(user, pass)`, `sendmail(from, [to], message.as_string())`, `quit()`.
- On SMTP failure: append a runlog entry with `status: "smtp-error"` and increment `state["report_send_failures"]` (initialized to 0 if absent). `last_report_sent` is NOT updated. The script exits 5. The next heartbeat will retry. After 3 consecutive failures, the script also injects a synthetic system entry into `state["prs"]["0"]` (the digits-key sentinel; `pr_number: 0`) at phase `PleaseAdvise`, surfacing the failure to the user via the next successful report.

## Persistence Rules

- Archive is written before SMTP send so a failed send still leaves a forensic copy.
- `state.last_report_sent` is updated ONLY after `smtplib` returns without raising. On retry the next heartbeat re-renders against the latest state (so the report content always reflects current reality, not a stale snapshot).
- `report_send_failures` counter resets to 0 on successful send and on the synthetic `__system__` entry being created.

## Non-Negotiables

- **Never skip the daily email.** If nothing happened, send the all-quiet liveness email.
- **No buried critical items.** PleaseAdvise rises to the top of the shortlist; the shortlist sits above all groupings in the body.
- **Inline CSS only.** No external assets in the email body.
- **Atomic state writes via `state_io.save_state`.** Direct writes to `state.json` are forbidden.

## Verification

Smoke-test against a configured project (does not require SMTP; uses `--dry-run`):

```bash
python3 scripts/run.py --dry-run --verbose
```

Expected: exit 0, runlog entry with `status: "dry-run"`, archive file present at `reports/{YYYY-MM-DD}.md`, `state.last_report_sent` UNCHANGED.

Run the unit tests (no SMTP, no `op` calls; all subprocess invocations mocked):

```bash
python3 -m unittest discover scripts/tests
```
