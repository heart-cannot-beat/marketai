# Entitlements, BYOK Credentials, Usage, and Provider Cost Controls — Design Specification

**Issue:** #18 — Implement entitlements, BYOK credentials, usage, and provider cost controls
**Status:** Draft
**Date:** 2026-08-31
**Approach:** Single self-consistent PR, 5 dependency-stage commits
**Branch:** `Raybbish/implement-entitlements-byok-credentials-usage-an`
**Contracts:** `docs/architecture/subscription-entitlement-usage-cost-ownership.md`, ADR 0005

---

## Overview

This specification defines the architecture for immutable plan versioning, entitlement snapshot propagation with bounded leases, BYOK credential management with KMS envelope encryption, provider selection (BYOK/Sandbox/Mock), append-only usage and cost ledger models, atomic cost reservation before job enqueue, and Owner/Member authorization — spanning both the control plane (Next.js/BoxyHQ/Prisma) and the domain service (FastAPI/SQLAlchemy/Alembic).

The implementation is delivered as a single self-consistent PR organized into five dependency-stage commits:

| Stage | Scope |
|-------|-------|
| 1 | Control plane — PlanVersion, AccessGrant, CommercialAccessBinding, EntitlementSnapshot, Outbox, Publication Module |
| 2 | Domain models — Alembic migration for entitlement projection, credentials, spend/capacity reservations, provider cost ledger, usage events and customer-notification outbox |
| 3 | Domain modules — EntitlementProjectionModule, PaidOperationAdmission, ExternalCallCoordinator, CredentialAdmin, CostReconciler, GuardrailAdmin and CustomerNotificationWriter |
| 4 | Integration — rewire GenerationCommandService with atomic reservation, Worker dispatch fence, Provider Adapter unification, BFF endpoints |
| 5 | Legacy migration bridge — compatibility view, phased deployment, end-to-end tests |

The authoritative contract is `docs/architecture/subscription-entitlement-usage-cost-ownership.md`. If code and contract conflict, the conflict is reported on the Issue before changing the contract.

---

## Section 1: Control Plane Data Model

### 1.1 Enums

```prisma
enum PlanVersionState {
  EFFECTIVE
  RETIRED
}

enum CommercialAccessSourceKind {
  ACCESS_GRANT
  SUBSCRIPTION
}

enum AccessGrantKind {
  BASE
  ADD_ON
}

enum RestrictionKind {
  SECURITY
  ABUSE
  COMPLIANCE
  BILLING
}

enum RestrictionEffect {
  DISABLE
  REDUCE_CAP
}

enum SubscriptionState {
  PROVISIONING
  ACTIVE
  PAST_DUE
  GRACE_PERIOD
  CANCELLED
  EXPIRED
}

enum SnapshotDeliveryState {
  PENDING
  DELIVERING
  DELIVERED
  FAILED
  DEAD_LETTER
}
```

### 1.2 PlanVersion

```prisma
model PlanVersion {
  id               String           @id @default(cuid())
  planKey          String
  versionNo        Int
  displayName      String
  state            PlanVersionState @default(EFFECTIVE)
  entitlements     Json             // validated key/value map
  entitlementsHash String           // SHA-256 of canonical JSON
  publishedBy      String
  publishedAt      DateTime         @default(now())
  retiredAt        DateTime?
  createdAt        DateTime         @default(now())

  accessGrants             AccessGrant[]
  subscriptions            Subscription[]
  commercialAccessBindings CommercialAccessBinding[]

  @@unique([planKey, versionNo])
  @@index([state])
}
```

Migration adds a trigger preventing UPDATE of `planKey`, `versionNo`, `entitlements`, `entitlementsHash`, `publishedBy`, `publishedAt` once the row exists:

```sql
CREATE FUNCTION plan_version_immutable_guard() RETURNS trigger AS $$
BEGIN
  IF OLD."planKey" IS DISTINCT FROM NEW."planKey"
     OR OLD."versionNo" IS DISTINCT FROM NEW."versionNo"
     OR OLD."entitlements"::text IS DISTINCT FROM NEW."entitlements"::text
     OR OLD."entitlementsHash" IS DISTINCT FROM NEW."entitlementsHash"
     OR OLD."publishedBy" IS DISTINCT FROM NEW."publishedBy"
     OR OLD."publishedAt" IS DISTINCT FROM NEW."publishedAt"
  THEN
    RAISE EXCEPTION 'PlanVersion content is immutable after publication';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_plan_version_immutable
  BEFORE UPDATE ON "PlanVersion"
  FOR EACH ROW EXECUTE FUNCTION plan_version_immutable_guard();
```

Retirement is one-way: EFFECTIVE → RETIRED, with consistent `retiredAt`; no reactivation.

Publishing or retiring a PlanVersion is a platform-operator command. It
requires platform authorization, MFA step-up, and a durable
`ControlPlaneAuditEvent` in the same transaction. The idempotent Beta seed
command runs under an explicitly authorized release identity, records
equivalent durable audit evidence, and cannot silently replace an existing
PlanVersion with different content.

### 1.3 AccessGrant

```prisma
model AccessGrant {
  id              String          @id @default(cuid())
  teamId          String
  team            Team            @relation(fields: [teamId], references: [id], onDelete: Restrict)
  planVersionId   String
  planVersion     PlanVersion     @relation(fields: [planVersionId], references: [id], onDelete: Restrict)
  grantKind       AccessGrantKind
  source          String          // "operator_invite", "support_grant", "promotion"
  reason          String          @default("")
  periodAnchor    DateTime?       // required for BASE; null for ADD_ON
  startsAt        DateTime
  endsAt          DateTime?       // null = no scheduled expiry (still revocable)
  grantedBy       String
  revokedAt       DateTime?
  revokedBy       String?
  revokeReason    String?
  createdAt       DateTime        @default(now())

  commercialAccessBinding CommercialAccessBinding?

  @@index([teamId, grantKind])
  @@index([teamId, revokedAt])
}
```

Migration CHECK constraints:

```sql
-- BASE requires periodAnchor
ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_base_requires_anchor
  CHECK ("grantKind" != 'BASE' OR "periodAnchor" IS NOT NULL);

-- ADD_ON requires non-empty reason
ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_addon_requires_reason
  CHECK ("grantKind" != 'ADD_ON' OR length("reason") > 0);

-- ADD_ON must not set periodAnchor
ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_addon_no_anchor
  CHECK ("grantKind" != 'ADD_ON' OR "periodAnchor" IS NULL);

-- endsAt must be after startsAt when present
ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_ends_after_starts
  CHECK ("endsAt" IS NULL OR "endsAt" > "startsAt");
```

### 1.4 Subscription (placeholder)

```prisma
model Subscription {
  id              String            @id @default(cuid())
  teamId          String
  team            Team              @relation(fields: [teamId], references: [id], onDelete: Restrict)
  planVersionId   String
  planVersion     PlanVersion       @relation(fields: [planVersionId], references: [id], onDelete: Restrict)
  state           SubscriptionState @default(PROVISIONING)
  periodStart     DateTime
  periodEnd       DateTime
  periodAnchor    DateTime
  graceEndsAt     DateTime?
  createdAt       DateTime          @default(now())
  updatedAt       DateTime          @updatedAt

  commercialAccessBinding CommercialAccessBinding?

  @@index([teamId, state])
}
```

Default is `PROVISIONING`, not `ACTIVE` — a subscription enters `ACTIVE` only after confirmed billing events.

Migration CHECK constraint:

```sql
ALTER TABLE "Subscription" ADD CONSTRAINT subscription_period_order
  CHECK ("periodEnd" > "periodStart");
```

### 1.5 CommercialAccessBinding

```prisma
model CommercialAccessBinding {
  id              String                      @id @default(cuid())
  teamId          String
  team            Team                        @relation(fields: [teamId], references: [id], onDelete: Restrict)
  sourceKind      CommercialAccessSourceKind
  accessGrantId   String?                     @unique
  accessGrant     AccessGrant?                @relation(fields: [accessGrantId], references: [id], onDelete: Restrict)
  subscriptionId  String?                     @unique
  subscription    Subscription?               @relation(fields: [subscriptionId], references: [id], onDelete: Restrict)
  planVersionId   String
  planVersion     PlanVersion                 @relation(fields: [planVersionId], references: [id], onDelete: Restrict)
  effectiveAt     DateTime
  deactivatedAt   DateTime?
  createdAt       DateTime                    @default(now())
  updatedAt       DateTime                    @updatedAt

  @@index([teamId, deactivatedAt])
}
```

No `@unique` on `teamId` — one-to-many relation with Team so deactivated bindings remain as history. Instead:

```sql
-- At most one active base source per Workspace (database-enforced)
CREATE UNIQUE INDEX commercial_access_binding_one_active
  ON "CommercialAccessBinding" ("teamId")
  WHERE "deactivatedAt" IS NULL;

-- Exactly one source reference must be non-null, matching sourceKind
ALTER TABLE "CommercialAccessBinding" ADD CONSTRAINT binding_source_exactly_one
  CHECK (
    ("sourceKind" = 'ACCESS_GRANT' AND "accessGrantId" IS NOT NULL AND "subscriptionId" IS NULL)
    OR
    ("sourceKind" = 'SUBSCRIPTION' AND "subscriptionId" IS NOT NULL AND "accessGrantId" IS NULL)
  );
```

Binding integrity: the referenced source must belong to the same Team, an AccessGrant source must be BASE, and `planVersionId` must match the source.

### 1.6 Restriction

```prisma
model Restriction {
  id              String            @id @default(cuid())
  teamId          String
  team            Team              @relation(fields: [teamId], references: [id], onDelete: Restrict)
  kind            RestrictionKind
  effect          RestrictionEffect
  entitlementKey  String
  restrictedValue Json              // false for DISABLE; reduced numeric for REDUCE_CAP
  reason          String
  imposedBy       String
  imposedAt       DateTime          @default(now())
  liftedAt        DateTime?
  liftedBy        String?
  liftReason      String?

  @@index([teamId, liftedAt])
}
```

Migration CHECK constraints:

```sql
-- DISABLE effect must carry JSON false
ALTER TABLE "Restriction" ADD CONSTRAINT restriction_disable_value
  CHECK ("effect" != 'DISABLE' OR "restrictedValue"::text = 'false');

-- REDUCE_CAP effect must carry a non-negative number
ALTER TABLE "Restriction" ADD CONSTRAINT restriction_reduce_cap_value
  CHECK ("effect" != 'REDUCE_CAP' OR (
    jsonb_typeof("restrictedValue"::jsonb) = 'number'
    AND ("restrictedValue"::jsonb)::numeric >= 0
  ));
```

### 1.7 CommercialAccessProjection

One row per Workspace — the authoritative publication state. Locked during snapshot publication to serialize concurrent version allocation.

```prisma
model CommercialAccessProjection {
  id                    String    @id @default(cuid())
  teamId                String    @unique
  team                  Team      @relation(fields: [teamId], references: [id], onDelete: Restrict)
  currentSnapshotId     String?   @unique
  currentSnapshot       EntitlementSnapshot? @relation(fields: [currentSnapshotId], references: [id], onDelete: Restrict)
  entitlementVersion    Int       @default(0)
  leaseSequence         Int       @default(0)
  semanticHash          String    @default("")
  entitlementRegistryVersion Int  @default(1)
  resolutionPolicyVersion Int     @default(1)
  platformLimitsVersion Int       @default(1)
  updatedAt             DateTime  @updatedAt
}
```

Publication transaction (executed under `SELECT ... FOR UPDATE` on this row):

```
1. resolve entitlements (base ∪ addons ∪ restrictions ∪ platform clamp)
2. compute new semantic_hash over source refs and periods, restrictions,
   resolved entitlements, entitlement-registry version,
   resolution-policy version and platform-limits version
3. IF semantic_hash != projection.semanticHash:
     projection.entitlementVersion += 1
4. projection.leaseSequence += 1
5. Team.projectionVersion += 1
6. compute valid_until = min(now + 24h, every active source deadline,
   periodEnd, graceDeadline)
7. INSERT or reference the durable ControlPlaneAuditEvent for this publication
8. build one canonical WorkspaceProjectionEnvelope, sign its protected header
   and complete payload, and INSERT EntitlementSnapshot with the exact bytes
9. INSERT EntitlementSnapshotOutbox (PENDING)
10. UPDATE projection.currentSnapshotId, semanticHash and policy versions
11. UPDATE Team.entitlementVersion and projectionVersion
```

Steps 3–11 are one database transaction. `leaseSequence` never resets — it
is strictly monotonic within a Workspace across renewals, Plan changes and
access-source replacement.

### 1.8 Add-on combination rules

Deterministic, typed join between base and add-on entitlements:

| Key type | Combination rule | Example |
|----------|-----------------|---------|
| Boolean | OR — any active grant enabling a feature enables it | base `module.scale.enabled=false` + addon `module.scale.enabled=true` → `true` |
| Numeric (absolute cap) | MAX — the highest cap from any active source wins | base `members.max=5` + addon `members.max=10` → `10` |

The combination rule is applied *before* restrictions and platform clamp. The canonical hash includes the combination method identifier so a rule change triggers republication.

### 1.9 EntitlementSnapshot

```prisma
model EntitlementSnapshot {
  id                    String   @id @default(cuid())
  teamId                String
  team                  Team     @relation(fields: [teamId], references: [id], onDelete: Restrict)
  schemaVersion         Int
  workspaceName         String
  authorizationEpoch    Int
  entitlementVersion    Int
  leaseSequence         Int
  projectionVersion     Int
  lifecycle             String
  suspended             Boolean
  planVersionId         String
  sourceRefs            Json     // [{ kind, id, planVersionId, startsAt, endsAt }]
  periodStart           DateTime
  periodEnd             DateTime
  periodAnchor          DateTime
  resolvedEntitlements  Json
  restrictionSummary    Json     // [{ id, kind, key, effect, value, reason }]
  semanticHash          String
  entitlementRegistryVersion Int
  resolutionPolicyVersion Int
  platformLimitsVersion Int
  issuedAt              DateTime @default(now())
  validUntil            DateTime // bounded by min(issuedAt+24h, endsAt, periodEnd, grace)
  auditEventId          String
  auditEvent            ControlPlaneAuditEvent @relation(fields: [teamId, auditEventId], references: [teamId, id], onDelete: Restrict)
  signatureHmac         String   // HMAC-SHA256 of canonical snapshot
  signatureKeyId        String   // identifies which key signed, for rotation
  signatureAlgorithm    String   @default("hmac-sha256")
  canonicalEnvelope     Bytes    // exact immutable bytes sent by push and pull
  createdAt             DateTime @default(now())

  projection CommercialAccessProjection?
  outboxEntry EntitlementSnapshotOutbox?

  @@unique([teamId, entitlementVersion, leaseSequence])
  @@unique([teamId, id])
  @@index([teamId, entitlementVersion])
}
```

`validUntil` is bounded: `min(issuedAt + 24h, grant.endsAt, periodEnd, graceDeadline)`. It can never authorize access beyond the earliest deadline of the underlying commercial source.

`semanticHash` covers every entitlement-semantic field: plan/source references
including source validity windows, periods, restriction IDs and values,
resolved entitlements, entitlement-registry/resolution-policy version, and
platform-limits version. Authorization, lifecycle, Workspace display name and
lease-only fields do not alter `entitlementVersion`; they are still signed and
ordered by `projectionVersion`/`leaseSequence`.

The signature covers the protected header (`schema_version`,
`signature_key_id`, `signature_algorithm`) and the complete payload. The
signature is outside the payload; it is never recursively embedded in the
signed entitlement object.

### 1.10 Transactional Outbox

```prisma
model EntitlementSnapshotOutbox {
  id                String                @id @default(cuid())
  snapshotId        String                @unique
  teamId            String
  snapshot          EntitlementSnapshot    @relation(fields: [teamId, snapshotId], references: [teamId, id], onDelete: Restrict)
  deliveryState     SnapshotDeliveryState  @default(PENDING)
  attempts          Int                    @default(0)
  maxAttempts       Int                    @default(5)
  lastAttemptAt     DateTime?
  nextAttemptAt     DateTime?
  lastError         String?
  deliveryLeaseId   String?
  deliveryLeaseExpiresAt DateTime?
  deliveredAt       DateTime?
  createdAt         DateTime              @default(now())

  @@unique([teamId, snapshotId])
  @@index([deliveryState, nextAttemptAt])
  @@index([teamId])
  @@index([deliveryLeaseExpiresAt])
}
```

State machine: `PENDING → DELIVERING → DELIVERED` (happy path), `DELIVERING → FAILED → DELIVERING → ...` (retries with backoff), `FAILED → DEAD_LETTER` (after `maxAttempts`). A delivery lease prevents concurrent dispatchers from double-delivering the same entry.

