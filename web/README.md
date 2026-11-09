# MarketAI control plane

Next.js SaaS control plane: authentication, Workspace (`Team`) records,
membership, invitations and account administration. It is a **hardened,
curated fork** of the [BoxyHQ SaaS Starter Kit](https://github.com/boxyhq/saas-starter-kit)
pinned in [`UPSTREAM.json`](UPSTREAM.json).

- Customer walkthrough (54 steps, 68 step-by-step screenshots): [HTML 图文教程](../docs/tutorials/marketai-tutorial.html)
  ([download/open instructions](../docs/tutorials/README.md), [text reference](../docs/user-guide.md))
- Project overview and current validation scope: [MarketAI README](../README.md)
- Documentation index: [docs/README.md](../docs/README.md)
- What changed relative to upstream: [`HARDENING.md`](HARDENING.md)
- Boundary, roles and the controlled update procedure:
  [`../docs/architecture/control-plane-fork-boundary.md`](../docs/architecture/control-plane-fork-boundary.md)
- Dependency and licence inventory: [`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md)

## Current product entry points

After login, invitation acceptance, Workspace selection or MFA setup, the
default entry is **Marketing Tests**, `/w/{slug}/experiments`. It hosts product
understanding, AI-drafted test plans, generation, AI QC, feedback revisions,
human approval, private Meta exports, CSV reporting and evidence-scoped results.
Historical experiments remain at `/w/{slug}/legacy-experiments`.

`/w/{slug}/generate` remains a supporting single-creative composer. Marketing
tests select frozen authoritative product photos and prepare their own linked
drafts; do not infer that all supporting composers have the same reference UI.

Owners manage OpenAI, Gemini and Seedream keys in **Settings → Provider
credentials** (`/w/{slug}/settings`). Mutations require MFA; saving a key does
not call the provider or verify authentication. Generation also needs valid
entitlements, a USD Spend Guardrail and a configured model cost ceiling.
Planning/result interpretation and visual QC have separate deployment settings;
see [development configuration](../docs/development.md#新版-ai-配置).

These pages issue authenticated BFF commands to the Domain API; they do not own
marketing state or execute AI calls themselves. Quality approval is not business
qualification. The marketing handoff exports files for manual Meta operations;
it does not publish ads. Gemini/Seedream live reference calls and genuine Meta
business outcomes still require their own evidence.

## What this service must never do

It does not connect to the MarketAI domain database, implement domain rules, or
perform external side effects. FastAPI and the Worker own creative-experiment
domain state, jobs, cost and audit; the control plane reaches them over HTTP
only. Prisma is configured with the control-plane datasource exclusively.

## Run it

```bash
cd web
cp .env.example .env                  # NEXTAUTH_SECRET + DATABASE_URL (runtime role)
cp .env.migrate.example .env.migrate  # MIGRATION_DATABASE_URL (schema owner)
npm ci

# Migration step: the only place the schema-owner credential is loaded.
# The subshell matters — `set +a` stops *further* exports, it does not unexport
# what was already exported, so without the parentheses the owner credential
# stays in the shell that then starts the server.
(set -a; . ./.env.migrate; set +a; npm run db:migrate:deploy)

npm run dev                           # runs with DATABASE_URL only
```

Two database identities against the same control-plane database, kept in
separate files on purpose. `MIGRATION_DATABASE_URL` owns the schema and is the
only identity that runs DDL; `DATABASE_URL` is the runtime role and has no DDL
privileges. `web/.env` is loaded automatically by Next.js and therefore becomes
the running process's environment, so the schema-owner credential must not live
there — in deployment it is injected into the migration job alone. Both roles
are provisioned by `deploy/postgres/control-plane-domain-roles.sql`, and
`npm run db:migrate:*` refuses to start without `MIGRATION_DATABASE_URL` rather
than silently retrying as the runtime role.

`GET /api/health` is liveness. `GET /api/readiness` reports configuration and
control-plane database health.

## Verify it

```bash
npm run verify                # format, lint, types, unit tests, production build
npm run inventory:licenses:check
```

The database-role isolation suite (`npm run test:db-isolation`) skips unless
`CONTROL_PLANE_DATABASE_URL`, `CONTROL_ROLE_ON_DOMAIN_DB_URL` and
`DOMAIN_ROLE_ON_CONTROL_DB_URL` point at a cluster provisioned with
`deploy/postgres/control-plane-domain-roles.sql`.

## Interface languages

The interface ships in `en` (the source language of every key, served without
a prefix) and `zh-CN` (under `/zh-CN/...`). A page's language is the
signed-in account's saved preference (`User.locale`), else the `NEXT_LOCALE`
cookie, else the browser's `Accept-Language`; `middleware.ts` applies it and
the top-bar switch and account settings change it. The strategy language of
new plans follows the interface language; the ad language is a plan field
(ADR 0013). AI quality-check findings and result explanations are written in
the round's strategy language (the interface language only for older plans
without one); a record written in another language is labelled, never
translated.

Every key exists in both `locales/en/<namespace>.json` and
`locales/zh-CN/<namespace>.json`: add or rename a key in both in the same
change. `__tests__/locales/` fails the build when the key sets differ, when an
English value contains CJK text, or when components, pages, lib or hooks hold
a CJK literal; `npm run i18n:missing` lists every gap at once.

Server errors render by code, never by their text. A Problem Details body
carries a stable `code` and, when the sentence needs numbers, `params`;
`detail` is a diagnostic for logs. `errorMessage(t, problem)` in
`lib/i18n/domain-errors.ts` renders `common:error-<code>` with the params (a
failed validation reads as its first field's reason), and a job or record's
`error_code` goes through `errorCodeMessage` from the same table. A new code
needs `error-<code>` in both `locales/en/common.json` and
`locales/zh-CN/common.json`: `__tests__/locales/error-codes.spec.ts` scans
`app/**` and the BFF for every code and fails on a missing one, and in Jest a
component that renders a code without copy fails its test.

## Provision a Workspace

Customers cannot create Workspaces in the invite-only Beta; both `POST
/api/teams` and the Workspace-creating branch of `POST /api/auth/join` fail
closed. Provisioning is an operator action with database access:

```bash
npm run ops:provision-workspace -- --name "Acme" --owner-email owner@acme.test \
  --operator-user-id USER_ID --beta-plan-version-id BETA_PLAN_VERSION_ID
```

Provisioning snapshots are delivered independently through the durable outbox.
Run the dispatcher continuously or on a short schedule. Invitation acceptance,
MFA confirmation, and every dispatcher cycle all retry the same idempotent
activation transition; a cycle scans a bounded ready batch even when it claims
no new delivery rows. Manual reconciliation is reserved for a genuinely stuck
repair case: the operator access boundary supplies a short-lived signed
`OPERATOR_IDENTITY_TOKEN`, and two distinct verified subjects must approve
before execution:

```bash
npm run ops:dispatch-entitlements
OPERATOR_IDENTITY_TOKEN="$ALICE_OPERATOR_TOKEN" npm run ops:reconcile-workspace-provisioning -- \
  request --slug acme --case-id CASE-12345 --reason "Snapshot delivery recovered"
OPERATOR_IDENTITY_TOKEN="$ALICE_OPERATOR_TOKEN" npm run ops:reconcile-workspace-provisioning -- \
  approve --case-id CASE-12345
OPERATOR_IDENTITY_TOKEN="$BOB_OPERATOR_TOKEN" npm run ops:reconcile-workspace-provisioning -- \
  approve --case-id CASE-12345
```

Expired or revoked first-Owner invitations use the same request/dual-approval
shape through `ops:reissue-first-owner`. The operator access boundary signs
five-minute ES256 assertions; the CLI receives only its public key set through
`OPERATOR_IDENTITY_JWKS`. Issuer private keys and free-form operator identifiers
are never accepted by the control-plane runtime.

## Scheduled maintenance

Expired session rows are pruned when the account is next seen, but a dormant
account's rows need a sweep. Run nightly:

```bash
npm run ops:prune-sessions
```

Run `npm run ops:dispatch-entitlements` on a short interval. Concurrent
instances are safe: rows are claimed with leases and stale settlement is a
no-op; ready provisioning Workspaces are also activated under a per-Workspace
row lock. Retryable failures remain durable; rejected or exhausted records move
to the dead-letter state and emit structured errors.

The operator scripts run compiled JavaScript from `.ops/`, emitted by
`npm run build`, so a production install (`npm ci --omit=dev`) can execute them
without a TypeScript runtime. They load `web/.env` the same way `next start`
does, so a checkout needs no extra setup. In a container the scheduler supplies the runtime identity and
nothing else — real environment variables take precedence over the file:

```yaml
# CronJob contract: the runtime role only, never the schema owner.
env:
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef: { name: marketai-control-plane, key: database-url }
command: ['npm', 'run', 'ops:prune-sessions']
```

It exits non-zero with a redacted message if the database is unreachable, and
refuses to start if `DATABASE_URL` is absent.

## Release

`npm run build` runs `prisma generate && next build && npm run build:ops`. The
last step compiles the operator scripts in `scripts/` to `.ops/` (see
[Scheduled maintenance](#scheduled-maintenance)). Nothing in the build path
reaches a database or mutates a schema: schema changes are applied as a
separate, reviewed release step:

```bash
(set -a; . ./.env.migrate; set +a; npm run db:migrate:status)
(set -a; . ./.env.migrate; set +a; npm run db:migrate:deploy)
```
