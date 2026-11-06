# Claude Code workflow

Work from one GitHub Issue at a time.

For v9 implementation, use [#14](https://github.com/Raybbish/marketai/issues/14) as the dependency map and select a leaf carrying `ready-for-agent`.

1. Read the full ticket and comments with `gh issue view <number> --comments`. Start only when it has `ready-for-agent` and every `Blocked by` issue is closed; then assign it to yourself.
2. Read `CONTEXT.md` and the contract files linked by the ticket. Contracts and ADRs are the source of truth; if code and contract conflict, report the conflict on the Issue before changing the contract.
3. Create `claude/issue-<number>-<slug>` from current `main`. Keep the diff inside the ticket scope and preserve unrelated user changes.
4. Deliver a vertical slice: migration, domain behavior, API/Worker path, authorization, observability and automated evidence required by the ticket. Add negative tenant and permission tests whenever data or commands are Workspace-scoped.
5. Run `make verify` plus every ticket-specific command. Open a PR that references the Issue and records migrations, test evidence, operational impact and remaining risks. The Issue remains open until the PR is merged.

## Public repository rules

This repository will be public. Treat every file, commit message, PR description and Issue comment as published.

- Never write the name of a real customer or partner, their product images or creative, real Workspace, account or ad-account IDs, or acceptance evidence and screenshots from a production environment.
- Tests, fixtures and documentation examples use fictional brands only: our own brand is Acme Charge, competitors are Brightvolt, Northpeak and Kestrel Power, and every domain is `*.example`.
- Real run records and customer material go to the local `data/` directory (gitignored) or the private `marketai-ops` repository.
- The `customer-denylist` workflow (`scripts/ci/customer_denylist.py`) fails a PR or main push whose added lines or new file names match the customer denylist, reporting `file:line` and a rule number only. The list lives only in the `CUSTOMER_DENYLIST` secret: never copy it, or any term from it, into the repository, a commit, a PR or an Issue. Scanner tests use made-up terms.

## Invariants

- The control plane owns identity, Workspace membership, invitations, subscriptions and administration. FastAPI and Worker exclusively own MarketAI domain data, rules, jobs, audit and external side effects.
- FastAPI derives Workspace and actor identity from verified BFF claims. Domain services receive an explicit TenantContext; `DEFAULT_WORKSPACE_ID` is not a tenant boundary.
- Production tenancy uses PostgreSQL RLS and separate database roles. SQLite remains a local compatibility path only where the ticket explicitly preserves it.
- Persist immutable versions and append-only evidence for historical decisions. Current state is a projection, not rewritten history.
- External or paid calls use durable jobs, idempotency, cost reservation and recoverable evidence. Controlled Mock is explicit and cannot mix with real evidence.
- Meta write access remains disabled in development. `META_WRITE_ACCESS_TOKEN` stays empty, and Scale never performs Meta writes.

## Contract routing

- SaaS identity, BFF, tenancy or file access: `docs/architecture/saas-ui-fastapi-integration-contract.md` and ADRs 0002–0004. MFA is checked only at login (ADR 0015); protected operations check role only.
- Control-plane fork, deployment-unit boundary or upstream updates: `docs/architecture/control-plane-fork-boundary.md` and `web/HARDENING.md`.
- Entitlements, BYOK, usage or cost: `docs/architecture/subscription-entitlement-usage-cost-ownership.md` and ADR 0005.
- Generation, Assets or six-area completion: `docs/product/v9-functional-alignment-acceptance.md`.
- Creative Library: `docs/product/creative-library-ownership-and-reuse-contract.md` and ADR 0007.
- Localization: `docs/product/localization-truth-package-contract.md` and ADR 0006.
- Scale: `docs/product/scale-advice-semantics-contract.md` and ADR 0008.
- Front flow (two-config home, product intake, test-plan drafting, external-asset tagging): `docs/product/front-flow-two-config-contract.md` and ADR 0011.
- Competitor ad scan (竞品扫描: competitor list and Meta page confirmation, paid scans, observations, assessments, snapshots, planner advisories): `docs/product/competitor-scan-spec.md`, ADR 0014 and Epic [#265](https://github.com/heart-cannot-beat/marketai-archive/issues/265) (slices; release record and rollback in `docs/product/ai-marketing-production-release.md`).