Dead-lettered entries do not directly suspend a Workspace. Periodic full reconciliation (every 6 hours per contract §7) repairs any projection drift regardless of outbox state. Dead letters trigger an alert but the reconciliation path ensures eventual convergence. However, if both push and pull fail for long enough, the lease will expire and the domain will correctly fail closed.

### 1.11 ControlPlaneAuditEvent

Append-only durable audit trail. The existing `sendAudit()` only emits a redacted structured log and is not a durable audit trail.

```prisma
model ControlPlaneAuditEvent {
  id              String    @id @default(cuid())
  scopeKind       String    // "PLATFORM" or "WORKSPACE"
  teamId          String?
  team            Team?     @relation(fields: [teamId], references: [id], onDelete: Restrict)
  actorKind       String
  actorId         String
  executor        String
  eventType       String
  resourceType    String
  resourceId      String
  requestId       String?
  traceId         String?
  details         Json      // redacted
  createdAt       DateTime  @default(now())

  entitlementSnapshots EntitlementSnapshot[]

  @@unique([teamId, id])
  @@index([teamId, createdAt])
  @@index([eventType, createdAt])
}
```

Platform-wide PlanVersion publication does not require a Team. Workspace
commercial-access changes and every Snapshot publication do.
`EntitlementSnapshot.auditEventId` is a same-Team foreign key to the durable
audit row. `sendAudit()` may mirror the event to operational logs only after
commit. A database trigger rejects UPDATE and DELETE of
`ControlPlaneAuditEvent`.

### 1.12 Publication triggers

Snapshot publication fires at these boundaries:

- AccessGrant created, revoked, or expired
- Subscription state change (provisioning → active, etc.)
- Restriction imposed or lifted
- CommercialAccessBinding activated or deactivated
- Workspace name, membership or role changes that advance the authorization projection
- Periodic lease renewal (every 6 hours, even if nothing changed)
- Platform limits version bumped (deployment config change)
- Time boundaries via scheduled scan; not waiting for user request

### 1.13 Team model additions

```prisma
model Team {
  // ... existing fields unchanged ...
  lifecycle String // PROVISIONING, ACTIVE, SUSPENDED, DELETION_PENDING, DELETED

  accessGrants              AccessGrant[]
  subscriptions             Subscription[]
  commercialAccessBindings  CommercialAccessBinding[]
  restrictions              Restriction[]
  entitlementSnapshots      EntitlementSnapshot[]
  commercialAccessProjection CommercialAccessProjection?
  auditEvents               ControlPlaneAuditEvent[]
}
```

All new relations use `onDelete: Restrict`.

Lifecycle backfill: existing Teams with `suspended=true` map to `SUSPENDED`; otherwise `ACTIVE`. Legacy `suspended` remains a fail-closed compatibility projection. Publication updates both fields transactionally.

The database stores only the uppercase lifecycle values shown above, enforced
by a database CHECK constraint. Canonical envelope serialization maps them to
the lowercase wire values (`provisioning`, `active`, `suspended`,
`deletion_pending`, `deleted`) before signing.

### 1.14 Workspace Projection Endpoints

The existing v1 authorization endpoint and JSON shape remain unchanged for old
domain instances. It never returns a partial v2 projection and never advances
v2 entitlement contents. The new pull-repair endpoint returns the exact
`application/octet-stream` bytes stored in `canonicalEnvelope`; the
Dispatcher POSTs those identical bytes.

The logical v2 envelope, shown as formatted JSON only for readability, is:

```json
{
  "schema_version": 2,
  "signature_key_id": "esk_1",
  "signature_algorithm": "hmac-sha256",
  "payload": {
    "snapshot_id": "es_...",
    "workspace_id": "...",
    "workspace_name": "Acme",
    "authorization_epoch": 5,
    "entitlement_version": 3,
    "lease_sequence": 12,
    "projection_version": 8,
    "suspended": false,
    "lifecycle": "active",
    "entitlement_snapshot": {
      "plan_version_id": "...",
      "source_refs": [
        {
          "kind": "ACCESS_GRANT",
          "id": "...",
          "plan_version_id": "...",
          "starts_at": "2026-08-01T00:00:00Z",
          "ends_at": null
        }
      ],
      "period_start": "2026-08-01T00:00:00Z",
      "period_end": "2026-09-01T00:00:00Z",
      "period_anchor": "2026-08-01T00:00:00Z",
      "resolved_entitlements": {
        "module.generate.enabled": true,
        "members.max": 5
      },
      "restriction_summary": [],
      "semantic_hash": "abc123...",
      "entitlement_registry_version": 1,
      "resolution_policy_version": 1,
      "platform_limits_version": 1,
      "issued_at": "2026-08-31T10:00:00Z",
      "valid_until": "2026-09-01T00:00:00Z",
      "audit_event_id": "evt_..."
    }
  },
  "signature": "..."
}
```

`schema_version: 2` — FastAPI checks this and rejects unknown versions. The
v1 endpoint applies only authorization fields through the existing v1
database function. The v2 push and pull paths atomically apply Workspace
identity, authorization, lifecycle and entitlement contents through
`marketai_apply_workspace_projection_v2`; they never advance
`entitlement_version` while retaining stale policy data. The standalone
`incrementEntitlementVersion()` path is replaced by the atomic publication
transaction.

---

## Section 2: Domain Data Model

### 2.0 Core invariants

- Entitlement Inbox and current Projection are stored in separate tables.
- All cross-table references must simultaneously bind `workspace_id` to prevent cross-tenant references.
- Operation, Jobs, per-call spend reservations and admission-time capacity
  reservations must be created in the same transaction. Execution-time
  Provider concurrency is acquired later by the Worker in its prepare
  transaction.
- Provider Cost, Usage, Guardrail Event and Entitlement Inbox are append-only enforced by database triggers.
- HTTP idempotency records can expire, but cost, Usage and reservation records use permanent unique `semantic_key_hash`.
- API, Worker and Readonly use different column-level or table-level permissions; only the Worker execution path can obtain credential ciphertext and invoke decryption.
- PostgreSQL uses RLS; SQLite retains equivalent repository checks and append-only triggers.

### 2.1 EntitlementSnapshotInboxRecord

Stores snapshots that have passed service-identity and signature verification. Invalid signatures cannot write to a tenant Inbox using an untrusted `workspace_id` — they produce only security logs and metrics.

```python
class EntitlementSnapshotInboxRecord(Base):
    __tablename__ = "entitlement_snapshot_inbox"

    snapshot_id                 # control-plane immutable message ID, primary key
    workspace_id
    entitlement_version
    lease_sequence
    projection_version
    schema_version
    delivery_kind               # push | full_repair

    canonical_envelope          # exact received protected-header/payload/signature bytes
    envelope_sha256
    semantic_hash
    signature
    signature_key_id
    signature_algorithm

    outcome                     # applied | duplicate | stale |
                                # version_jump_paused | conflict
    received_at
    processed_at
    projection_applied_at       # null when not applied
```

Constraints:

- `UNIQUE(workspace_id, entitlement_version, lease_sequence)`.
- `UNIQUE(workspace_id, snapshot_id)` for composite tenant FK.
- Same `snapshot_id` with different `envelope_sha256` is a protocol conflict triggering an alert.
- Table is protected by PostgreSQL and SQLite triggers prohibiting UPDATE and DELETE.
- `schema_version` must be explicitly provided by the message; no default value.

Processing order:

1. Verify service identity, message size, signature algorithm, signing key and schema.
2. Parse and validate complete payload.
3. Lock Workspace current Entitlement Projection.
4. Compute result and insert Inbox row with final outcome.
5. If application is permitted, update Projection plus Workspace
   identity/name, authorization, lifecycle and admission fields.
6. Inbox and Projection writes commit in the same transaction.

### 2.2 EntitlementProjectionRecord

One row per Workspace holding the current executable entitlement state:

```python
class EntitlementProjectionRecord(Base):
    __tablename__ = "entitlement_projections"

    workspace_id                # primary key
    current_snapshot_id         # composite FK to same-Workspace Inbox

    entitlement_version
    lease_sequence
    projection_version
    schema_version

    plan_version_id
    source_refs_json
    period_start
    period_end
    period_anchor
    resolved_entitlements_json
    restriction_summary_json

    semantic_hash
    entitlement_registry_version
    resolution_policy_version
    platform_limits_version

    issued_at
    valid_until
    control_plane_audit_event_id

    signature
    signature_key_id
    signature_algorithm

    applied_at
```

Constraints:

- All version numbers non-negative.
- `valid_until > issued_at`.
- Current Snapshot must belong to the same Workspace.
- `semantic_hash` covers all non-lease semantic fields: `plan_version_id`, `source_refs`, `period_start`/`period_end`/`period_anchor`, `resolved_entitlements`, restriction IDs/effects/values, entitlement key registry version, resolution policy version, platform limits version.

Application rules:

- Same Snapshot ID: idempotent success.
- Same version, same lease sequence: duplicate.
- Older version or older lease sequence: stale, write Inbox only.
- Same `entitlement_version`, higher `lease_sequence`: must have identical
  `semantic_hash`; update lease fields, signature, audit reference and
  `applied_at`, and apply the signed Workspace name/authorization/lifecycle
  fields under their monotonic `projection_version` and
  `authorization_epoch` guards. Entitlement contents remain unchanged.
- Next `entitlement_version`: normal application.
- Version jump on ordinary push: record `version_jump_paused`, do not elevate entitlements, trigger full repair.
- Signed `full_repair` can apply a complete cross-version Snapshot.
- Lease expiry permits only safe reads, exports, reconciliation and necessary settlement.
- Existing `workspaces.entitlement_version`, `projection_version` and this table must be updated in the same transaction. The standalone `incrementEntitlementVersion` path is replaced; all increments go through the Snapshot Application Module.
- The application function is a narrow-permission `SECURITY DEFINER` function. The API runtime role must not directly modify Inbox, Projection or Workspace admission fields.

### 2.3 WorkspaceRecord extensions

- `lifecycle`: domain projection of control-plane truth. States: `provisioning`, `active`, `suspended`, `deletion_pending`, `deleted`. Domain commands cannot modify it directly.
- `budget_currency`: backfilled from each row's existing `default_currency`, not an unconditional `USD`. Must match `^[A-Z]{3}$`. Cannot be modified while outstanding Spend Reservations exist.

Existing `suspended` remains as a compatibility field, synchronized by the same projection transaction. All side-effect gates check `lifecycle` simultaneously.

`default_currency` continues to represent the general domain display default; `budget_currency` is dedicated to cost and Guardrail calculations.

### 2.4 CredentialVersionRecord

```python
class CredentialVersionRecord(Base):
    __tablename__ = "credential_versions"

    id
    workspace_id
    credential_key
    version_no
    provider
    display_label
    state                       # active | rotated | revoked

    encrypted_secret
    encrypted_data_key
    encryption_key_id
    encryption_algorithm
    nonce
    aad_schema_version

    created_by
    created_at
    rotated_at
    rotated_by                  # nullable
    revoked_at
    revoked_by                  # nullable
    revoke_reason               # nullable
```

No `secret_sha256_prefix`. No non-encrypted digest of plaintext is stored.

Constraints:

- `UNIQUE(workspace_id, id)` provides the candidate key for tenant-safe
  composite foreign keys.
- `UNIQUE(workspace_id, credential_key, version_no)`.
- At most one active version per Workspace/credential_key via partial unique index.
- `version_no >= 1`.
- State transitions are one-way only: `active → rotated`, `active → revoked`, `rotated → revoked`.
- Provider, credential key, version, ciphertext envelope and creation metadata are immutable after insertion.
- Rotated/revoked state and corresponding time/actor fields must be consistent.
- Rotation atomically locks the current active version, inserts a new version, marks the old one `rotated` and updates the Binding — all in one transaction.
- Every create, rotate and revoke command appends an AuditEventRecord.

### 2.5 CredentialBindingRecord

Binding is the current projection; history is preserved by append-only AuditEvents.

```python
class CredentialBindingRecord(Base):
    __tablename__ = "credential_bindings"

    id
    workspace_id
    binding_scope
    provider
    credential_version_id
    revision
    bound_by
    created_at
    updated_at
```

Constraints:

- `UNIQUE(workspace_id, binding_scope, provider)`.
- CredentialVersion uses composite FK: `(workspace_id, credential_version_id) → credential_versions(workspace_id, id)`.
- Binding's `provider` must match CredentialVersion's `provider`.
- Only active CredentialVersions can be bound.
- Updates use `revision`/ETag to prevent concurrent overwrites.
- Jobs do not freeze CredentialVersion; Worker resolves the current active Binding at call time and records the actual `credential_version_id` in call and cost evidence.

Permissions:

- API can read non-secret metadata, create ciphertext, rotate, revoke and bind.
- API has no ordinary SELECT path for secret envelopes.
- Worker can only read active Bindings and required envelopes; cannot create or modify credentials.
- Readonly role never sees ciphertext fields.

### 2.6 KMS and Credential Protection Seam

```python
@dataclass(frozen=True)
class EncryptedEnvelope:
    ciphertext: bytes
    encrypted_data_key: bytes
    nonce: bytes
    encryption_key_id: str
    encryption_algorithm: str
    aad_schema_version: int


class CredentialProtector(Protocol):
    def seal(
        self,
        plaintext: bytes,
        context: CredentialEncryptionContext,
    ) -> EncryptedEnvelope:
        ...

    @contextmanager
    def open(
        self,
        envelope: EncryptedEnvelope,
        context: CredentialEncryptionContext,
    ) -> Iterator[memoryview]:
        ...
```

`CredentialEncryptionContext` binds at least: `workspace_id`, `credential_version_id`, `credential_key`, `provider`, `version_no`.

Implementation responsibilities:

- Generate random 256-bit data key per encryption.
- Generate unique AES-GCM nonce per encryption.
- Use serialized context as AAD.
- KMS wrap/unwrap data key.
- Verify GCM authentication tag.
- Minimize plaintext and data key lifetime; no guarantee of Python-level memory zeroing.

Local Adapter may use deterministic KMS wrapping simulation, but data key and GCM nonce must still be random. Tests can inject a fixed random source for reproducible results.

### 2.7 SpendGuardrailRecord

One row per Workspace, also serving as the serialization lock for cost reservation:

```python
class SpendGuardrailRecord(Base):
    __tablename__ = "spend_guardrails"

    workspace_id                # primary key
    revision
    budget_currency

    per_operation_limit
    rolling_24h_limit
    period_limit
    outstanding_exposure_limit

    platform_policy_version
    created_at
    updated_at
    updated_by
```

All amounts non-negative. Owner values can only be less than or equal to platform absolute limits.

Creating a paid side-effect must `SELECT ... FOR UPDATE` this row, then in the same transaction compute: this Quote; rolling 24h settled cost plus current outstanding exposure; current commercial period settled cost plus current outstanding exposure; total outstanding exposure plus this Reservation.

Guardrail missing, currency mismatch or untrusted projection → fail closed.

### 2.8 SpendGuardrailEventRecord

```python
class SpendGuardrailEventRecord(Base):
    __tablename__ = "spend_guardrail_events"

    id
    workspace_id
    revision_before
    revision_after
    old_values_json             # null for initial creation
    new_values_json
    old_budget_currency
    new_budget_currency
    reason
    actor_id
    audit_event_id
    mfa_evidence_ref            # no MFA secret/token stored
    created_at
```

Table is protected by database triggers prohibiting UPDATE and DELETE. One Owner command produces one event containing the complete before/after snapshot, not one row per field.

### 2.9 SpendReservationRecord

```python
class SpendReservationRecord(Base):
    __tablename__ = "spend_reservations"

    id
    workspace_id
    operation_id
    job_id                      # required: one Reservation per planned call/Job
    semantic_key_hash

    state                       # reserved | partially_settled |
                                # settled | released | uncertain

    provider
    execution_mode
    provider_currency
    maximum_provider_amount

    budget_currency
    maximum_budget_amount
    outstanding_budget_amount

    period_start
    period_end
    entitlement_version
    lease_sequence
    guardrail_revision
    platform_policy_version

    created_at
    updated_at
    settled_at
    released_at
```

Constraints:

- `UNIQUE(workspace_id, id)` provides the candidate key for tenant-safe
  Ledger and ProviderCallAttempt foreign keys.
- `UNIQUE(workspace_id, semantic_key_hash)`, permanent.
- Operation, Job use Workspace composite FK.
- All amounts non-negative; `outstanding <= maximum`.
- Mock does not create Spend Reservations.
- `uncertain` preserves outstanding exposure until manual or automatic reconciliation.
- State updates use CAS/row lock, committed in the same transaction as the corresponding Ledger Entry.

### 2.10 CapacityReservationRecord

For atomic occupation of Provider concurrency, storage and other hard entitlement capacity:

