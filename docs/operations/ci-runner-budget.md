# CI runner budget

MarketAI selects its CI runners before starting the seven existing test jobs.
The preflight job always uses standard GitHub-hosted `ubuntu-24.04`, and all
existing test gates and job names are preserved. GitHub fallback gets twice the
original Blacksmith job timeouts for its different hardware (40-minute backend
and web, 60-minute golden path). Blacksmith keeps those timeouts, except that
the backend `verify` job has 30 minutes (raised from 20 by #375).
The golden-path project's UI assertions get a bounded 30-second wait on GitHub:
the existing MFA flow hashes ten recovery codes and provisions the Workspace
before returning, and its request was still pending at the old 5-second cutoff
on this runner. Authentication, hash strength, assertions and retry counts stay
the same; Blacksmith keeps its original 5-second assertion wait.

## Admission

Blacksmith grants this organization 3,000 **2-vCPU Linux x64 equivalent minutes**
per month. The guard admits a run only when all of these are true:

- `BLACKSMITH_FORCE_GITHUB` is unset, empty, `false`, or `0`.
- The repository has an approved `BLACKSMITH_USAGE_TOKEN` Actions secret.
- The pinned official Blacksmith CLI authenticates and returns a recognized,
  finite, nonnegative organization-wide monthly aggregate of `billing_minutes`.
- GitHub returns complete listings of concurrent runs and recent runs.
- Reported usage plus **210 equivalent minutes for this entire run**, plus
  **210 for each attempt of other active or recently created CI runs**, is
  **below 2,400**. Previous attempts of the current run are reserved as well.

The remaining 600 minutes are deliberately not allocated. Each 210-minute
reservation covers the full existing Blacksmith job timeouts, vCPU weighting,
and five extra provisioning minutes per weighted job. A regression check must
continue to prove that it covers future workflow changes. Recently created runs
are conservatively reserved even if they have already finished or used GitHub,
to accommodate billing ingestion lag. A run at the threshold selects GitHub.

CLI 0.5.3's `usage --help` defines `billing_minutes` in normalized **vCPU minutes**:
one minute on a 2-vCPU x64 machine counts as two, not one. Divide its aggregate
by two before comparing with the 3,000-minute grant. For example, 3,390 CLI
billing minutes are 1,695 free-tier equivalent minutes. Never substitute
`runtime_minutes`, raw `billable_minutes`, a single repository's rows, or a
missing value interpreted as zero. The CLI query is unfiltered by repository
and covers UTC month start through the current time. A month boundary during
the check selects GitHub; the next run makes a fresh monthly query.

Missing credentials, API failures, timeouts, checksum changes, unknown CLI JSON,
truncated run lists and malformed data all select GitHub. The workflow does not
reuse cached usage. The selected provider and reason appear in its job summary.
The CLI binary is SHA-256 pinned with automatic updates disabled. If the official
`latest` binary changes, the checksum mismatch intentionally selects GitHub until
the new version and its output contract have been reviewed.

## Activation and emergency fallback

Without a secret, CI is already safe to run: **every job selects GitHub**.
Do not add an unapproved human CLI credential or browser cookie. Blacksmith's
organization token has additional cache and Testbox management permissions;
creating one requires the account owner's explicit approval.

After approval, use the official CLI's `org-token create`, supply the token to
`gh secret set BLACKSMITH_USAGE_TOKEN --repo heart-cannot-beat/marketai` through
standard input, and keep it out of command arguments, logs and checked-in files.
Before enabling Blacksmith, verify a live organization query against the dashboard
and the parser, including the 2-vCPU conversion. An unknown output schema must
remain a GitHub fallback until its aggregate has been verified. Fork PRs that do
not receive the secret use GitHub automatically.

To force GitHub for the next preflight, regardless of remaining Blacksmith credit:

```sh
gh variable set BLACKSMITH_FORCE_GITHUB --body true --repo heart-cannot-beat/marketai
```

Set it to `false` or delete it only after confirming the Blacksmith source and
available headroom. This does not migrate jobs that have already been scheduled.

## Scope and billing limits

This guard controls **new runs of this version of `ci.yml`**. Rebase older PR
branches to obtain it. Re-running a historical workflow uses that historical
workflow, not the new guard. Other workflows currently already use GitHub.

The organization-wide usage query includes other repositories' recorded spend,
but concurrent-run reservations cover this repository's CI. New Blacksmith
workflows, another repository, a very delayed billing report, or an old unguarded
workflow can spend outside this admission policy. A provider-enforced cap or
disabling those workloads is needed for an absolute organization-wide guarantee.
Sticky disks, Docker-layer storage, static IPs and agent usage are separate costs;
the runner selector neither enables nor deletes these resources.

Standard GitHub runners can consume the private organization's own allowance.
The existing organization Actions budget was verified on 2026-11-04 as **$0 with
`prevent_further_usage=true`**. Preserve it. If its allowance is exhausted, CI can
stop rather than incur paid GitHub usage; this change does not raise that budget.

References: [Blacksmith CLI usage](https://docs.blacksmith.sh/blacksmith-cli/usage),
[token permissions](https://docs.blacksmith.sh/blacksmith-cli/overview#token-types),
[free-tier conversion](https://docs.blacksmith.sh/blacksmith-runners/overview#faq),
[GitHub budgets](https://docs.github.com/en/billing/concepts/budgets-and-alerts).
