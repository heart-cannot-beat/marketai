# Open-source SaaS control-plane selection

Date: 2026-08-25  
Decision owner: MarketAI architecture  
Scope: invite-only self-service Beta; Owner and Member roles; FastAPI/Worker remain the exclusive domain service

## Decision

Adopt a pinned fork of [BoxyHQ SaaS Starter Kit](https://github.com/boxyhq/saas-starter-kit) as MarketAI's SaaS control-plane and frontend-shell starting point, subject to the production gates below.

This is a source-code adoption, not approval to deploy the upstream starter unchanged. The fork owns only identity, Workspace membership, invitations, subscriptions, account administration, and the web shell. It must never write MarketAI domain tables.

The browser will use the control plane as a BFF. The BFF validates its own session and Workspace membership, then calls the private FastAPI domain service with a short-lived, asymmetric signed service JWT. FastAPI derives `user_id`, `workspace_id`, and role from verified claims rather than request parameters. The exact claims, revocation behavior, and secret lifecycle belong to the identity-boundary decision that follows this ticket.

## Why this option

BoxyHQ is the only compared candidate that already contains the generic multi-tenant capabilities MarketAI needs: team creation, membership, invitations, roles and permissions, team-scoped payments, API keys, audit-log and webhook integration, authentication, security headers, and Playwright tests. Its source is Apache-2.0 licensed, is not archived, and its default branch was still receiving dependency maintenance in May 2026. See the [feature list](https://github.com/boxyhq/saas-starter-kit#-features), [Prisma control-plane schema](https://github.com/boxyhq/saas-starter-kit/blob/main/prisma/schema.prisma), [permissions table](https://github.com/boxyhq/saas-starter-kit/blob/main/lib/permissions.ts), [package versions](https://github.com/boxyhq/saas-starter-kit/blob/main/package.json), [CI workflow](https://github.com/boxyhq/saas-starter-kit/blob/main/.github/workflows/main.yml), and [Apache-2.0 license](https://github.com/boxyhq/saas-starter-kit/blob/main/LICENSE).

Its data model also maps cleanly to the accepted Beta vocabulary: `Team` becomes the implementation name behind MarketAI `Workspace`; `TeamMember` becomes membership; and the fork exposes only `OWNER` and `MEMBER` in the first release. The existing upstream `ADMIN` role is not part of the customer-facing Beta contract.

## Comparison

| Criterion | BoxyHQ SaaS Starter Kit | Open SaaS | FastAPI template + Supabase |
| --- | --- | --- | --- |
| License and activity | Apache-2.0; not archived; current dependency maintenance, though latest tagged release is older | MIT; active in August 2026 | MIT projects; both actively maintained |
| Identity | NextAuth email/password, magic link, OAuth and optional enterprise SSO | Wasp full-stack auth | Supabase Auth or the FastAPI template's JWT auth |
| Workspace and roles | Team, membership, invitation and RBAC are present | No Workspace/team/membership entities in the shipped schema | Must be designed and built |
| Billing | Team-scoped Stripe checkout, portal and webhook persistence exist | Stripe, Polar and Lemon Squeezy adapters exist, but billing is user-scoped | Must be designed and built |
| Administration | Team settings and optional audit/webhook/SSO surfaces exist | Global user/admin dashboard exists | Global superuser dashboard exists; no Workspace administration |
| FastAPI boundary | Requires an explicit BFF/service-token adapter; good separation once added | Wasp expects its Node/Prisma operations to be the application backend | Supabase JWT/JWKS integrates well, but the composition still lacks most SaaS control-plane behavior |
| Duplicate-persistence risk | Low only when Prisma and SQLAlchemy use separate databases/schemas and DB roles | High: the template encourages product data and jobs in Wasp/Prisma | High if the second FastAPI backend is retained; moderate with Supabase-only auth |
| Upgrade strategy | Maintain a pinned fork and manually review upstream changes | Framework/template update path is documented, but Wasp becomes a major runtime dependency | Copier helps template generation; Supabase self-hosting adds a large operational surface |
| Fit for this decision | Best functional fit, with mandatory hardening | Strong single-user SaaS starter, weak multi-tenant fit | Strong components, not a complete SaaS control plane |

### Open SaaS

[Open SaaS](https://github.com/wasp-lang/open-saas) is MIT licensed and actively maintained. It provides Wasp/React/Node/Prisma, authentication, several payment providers, an admin dashboard, jobs, file uploads, email, and E2E tests; its [README](https://github.com/wasp-lang/open-saas/blob/main/README.md) documents that scope. However, the shipped [Prisma schema](https://github.com/wasp-lang/open-saas/blob/main/template/app/schema.prisma) attaches subscriptions and product examples directly to `User` and contains no Workspace, membership, role, or invitation model.

Wasp also owns generated auth/session entities and application operations. Its documentation states that sessions are persisted on the server and the client, and its custom APIs are handled by Node/Express with Wasp authentication ([auth overview](https://wasp.sh/docs/auth/overview), [custom API contract](https://wasp.sh/docs/api/%40wasp.sh/spec/functions/api)). Adopting it would either require rebuilding the missing tenancy layer or pull MarketAI domain behavior into a second application backend. Both outcomes work against the established boundary.

### FastAPI template plus Supabase

The official [Full Stack FastAPI Template](https://github.com/fastapi/full-stack-fastapi-template) is a high-quality MIT base with React, PostgreSQL, SQLModel, JWT authentication, a superuser dashboard, Docker Compose, CI and Playwright. Its [README](https://github.com/fastapi/full-stack-fastapi-template/blob/master/README.md) does not provide Workspace membership, invitations, Workspace-scoped roles, or subscriptions. Using its backend would also duplicate the FastAPI service MarketAI already has.

[Supabase Auth](https://supabase.com/docs/guides/auth) is a credible standalone identity component: it issues JWTs and integrates with Row Level Security. Self-hosted asymmetric keys expose JWKS that another service can verify ([self-hosted auth keys](https://supabase.com/docs/guides/self-hosting/self-hosted-auth-keys)). It therefore has the cleanest direct token-verification story. It still does not supply MarketAI's Workspace, invitation, subscription, and administration product as a complete control plane.

Self-hosting also transfers provisioning, hardening, PostgreSQL maintenance, backups, disaster recovery, monitoring, high availability, and upgrades to MarketAI. The self-hosted Studio is a single project and does not support multiple organizations/projects ([self-hosting responsibilities and limits](https://supabase.com/docs/guides/self-hosting)). This composition is the fallback if the BoxyHQ hardening spike fails, not the first choice.

## Mandatory production gates for the BoxyHQ fork

The fork is accepted only after all of these gates pass:

1. **Freeze the boundary.** Prisma receives credentials only for a dedicated control-plane database/schema. SQLAlchemy receives credentials only for the domain database/schema. Neither role can write the other's tables. Cross-boundary access goes through versioned HTTP contracts.
2. **Use a BFF service identity.** Do not teach FastAPI to decode a NextAuth session cookie and do not forward a browser-supplied Workspace ID. The control plane validates the session and membership, then mints a maximum five-minute ES256 JWT with fixed issuer and audience. FastAPI validates JWKS, signature, issuer, audience, expiry and required tenant claims.
3. **Enforce invitation-only entry.** Disable public signup and arbitrary team creation. A valid, unexpired, single-use invitation must be consumed transactionally before the account can enter a Workspace. Normalize email matching and audit invitation issue, revoke, resend and accept actions.
4. **Reduce RBAC to the Beta contract.** Customer-facing roles are only Owner and Member. All membership, subscription, provider-secret and budget-limit mutations require Owner on the server; UI hiding is never authorization.
5. **Patch upstream authorization defects.** The upstream payment APIs currently check membership without enforcing their declared Owner-only `team_payments` permission ([open issue](https://github.com/boxyhq/saas-starter-kit/issues/2511)). Add server-side authorization and negative integration tests before enabling billing.
6. **Disable webhooks until hardened.** The upstream webhook endpoint accepts non-HTTPS destinations and the defect remains open ([open issue](https://github.com/boxyhq/saas-starter-kit/issues/2506)). The Beta keeps the feature off until HTTPS, private-network/loopback blocking, DNS-rebinding defenses, egress controls, signing and retry/idempotency tests are complete.
7. **Replace schema push with migrations.** The upstream build script runs `prisma db push` ([package scripts](https://github.com/boxyhq/saas-starter-kit/blob/main/package.json)). Production deploys must run reviewed `prisma migrate deploy` as a separate release step with backup and rollback procedure.
8. **Turn production protections on deliberately.** Security headers must be enabled and the CSP tightened; secrets must come from a secret manager; login and mutation routes require rate limits; billing webhooks require signature verification, idempotency and replay tests; logs must redact cookies, JWTs, payment data and BYOK material.
9. **Make upgrades controlled.** Pin the initial upstream commit, retain an `upstream` remote, review monthly, and merge only through dependency, migration, SAST/dependency scan, unit, negative-auth, contract and E2E gates. No automatic upstream rebases or production image updates.
10. **Prove isolation.** Automated tests must show that a Member cannot administer a Workspace, that one Workspace cannot read or mutate another, that a forged tenant claim fails, and that the control-plane DB role cannot touch domain tables (and vice versa).

## First adoption slice

The next specification should not import the entire starter at once. The first tracer bullet is:

1. pinned BoxyHQ fork boots as a separate web/control-plane service;
2. public registration is closed;
3. an invited Owner creates/activates one Workspace and invites one Member;
4. both can open the v9 shell, but only the Owner can open membership and billing administration;
5. the BFF calls a read-only FastAPI identity endpoint with a short-lived service token;
6. FastAPI returns data only for the token's Workspace;
7. cross-tenant and Member-to-Owner escalation tests fail closed.

Stripe live mode, SSO, SCIM, customer webhooks, and self-hosted Retraced are not required for this tracer bullet. They stay disabled until their own production gates are specified and tested.

## Reconsideration triggers

Reopen this decision if any of these occurs during the tracer bullet:

- invitation-only enforcement requires replacing rather than adapting the authentication core;
- the BFF/service-JWT boundary cannot be implemented without sharing session or database secrets with FastAPI;
- the fork cannot pass the negative authorization and database-isolation suite;
- maintaining the pinned fork requires more work than implementing Workspace/invitation/billing on the Supabase composition;
- upstream becomes archived or its security/dependency backlog cannot be kept within MarketAI's patch policy.