```python
class CapacityReservationRecord(Base):
    __tablename__ = "capacity_reservations"

    id
    workspace_id
    operation_id
    job_id
    entitlement_key
    quantity
    unit
    acquisition_phase           # admission | execution
    semantic_key_hash
    state                       # reserved | committed | released | expired
    entitlement_version
    lease_sequence
    expires_at
    created_at
    updated_at
    resolved_at
```

Constraints:

- `UNIQUE(workspace_id, id)`.
- `UNIQUE(workspace_id, semantic_key_hash)`.
- `quantity` must be positive.
- Operation, Job use tenant composite FK.
- Admission creates only `acquisition_phase=admission` rows. Execution-time
  capacity requirements are frozen on the Job and the Worker creates/acquires
  the `execution` row immediately before dispatch.
- Provider concurrency is acquired before call and released on completion or confirmed failure.
- Expired capacity after crashes is recovered by reconciler using Job, Spend Reservation and Provider result state.
- Usage Projection cannot substitute for this table as an admission gate.

### 2.11 ProviderCostLedgerEntryRecord

```python
class ProviderCostLedgerEntryRecord(Base):
    __tablename__ = "provider_cost_ledger"

    id
    workspace_id
    operation_id
    job_id
    spend_reservation_id
    credential_version_id

    entry_type                  # quote | reservation | release |
                                # reported_cost | estimated_settlement |
                                # adjustment
    direction                   # increase | decrease
    execution_mode              # byok | sandbox | mock
    semantic_key_hash

    provider
    model

    provider_amount
    provider_currency

    budget_amount
    budget_currency
    exchange_rate
    exchange_rate_source
    exchange_rate_observed_at
    price_rule_version

    related_entry_id            # release/settlement target
    adjusts_entry_id            # Adjustment only
    provider_evidence_ref
    reconciliation_evidence_ref
    audit_event_id
    audit_actor_id

    simulated_amount            # Mock only, optional
    simulated_currency

    note
    created_at
```

Constraints:

- `UNIQUE(workspace_id, id)` provides the candidate key for Ledger
  self-references and other tenant-safe foreign keys.
- `UNIQUE(workspace_id, semantic_key_hash)`.
- All FKs (Operation, Job, Spend Reservation, CredentialVersion, self-referencing Ledger Entry) bind `workspace_id`.
- Amounts non-negative, exchange rate > 0, currency uppercase three-character.
- Quote, Reservation, Reported Cost, Estimated Settlement use `increase`. Release uses `decrease`. Adjustment can use either direction but must reference the corrected record, reconciliation evidence and audit actor.
- BYOK Reported/Estimated entries must record the actual CredentialVersion ID.
- Mock `provider_amount` and `budget_amount` must be zero; simulated estimates go only into `simulated_*` fields.
- Table is protected by PostgreSQL and SQLite triggers prohibiting UPDATE and DELETE.
- Amounts should use `Numeric(24, 12)` to avoid premature rounding on small Provider unit costs.

Recommended indexes:

- `(workspace_id, execution_mode, entry_type, created_at)`
- `(workspace_id, spend_reservation_id, created_at)`
- `(workspace_id, operation_id, created_at)`

The old `cost_ledger` is preserved unchanged. Historical `actual` to `estimated_settlement` bridging is done via a read-only compatibility view, not by overwriting old records.

### 2.12 UsageEventRecord

```python
class UsageEventRecord(Base):
    __tablename__ = "usage_events"

    id
    workspace_id
    metric_key
    measurement_kind            # delta | gauge
    direction                   # increase | decrease | set
    quantity                    # always non-negative
    unit
    execution_mode              # byok | sandbox | mock | not_applicable
    source_resource_type
    source_resource_id
    semantic_key_hash
    occurred_at
    created_at
```

Constraints:

- `UNIQUE(workspace_id, semantic_key_hash)`.
- `delta` only pairs with `increase`/`decrease`.
- `gauge` only pairs with `set`.
- `quantity` non-negative.
- Metric registry validates each metric's legal unit, measurement kind and execution mode.
- Table is protected by database triggers prohibiting UPDATE and DELETE.
- Period endings never delete events.
- First version queries directly from append events (strong consistency, satisfying the ≤5-minute staleness requirement). Materialized Usage Period Projections are added only when measured performance requires them.

### 2.13 CustomerNotificationOutboxRecord

Durable customer-notification intent is separate from operational telemetry.
Issue #18 creates and queries this outbox; external email/push delivery
Adapters remain deferred.

```python
class CustomerNotificationOutboxRecord(Base):
    __tablename__ = "customer_notification_outbox"

    id
    workspace_id
    notification_type           # guardrail_threshold | guardrail_rejected
    dimension
    threshold                   # bounded value such as 80 or 100
    guardrail_revision
    platform_policy_version
    window_kind                 # operation | rolling_24h |
                                # commercial_period | outstanding
    window_key                  # stable identity, not a moving timestamp
    period_start
    period_end
    crossing_sequence
    deduplication_key
    customer_payload_json       # customer-safe; no secret/internal evidence

    delivery_state              # pending | delivering | delivered |
                                # failed | dead_letter
    attempts
    next_attempt_at
    delivery_lease_id
    delivery_lease_expires_at
    delivered_at
    created_at

class GuardrailNotificationStateRecord(Base):
    __tablename__ = "guardrail_notification_states"

    id
    workspace_id
    dimension
    threshold
    guardrail_revision
    platform_policy_version
    window_kind
    window_key
    period_start
    period_end
    state                       # below | notified
    crossing_sequence
    updated_at
```

Constraints:

- `UNIQUE(workspace_id, id)`.
- `UNIQUE(workspace_id, deduplication_key)`.
- Notification state has
  `UNIQUE(workspace_id, guardrail_revision, platform_policy_version, dimension, window_key, threshold)`,
  `CHECK(state IN ('below', 'notified'))` and
  `CHECK(crossing_sequence >= 0)`. The row is locked before a transition. A
  commercial-period key identifies its fixed period; a rolling-24-hour key
  remains stable while hysteresis state tracks crossings instead of using a
  continuously moving timestamp as identity.
- The Outbox deduplication key additionally includes `crossing_sequence`.
  Successful 80% warnings transition `below → notified`; a versioned
  hysteresis reset transitions back to `below` and permits a later crossing.
  A 100% rejection is notified once per revision/period and does not reset
  merely because the rejected proposal did not commit.
- Payload and deduplication fields are immutable; delivery-state fields follow
  a lease/CAS state machine.
- RLS and tenant-safe query views apply. Delivery failures never alter
  admission or settlement truth.

### 2.14 Job and Operation association

Existing `operation_records` gains:

- `semantic_key_hash`: permanent semantic identity
- `request_fingerprint_hash`: detects same key/different payload
- `admission_contract_version`
- `UNIQUE(workspace_id, semantic_key_hash)` for new-contract Operations

Existing `jobs` table gains:

- `operation_id`: nullable for legacy Jobs; required for new paid Jobs
- `entitlement_version_at_enqueue`
- `execution_contract_version`

With composite FK: `(workspace_id, operation_id) → operation_records(workspace_id, id)`.

Existing `operation_state_events` gains `workspace_id`, backfilled from its
Operation, and replaces its ID-only reference with:
`(workspace_id, operation_id) → operation_records(workspace_id, id)`. New
Operation state transitions update the current Operation projection and append
the same-Workspace event in one flush.

New paid command transaction sequence:

1. Validate lifecycle, Actor, role and Entitlement lease.
2. Claim semantic intent by inserting the new Operation shell, or replay the
   existing Operation before current pricing and Guardrail evaluation.
3. Validate Entitlements and explicit Provider mode; freeze Quote, price and
   exchange rate.
4. Build domain drafts and preallocate Job IDs.
5. Lock Guardrail and admission-time capacity in stable order.
6. Insert Jobs referencing the already-created Operation.
7. Insert per-call Spend Reservations referencing their Jobs.
8. Insert admission-time Capacity Reservations and Quote/Reservation Ledger
   Entries. Execution-time concurrency is not occupied here.
9. Commit the entire transaction.

Worker re-validates before each external call: Workspace lifecycle, Entitlement lease, Capacity Reservation, Spend Reservation, Credential Binding and CredentialVersion state.

### 2.15 Deletion and retention

- Credentials, assets, prompts and general domain content are deletable during Workspace deletion.
- Provider Cost Ledger does not use simple `ON DELETE CASCADE` from Workspace.
- Retained cost evidence uses an irreversible Workspace pseudonym/tombstone that cannot reverse-lookup users or assets.
- Usage retention follows deployment-region retention policy, but period resets never delete UsageEvents.
- Deletion flow must first process Reservations, Credentials and retained-evidence conversion before advancing Workspace to `deleted`.

### 2.16 Migration and permissions

One Alembic migration (revision ID and `down_revision` generated from latest `main` at implementation time):

1. Extend Workspace and Job; backfill lifecycle and budget_currency.
2. Create Entitlement Inbox.
3. Create Entitlement Projection.
4. Create CredentialVersion and CredentialBinding.
5. Create SpendGuardrail and Guardrail Event.
6. Create SpendReservation and CapacityReservation.
7. Create Provider Cost Ledger.
8. Create UsageEvent, Guardrail Notification State and Customer Notification
   Outbox.
9. Add composite FKs, candidate-key UNIQUE constraints, CHECKs, partial
   unique indexes and query indexes.
10. Install PostgreSQL/SQLite append-only and immutable-payload triggers.
11. Add `marketai_apply_workspace_projection_v2`; retain v1 temporarily.
12. Enable and force RLS.
13. Configure least-privilege grants.
14. Preserve old `cost_ledger` — no rename, no delete.

Permission matrix:

| Table/capability | API | Worker | Readonly |
|---|---|---|---|
| Entitlement apply | Execute dedicated function only | Prohibited | Prohibited |
| Entitlement projection | Read | Read | Safe fields only |
| Credential metadata | Owner-command read/write | Read active metadata | Sanitized read |
| Credential envelope | INSERT, no ordinary read | Execution-path read | Prohibited |
| Credential decrypt | Prohibited | Invoke KMS | Prohibited |
| Spend/Capacity Reservation | Create and read | Validate, settle, release | Read-only |
| Provider Cost Ledger | INSERT/SELECT | INSERT/SELECT | Read-only |
| Guardrail | Owner-command update | Read | Read-only |
| UsageEvent | INSERT/SELECT | INSERT/SELECT | Read-only |
| Customer notification state/outbox | Lock state; insert/query safe intents | Delivery CAS only | Safe fields only |

---

## Section 3: Domain Modules

### 3.0 Design principles

- Only seams with genuine multiple Adapter implementations define a Protocol: Provider Adapter, Credential Protector, FX Rate Provider.
- Entitlement checks, Guardrail calculation, Reservation writes are internal implementation of Admission Module — not exposed as shallow interfaces that can be called out of order.

Top-level Modules:

- EntitlementProjectionModule
- PaidOperationAdmission
- CredentialAdmin
- ExternalCallCoordinator
- CostReconciler
- UsageRecorder
- UsageQuery
- GuardrailAdmin
- CustomerNotificationWriter

Transaction ownership:

- Domain command modules (e.g., GenerationCommandService) own the commit.
- `PaidOperationAdmission.admit_in_transaction()` is called within that transaction.
- CredentialAdmin, ProjectionModule and GuardrailAdmin each own independent command transactions.
- ExternalCallCoordinator uses "prepare tx → network call → complete tx" and never holds a database transaction across a network call.

### 3.1 Supplementary models from Section 2

**Operation permanent semantic idempotency** — add to OperationRecord:

- `semantic_key_hash: str`
- `request_fingerprint_hash: str`
- `admission_contract_version: int`
- `UNIQUE(workspace_id, semantic_key_hash)`

Same semantic key + same fingerprint → return original Operation. Same key + different fingerprint → conflict. This provides unified permanent idempotency across Mock, Sandbox and BYOK without depending on HTTP idempotency records that expire after 24 hours.

**ProviderCallAttemptRecord** — persistent Provider call attempt:

```python
class ProviderCallAttemptRecord(Base):
    __tablename__ = "provider_call_attempts"

    id
    workspace_id
    operation_id
    job_id
    spend_reservation_id
    call_semantic_key_hash
    attempt_no

    execution_mode
    provider
    model
    request_fingerprint_hash
    provider_idempotency_key

    credential_version_id       # actual version resolved before call
    state                       # prepared | dispatching | succeeded |
                                # failed | uncertain
    provider_request_id
    provider_evidence_ref
    error_code
    error_detail

    prepared_at
    dispatching_at
    completed_at
    updated_at
```

Constraints:

- `UNIQUE(workspace_id, id)`.
- `UNIQUE(workspace_id, call_semantic_key_hash, attempt_no)`.
- All FK references (Operation, Job, Reservation, Credential) bind same `workspace_id`.
- After `dispatching`, Worker crash → `uncertain`, never blind retry.
- State transitions use CAS and append AuditEvent.
- Each planned external call gets its own Spend Reservation. One Operation can have multiple Spend Reservations to prevent multi-Job settlement conflicts.

### 3.2 EntitlementProjectionModule

```python
class EntitlementProjectionModule:
    async def ingest(
        self,
        signed_envelope: bytes,
        *,
        delivery_kind: Literal["push", "full_repair"],
    ) -> InboxOutcome:
        ...

    async def repair(self, workspace_id: str) -> InboxOutcome:
        ...
```

`ingest` performs: verify service identity, schema, key ID, algorithm and
signature → parse the complete Workspace Projection Envelope → call
`marketai_apply_workspace_projection_v2` → atomically write Inbox,
Entitlement Projection and Workspace admission → return the closed outcome
set `applied | duplicate | stale | version_jump_paused | conflict`.

Ordinary push encountering a version jump writes Inbox and requests repair; `full_repair` can apply a complete cross-version Snapshot. Invalid signatures produce no tenant Inbox row.

### 3.3 EntitlementPolicy

Internal implementation within the Admission Module, not a public Protocol:

```python
@dataclass(frozen=True)
class EntitlementRequirements:
    feature_keys: tuple[str, ...]
    numeric_requirements: tuple[NumericRequirement, ...]

@dataclass(frozen=True)
class EntitlementRef:
    entitlement_version: int
    lease_sequence: int
    period_start: datetime | None
    period_end: datetime | None
    semantic_hash: str

def authorize(
    session: Session,
    context: TenantContext,
    requirements: EntitlementRequirements,
    now: datetime,
) -> EntitlementRef:
    ...
```

One call completes lifecycle, lease, feature and static numeric cap checks. Unknown keys, missing Projection and expired lease all fail closed. Dynamic capacity "check-and-occupy" is not performed here to avoid TOCTOU.

### 3.4 Provider Resolution

Execution mode must be explicitly submitted:

```python
ExecutionMode = Literal["byok", "sandbox", "mock"]

@dataclass(frozen=True)
class ProviderSelection:
    execution_mode: ExecutionMode
    provider: str
    model: str
    binding_scope: str
    credential_binding_id: str | None
```

Rules:

- `requested_mode` is required — `None` is not permitted.
- No automatic fallback from BYOK to Sandbox or Mock.
- BYOK Admission confirms only that a resolvable active Binding exists.
- Admission does not freeze CredentialVersion. Worker resolves the actual version before the call and records it in ProviderCallAttempt and cost evidence.
- Mock requires both Workspace Entitlement and deployment configuration to be enabled.
- Sandbox refers only to Provider-official no-charge sandbox or an explicitly no-charge Adapter; platform shared paid API keys are never labeled Sandbox.
- Provider failure never switches execution mode.

### 3.5 CostQuoteEngine

```python
@dataclass(frozen=True)
class FrozenQuote:
    call_semantic_key_hash: str
    provider: str
    model: str
    execution_mode: ExecutionMode
    quantity: int

    provider_amount: Decimal
    provider_currency: str

    budget_amount: Decimal
    budget_currency: str

    exchange_rate: Decimal
    exchange_rate_source: str
    exchange_rate_observed_at: datetime
    fx_buffer_bps: int

    price_rule_version: str
    pricing_input_hash: str

    simulated_amount: Decimal | None
    simulated_currency: str | None

@dataclass(frozen=True)
class QuoteBundle:
    call_quotes: tuple[FrozenQuote, ...]
    total_budget_amount: Decimal
```

Rules:

- Each planned external call gets a separate quote and reservation.
- Guardrail per-operation limit checks `total_budget_amount`.
- `config.COST_PER_IMAGE` is only the seed for the first price registry, not a platform limit.
- Price registry must have a version.
- Non-USD Workspace without FX Adapter → `UnsupportedCurrencyPairError`, not a 1.0 fallback rate.
- Conservative buffer and observed rate are stored separately.
- Mock real amounts are zero; simulated amounts go only to `simulated_*`.

### 3.6 ReservationBook

Guardrail check, capacity check and Reservation insert are merged into one internal Module to prevent check-then-reserve TOCTOU:

