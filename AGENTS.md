## Public repository rules

This repository will be public. Treat every file, commit message, PR description and Issue comment as published.

- Never write the name of a real customer or partner, their product images or creative, real Workspace, account or ad-account IDs, or acceptance evidence and screenshots from a production environment.
- Tests, fixtures and documentation examples use fictional brands only: our own brand is Acme Charge, competitors are Brightvolt, Northpeak and Kestrel Power, and every domain is `*.example`.
- Real run records and customer material go to the local `data/` directory (gitignored) or the private `marketai-ops` repository.
- The `customer-denylist` workflow (`scripts/ci/customer_denylist.py`) fails a PR or main push whose added lines or new file names match the customer denylist, reporting `file:line` and a rule number only. The list lives only in the `CUSTOMER_DENYLIST` secret: never copy it, or any term from it, into the repository, a commit, a PR or an Issue. Scanner tests use made-up terms.

## Agent skills

### Issue tracker

Issues and specs are tracked in GitHub Issues for `Raybbish/marketai`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five canonical Matt Pocock triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. See `docs/agents/domain.md`.