```python
@dataclass(frozen=True)
class CapacityRequest:
    entitlement_key: str
    quantity: Decimal
    unit: str
    acquisition_phase: Literal["reserve_on_admission", "acquire_on_execution"]

@dataclass(frozen=True)
class ReservationPlan:
    quote_bundle: QuoteBundle
    capacity_requests: tuple[CapacityRequest, ...]
    entitlement_ref: EntitlementRef
    operation_id: str
    job_ids_by_call_key: Mapping[str, str]

@dataclass(frozen=True)
class ReservationBundle:
    operation_id: str
    spend_reservation_by_call_key: Mapping[str, str]
    capacity_reservation_ids: tuple[str, ...]
    job_ids: tuple[str, ...]
    guardrail_revision: int
```

Internal fixed order:

1. Lock SpendGuardrail row.
2. Lock all capacity counts/Reservation rows in stable order.
3. Calculate Guardrails.
4. Check admission-time capacity.
5. Create and associate Jobs using their preallocated IDs and
   `execution_contract_version`.
6. Create per-call Spend Reservations referencing those Jobs.
7. Write Quote and Reservation Ledger Entries.
8. Create only `reserve_on_admission` Capacity Reservations.
9. Persist any successful Guardrail threshold-state transition and
   notification intent.
10. Return bundle.

Guardrail formulas:

```
per_operation
  = total proposed chargeable quote for this Operation

rolling_24h
  = past 24h chargeable settlement/adjustment net
    + current outstanding exposure
    + proposed exposure

period
  = current commercial period chargeable settlement/adjustment net
    + period's outstanding exposure
    + proposed exposure

outstanding
  = all current outstanding exposure
    + proposed exposure
```

Quote, Reservation Ledger Entry and `SpendReservation.outstanding` must not be double-counted.

Capacity is split:

- `reserve_on_admission`: known upload bytes, etc.
- `acquire_on_execution`: real Provider concurrency.

Every `CapacityRequest` carries this acquisition phase. Provider concurrency
is frozen as a Job requirement but is not occupied by Admission; the Worker
creates/acquires its execution-phase Capacity Reservation before the actual
call. Reaching the concurrency limit keeps the Job queued rather than failing
an entire multi-asset Operation.

### 3.7 PaidOperationAdmission

```python
@dataclass(frozen=True)
class AdmissionRequest:
    command_type: str
    semantic_key_hash: str
    request_fingerprint_hash: str

    entitlement_requirements: EntitlementRequirements
    provider_selection: RequestedProviderSelection
    billable_calls: tuple[BillableCallSpec, ...]
    capacity_requests: tuple[CapacityRequest, ...]

    operation_payload: OperationPayload
    job_specs: tuple[JobSpec, ...]


class PaidOperationAdmission:
    def admit_in_transaction(
        self,
        session: Session,
        context: TenantContext,
        request: AdmissionRequest,
    ) -> AdmissionResult:
        ...
```

Fixed order:

1. Validate Workspace lifecycle, Actor and role.
2. Validate Entitlement lease.
3. Claim semantic intent: insert the Operation shell with
   `admission_contract_version`, or replay the existing Operation using
   `semantic_key_hash` and fingerprint.
4. Replay → return original AdmissionResult immediately, not subject to current pricing or Guardrails.
5. Validate feature Entitlements.
6. Validate explicit Provider mode.
7. Generate per-call Frozen Quotes.
8. Atomically check and occupy Guardrails/capacity via ReservationBook.
9. Finalize the already-created Operation's admission state and domain
   associations. ReservationBook has created Jobs and Reservations exactly
   once inside the same transaction.
10. Return result.

HTTP idempotency and permanent semantic idempotency both converge at step 3. The replay check must not be deferred until after all Guardrail checks.

### 3.8 CredentialAdmin

```python
class CredentialAdmin:
    def create(..., expected_absent: bool = True) -> CredentialMetadata:
        ...

    def rotate(
        ...,
        expected_active_version: int,
    ) -> CredentialMetadata:
        ...

    def revoke(
        ...,
        expected_revision: int,
        reason: str,
    ) -> None:
        ...

    def bind(
        ...,
        expected_binding_revision: int | None,
    ) -> CredentialBindingView:
        ...

    def list_metadata(...) -> list[CredentialMetadata]:
        ...
```

Behavior:

- `create` encountering an existing active Credential returns conflict; it does not silently rotate.
- Only `rotate` creates the next version.
- `rotate`/`bind`/`revoke` use revision or expected version to prevent concurrent overwrites.
- `list_metadata` is Owner-only; Members cannot view the credential list.
- Revoking the active version leaves the existing Binding as a historical projection, but resolution returns unavailable; the system does not auto-bind an old version or a different credential.
- Every command appends AuditEvent.
- Worker uses a separate `CredentialAccessor`, but it is callable only by ExternalCallCoordinator. It derives Workspace from TenantContext and does not accept a separate, potentially inconsistent `workspace_id` parameter.

### 3.9 ExternalCallCoordinator

Replaces the loose combination of WorkerPreCallGate + CredentialAccessor + CostSettler:

```python
class ExternalCallCoordinator:
    async def execute(
        self,
        context: TenantContext,
        *,
        job_id: str,
        call_semantic_key_hash: str,
    ) -> ExternalCallOutcome:
        ...
```

Internal flow:

**Prepare transaction:**

1. Lock Job, its payable Spend Reservation when present, and related Capacity
   state.
2. Re-check lifecycle and Entitlement lease.
3. Acquire execution-time Provider concurrency capacity.
4. Check Spend Reservation still available.
5. Resolve active Credential Binding.
6. Decrypt actual CredentialVersion.
7. Create `ProviderCallAttempt(state="prepared")` with the actual
   `credential_version_id`, frozen request fingerprint and Provider
   idempotency key.
8. After every pre-dispatch invariant is durable in the transaction,
   transition the Attempt to `dispatching`.
9. Advance Job/Operation to running.
10. Commit transaction.

Once `dispatching` commits, the system assumes the remote side-effect may have occurred.
`prepared` alone never authorizes a Provider call; in this version its
`prepared → dispatching` transition occurs inside the same prepare
transaction.

**Provider call:**

- Database transaction is closed.
- Provider Adapter receives the persisted idempotency key.
- Adapter capabilities declare idempotency-key acceptance and result lookup by
  Provider request ID and/or persisted idempotency key.
- Adapter failures carry a typed dispatch certainty:
  `not_dispatched | may_have_dispatched | dispatched`. HTTP status or
  arbitrary exception text never supplies this classification.
- The existing `generate_with_retry` in `base.py` is not suitable for
  non-idempotent paid calls. Retry is permitted only after
  `not_dispatched`, or when the Provider has confirmed acceptance of the
  persisted idempotency key and guarantees same-key replay safety.

**Complete transaction:**

On success, atomically: ProviderCallAttempt → `succeeded`; write Reported Cost or Estimated Settlement; update Spend Reservation; release remaining exposure; release Provider concurrency capacity; write UsageEvent; update Asset/RunItem; update Job and Operation.

On definite failure: write failure evidence and release unused reservations.

On uncertain outcome (network ambiguity or Worker crash after dispatching): Attempt, Job, Operation and Spend Reservation enter `uncertain`; preserve exposure; do not automatically re-call Provider; hand off to CostReconciler.

This persistent dispatch fence is the core evidence for "#18 kill/retry/uncertain does not double-charge."

### 3.10 CostReconciler

```python
class CostReconciler:
    def reconcile_attempt(
        self,
        context: TenantContext,
        attempt_id: str,
        evidence: ReconciliationEvidence,
    ) -> ReconciliationResult:
        ...

    def record_adjustment(
        self,
        context: TenantContext,
        *,
        target_entry_id: str,
        direction: str,
        provider_amount: Decimal,
        evidence_ref: str,
        reason: str,
        semantic_key_hash: str,
    ) -> None:
        ...
```

Rules:

- Prefer Provider request ID/idempotency key to query results.
- Provider confirms non-execution → release Reservation.
- Provider confirms execution → write Reported or Estimated Settlement.
- Unable to confirm → continue preserving uncertain exposure.
- First settlement above Reservation: record the full Settlement exactly once,
  set outstanding to zero, and emit the non-financial breach delta plus alert.
  Do not create an Adjustment for that initial overage. Only a later correction
  to an already-recorded Settlement creates an Adjustment referencing the
  original entry.
- All reconciliation/adjustment uses permanent semantic key; duplicate evidence is idempotent.

### 3.11 UsageRecorder and UsageQuery

```python
class UsageRecorder:
    def record_many(
        self,
        session: Session,
        context: TenantContext,
        events: Sequence[UsageFact],
    ) -> None:
        ...
```

Same semantic key + same payload → idempotent; same key + different payload → conflict.

```python
class UsageQuery:
    def aggregate(
        self,
        session: Session,
        context: TenantContext,
        request: UsageQueryRequest,
    ) -> UsageAggregate:
        ...
```

Metric registry defines unit, measurement kind, and aggregation policy per metric:

| Metric | Kind | Aggregation |
|--------|------|-------------|
| provider_calls | delta | sum |
| processing_seconds | delta | sum |
| provider_concurrent_peak | gauge | maximum |
| active_members | gauge | latest |
| storage_bytes | defined by product | delta balance or gauge latest |

### 3.12 GuardrailAdmin

Raising limits requires Preview and Confirm:

```python
class GuardrailAdmin:
    def get(...) -> SpendGuardrailView:
        ...

    def preview_update(
        self,
        context: TenantContext,
        request: GuardrailUpdateRequest,
        *,
        expected_revision: int,
        mfa_evidence_ref: str | None,
    ) -> GuardrailUpdatePreview:
        ...

    def confirm_update(
        self,
        context: TenantContext,
        *,
        preview_token: str,
    ) -> SpendGuardrailView:
        ...
```

Preview token binds: Workspace, Actor, current Guardrail revision, complete proposed values, budget currency, MFA evidence, expiry.

Rules:

- Lowering limits does not require MFA but still requires Owner permission and revision.
- Any increase or budget currency change requires MFA step-up and second confirmation.
- Values above platform absolute limit → explicit rejection, not silent clamp.
- Currency change blocked while outstanding Reservations exist.
- Confirm atomically updates Guardrail, revision and appends Guardrail Event.
- `ensure_default` is a provisioning-internal function, not part of the customer-callable interface.

### 3.13 CustomerNotificationWriter

```python
class CustomerNotificationWriter:
    def enqueue(
        self,
        session: Session,
        context: TenantContext,
        intent: CustomerNotificationIntent,
    ) -> NotificationOutboxRef:
        ...
```

This deep Module owns threshold calculation, a row lock on
`GuardrailNotificationStateRecord`, hysteresis transitions, deterministic
deduplication, customer-safe payload construction and Outbox insertion. A
successful Reservation that first crosses 80% writes the state transition and
intent in the admission transaction. A 100% rejection rolls back the attempted
admission, then the command Module opens a short independent transaction to
lock notification state and enqueue the rejection intent. Failure to enqueue
never permits the rejected operation; it emits an operational alert for the
missing customer-notification intent. External delivery Adapters are deferred,
but the durable intent and in-app query path are part of #18.

### 3.14 Errors and Worker instructions

Admission errors:

- `ExplicitExecutionModeRequiredError`
- `UnsupportedCurrencyPairError`
- `SemanticIntentConflictError`
- `GuardrailRevisionConflictError`
- `PlatformLimitExceededError`
- `ConfirmationRequiredError`

Worker receives typed instructions from ExternalCallCoordinator instead of a plain `proceed: bool`:

- `PROCEED`
- `WAIT_FOR_CAPACITY`
- `PAUSE_FOR_ENTITLEMENT_REPAIR`
- `CANCEL_BEFORE_DISPATCH`
- `SETTLED`
- `UNCERTAIN_RECONCILIATION_REQUIRED`

### 3.15 Existing code integration

`create_run` transformation order:

1. Upper-level Generation Command Module opens a tenant transaction.
2. Build Run, Asset/RunItem drafts.
3. Construct `AdmissionRequest` with one `BillableCallSpec` per planned asset call.
4. Call `PaidOperationAdmission.admit_in_transaction()`.
5. Associate Run, Assets, Operation, Jobs and per-call Reservations.
6. Single commit.

Worker transformation:

- Remove `generate_with_retry` from the new paid execution path. It may
  remain only for explicitly non-payable legacy/development paths until their
  separate cleanup.
- Generation Jobs use `ExternalCallCoordinator.execute()` to call Provider.
- Asset, Job, Operation, Usage, Ledger and Reservation completion states commit in one finalization transaction.
- `cancel_run` releases only calls that have not entered `dispatching`; already-dispatched calls enter result-waiting or uncertain reconciliation.
- Old `_daily_cost_exposure` and direct `CostLedgerRecord` writes are removed.
- Old `actual` evidence is mapped to `estimated_settlement` via the compatibility bridge, not overwritten.

---

## Section 4: Integration Layer

### 4.0 Integration invariants

- Push and Pull Repair use the identical signed `WorkspaceProjectionEnvelope`.
- Control plane → domain and domain → control plane use two different service credentials.
- Dispatcher does not execute network requests inside a database transaction.
- `lease_sequence` is strictly monotonic within a Workspace and never resets when entitlement version changes.
- New domain endpoints must be added to OpenAPI and the BFF generation allowlist; the catch-all route does not equal arbitrary path forwarding.
- All external paid calls must go through the persistent `ProviderCallAttempt` dispatch fence.
- Old and new execution paths are switched via deployment flags, not by fail-closing all traffic before data backfill.

### 4.1 Control plane Publication Module

The standalone `incrementEntitlementVersion()` call path is removed.
`incrementAuthorizationEpoch()` remains the counter update for non-commercial
authorization changes, but Workspace name, membership, role and lifecycle
commands must also invoke the Publication Module so the signed v2 projection
advances immediately.

Publication Transaction:

1. `SELECT ... FOR UPDATE` lock CommercialAccessProjection.
2. Resolve base, add-ons, Restrictions and platform limits.
3. Compute `semantic_hash` covering all non-lease fields.
4. If `semantic_hash` changed: `entitlementVersion += 1`.
5. Always: `leaseSequence += 1`, `Team.projectionVersion += 1`.
6. `Team.entitlementVersion` is set directly from the Projection value, not independently incremented.
7. Create immutable Snapshot and precise canonical signed envelope.
8. Update current snapshot pointer.
9. Insert Outbox `PENDING` record.
10. Single commit.

`leaseSequence` does not reset to 1 on a new entitlement version.

Signed envelope includes at least:

```json
{
  "schema_version": 2,
  "signature_key_id": "esk_...",
  "signature_algorithm": "hmac-sha256",
  "payload": {
    "snapshot_id": "es_...",
    "workspace_id": "ws_...",
    "workspace_name": "Acme",
    "authorization_epoch": 7,
    "entitlement_version": 3,
    "lease_sequence": 12,
    "projection_version": 19,
    "lifecycle": "active",
    "suspended": false,
    "entitlement_snapshot": {}
  },
  "signature": "..."
}
```

Signature covers the protected header (schema version, key ID and algorithm)
and the complete payload. Snapshot stores the exact canonical envelope bytes
from publication time; Dispatcher and Pull Repair return those bytes without
re-serialization.

Changes that must call the Publication Module: Base AccessGrant
activation/revocation/start/expiry, ADD_ON Grant changes, Restriction
imposition/lifting, Subscription state/period/grace/PlanVersion binding
changes, Workspace name/membership/role/authorization changes, Workspace
lifecycle changes, entitlement registry or resolution policy changes,
platform entitlement limit version changes, and periodic lease renewal. Time
boundaries are published via scheduled scan, not by waiting for user requests.

### 4.2 Outbox Dispatcher

Uses the delivery states from Section 1: PENDING, DELIVERING, DELIVERED, FAILED, DEAD_LETTER.

One claim cycle:

1. Short transaction: `FOR UPDATE SKIP LOCKED` to select retryable records.
2. Set `deliveryState = DELIVERING`, `deliveryLeaseId = current dispatcher ID`, `deliveryLeaseExpiresAt = now + lease`, `attempts += 1`.
3. Commit claim transaction.
4. Execute HTTP POST outside the transaction.
5. New short transaction: CAS update final state using `deliveryLeaseId`.

Crash recovery: DELIVERING with expired lease can be reclaimed. No row lock is held during the network request.

Response handling:

- 2xx (including duplicate/stale/version-jump-paused) → DELIVERED.
- 409 protocol conflict → DEAD_LETTER + high-priority alert.
- 400/422 deterministic payload/schema/signature errors → DEAD_LETTER + alert.
- 408, 429, 5xx, connection errors, timeouts → FAILED + backoff retry.
- 401/403 → FAILED + high-priority alert (credential configuration fault), not immediate permanent dead-letter.

Dead letters do not directly change Workspace state. If both push and pull fail long-term, lease expiry causes the domain to correctly fail closed.

### 4.3 Dual service identities

No reuse of the existing `PROJECTION_SYNC_SERVICE_TOKEN` for both directions. Two independent credentials:

```
DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN
    FastAPI → Next.js Pull Repair

CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN
    Next.js Dispatcher → FastAPI Inbox
```

Signing keys are a third separate secret family:

```
ENTITLEMENT_SIGNING_ACTIVE_KEY
ENTITLEMENT_SIGNING_PREVIOUS_KEYS
```

The control plane signs; the domain holds the verification key ring. Each key has a key ID; old keys are retained at least until all published Snapshots' retry and lease windows have closed.

Production environments additionally use private networking, Ingress allowlists or mTLS to restrict `/api/internal/*`; Bearer tokens are not the sole network defense.

### 4.4 Push and Pull Endpoints

**Control plane Pull Endpoint:**

Preserve existing authorization endpoint for v1 compatibility:

```
GET /api/internal/domain/workspaces/{workspace_id}/authorization-snapshot
```

New complete repair endpoint:

```
GET /api/internal/domain/workspaces/{workspace_id}/workspace-projection-snapshot
Authorization: Bearer DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN
Accept: application/octet-stream
```

Returns the current complete, signed `WorkspaceProjectionEnvelope`. The upgraded domain `ProjectionSyncer` must call this endpoint. It cannot first advance `entitlement_version` from the old endpoint, then retain old entitlement contents.

`budget_currency` is not included in the control plane Snapshot; it remains a domain-side Owner-managed cost setting.

**FastAPI Push Endpoint:**

```
POST /api/internal/domain/workspaces/{workspace_id}/entitlement-inbox
Authorization: Bearer CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN
Content-Type: application/octet-stream
```

Requirements: does not go through DelegatedIdentityMiddleware; constant-time comparison for the independent service token; strict request body size limit; `Cache-Control: no-store`; path `workspace_id` must exactly match the signed payload's workspace_id; raw bytes pass directly to `EntitlementProjectionModule.ingest()`.

Responses: 200 (applied/duplicate/stale/version_jump_paused), 400
(unparseable envelope), 401/403 (service identity failure), 409 (same identity
or sequence with conflicting signed contents), 422
(signature/schema/semantic validation failure), 503 (verification key ring,
database or required dependency unavailable).

### 4.5 Credential Routes

```
POST /api/v1/credentials
POST /api/v1/credentials/{credential_key}/rotate
POST /api/v1/credentials/{credential_version_id}/revoke
GET  /api/v1/credentials
PUT  /api/v1/credentials/{credential_version_id}/bindings/{scope}
GET  /api/v1/credentials/{credential_version_id}/bindings
```

Rules:

- Use `TenantContext.initiator_role == "owner"` only; no loose OR between `context.role` and `initiator_role`.
- These endpoints accept only delegated user identity, not Worker/system context pretending to be Owner.
- create, rotate, revoke, bind require `Idempotency-Key`.
- rotate/revoke/bind additionally require revision/`If-Match`.
- Plaintext secret field has a strict size limit and is prohibited from logs, exception details, audit details, Tracing and analytics events.
- All responses set `Cache-Control: no-store`.
- list/binding are also Owner-only.
- Return values contain only `CredentialMetadata`.

### 4.6 Guardrail and Step-up Routes

```
GET  /api/v1/spend-guardrail
POST /api/v1/spend-guardrail/preview
POST /api/v1/spend-guardrail/confirm
```

Budget currency modification is included in the same Preview/Confirm command.

`mfa_evidence_ref` is not a browser-submitted plain string. The control plane completes MFA, then the BFF issues a short-lived step-up assertion for FastAPI, binding at least: `workspace_id`, `actor_id`, `session_id`, `action`, `requested_values_hash`, `auth_time`, `jti`, `issued_at`, `expires_at`.

FastAPI validates signature, audience, action, Workspace, Actor, validity and one-time `jti`.

Preview token additionally binds: current Guardrail revision, complete before/after values, budget currency, step-up assertion ID, expiry.

Confirm requires `Idempotency-Key` and uses revision CAS. Values above platform absolute limit → explicit rejection, not silent clamp.

### 4.7 Usage and Cost Query Routes

```
GET /api/v1/usage
GET /api/v1/cost-summary
GET /api/v1/operations/{operation_id}/cost-evidence
```

Requirements: cursor pagination with explicit time/period parameters;
aggregation separately shows BYOK, Sandbox and Mock for new evidence, plus
distinct read-only `legacy_platform` and `legacy_unknown` buckets from the
compatibility view; explicit distinction of Reservation, Reported, Estimated,
Adjustment and Release; Member responses omit `credential_version_id`,
internal Provider request ID, raw evidence URL and sensitive audit notes;
Owner sees safe CredentialMetadata references only, not secrets/envelopes;
query DTOs are separate from internal Ledger Records — no direct ORM row
serialization. Legacy modes are never coerced to Sandbox or admitted into new
Guardrail aggregates.

### 4.8 BFF and OpenAPI

The existing BFF catch-all route is constrained by a generated operation allowlist, path binding, request validators and response validators. It does not automatically forward arbitrary new paths.

Every new endpoint must:

1. Be added to FastAPI OpenAPI.
2. Have body/response size limits.
3. Be annotated for Idempotency-Key, If-Match and step-up assertion requirements.
4. Run `scripts/generate_all.sh`.
5. Commit updated: `domain-v1.d.ts`, `operation-map.d.ts`, `operation-registry.ts`, `allowlist-binding.ts`, `allowlist.ts`, `validators.ts`, `paths.ts`.
6. Pass OpenAPI drift and breaking-change checks.

BFF pipeline additions: credential request sensitive body log suppression; step-up assertion server-side issuance and injection; new operation idempotency/revision policy; new endpoint allowlist entries.

The browser cannot construct trusted MFA assertions passed to FastAPI.

### 4.9 Generation Command Transaction

Correct ordering (not Admission-first, then Run/Asset):

1. Open a tenant transaction.
2. Validate lifecycle and lease.
3. Claim semantic intent: create the Operation shell with
   `admission_contract_version`, or replay it using the permanent semantic
   key and request fingerprint.
4. Replay → return existing Operation/Run immediately, no re-quoting or resource re-creation.
5. Write and flush GenerationRun, Asset/RunItem pending records and
   preallocate Job IDs.
6. Construct per-call `BillableCallSpec`.
7. Freeze per-call Quotes.
8. Lock and check Guardrails, storage capacity and other admission-time resources.
9. Create Jobs with `execution_contract_version`, referencing the existing
   Run/Asset and Operation.
10. Create per-call Spend Reservations referencing those Jobs.
11. Create admission-time Capacity Reservations and write Quote/Reservation
    Ledger Entries. Provider concurrency remains only a Job requirement.
12. Finalize the Operation admission state.
13. Single commit.

The public deep Module is: `GenerationCommandService.start(context, command) -> OperationView`. `PaidOperationAdmission` is an internal Module; the API endpoint does not assemble transaction steps itself.

Semantic key must cover the complete paid intent: frozen recipe, prompt, seed, provider, model, execution mode, reference versions, parameters, variant/call layout, explicit regeneration intent. An intentional regeneration command requires a distinct intent/seed nonce and receives a distinct semantic key. Reusing a successful Asset does not create Job, Spend Reservation or Provider concurrency Reservation.

### 4.10 Worker and Provider Call

Worker uses:

```python
ExternalCallCoordinator.execute(
    context,
    job_id=...,
    call_semantic_key_hash=...,
)
```

**Prepare:** Lock Job, its payable Spend Reservation when present, and relevant Capacity state → check
lifecycle and Entitlement lease → atomically acquire execution-time Provider
concurrency → resolve active CredentialVersion → create
`ProviderCallAttempt(state="prepared")` with the actual CredentialVersion ID,
request fingerprint and Provider idempotency key → transition it to
`dispatching` in the same transaction → update Job/Operation → commit. Only
after the `dispatching` prepare commit succeeds can the Provider be called.

**Secret lifecycle:** Credential may be resolved in prepare, but: commit failure must clear the secret and never call Provider. After successful commit, the secret survives only until this Provider call completes. Revocation after the dispatch fence does not masquerade as cancellation of an already-authorized and possibly-dispatched call. The actual version used always comes from the Attempt record.

**Provider result persistence:** After Provider success, write the result to a persistent Blob staging/committed path keyed by Attempt ID, then execute the finalization transaction. The finalization transaction atomically writes: Attempt outcome, Asset/RunItem, Spend settlement/release, Capacity release, UsageEvent, Job, Operation.

If Provider returned but Blob or database save fails: Attempt/Job/Operation → `uncertain`; preserve or settle cost evidence; do not automatically re-call Provider; Reconciler attempts to recover the saved Blob or Provider result.

### 4.11 Provider Adapter Interface

No parallel BYOK overload; no legacy `generate()` permanently kept for Sandbox. Unified interface:

```python
@dataclass(frozen=True)
class ProviderCallRequest:
    prompt: str
    reference_inputs: tuple[ProviderInputRef, ...]
    aspect_ratio: str
    image_size: str
    model: str
    idempotency_key: str
    credential: SecretValue | None

@dataclass(frozen=True)
class ProviderCapabilities:
    accepts_idempotency_key: bool
    supports_result_lookup_by_request_id: bool
    supports_result_lookup_by_idempotency_key: bool

DispatchCertainty = Literal[
    "not_dispatched",
    "may_have_dispatched",
    "dispatched",
]

class ProviderDispatchError(Exception):
    code: str
    dispatch_certainty: DispatchCertainty
    idempotency_key_accepted: bool
    provider_request_id: str | None

@dataclass(frozen=True)
class ProviderCallResult:
    output_bytes: bytes
    provider_request_id: str | None
    usage_evidence: ProviderUsage | None
    idempotency_key_accepted: bool

@dataclass(frozen=True)
class ProviderLookupRequest:
    provider_request_id: str | None
    idempotency_key: str | None

ProviderLookupState = Literal[
    "executed",
    "not_executed",
    "pending",
    "unknown",
]

@dataclass(frozen=True)
class ProviderLookupResult:
    state: ProviderLookupState
    provider_request_id: str | None
    result_ref: str | None
    usage_evidence: ProviderUsage | None

class ImageProvider(Protocol):
    capabilities: ProviderCapabilities

    async def execute(
        self,
        request: ProviderCallRequest,
    ) -> ProviderCallResult:
        ...

    async def lookup(
        self,
        request: ProviderLookupRequest,
    ) -> ProviderLookupResult:
        ...
```

`ProviderLookupRequest` requires exactly one lookup key supported by the
Adapter. This permits reconciliation after a request may have been transmitted
but no Provider request ID was received.

Every expected Provider/network failure is translated into
`ProviderDispatchError`; HTTP status alone never determines certainty. Retry
rules: `not_dispatched` → retry is allowed; Provider-confirmed idempotency-key
acceptance → retry only with the identical key and payload; any other
`may_have_dispatched`, timeout, connection interruption or unknown error →
`uncertain`.

Delete the generic "all network errors auto-retry" paid path. Existing environment API keys are retained only in an explicit development compatibility path; production BYOK never falls back to platform shared paid credentials. Mock uses Mock Adapter. Sandbox must be an explicitly no-charge independent Adapter.

### 4.12 Mutation Policy

Lease enforcement is not only in PaidOperationAdmission. A mutation policy registry:

| Operation | Policy |
|-----------|--------|
| Create Run, upload, create credential, rotate credential, bind credential | LEASE_REQUIRED |
| Read, export, cost query | READ_ONLY |
| Revoke credential, lower Guardrail, cancel undispatched work, reconcile | RECOVERY_ALLOWED |
| Raise Guardrail, start Provider call | LEASE_REQUIRED |

OpenAPI operation registry records the policy; FastAPI centrally enforces it; specific Modules still perform final checks.

JWT `entitlement_version` continues detecting sync timing but cannot substitute for local Projection content and lease.

### 4.13 Configuration

**Control plane:**

```
CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN
DOMAIN_INTERNAL_BASE_URL
ENTITLEMENT_SIGNING_ACTIVE_KEY_ID
ENTITLEMENT_SIGNING_KEY_RING
ENTITLEMENT_RENEWAL_INTERVAL_SECONDS
ENTITLEMENT_DISPATCH_INTERVAL_SECONDS
ENTITLEMENT_DISPATCH_LEASE_SECONDS
```

**Domain:**

```
DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN
CONTROL_PLANE_INTERNAL_BASE_URL
ENTITLEMENT_VERIFICATION_KEY_RING
ENTITLEMENT_MAX_LEASE_SECONDS

MARKETAI_CREDENTIAL_KMS_ADAPTER
MARKETAI_CREDENTIAL_KMS_KEY_ID
MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET

MARKETAI_PLATFORM_MAX_PER_OPERATION
MARKETAI_PLATFORM_MAX_ROLLING_24H
MARKETAI_PLATFORM_MAX_PERIOD
MARKETAI_PLATFORM_MAX_OUTSTANDING
MARKETAI_PLATFORM_POLICY_VERSION

MARKETAI_PRICE_REGISTRY_VERSION
MARKETAI_PRICE_REGISTRY_HASH
```

Rules:

- The domain does not configure renewal interval; renewal is the control plane's responsibility.
- The domain only validates that Snapshot lease does not exceed the allowed maximum.
- Local KMS is permitted only for test/development; production configured as local fails at startup.
- Price content changed without matching version/hash change → startup failure.
- New configuration uses `MARKETAI_` prefix.
- Old `MAX_COST_PER_RUN`, `MAX_COST_PER_DAY` are compatibility values during migration; removed after cutover to avoid two parallel platform limit systems.

### 4.14 Database Grants

The correct readonly role name is `marketai_domain_readonly`.

Credential metadata should use a `security-barrier` view rather than mixing table-level and column-level SELECT on the ORM table.

Permission principles:

- Entitlement Inbox/Projection: API executes only the dedicated apply function; no direct write.
- Credential base table: Worker reads envelopes; API writes through controlled Module/function; Readonly prohibited.
- Credential metadata view: API/Readonly can read as needed, still under RLS.
- Provider Cost, Usage, Guardrail Event: API/Worker INSERT/SELECT only, no UPDATE/DELETE.
- Spend/Capacity Reservation: API creates; Worker CAS updates.
- ProviderCallAttempt: Worker INSERT/CAS UPDATE; API/Readonly only safe views.
- Guardrail Notification State/Outbox: command Modules lock/update transition
  state and insert/query safe intents; delivery role can update Outbox
  delivery state only.
- Worker cannot modify Guardrails or credential management state.
- All SECURITY DEFINER functions use fixed empty `search_path`, schema-qualified table names, and revoke PUBLIC execute.

### 4.15 Workspace Provisioning

Provisioning Module in one control plane transaction:

1. Create Team with `lifecycle=provisioning`.
2. Create first Owner membership.
3. Ensure valid Beta PlanVersion exists.
4. Create BASE AccessGrant.
5. Create CommercialAccessProjection.
6. Publish initial signed Snapshot.
7. Write Outbox.

Domain receives the initial Snapshot, including the signed Workspace display
name required to create the Workspace projection:

1. Create/update Workspace Projection.
2. Create SpendGuardrail using domain deployment default currency.
3. Return `applied`.

Control plane receives `applied`:

1. Switch Team lifecycle to `active`.
2. Increase authorization/projection version.
3. Publish second `active`-lifecycle Snapshot.

Before the second Snapshot arrives, the Workspace remains `provisioning` and all paid or resource-creation commands fail closed.

### 4.16 Deployment sequence

Even within a single PR, production deployment is phased:

| Phase | Action | Safe rollback |
|-------|--------|---------------|
| 0 | Backup, migration dry run, table counts, role and target verification | No state changed |
| 1 | Domain expand migration plus updated role matrix | Leave schema dormant |
| 2 | Deploy FastAPI/Worker with v2 ingestion and recovery; admission disabled | Redeploy compatible code |
| 3 | Control-plane schema and Publication Module; dispatcher disabled | Disable dispatcher; if old code is restored, disable commercial mutation commands |
| 4 | Idempotently seed Beta PlanVersion, eligible AccessGrants and initial snapshots | Revoke/deactivate through control-plane commands |
| 5 | Enable dispatcher for canary cohort; verify actual domain versions and valid leases | Disable dispatcher; retain snapshots |
| 6 | Converge all eligible Workspaces; verify Guardrail and lifecycle projections | Pause before admission |
| 7 | Run shadow admission comparison without Provider calls or payable reservations | Disable shadow mode |
| 8 | Enable new admission by Workspace cohort; persist `admission_contract_version` on every Operation and `execution_contract_version` on every Job | Stop new paid admission; do not switch existing Operations |
| 9 | Stop creating legacy-path Operations; let existing legacy Operations finish on their original coordinator | Recovery-only mode |
| 10 | Bake for at least 72 hours with reconciliation and drift monitoring | Forward fix or recovery-only mode |
| 11 | Later release removes legacy flags and code after evidence retention is approved | Separate contract migration |

"Every outbox row is DELIVERED" is insufficient as a cutover gate. The domain must confirm expected snapshot ID, entitlement version, lease sequence, lifecycle and valid lease with no unresolved version jump. Old cost writes are not globally disabled until legacy Operations have drained. New Operations never fall back to the old global credential or Guardrail path.

---

## Section 5: Testing Strategy and Acceptance Evidence

### 5.0 Evidence scope and traceability

This section distinguishes:

- **Issue #18 acceptance** — required for merging #18.
- **Full Contract §14 acceptance** — required before the complete commercial-access contract can be declared implemented.

A structural assertion, analogous test, skipped test, or xfail does not satisfy a deferred contract criterion.

| §14 | Criterion | #18 status |
|-----|-----------|------------|
| 1 | Cross-tenant isolation | Covered |
| 2 | Billing Webhook replay safety | Deferred to Billing Provider implementation |
| 3 | Checkout cannot elevate | Deferred to Checkout/Billing implementation |
| 4 | Outbox, version jumps, outage, lease expiry | Covered |
| 5 | Commercial lifecycle | Beta AccessGrant/Restriction subset covered; subscription grace/upgrade/downgrade deferred |
| 6 | Atomic Guardrail contention | Covered |
| 7 | Worker crash and uncertain-call safety | Covered |
| 8 | Cost evidence replay | Covered |
| 9 | Mock isolation | Covered |
| 10 | Owner/Member UI and API permissions | API covered; Playwright deferred to UI tickets |
| 11 | Workspace deletion and retention | Deferred to deletion workflow implementation |

Snapshot replay tests belong to criterion 4; they must not be presented as evidence for criterion 2.

### 5.1 Test tiers

| Tier | Database/runtime | Purpose |
|------|-----------------|---------|
| Unit | None; pure fakes | Resolution, semantic hashing, canonical signing, state transitions, Decimal arithmetic, metric aggregation |
| SQLite compatibility | SQLite | Repository portability only; never RLS, role, locking, or concurrency acceptance evidence |
| PostgreSQL integration | PostgreSQL with production migrations and roles | RLS, grants, composite FKs, partial indexes, append-only triggers, SECURITY DEFINER, CAS and atomic reservations |
| Process E2E | Next.js BFF + FastAPI + Worker + PostgreSQL | Admission through provider dispatch, settlement, recovery and API redaction |
| Playwright | Deployed UI stack | Deferred until Owner/Member pages exist |

Performance targets are monitored separately and are not correctness assertions. Concurrency tests use barriers and independent database connections — never sleeps or multiple tasks sharing one SQLAlchemy session.

Required fixtures:

- Two independently provisioned Workspaces with distinct Owners and Members.
- Real API, Worker and read-only database roles without superuser bypass.
- A controllable clock for lease, period and rolling-window boundaries.
- Scripted Provider Adapters for idempotent, non-idempotent, lookup-capable, definite-not-dispatched and uncertain outcomes.
- Fault injection at every dispatch boundary.
- A canary credential secret for leak detection.
- A fake KMS plus a real envelope-encryption test adapter using a deterministic sequence of distinct nonces. A fixed reused nonce is forbidden.

### 5.2 Entitlement, publication and tenant-isolation suites

Required evidence:

- Cross-Team isolation for PlanVersion bindings, AccessGrant, Subscription placeholder, Restriction, CommercialAccessProjection and publication.
- RLS isolation for every new domain table and customer-facing view under all three database roles.
- Composite tenant FKs reject cross-Workspace Operation, OperationStateEvent,
  Job, reservation, credential and ledger references.
- Canonical signed bytes verify correctly; unknown key IDs, tampering, wrong Workspace and wrong directional service tokens are rejected.
- Push and Pull return byte-for-byte identical persisted envelopes. Changing
  Team name or authorization state after publication cannot change an older
  Snapshot's bytes.
- Protected-header tampering (schema version, key ID or algorithm) fails
  verification. The signature is outside the signed payload.
- Invalid signatures create no tenant inbox row; they produce only security telemetry.
- Duplicate snapshots are idempotent; stale entitlement versions and stale globally monotonic lease sequences cannot regress state.
- Reusing a `snapshot_id` with different canonical bytes creates a protocol-conflict alert.
- `conflict` is the single outcome for same identity/sequence with different
  signed content and maps consistently to HTTP 409 and Outbox dead letter.
- A version gap cannot elevate entitlements. Full signed repair converges through the dedicated repair path.
- Lease renewal preserves every semantic field and increases only permitted lease/audit fields.
- Workspace name, authorization or lifecycle changes can keep the same
  entitlement version while advancing projection/lease ordering; they update
  Workspace projection fields without changing entitlement contents.
- Semantic hash changes when source references, access periods, restrictions, resolution policy, key registry or platform-limit version changes — even when the resolved visible values happen to remain equal.
- Outbox claim uses committed lease/CAS semantics; multiple dispatchers cannot own the same attempt simultaneously.
- Expired DELIVERING rows are reclaimed; terminal failures reach DEAD_LETTER without suspending the Workspace directly.
- Network delivery occurs outside the publication transaction.
- Signing-key rotation accepts the configured overlap and rejects retired keys.
- Provisioning proves the two-step sequence: provisioning snapshot applied first, then active snapshot. No paid operation is admitted between them.
- Initial v2 provisioning creates the domain Workspace with the signed
  Workspace name without first applying a partial v1 entitlement projection.
- PlanVersion publish/retire and deterministic Beta seed tests require the
  platform operator/release identity, MFA or equivalent release step-up, and
  same-transaction durable audit evidence.
- Billing Webhook and Checkout tests remain explicitly deferred; snapshot behavior is not their substitute.

### 5.3 Admission, idempotency, capacity and Guardrails

Concurrency evidence must open one transaction and connection per contender and synchronize them immediately before reservation.

Required cases:

- Exactly the reservations permitted by per-operation, rolling-24-hour, commercial-period and outstanding-exposure limits commit.
- Settled net cost, outstanding reservations and proposed reservations are counted once — never double-counted.
- Rolling-window and period-anchor boundary tests use the controlled clock.
- Missing Guardrail, unsupported currency and missing FX quote fail closed.
- Non-USD quotes succeed only with frozen exchange-rate provenance.
- Lowering a Guardrail applies immediately to new reservations without rewriting existing exposure.
- Increasing limits requires a valid step-up assertion and confirmed preview bound to actor, Workspace, action, limits, currency, revision and expiry.
- Tampered, expired, replayed or cross-Workspace preview/assertion tokens fail.
- Limits above the platform maximum are rejected, not silently clamped.
- Budget currency cannot change while payable reservations remain outstanding.
- One planned payable Provider call creates one SpendReservation. A multi-asset Operation cannot hide several calls behind one reservation.
- Storage capacity is reserved at admission; Provider concurrency is acquired at Worker execution. Ten queued calls under a concurrency limit of two may be admitted, but no more than two may dispatch simultaneously.
- `CapacityRequest.acquisition_phase` is enforced: Admission cannot create an
  occupied execution-concurrency row and Worker cannot reinterpret storage as
  execution capacity.
- Same semantic intent replays the original Operation before current quote or Guardrail calculation.
- Same semantic key with a different request fingerprint produces a conflict.
- Expiry of an HTTP idempotency record cannot create a second semantic Operation.
- A legitimate regenerate command requires a distinct intent/seed nonce and receives a distinct semantic key.
- Transaction failure rolls back Operation, Jobs, spend and capacity reservations together.
- Operation is inserted once during semantic-intent claim; Jobs exist before
  tenant-safe reservations reference them; all new Operations/Jobs persist
  their admission/execution contract versions.
- Lease-expiry tests use the mutation-policy registry: new operations, uploads, reservations, binding and Guardrail increases are blocked; reads and exports remain available; revoke credential, lower Guardrail, cancel undispatched work, settle, release and reconcile remain available as recovery operations.
- First crossing of an 80% threshold writes one durable notification intent in
  the admission transaction. Repeated requests deduplicate it. A 100%
  rejection writes one intent in the independent post-rejection transaction
  without admitting the command.
- Concurrent threshold-crossing contenders lock the same notification-state
  row and produce one crossing sequence and one durable outbox intent.
- Rolling-window hysteresis resets an 80% notification only after the
  configured lower threshold is crossed; a rejected 100% proposal cannot
  self-reset merely because it did not commit.

### 5.4 Credential and secret-safety suites

Required cases:

- Create conflicts when an active credential already exists; rotation is explicit.
- Rotate, revoke and bind enforce revision/If-Match CAS.
- State transitions are one-way and the partial unique active-version index is enforced.
- Rotation preserves DataSource identity and historical CredentialVersion references.
- Worker resolution records the exact CredentialVersion used by the call.
- Member and system contexts cannot create, list, rotate, revoke or bind credentials.
- No reveal endpoint exists.
- Swapping ciphertext, encrypted data key, nonce or AAD across Workspace, credential key or version fails decryption.
- Production startup rejects the local deterministic KMS.
- A canary secret is absent from HTTP responses, error bodies, logs, audit events, job payloads, exports, read-only views and telemetry spans.
- Credential metadata views expose only approved metadata; encrypted envelope columns remain inaccessible to the read-only role.
- The test adapter may receive plaintext at the final Provider boundary. That expected in-memory handoff is the only allowed observation of the canary secret.

### 5.5 Worker dispatch and crash safety

The dispatch-fence transaction commit is the authorization linearization point:

- Revocation or lease expiry before prepare commit blocks dispatch and releases or preserves reservations according to the typed outcome.
- Once prepare commits, the attempt owns its frozen Provider, request, CredentialVersion and reservation. Later revocation blocks subsequent attempts but does not rewrite this attempt's evidence.

Fault injection must cover:

1. Before prepare commit.
2. After prepare commit but before sending bytes.
3. After request transmission but before response.
4. After Provider success but before durable result storage.
5. After durable result storage but before finalization commit.
6. After finalization commit but before queue acknowledgement.

Assertions:

- Before prepare commit: no Provider call or cost evidence; safe requeue.
- After a possibly dispatched boundary: non-idempotent calls become uncertain and are never blindly retried.
- A Provider-confirmed idempotency key may retry only with the identical key and payload.
- Durable results can be finalized again without repeating the Provider call.
- Queue redelivery after finalization returns the terminal result without double settlement.
- Provider result lookup can reconcile success or definite non-execution.
- Outcome classification comes from the Provider Adapter contract; HTTP 400 or 500 alone must not be assumed to mean "not charged."
- Every Provider failure supplies typed dispatch certainty. A crash before a
  Provider request ID exists can reconcile by the persisted idempotency key
  only when the Adapter declares that lookup capability.
- Actual cost above reservation is recorded in full, outstanding exposure
  reaches zero, and a Guardrail breach alert is emitted. The first overage
  creates no Adjustment; only a later correction to an existing Settlement
  does.

### 5.6 Cost, usage and execution-mode evidence

Required cases:

- Ledger rows are append-only and replay deterministically to the same payable balance.
- Reservation, Reported Settlement, Estimated Settlement, Adjustment and Release transitions have valid linkage.
- A call receives Reported or Estimated settlement, never both as competing terminal facts.
- Adjustments reference their target and cannot cause replay double-counting.
- Provider and budget currencies, frozen rate, rate source and quote timestamp are preserved.
- Unsupported currency conversion fails before reservation.
- Semantic uniqueness is scoped per Workspace: duplicates within a Workspace fail; equal hashes across different Workspaces are allowed.
- The legacy ledger remains untouched and the compatibility bridge is deterministic and idempotent.
- Legacy evidence remains in explicit `legacy_platform` or
  `legacy_unknown` query buckets and never appears as Sandbox or enters new
  Guardrail aggregates.
- Usage delta/gauge and increase/decrease/set semantics follow the metric registry's aggregation policy.
- Rebuilding usage projections from events produces the same result as the online projection.

Mode isolation must cover BYOK, Sandbox and Mock independently:

- Mock payable Provider and budget amounts are zero.
- Simulated Mock amounts use separate fields.
- Mock creates no payable SpendReservation and never enters real cost, Billing, real-usage or experiment aggregates.
- Sandbox uses only an explicitly no-charge sandbox adapter; it cannot use a shared platform paid credential.
- Provider failure never changes execution mode.
- Missing explicit mode never silently selects Mock, Sandbox or BYOK.

### 5.7 API, BFF and database-security evidence

Required cases:

- Generated BFF allowlist exposes only declared paths, methods and bodies; arbitrary pass-through paths fail.
- Idempotency and If-Match headers are forwarded only according to endpoint policy.
- Credential endpoints return `Cache-Control: no-store`.
- Sensitive request bodies are excluded from logging and tracing.
- Member cost and usage responses redact CredentialVersion IDs and internal Provider identifiers.
- Owner/Member API positive and negative tests run through the BFF, not only direct FastAPI calls.
- API, Worker and read-only roles have only their documented grants.
- The API role can execute the entitlement apply function but cannot update its tables directly.
- SECURITY DEFINER functions have a fixed safe `search_path` and cannot be used for cross-tenant writes.
- Worker cannot administer Guardrails or credentials.
- Append-only triggers reject both UPDATE and DELETE.
- Playwright visibility tests remain deferred until the corresponding pages exist.

### 5.8 Migration and deployment tests

Acceptance includes:

- Fresh-database migration and upgrade from the current production schema.
- `budget_currency` backfill from the existing default currency.
- Existing ledger rows remain unchanged.
- Repeated compatibility-view queries return one deterministic public row per
  legacy source row without duplication.
- Partial indexes, composite FK candidate keys, composite FKs, triggers,
  views, functions and grants are present after migration.
- `ControlPlaneAuditEvent` and EntitlementSnapshot immutable triggers reject
  UPDATE and DELETE; Snapshot-to-audit and Outbox-to-Snapshot tenant
  relationships cannot cross Team.
- Production configuration rejects identical directional tokens, missing signing keys and local KMS.
- Dispatcher-disabled deployment can ingest snapshots before publication is enabled.
- Cutover smoke test proves new admission no longer reads the legacy global Guardrail or platform-paid credential path.

### 5.9 CI gates and retained evidence

Required PR gates:

- Unit and property tests.
- SQLite compatibility suite.
- PostgreSQL migration, RLS, role and concurrency suite.
- Process-level admission/dispatch/reconciliation E2E.
- OpenAPI generation and generated BFF diff check.
- Typecheck, lint and build for both services.
- Canary-secret leak scan.

Race tests must use deterministic barriers and run across multiple seeds. Evidence artifacts record the commit, migration revision, PostgreSQL version, suite result and §14/#18 traceability status.

Deferred criteria remain visibly DEFERRED; they may not be reported as passed or silently skipped.

---

## Section 6: Migration, Rollback, and Legacy Bridge

### 6.0 Migration scope and principles

Control plane and domain service retain independent databases and migration ownership:

| System | Database | Tool | Migration location |
|--------|----------|------|-------------------|
| Control plane | marketai_control | Prisma Migrate | `web/prisma/migrations/` |
| Domain | marketai_domain or local SQLite | Alembic | `migrations/versions/` |

The domain Alembic directory is at repository root, not under `app/adapters/persistence/`. The current head is `b9c0d1e2f3a4`; the new revision must resolve the live head when implementation starts.

Each database receives one additive expand migration in #18. Provisioning, historical reconciliation and entitlement publication are separate idempotent commands, not schema-migration side effects. Destructive contract cleanup happens only in a later release after the bake period.

Migration principles:

- Expand first; backfill; validate; cut over; contract later.
- Old and new binaries must tolerate the expanded schema during rolling deployment.
- No migration performs network calls, snapshot delivery, KMS calls or Provider calls.
- No rollback may discard credential or financial evidence.
- Every Operation and Job permanently records which admission/execution
  contract handled it; a single Operation never switches between legacy and
  new coordinators.

### 6.1 Control-plane Prisma migration

Generate a create-only migration:

```bash
cd web
npm run db:migrate:dev -- --create-only --name entitlement_commercial_access
```

Then edit the generated SQL to add PostgreSQL constraints and triggers Prisma cannot express. Production applies via `npm run db:migrate:deploy`.

The migration creates: PlanVersion, AccessGrant, Subscription, CommercialAccessBinding, Restriction, CommercialAccessProjection, EntitlementSnapshot, EntitlementSnapshotOutbox, ControlPlaneAuditEvent.

It also adds canonical Workspace lifecycle to Team.

**Existing Team lifecycle backfill:**

Existing Teams must not all default to ACTIVE because the current schema already contains `suspended`.

1. Add nullable `lifecycle`.
2. Backfill `SUSPENDED` where `suspended=true`; otherwise `ACTIVE`.
3. Add lifecycle validity constraint and make it non-null.

Legacy `suspended` remains during compatibility. Publication updates both fields transactionally: SUSPENDED, DELETION_PENDING and DELETED publish `suspended=true` for v1 consumers; PROVISIONING and ACTIVE publish `suspended=false`. Lifecycle remains the canonical v2 fact.

**Required database invariants:**

- Active base binding index uses `"deactivatedAt" IS NULL`, not a nonexistent or derived `isActive` flag.
- Exactly one source reference matches `sourceKind`.
- An AccessGrant referenced by CommercialAccessBinding must be BASE.
- Binding `planVersionId` must match its referenced grant or subscription.
- Access periods satisfy `startsAt < endsAt` when an end exists.
- Subscription periods satisfy `periodStart < periodEnd`.
- PlanVersion permits only: immutable content after insertion, and one transition from EFFECTIVE to RETIRED (setting `retiredAt` as part of that transition). DRAFT is not introduced.
- EntitlementSnapshot receives an append-only trigger rejecting both UPDATE and DELETE. Outbox rows remain mutable according to their delivery state machine.
- `ControlPlaneAuditEvent` receives an append-only trigger rejecting both
  UPDATE and DELETE.
- `CommercialAccessProjection.currentSnapshotId` is unique. Snapshot-to-audit
  and Outbox-to-Snapshot relations use same-Team composite foreign keys.
- Every EntitlementSnapshot stores a non-null exact `canonicalEnvelope` plus
  the entitlement-registry and resolution-policy versions used to publish it.

Seeding the Beta PlanVersion, AccessGrants and bindings is not part of the
Prisma migration. It uses deterministic keys, requires the authorized release
identity and its step-up control, emits durable audit evidence and can be
rerun safely. Existing content under the same deterministic identity must
match exactly or the seed command fails.

### 6.2 Domain Alembic migration

The existing Operation model is extended; a second Operation table is not created.

Recommended dependency order:

1. Extend workspaces.
2. Extend operation_records.
3. Extend jobs and operation_state_events.
4. Create entitlement inbox and projection.
5. Create credentials and bindings.
6. Create Guardrails and Guardrail events.
7. Create spend and capacity reservations.
8. Create Provider call attempts.
9. Create Provider cost ledger.
10. Create usage events.
11. Create Guardrail notification state and customer notification outbox.
12. Create metadata and compatibility views.
13. Install functions, triggers, RLS and grants.

**Workspace expansion:**

1. Add `lifecycle` and `budget_currency` as nullable columns.
2. Backfill `lifecycle` from existing `suspended` (not unconditional `active` default).
3. Backfill `budget_currency` from each row's existing `default_currency`.
4. Validate lifecycle and `^[A-Z]{3}$` currency constraints.
5. Make both columns non-null.
6. Remove temporary server defaults so later writes must choose values deliberately.

Existing `suspended` and authorization columns remain during compatibility.

**Operation and Job expansion:**

Add to `operation_records`: `semantic_key_hash`, `request_fingerprint_hash`, `admission_contract_version`. Add unique constraint on `(workspace_id, semantic_key_hash)` for new-contract Operations. Legacy Operations receive a deterministic legacy identity or remain governed by `admission_contract_version=0`; they must not be mistaken for newly admitted semantic commands.

Add to `jobs`: `operation_id`, `entitlement_version_at_enqueue`, `execution_contract_version`. Use composite FK: `(workspace_id, operation_id) → operation_records(workspace_id, id)`. Existing Jobs remain version 0 with nullable new fields. A CHECK requires version-1 Jobs to carry their Operation and entitlement version.

Add `workspace_id` to `operation_state_events`, backfill it from the referenced
Operation, validate it, make it non-null and replace the ID-only reference
with `(workspace_id, operation_id) → operation_records(workspace_id, id)`.
The current Operation projection and each new state event must be written in
the same transaction.

Every table targeted by a same-Workspace composite foreign key adds the
matching `UNIQUE(workspace_id, id)` candidate key, including CredentialVersion,
SpendReservation, CapacityReservation, ProviderCallAttempt and
ProviderCostLedgerEntry.

**Entitlement application interface:**

A single deep database interface:

```sql
marketai_apply_workspace_projection_v2(...)
```

It atomically: applies Workspace identity/name, authorization and lifecycle;
inserts the verified canonical inbox envelope; enforces entitlement-version,
lease-sequence and projection-version ordering; updates the current
EntitlementProjection; returns
applied/duplicate/stale/version_jump_paused/conflict.

Push and pull paths call this same interface. The existing v1 function remains temporarily for old callers. Dispatcher activation is forbidden until every deployed domain instance supports v2. The v1 function is revoked and removed only in a later contract migration.

**RLS, views and grants:**

- Every Workspace-owned table receives `ENABLE` and `FORCE ROW LEVEL SECURITY`.
- Tables written through `marketai_apply_workspace_projection_v2` receive no direct runtime DML grant.
- Credential metadata view must be a security-invoker/security-barrier view with tenant filtering.
- Both the Alembic migration and `deploy/postgres/domain-tenant-roles.sql` must be updated. The role convergence script revokes grants before rebuilding its explicit matrix.
- Append-only triggers apply to: `entitlement_snapshot_inbox`,
  `spend_guardrail_events`, `provider_cost_ledger`, `usage_events`.
- Guardrail Notification State transitions use row locks/CAS. Customer
  Notification Outbox payload/deduplication fields are immutable; only its
  delivery lease/state fields are mutable through CAS.
- Guardrail Notification State installs the exact revision/policy/window/
  dimension/threshold unique key plus state and non-negative crossing-sequence
  CHECK constraints from Section 2.13.
- Trigger functions use pinned `search_path`; PUBLIC receives no execution privilege.

### 6.3 Legacy cost evidence bridge

One bridge strategy in #18: a **read-only compatibility view**. Historical rows are not also copied into the new ledger (running both would expose every legacy row twice and could double reporting totals).

The view:

- Prefixes its public evidence ID with the source to prevent ID collision.
- Preserves the original row ID separately.
- Maps `reserved` → `reservation`.
- Maps `released` → `release`.
- Maps `actual` → `estimated_settlement` (correct because the current implementation records configured unit price when Provider usage is absent).

Execution mode is not hardcoded to `sandbox`:

- A legacy row joined to a run whose provider is `mock` → `mock`.
- A non-Mock legacy Provider call → `legacy_platform`.
- Missing provenance → `legacy_unknown`.

Historical global-key calls were not Provider sandbox calls; labelling them `sandbox` would corrupt mode isolation evidence.

Legacy rows contain only one currency. The bridge exposes the same amount for historical display; exchange rate and rate-provider provenance remain null with `legacy_single_currency` provenance. It must not invent an audited FX rate of 1.0.

The compatibility view is SELECT-only, used by reporting and audit queries,
and hidden behind the CostEvidenceQuery Module. It is excluded from the new
CostReconciler mutation path, Guardrail admission and outstanding-reservation
calculations.

### 6.4 Rollback policy

Database rollback and product rollback are different operations.

**Before feature activation** — if no snapshots, credentials, reservations, usage or Provider ledger facts have been written: disable the new code; Alembic downgrade may run only after a preflight proves every new table empty; control-plane cleanup uses a corrective forward migration (`prisma migrate resolve --rolled-back` only repairs migration history for a failed migration, it does not undo a successfully applied migration).

**After snapshots but before payable cutover** — disable dispatcher and new
admission; redeploy compatible code; keep the additive schema and historical
rows; deactivate or revoke seeded access through normal control-plane
commands; do not delete audit history.

**After any credential or payable evidence exists** — schema rollback is prohibited. Credentials cannot be re-derived and Provider cost/usage evidence cannot be discarded. Recovery consists of: fail closed for new paid mutations if necessary; keep reconciliation, settlement, release, credential revocation and reads running; deploy a forward corrective migration or code fix; retain all new tables and evidence. A database restore is disaster recovery, not routine feature rollback.

### 6.5 Phased deployment sequence

| Phase | Action | Safe rollback |
|-------|--------|---------------|
| 0 | Backup, migration dry run, table counts, role and target verification | No state changed |
| 1 | Domain expand migration plus updated role matrix | Leave schema dormant |
| 2 | Deploy FastAPI/Worker with v2 ingestion and recovery; admission disabled | Redeploy compatible code |
| 3 | Control-plane schema and Publication Module; dispatcher disabled | Disable dispatcher; if old code is restored, disable commercial mutation commands |
| 4 | Idempotently seed Beta PlanVersion, eligible AccessGrants and initial snapshots | Revoke/deactivate through control-plane commands |
| 5 | Enable dispatcher for canary cohort; verify actual domain versions and valid leases | Disable dispatcher; retain snapshots |
| 6 | Converge all eligible Workspaces; verify Guardrail and lifecycle projections | Pause before admission |
| 7 | Run shadow admission comparison without Provider calls or payable reservations | Disable shadow mode |
| 8 | Enable new admission by Workspace cohort; persist Operation admission and Job execution contract versions | Stop new paid admission; do not switch existing Operations |
| 9 | Stop creating legacy-path Operations; let existing legacy Operations finish on their original coordinator | Recovery-only mode |
| 10 | Bake for at least 72 hours with reconciliation and drift monitoring | Forward fix or recovery-only mode |
| 11 | Later release removes legacy flags and code after evidence retention is approved | Separate contract migration |

Cutover gate: domain must confirm expected snapshot ID, entitlement version, lease sequence, lifecycle and valid lease with no unresolved version jump. Old cost writes are not globally disabled until legacy Operations have drained.

### 6.6 SQLite compatibility

SQLite uses the same Alembic revision with dialect-specific implementations behind the persistence Adapter. SQLite supports partial indexes, so the active-credential partial unique index is still created.

Accepted limitations:

- No RLS or database roles.
- No SECURITY DEFINER.
- No row-level `SELECT ... FOR UPDATE`.
- Database-wide write serialization is not evidence of PostgreSQL reservation correctness.
- Credential metadata protection is repository-enforced rather than grant-enforced.
- Batch table rebuilds must reinstall affected triggers and indexes.

The SQLite Adapter implements the same entitlement-application interface transactionally for local development. Callers must not branch on database dialect.

SQLite remains compatibility evidence only; all locking, RLS, grants, function security and concurrency acceptance tests run on PostgreSQL.

---

## Section 7: Observability

### 7.0 Scope and evidence classes

Four evidence classes must remain separate:

| Class | Purpose | Authoritative | Failure behavior |
|-------|---------|--------------|------------------|
| Domain facts | Entitlement, reservation, attempt, Usage and Provider Cost history | Yes | Mutation fails if the fact cannot commit |
| Audit trail | Actor-attributed administrative and security-sensitive commands | Yes | Commits in the same transaction as the command |
| Operational telemetry | Logs, metrics and traces used to operate the system | No | Export failure never changes domain outcome |
| Customer notification | Owner warnings such as 80% Guardrail use | Durable delivery intent | Retried independently; not an ops page |

Metrics, logs and traces never replace EntitlementSnapshot,
ProviderCallAttempt, SpendReservation, ProviderCostLedgerEntry, UsageEvent,
AuditEvent or CustomerNotificationOutbox delivery intent.

The current control-plane `sendAudit()` only emits a redacted structured log. It is not a durable audit trail. Section 1 and Section 6 therefore add an append-only `ControlPlaneAuditEvent` table.

### 7.1 Observability modules

**AuditTrail:** Appends a durable event inside the caller's existing transaction and returns its ID. Domain Adapter uses the existing `append_event()`. Control-plane Adapter writes `ControlPlaneAuditEvent`. Audit failure aborts the associated state mutation. Audit details pass through mandatory redaction. Raw Workspace and actor identifiers are permitted only in tenant-scoped or access-controlled audit storage.

**OperationalTelemetry:** Accepts a typed, versioned signal and owns: structured logging, metric instruments and label allowlists, trace/span creation, redaction, sampling, cardinality enforcement, exporter failure handling. Callers do not construct arbitrary metric labels or log dictionaries.

Adapters: control-plane OTLP/structured-log Adapter; domain OTLP/structured-log Adapter; no-op Adapter for tests and local environments without an exporter.

**CustomerNotificationWriter:** Persists deduplicated customer-safe intent in
the domain Outbox. It does not emit pages or depend on metric delivery.
External channel Adapters consume the Outbox only after their separate release
gate is satisfied.

### 7.2 Structured event contract

Every operational event uses a common envelope:

- `event`, `event_schema_version`, `timestamp`, `severity`, `service`, `environment`, `deployment_id`, `outcome`, `reason_code`, `request_id`, `trace_id`, `workspace_ref`

`workspace_ref` is an opaque HMAC-derived operational correlation value using a dedicated observability key. Raw `workspace_id` is excluded from central telemetry.

Prohibited from logs and traces: credential plaintext/ciphertext/data keys/nonces/AAD; MFA assertions and preview tokens; authorization headers/cookies/service tokens/signing material; Provider request/response bodies; prompt/asset/customer content; raw Workspace/user/credential identifiers; `semantic_key_hash`; Provider request IDs; exact financial amounts (unless explicitly required by a restricted security event); arbitrary exception text or Provider response text.

Operational references (Operation, attempt, reservation IDs) use opaque `resource_ref` values. Exact IDs remain in authoritative tables and tenant audit.

All reason, provider, command and outcome values come from bounded registries. Unknown values normalize to `other`; untrusted input never becomes a new metric label or event name.

Success events are emitted only after commit. A pre-commit signal must be named `*.started` and cannot be interpreted as successful admission or settlement.

### 7.3 Event catalog

**Entitlement propagation:**

| Event | Required bounded fields |
|-------|----------------------|
| entitlement.snapshot.published | outcome, semantic_changed, lifecycle |
| entitlement.outbox.delivery | outcome, attempt_bucket, delivery_kind |
| entitlement.outbox.dead_lettered | reason_code, lease_risk |
| entitlement.inbox.applied | outcome, delivery_kind, version_gap |
| entitlement.signature.rejected | rejection_reason, key_status |
| entitlement.repair.requested | reason_code, version_gap_bucket |
| entitlement.repair.completed | outcome |
| entitlement.lease.state_changed | from_state, to_state, reason_code |

Signature rejection fields are derived only from trusted route/configuration state. An attacker-supplied key ID is represented as `known`, `unknown` or `missing`, never logged verbatim.

Lease expiry is logged once per Workspace and lease sequence when state crosses into expired. Individual rejected requests increment an aggregate counter but do not emit an unbounded stream of identical expiry logs.

**Admission and Guardrails:**

| Event | Required fields |
|-------|----------------|
| admission.completed | outcome, command_type, execution_mode, reason_code |
| admission.replayed | command_type, execution_mode |
| guardrail.reservation_rejected | dimension, reason_code, execution_mode |
| guardrail.configuration_changed | direction, changed_dimensions, step_up_used |
| guardrail.threshold_crossed | dimension, threshold, period_kind |

Exact limits, current spend and attempted amounts remain in the Guardrail event/audit records. Central logs carry only configured buckets.

Threshold events are emitted from the same durable transition state used by
the customer Outbox. They are deduplicated by Workspace, Guardrail/platform
policy version, dimension, stable window key, threshold and crossing sequence
— not emitted on every subsequent request.

**Dispatch, reconciliation and credentials:**

| Event | Required fields |
|-------|----------------|
| dispatch.attempt_state_changed | from_state, to_state, provider, execution_mode |
| dispatch.capacity_waited | provider, wait_bucket |
| dispatch.result_lookup | provider, outcome |
| reconciliation.completed | from_state, to_state, provider, outcome |
| cost.breach_detected | execution_mode, currency, overage_bucket |
| credential.admin_completed | operation, provider, outcome |
| credential.unwrap_completed | KMS backend, provider, outcome |
| credential.binding_resolved | provider, outcome |

ProviderCallAttempt remains the authoritative record of the exact CredentialVersion and Provider request ID. Those identifiers are not duplicated into central logs.

### 7.4 Metrics

Metric labels never contain: Workspace, user, Operation, Job, reservation, attempt, snapshot or credential identifiers; hashes or arbitrary error text; arbitrary command, entitlement or Provider names.

Allowed labels come from bounded registries: `outcome`, `reason_code`, `delivery_kind`, `execution_mode`, `entry_type`, `provider`, `command_type`, `dimension`, `state`, configured ISO currency, `idempotency_supported`.

Service, environment, region and deployment are resource attributes, not repeated business labels.

**Control-plane metrics:**

| Instrument | Type | Labels |
|-----------|------|--------|
| marketai.control_plane.entitlement.snapshot.published | Counter | outcome |
| marketai.control_plane.entitlement.outbox.delivery | Counter | outcome, reason_code |
| marketai.control_plane.entitlement.outbox.pending | Observable gauge | state |
| marketai.control_plane.entitlement.outbox.oldest_age | Observable gauge | state |
| marketai.control_plane.entitlement.outbox.delivery_duration | Histogram | outcome |
| marketai.control_plane.entitlement.audit_failure | Counter | operation |

**Domain metrics:**

| Instrument | Type | Labels |
|-----------|------|--------|
| marketai.domain.entitlement.inbox.apply | Counter | outcome, delivery_kind |
| marketai.domain.entitlement.lease_workspaces | Observable gauge | lease_state |
| marketai.domain.entitlement.lease_min_remaining | Observable gauge | lifecycle |
| marketai.domain.entitlement.repair | Counter | outcome, reason_code |
| marketai.domain.admission.commands | Counter | outcome, command_type, execution_mode |
| marketai.domain.admission.duration | Histogram | command_type, outcome |
| marketai.domain.guardrail.rejections | Counter | dimension, reason_code |
| marketai.domain.dispatch.attempts | Counter | provider, execution_mode, outcome |
| marketai.domain.dispatch.duration | Histogram | provider, outcome |
| marketai.domain.dispatch.capacity_wait_duration | Histogram | provider |
| marketai.domain.reservation.state_count | Observable gauge | state, execution_mode |
| marketai.domain.reservation.oldest_uncertain_age | Observable gauge | provider |
| marketai.domain.reconciliation.attempts | Counter | provider, outcome |
| marketai.domain.ledger.entries | Counter | entry_type, execution_mode, currency |
| marketai.domain.credential.admin | Counter | operation, provider, outcome |
| marketai.domain.credential.unwrap_duration | Histogram | KMS backend, outcome |
| marketai.domain.telemetry.export_failures | Counter | signal_type |

There is no per-Workspace metric.

Exact outstanding spend, settlement amounts and Guardrail utilization are queried from authoritative database projections. Process counters and gauges can be dropped, delayed or reset and therefore cannot be financial or admission truth.

### 7.5 Alerts and customer notifications

Alert predicates, ownership, runbook URL, evaluation window and environment-specific thresholds are version-controlled. Absolute values are not hardcoded into the design.

**Operational alerts:**

| Alert | Signal | Default severity |
|-------|--------|-----------------|
| EntitlementDeliveryAtRisk | Oldest pending delivery approaches renewal safety budget | Warning |
| ActiveLeaseCoverageLost | Active Workspace unexpectedly expires because propagation failed | Critical |
| OutboxDeadLetterWithLeaseRisk | Dead letter plus insufficient remaining lease | Critical |
| ProjectionRepairLoop | Repeated repair without convergence | Warning |
| AuthenticatedSignatureFailure | Authenticated control-plane delivery fails signature verification | Critical |
| AdmissionInvariantFailure | Internal/system rejection reason, not normal customer limit | Critical |
| UncertainExposureStale | Oldest uncertain reservation exceeds reconciliation SLO | Warning/Critical by age |
| CostBeyondReservation | Actual payable cost exceeds reserved amount | Critical |
| KMSUnavailable | Credential unwrap failures exceed error-budget threshold | Critical |
| CredentialAuthenticationFailureSpike | Repeated envelope authentication failures | Critical |
| DispatcherOrWorkerHeartbeatMissing | Expected processing loop stops | Critical |
| TelemetryBlindSpot | Export failures or missing scrape/export heartbeat | Warning |
| CredentialLeakCanaryDetected | Canary secret appears in any forbidden sink | Critical |

A single normal entitlement expiry, Guardrail rejection or customer cancellation is not an operational incident.

Signature noise from unauthenticated internet traffic is rate-limited and aggregated. A failure from an authenticated directional control-plane identity is treated much more seriously.

**Customer notifications:**

- At 80%: notify Owner once per durable threshold-crossing sequence.
- At 100%: record the rejection and notify Owner.
- When usage falls below the reset threshold or a new period begins, notification eligibility resets.

Notifications use `GuardrailNotificationStateRecord` plus
`CustomerNotificationOutboxRecord` with a deterministic deduplication key.
The versioned hysteresis policy governs 80% reset; 100% rejection notification
eligibility resets only on the documented revision/window boundary. Issue #18
provides the durable intent and in-app query path; external email/push channel
Adapters remain deferred and disabled. Metric delivery is not used to send
customer notifications.

### 7.6 Distributed tracing

The current `request_id` and `trace_id` fields are correlation IDs, not proof that distributed tracing exists. Real tracing requires W3C `traceparent` propagation through: Browser/BFF → FastAPI → Operation/Job → Worker → Provider Adapter.

A long-running Worker span starts a new trace or uses a span link to the admitted request context; it does not keep an HTTP parent span open for the entire queue duration.

Sanitized spans: `entitlement.publish`, `entitlement.deliver`, `entitlement.apply`, `admission.evaluate`, `dispatch.prepare`, `provider.call`, `dispatch.finalize`, `cost.reconcile`, `credential.unwrap`.

Allowed span attributes are limited to bounded operational values: provider, execution mode, command type, outcome, reason code, delivery kind, version-gap boolean, idempotency capability, retry classification.

No Workspace or business-resource identifiers, semantic hashes, monetary amounts, Provider request IDs or credential identifiers are attached. Trace baggage never contains tenant or authentication data.

Automatic HTTP instrumentation must disable capture of: request and response bodies; authorization and cookie headers; query strings containing signed or sensitive values; Provider payloads and URLs containing credentials.

Success traces may be sampled. Security rejection, dead-letter, uncertain dispatch and cost-breach signals are never sampled out of logs or metrics.

### 7.7 Authoritative audit integration

Not every machine transition needs a duplicate AuditEvent.

| Transition | Authoritative evidence |
|-----------|----------------------|
| Plan publish/retire, grant, restriction and binding administration | ControlPlaneAuditEvent |
| Snapshot publication and delivery | Snapshot + Outbox; publication references control audit |
| Entitlement application | Inbox + EntitlementProjection |
| Credential create/rotate/revoke/bind | Credential records + domain AuditEvent |
| Admission and Operation lifecycle | Operation + OperationStateEvent |
| Provider dispatch | ProviderCallAttempt |
| Reservation state | SpendReservation plus linked ProviderCostLedgerEntry facts |
| Reported/estimated cost and adjustments | ProviderCostLedgerEntry |
| Usage | UsageEvent |
| Manual reconciliation or override | Domain AuditEvent plus affected fact rows |

Audit events are reserved for actor-attributed commands, manual recovery, administrative changes and security-sensitive decisions. Machine facts remain in their specialized append-only stores.

Domain audit preserves: Workspace ID, Initiator ID and admitted role, Executor, Request and trace correlation, Authorization epoch, Stable event and resource types, Redacted details.

Invalid entitlement signatures do not enter a tenant AuditEvent because the claimed Workspace is untrusted. They produce a platform security signal instead.

Operational logs and traces have independently configured, finite retention. Audit, Provider Cost and Usage retention follow their contractual policies.

### 7.8 Verification

Automated evidence includes:

- Event-schema tests rejecting unknown fields and unbounded labels.
- Metric cardinality-budget tests.
- Canary-secret scans across logs, metrics, traces, audit, error responses and exports.
- Tests proving exporter failure does not roll back business state.
- Tests proving required AuditTrail failure does roll back its associated command.
- Commit-order tests preventing false `*.completed` events before transaction commit.
- Lease and threshold deduplication tests.
- Trace-propagation tests through BFF, FastAPI, Job and Worker.
- Tests proving Provider instrumentation captures no body, credential header, prompt or signed URL.
- Alert-query tests against controlled clocks and seeded failure states.
- Customer-notification deduplication and period-reset tests.
- Runbook and alert-owner validation in CI.

---

## Section 8: Risks, Release Gates, Open Decisions, and Deferred Work

### 8.0 Scope

This section distinguishes five categories:

1. **Known risk** — implementation must mitigate and test it.
2. **Merge gate** — #18 cannot merge without evidence.
3. **Production release gate** — code may merge, but the affected capability remains disabled.
4. **Open decision** — requires an owner, deadline and fail-closed default.
5. **Deferred or explicitly excluded work** — does not count as completed Contract §14 evidence.

An unanswered question never silently selects a permissive behavior.

### 8.1 Risk register

| ID | Risk | Required mitigation | Gate/owner |
|----|------|---------------------|------------|
| R1 | Entitlement propagation failure causes widespread lease expiry | Canary cohorts; jittered renewals; signed full-repair pull; minimum lease coverage alert; no unsigned manual override | Production / Control plane |
| R2 | No production KMS Adapter | Production rejects local KMS; BYOK remains disabled until a production Adapter and rotation drill pass | Production BYOK / Security |
| R3 | Large migration or role convergence causes outage | Staging-clone rehearsal; row/lock inventory; lock timeout; backup; updated role scripts; expand-only schema | Merge and production / Data |
| R4 | Semantic intent is classified incorrectly | Versioned canonicalization; permanent semantic key plus request fingerprint; explicit regeneration nonce; conflict on same key/different fingerprint | Merge / Domain |
| R5 | Worker crash or ambiguous Provider outcome causes duplicate payment | Durable dispatch fence; capability registry; same Provider idempotency key on permitted retry; uncertain state and reconciliation | Merge / Worker |
| R6 | Actual cost exceeds reservation | Settle the full amount exactly once; record breach delta; block future reservations as required; never double-count Adjustment | Merge / Cost |
| R7 | Currency configuration allows unsafe admission | Beta supports USD only; non-USD admission fails closed; inventory existing Workspaces before seeding | Production / Product and Cost |
| R8 | Credential plaintext leaks through logs, errors, jobs or tracing | Central redaction; no secret-bearing job payload; canary scans; sanitized Provider errors; shortest possible plaintext lifetime | Merge / Security |
| R9 | RLS, grants or SECURITY DEFINER functions enable cross-tenant access | FORCE RLS; fixed search_path; composite tenant FKs; explicit role convergence; raw-SQL negative tests | Merge / Data and Security |
| R10 | Legacy bridge double-counts or misclassifies historical cost | View-only bridge; source-prefixed IDs; legacy_platform/legacy_unknown; exclude from admission aggregates | Merge / Cost |
| R11 | Rollback destroys credentials or financial evidence | Forward-only after first sensitive/payable write; recovery-only mode; corrective migration; destructive downgrade preflight | Production / Release |
| R12 | SQLite success hides PostgreSQL failure | PostgreSQL acceptance gate; production rejects SQLite; SQLite provides functional compatibility only | Merge / Domain |
| R13 | Dispatcher instances race or abandon delivery leases | Claim transaction commits before network; database time; lease/CAS ownership; expired-claim recovery; idempotent inbox | Merge / Control plane |
| R14 | Audit or telemetry becomes a second inconsistent truth | Specialized fact tables remain authoritative; audit is transactional; telemetry is best-effort and post-commit | Merge / Platform |
| R15 | Observability is blind or leaks tenant identifiers | No tenant IDs in metric labels/traces; telemetry health alert; bounded registries; canary-secret verification | Production / Platform |

**Cost overage clarification:**

Two different cases must not be conflated:

- First settlement reports $7 against a $5 reservation: write one $7 settlement and emit a $2 breach delta. Do not also write a $2 Adjustment.
- An earlier $5 settlement is later corrected to $7: append a $2 Adjustment referencing the original settlement.

This preserves deterministic replay.

**Entitlement repair clarification:**

The fallback is an authenticated pull of the same signed full WorkspaceProjectionEnvelope. There is no endpoint that manually edits the domain projection, extends a lease or bypasses signature verification.

### 8.2 Release gates and open decisions

Every open decision has a fail-closed default.

| ID | Decision | Required by | Default if unresolved |
|----|----------|-------------|----------------------|
| D1 | Production KMS vendor, region, key hierarchy and rotation procedure | Before production BYOK | BYOK disabled |
| D2 | Capability matrix for each Provider: idempotency acceptance, lookup keys, dispatch-certainty classification, true sandbox, pricing evidence | Before enabling that Provider | Provider unavailable |
| D3 | MFA/step-up issuer and challenge flow | Before enabling Guardrail increases | Reads and decreases allowed; increases rejected |
| D4 | Eligible existing Workspace cohort and USD compatibility | Before Beta seed/backfill | Workspace not seeded or paid admission disabled |
| D5 | Audit, financial evidence, trace and log retention policy | Before production | Conservative retention with no destructive cleanup |
| D6 | Alert SLOs, paging ownership and runbooks | Before cohort cutover | Cutover blocked |

These are not open architecture questions:

- Beta PlanVersion v1 uses the Contract §8 defaults. A later change creates a new immutable PlanVersion.
- PlanVersion publish/retire requires platform authorization, step-up and
  durable audit; the seed release identity is not an authorization bypass.
- Sandbox is unavailable unless a specific Adapter is certified no-charge.
- Workspace deletion orchestration is deferred.
- ControlPlaneAuditEvent is required by #18.
- Lease sequence is strictly monotonic within each Workspace and never resets.
- PostgreSQL is the acceptance database.

### 8.3 Signing-key rotation runbook

Signing-key rotation is a production gate, but its architecture is resolved:

1. Add the new verification key to the domain trust ring.
2. Deploy and verify both old and new keys are accepted.
3. Switch the control plane's active signing key.
4. Continue accepting the old key for at least the maximum snapshot validity, clock-skew allowance and repair/retry window.
5. Confirm no valid active snapshot or delivery still depends on the old key.
6. Retire the old key and alert on any subsequent use.

Signing keys, directional service tokens and observability-correlation keys are separate key families.

### 8.4 Required cross-section amendments

The five original Section 1 addenda remain explicit:

1. `ControlPlaneAuditEvent` is durable and append-only;
   `sendAudit()` only mirrors committed events.
2. `Team.lifecycle` is canonical and existing `suspended=true` backfills to
   SUSPENDED.
3. Lifecycle is carried in the signed Workspace Projection Envelope.
4. `projection_version` is carried in that signed envelope.
5. `lease_sequence` is strictly monotonic per Workspace and never resets.

The consolidated cross-section corrections are also normative:

- Push and Pull use one byte-identical persisted v2 envelope whose protected
  header and complete payload are signed. A v1 application never advances
  entitlement contents.
- Operation and Job records carry
  `admission_contract_version`/`execution_contract_version`; existing
  Operations finish on their original coordinator.
- Initial legacy compatibility is the read-only bridge view only; historical
  rows are not also copied.
- A first settlement above its Reservation is recorded once, with
  non-financial breach evidence rather than a duplicate Adjustment.
- Customer Guardrail warnings create a durable, deduplicated domain Outbox
  intent; external delivery channel Adapters remain deferred.

### 8.5 Deferred follow-up work

| Item | Condition |
|------|-----------|
| Stripe/Billing Provider Adapter, Webhook Inbox and reconciliation | Required before paid subscriptions |
| Checkout and payment UI | Required before self-service paid access |
| Subscription grace, upgrade and downgrade behavior | Requires Billing Provider |
| Production KMS Adapter | Required before production BYOK |
| Non-USD quoting and settlement | Requires certified FX Adapter |
| Owner/Member pages and Playwright visibility tests | Required for Contract §14 criterion 10 |
| Full Workspace deletion and pseudonymized retention orchestration | Required for Contract §14 criterion 11 |
| MFA challenge UI and assertion issuer | Required before Guardrail increases |
| Provider-specific Sandbox Adapter | Enabled only after no-charge certification |
| Customer notification email/push channel Adapters | Durable Outbox intent and in-app query are in #18; external delivery requires a later release |
| Materialized Usage period projections | Added only when measured query performance requires them |

### 8.6 Explicitly excluded from Beta

These items are not merely deferred implementation tasks:

- Kafka or another message bus.
- Credits wallet or prepaid Provider balance.
- Platform-shared paid Provider credentials.
- Automatic overage billing.
- Automatic Plan upgrade or Guardrail increase.
- Treating Provider Cost as MarketAI subscription revenue.
- Using telemetry or projected dashboard values for admission decisions.
- Allowing Provider failure to change execution mode.

### 8.7 Acceptance-status statement

Completing #18 does not by itself satisfy the complete Contract §14 gate.

After #18:

- Criteria 1, 4, 6, 7, 8 and 9 can be fully evidenced.
- Criterion 5 is evidenced only for Beta AccessGrant and Restriction behavior.
- Criterion 10 has API permission evidence but not Playwright page evidence.
- Criteria 2, 3 and 11 remain deferred.

The project must not report the full commercial-access contract as implemented while those rows remain deferred. Merging #18 with only the deterministic local KMS is permitted by the ticket, but production BYOK remains disabled.

### 8.8 Architecture decisions to record

Three decisions merit ADRs because they are difficult to reverse and result from real trade-offs:

1. Signed bounded entitlement leases across independently owned databases.
2. Durable Provider dispatch fence with uncertain-state reconciliation.
3. Forward-only payable cutover with a view-only legacy evidence bridge.
