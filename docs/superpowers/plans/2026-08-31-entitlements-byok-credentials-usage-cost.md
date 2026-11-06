# Entitlements, BYOK Credentials, Usage, and Provider Cost Controls — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement immutable plan versioning, entitlement snapshot propagation with bounded leases, BYOK credential management, provider selection, append-only usage and cost ledger models, atomic cost reservation before job enqueue, and Owner/Member authorization — delivered as a single PR organized into five dependency-stage commits.

**Architecture:** The system spans three deployment units: Next.js control plane (`web/`), FastAPI domain API (`app/`), and Worker (`worker/`). The control plane owns identity, subscriptions, and entitlement publication; the domain owns enforcement, credentials, usage, and cost. Entitlement snapshots propagate from control plane to domain via a transactional outbox + push/pull pattern, with HMAC-signed bounded leases (24h, 6h renewal). Every paid external call passes through a durable dispatch fence (ProviderCallAttempt) that prevents double-charging on crashes.

**Tech Stack:** TypeScript/Next.js/Prisma (control plane), Python/FastAPI/SQLAlchemy/Alembic (domain), PostgreSQL with RLS, HMAC-SHA256 envelope signing, AES-256-GCM envelope encryption for credentials.

**Spec:** `docs/superpowers/specs/2026-08-31-entitlements-byok-credentials-usage-cost-design.md`

## Global Constraints

- PostgreSQL is the acceptance database; SQLite is a local compatibility path only.
- All cross-table references simultaneously bind `workspace_id` (composite FK).
- Financial amounts use `Numeric(24, 12)`.
- Append-only tables (`entitlement_snapshot_inbox`, `provider_cost_ledger`, `usage_events`, `spend_guardrail_events`) are enforced by database triggers rejecting UPDATE and DELETE.
- RLS is FORCE-enabled on every Workspace-owned table.
- SECURITY DEFINER functions use empty `search_path` and schema-qualified names.
- `lease_sequence` is strictly monotonic per Workspace, never resets.
- `DEFAULT_WORKSPACE_ID` is not a tenant boundary.
- `META_WRITE_ACCESS_TOKEN` stays empty; Scale never performs Meta writes.
- Existing `cost_ledger` is preserved unchanged — no rename, no delete.
- The contract at `docs/architecture/subscription-entitlement-usage-cost-ownership.md` is the source of truth.

---

## File Structure

### Stage 1 — Control Plane (web/)

| Action | Path | Responsibility |
|--------|------|----------------|
| Modify | `web/prisma/schema.prisma` | Add 9 models, 8 enums, Team.lifecycle |
| Create | `web/prisma/migrations/<timestamp>_entitlement_commercial_access/migration.sql` | Prisma migration + hand-edited constraints/triggers |
| Create | `web/lib/entitlements/publication.ts` | Publication Module: resolve, hash, sign, snapshot, outbox |
| Create | `web/lib/entitlements/signing.ts` | HMAC-SHA256 envelope signing and verification |
| Create | `web/lib/entitlements/resolution.ts` | Entitlement resolution: base + addons + restrictions + platform clamp |
| Create | `web/lib/entitlements/dispatcher.ts` | Outbox dispatcher: claim, deliver, CAS update |
| Create | `web/lib/entitlements/types.ts` | Shared types: WorkspaceProjectionEnvelope, FrozenQuote, etc. |
| Create | `web/pages/api/internal/domain/workspaces/[workspaceId]/workspace-projection-snapshot.ts` | v2 pull-repair endpoint |
| Modify | `web/middleware.ts` | Add new internal route to service-auth bypass |
| Create | `web/tests/entitlements/publication.test.ts` | Publication unit tests |
| Create | `web/tests/entitlements/resolution.test.ts` | Resolution unit tests |
| Create | `web/tests/entitlements/signing.test.ts` | Signing unit tests |
| Create | `web/tests/entitlements/dispatcher.test.ts` | Dispatcher unit tests |

### Stage 2 — Domain Models (app/, migrations/)

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `app/entitlements/__init__.py` | Package init |
| Create | `app/entitlements/models.py` | SQLAlchemy models: EntitlementSnapshotInboxRecord, EntitlementProjectionRecord |
| Create | `app/entitlements/credentials.py` | CredentialVersionRecord, CredentialBindingRecord |
| Create | `app/entitlements/cost_models.py` | SpendGuardrailRecord, SpendReservationRecord, CapacityReservationRecord, ProviderCostLedgerEntryRecord, ProviderCallAttemptRecord |
| Create | `app/entitlements/usage_models.py` | UsageEventRecord, CustomerNotificationOutboxRecord, GuardrailNotificationStateRecord, SpendGuardrailEventRecord |
| Create | `migrations/versions/<rev>_entitlement_commercial_access.py` | Alembic migration: all new tables, extensions, constraints, triggers, RLS, grants |
| Modify | `app/adapters/persistence/models.py` | Extend WorkspaceRecord (lifecycle, budget_currency), OperationRecord (semantic_key_hash, etc.), JobRecord (operation_id, etc.) |
| Modify | `deploy/postgres/domain-tenant-roles.sql` | Updated role convergence with new table grants |

### Stage 3 — Domain Modules (app/)

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `app/entitlements/projection.py` | EntitlementProjectionModule: ingest, repair, verify signature |
| Create | `app/entitlements/policy.py` | EntitlementPolicy: authorize (lifecycle, lease, feature, numeric checks) |
| Create | `app/entitlements/admission.py` | PaidOperationAdmission: 10-step admit_in_transaction |
| Create | `app/entitlements/credential_admin.py` | CredentialAdmin: create, rotate, revoke, bind, list_metadata |
| Create | `app/entitlements/credential_protector.py` | CredentialProtector protocol + LocalKmsAdapter |
| Create | `app/entitlements/coordinator.py` | ExternalCallCoordinator: prepare tx → call → complete tx |
| Create | `app/entitlements/cost_reconciler.py` | CostReconciler: reconcile_attempt, record_adjustment |
| Create | `app/entitlements/usage.py` | UsageRecorder + UsageQuery |
| Create | `app/entitlements/guardrail_admin.py` | GuardrailAdmin: get, preview_update, confirm_update |
| Create | `app/entitlements/notification_writer.py` | CustomerNotificationWriter: threshold, hysteresis, enqueue |
| Create | `app/entitlements/quote_engine.py` | CostQuoteEngine: freeze per-call quotes |
| Create | `app/entitlements/reservation_book.py` | ReservationBook: guardrail check + capacity + reservation insert |
| Create | `app/entitlements/types.py` | Shared domain types and value objects |

### Stage 4 — Integration (app/, web/)

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `app/providers/adapter.py` | Unified ImageProvider protocol with execute(), lookup(), ProviderCapabilities |
| Modify | `app/providers/mock.py` | Implement new unified interface |
| Modify | `app/providers/gemini.py` | Implement new unified interface |
| Modify | `app/providers/openai_img.py` | Implement new unified interface |
| Modify | `app/providers/seedream.py` | Implement new unified interface |
| Modify | `app/store.py` | Rewire create_run to use PaidOperationAdmission |
| Modify | `app/worker_runtime.py` | Worker uses ExternalCallCoordinator.execute() |
| Create | `app/entitlements/api.py` | FastAPI router: credential, guardrail, usage, cost routes |
| Modify | `app/main.py` | Register entitlement inbox endpoint + new API router |
| Modify | `app/platform/projection_sync.py` | Upgrade to v2 envelope pull |

### Stage 5 — Legacy Bridge + E2E

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `app/entitlements/legacy_bridge.py` | Read-only compatibility view builder |
| Create | `tests/test_entitlements_e2e.py` | End-to-end admission→dispatch→settlement tests |
| Create | `tests/test_entitlement_propagation.py` | Snapshot push/pull/replay/lease tests |
| Create | `tests/test_credential_safety.py` | Credential canary, encryption, isolation tests |
| Create | `tests/test_cost_evidence.py` | Ledger replay, mode isolation, legacy bridge tests |
| Create | `tests/test_admission_concurrency.py` | Guardrail contention, capacity reservation tests |

---

## Stage 1: Control Plane — Commercial Access Models, Publication Module, Signed Snapshots

### Task 1: Prisma Schema — Enums, PlanVersion, and AccessGrant Models

**Files:**
- Modify: `web/prisma/schema.prisma`

**Interfaces:**
- Consumes: existing Team model (schema.prisma:103–135)
- Produces: `PlanVersion`, `AccessGrant`, `Subscription`, `CommercialAccessBinding`, `Restriction`, `CommercialAccessProjection`, `EntitlementSnapshot`, `EntitlementSnapshotOutbox`, `ControlPlaneAuditEvent` Prisma models; 8 new enums; `Team.lifecycle` field and new relations

All Prisma models below are taken verbatim from Spec §1.2–§1.13. The migration (Task 2) adds the CHECK constraints, partial indexes, triggers and immutability guards that Prisma cannot express natively.

- [ ] **Step 1: Add enums to schema.prisma**

Add after the existing `Role` enum and `DelegatedSigningKeyState` enum:

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

- [ ] **Step 2: Add PlanVersion model**

```prisma
model PlanVersion {
  id               String           @id @default(cuid())
  planKey          String
  versionNo        Int
  displayName      String
  state            PlanVersionState @default(EFFECTIVE)
  entitlements     Json
  entitlementsHash String
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

- [ ] **Step 3: Add AccessGrant model**

```prisma
model AccessGrant {
  id              String          @id @default(cuid())
  teamId          String
  team            Team            @relation(fields: [teamId], references: [id], onDelete: Restrict)
  planVersionId   String
  planVersion     PlanVersion     @relation(fields: [planVersionId], references: [id], onDelete: Restrict)
  grantKind       AccessGrantKind
  source          String
  reason          String          @default("")
  periodAnchor    DateTime?
  startsAt        DateTime
  endsAt          DateTime?
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

- [ ] **Step 4: Add Subscription (placeholder), CommercialAccessBinding, Restriction models**

Add all three models per Spec §1.4–§1.6. See spec for exact field definitions.

- [ ] **Step 5: Add CommercialAccessProjection, EntitlementSnapshot, EntitlementSnapshotOutbox, ControlPlaneAuditEvent models**

Add all four models per Spec §1.7–§1.11. Key details:
- `CommercialAccessProjection` has `@@unique` on `teamId` for one-row-per-Workspace
- `EntitlementSnapshot` has `@@unique([teamId, entitlementVersion, leaseSequence])` and `@@unique([teamId, id])`
- `EntitlementSnapshotOutbox` has `@@unique([teamId, snapshotId])`
- `ControlPlaneAuditEvent` has `@@unique([teamId, id])`

- [ ] **Step 6: Add Team.lifecycle and new Team relations**

Add to the Team model:
```prisma
lifecycle String // PROVISIONING, ACTIVE, SUSPENDED, DELETION_PENDING, DELETED

accessGrants               AccessGrant[]
subscriptions              Subscription[]
commercialAccessBindings   CommercialAccessBinding[]
restrictions               Restriction[]
entitlementSnapshots       EntitlementSnapshot[]
commercialAccessProjection CommercialAccessProjection?
auditEvents                ControlPlaneAuditEvent[]
```

- [ ] **Step 7: Validate schema compiles**

Run: `cd web && npx prisma validate`
Expected: no errors

- [ ] **Step 8: Commit**

```bash
git add web/prisma/schema.prisma
git commit -m "feat(control-plane): add commercial access Prisma models

Adds PlanVersion, AccessGrant, Subscription, CommercialAccessBinding,
Restriction, CommercialAccessProjection, EntitlementSnapshot,
EntitlementSnapshotOutbox, ControlPlaneAuditEvent models and Team.lifecycle.
CHECK constraints and triggers are added in the migration (Task 2)."
```

---

### Task 2: Control Plane Prisma Migration with Database Invariants

**Files:**
- Create: `web/prisma/migrations/<timestamp>_entitlement_commercial_access/migration.sql`

**Interfaces:**
- Consumes: Prisma schema from Task 1
- Produces: Database tables with all CHECK constraints, partial unique indexes, immutability triggers, and lifecycle backfill

The generated Prisma migration creates the tables. This task edits the generated SQL to add invariants Prisma cannot express (Spec §1.2–§1.11, §6.1).

- [ ] **Step 1: Generate create-only migration**

```bash
cd web
npm run db:migrate:dev -- --create-only --name entitlement_commercial_access
```

This generates a migration directory with `migration.sql`. The SQL creates tables but lacks CHECK constraints, triggers, and partial indexes.

- [ ] **Step 2: Edit migration — Team lifecycle backfill**

Add to the generated SQL, after the Team ALTER statement:

```sql
-- Step 1: Add nullable lifecycle
-- (Prisma generates this as required; change to nullable first)
-- Replace the generated NOT NULL with:
ALTER TABLE "Team" ALTER COLUMN "lifecycle" DROP NOT NULL;

-- Step 2: Backfill from existing suspended flag
UPDATE "Team" SET "lifecycle" = CASE WHEN "suspended" = true THEN 'SUSPENDED' ELSE 'ACTIVE' END;

-- Step 3: Add CHECK and make non-null
ALTER TABLE "Team" ADD CONSTRAINT team_lifecycle_valid
  CHECK ("lifecycle" IN ('PROVISIONING', 'ACTIVE', 'SUSPENDED', 'DELETION_PENDING', 'DELETED'));
ALTER TABLE "Team" ALTER COLUMN "lifecycle" SET NOT NULL;
```

- [ ] **Step 3: Edit migration — PlanVersion immutability trigger**

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

- [ ] **Step 4: Edit migration — AccessGrant CHECK constraints**

```sql
ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_base_requires_anchor
  CHECK ("grantKind" != 'BASE' OR "periodAnchor" IS NOT NULL);

ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_addon_requires_reason
  CHECK ("grantKind" != 'ADD_ON' OR length("reason") > 0);

ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_addon_no_anchor
  CHECK ("grantKind" != 'ADD_ON' OR "periodAnchor" IS NULL);

ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_ends_after_starts
  CHECK ("endsAt" IS NULL OR "endsAt" > "startsAt");
```

- [ ] **Step 5: Edit migration — CommercialAccessBinding partial unique index and source CHECK**

```sql
CREATE UNIQUE INDEX commercial_access_binding_one_active
  ON "CommercialAccessBinding" ("teamId")
  WHERE "deactivatedAt" IS NULL;

ALTER TABLE "CommercialAccessBinding" ADD CONSTRAINT binding_source_exactly_one
  CHECK (
    ("sourceKind" = 'ACCESS_GRANT' AND "accessGrantId" IS NOT NULL AND "subscriptionId" IS NULL)
    OR
    ("sourceKind" = 'SUBSCRIPTION' AND "subscriptionId" IS NOT NULL AND "accessGrantId" IS NULL)
  );
```

- [ ] **Step 6: Edit migration — Subscription, Restriction CHECK constraints**

```sql
ALTER TABLE "Subscription" ADD CONSTRAINT subscription_period_order
  CHECK ("periodEnd" > "periodStart");

ALTER TABLE "Restriction" ADD CONSTRAINT restriction_disable_value
  CHECK ("effect" != 'DISABLE' OR "restrictedValue"::text = 'false');

ALTER TABLE "Restriction" ADD CONSTRAINT restriction_reduce_cap_value
  CHECK ("effect" != 'REDUCE_CAP' OR (
    jsonb_typeof("restrictedValue"::jsonb) = 'number'
    AND ("restrictedValue"::jsonb)::numeric >= 0
  ));
```

- [ ] **Step 7: Edit migration — ControlPlaneAuditEvent and EntitlementSnapshot append-only triggers**

```sql
CREATE FUNCTION control_plane_audit_event_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'ControlPlaneAuditEvent rows are immutable';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_event_immutable
  BEFORE UPDATE OR DELETE ON "ControlPlaneAuditEvent"
  FOR EACH ROW EXECUTE FUNCTION control_plane_audit_event_immutable();

CREATE FUNCTION entitlement_snapshot_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'EntitlementSnapshot rows are immutable';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_entitlement_snapshot_immutable
  BEFORE UPDATE OR DELETE ON "EntitlementSnapshot"
  FOR EACH ROW EXECUTE FUNCTION entitlement_snapshot_immutable();
```

- [ ] **Step 8: Apply migration and verify**

```bash
cd web && npm run db:migrate:dev
```

Expected: migration applies cleanly. Verify with:
```bash
npx prisma db execute --stdin <<< "SELECT COUNT(*) FROM \"PlanVersion\";"
```

- [ ] **Step 9: Commit**

```bash
git add web/prisma/
git commit -m "feat(control-plane): Prisma migration with CHECK constraints, triggers, and lifecycle backfill"
```

---

### Task 3: Entitlement Signing, Resolution, and Types

**Files:**
- Create: `web/lib/entitlements/types.ts`
- Create: `web/lib/entitlements/signing.ts`
- Create: `web/lib/entitlements/resolution.ts`
- Create: `web/tests/entitlements/signing.test.ts`
- Create: `web/tests/entitlements/resolution.test.ts`

**Interfaces:**
- Consumes: Prisma models from Task 1
- Produces:
  - `signEnvelope(payload: SnapshotPayload, keyId: string, key: Buffer): SignedEnvelope`
  - `verifyEnvelope(envelope: SignedEnvelope, keyRing: Map<string, Buffer>): SnapshotPayload`
  - `resolveEntitlements(base: PlanVersionEntitlements, addOns: PlanVersionEntitlements[], restrictions: RestrictionInput[]): ResolvedEntitlements`
  - `computeSemanticHash(inputs: SemanticHashInputs): string`
  - `WorkspaceProjectionEnvelope` type (Spec §1.14)

- [ ] **Step 1: Create types.ts**

```typescript
// web/lib/entitlements/types.ts
export interface SnapshotPayload {
  snapshot_id: string;
  workspace_id: string;
  workspace_name: string;
  authorization_epoch: number;
  entitlement_version: number;
  lease_sequence: number;
  projection_version: number;
  suspended: boolean;
  lifecycle: string;
  entitlement_snapshot: {
    plan_version_id: string;
    source_refs: SourceRef[];
    period_start: string;
    period_end: string;
    period_anchor: string;
    resolved_entitlements: Record<string, boolean | number>;
    restriction_summary: RestrictionSummaryEntry[];
    semantic_hash: string;
    entitlement_registry_version: number;
    resolution_policy_version: number;
    platform_limits_version: number;
    issued_at: string;
    valid_until: string;
    audit_event_id: string;
  };
}

export interface SignedEnvelope {
  schema_version: 2;
  signature_key_id: string;
  signature_algorithm: 'hmac-sha256';
  payload: SnapshotPayload;
  signature: string;
}

export interface SourceRef {
  kind: 'ACCESS_GRANT' | 'SUBSCRIPTION';
  id: string;
  plan_version_id: string;
  starts_at: string;
  ends_at: string | null;
}

export interface RestrictionSummaryEntry {
  id: string;
  kind: string;
  key: string;
  effect: string;
  value: boolean | number;
  reason: string;
}

export interface RestrictionInput {
  id: string;
  kind: string;
  entitlementKey: string;
  effect: 'DISABLE' | 'REDUCE_CAP';
  restrictedValue: boolean | number;
  reason: string;
}

export type PlanVersionEntitlements = Record<string, boolean | number>;

export interface ResolvedEntitlements {
  entitlements: Record<string, boolean | number>;
}

export interface SemanticHashInputs {
  planVersionId: string;
  sourceRefs: SourceRef[];
  periodStart: string;
  periodEnd: string;
  periodAnchor: string;
  resolvedEntitlements: Record<string, boolean | number>;
  restrictionIds: string[];
  restrictionEffects: string[];
  restrictionValues: (boolean | number)[];
  entitlementRegistryVersion: number;
  resolutionPolicyVersion: number;
  platformLimitsVersion: number;
}
```

- [ ] **Step 2: Write signing tests**

```typescript
// web/tests/entitlements/signing.test.ts
import { signEnvelope, verifyEnvelope } from '@/lib/entitlements/signing';
import type { SnapshotPayload } from '@/lib/entitlements/types';

const TEST_KEY_ID = 'esk_test_1';
const TEST_KEY = Buffer.from('a]'.repeat(16), 'utf-8').subarray(0, 32);

function makePayload(overrides?: Partial<SnapshotPayload>): SnapshotPayload {
  return {
    snapshot_id: 'es_test_1',
    workspace_id: 'ws_1',
    workspace_name: 'Test',
    authorization_epoch: 1,
    entitlement_version: 1,
    lease_sequence: 1,
    projection_version: 1,
    suspended: false,
    lifecycle: 'active',
    entitlement_snapshot: {
      plan_version_id: 'pv_1',
      source_refs: [],
      period_start: '2026-08-01T00:00:00Z',
      period_end: '2026-09-01T00:00:00Z',
      period_anchor: '2026-08-01T00:00:00Z',
      resolved_entitlements: { 'module.generate.enabled': true },
      restriction_summary: [],
      semantic_hash: 'abc123',
      entitlement_registry_version: 1,
      resolution_policy_version: 1,
      platform_limits_version: 1,
      issued_at: '2026-08-31T10:00:00Z',
      valid_until: '2026-09-01T00:00:00Z',
      audit_event_id: 'evt_1',
    },
    ...overrides,
  };
}

describe('signEnvelope', () => {
  it('produces a valid signed envelope', () => {
    const payload = makePayload();
    const envelope = signEnvelope(payload, TEST_KEY_ID, TEST_KEY);
    expect(envelope.schema_version).toBe(2);
    expect(envelope.signature_key_id).toBe(TEST_KEY_ID);
    expect(envelope.signature_algorithm).toBe('hmac-sha256');
    expect(typeof envelope.signature).toBe('string');
    expect(envelope.signature.length).toBeGreaterThan(0);
  });

  it('round-trips through verify', () => {
    const payload = makePayload();
    const envelope = signEnvelope(payload, TEST_KEY_ID, TEST_KEY);
    const keyRing = new Map([[TEST_KEY_ID, TEST_KEY]]);
    const verified = verifyEnvelope(envelope, keyRing);
    expect(verified.workspace_id).toBe('ws_1');
  });
});

describe('verifyEnvelope', () => {
  it('rejects tampered payload', () => {
    const payload = makePayload();
    const envelope = signEnvelope(payload, TEST_KEY_ID, TEST_KEY);
    envelope.payload.workspace_id = 'ws_evil';
    const keyRing = new Map([[TEST_KEY_ID, TEST_KEY]]);
    expect(() => verifyEnvelope(envelope, keyRing)).toThrow();
  });

  it('rejects unknown key ID', () => {
    const payload = makePayload();
    const envelope = signEnvelope(payload, TEST_KEY_ID, TEST_KEY);
    const emptyRing = new Map<string, Buffer>();
    expect(() => verifyEnvelope(envelope, emptyRing)).toThrow();
  });

  it('rejects tampered protected header fields', () => {
    const payload = makePayload();
    const envelope = signEnvelope(payload, TEST_KEY_ID, TEST_KEY);
    (envelope as any).schema_version = 99;
    const keyRing = new Map([[TEST_KEY_ID, TEST_KEY]]);
    expect(() => verifyEnvelope(envelope, keyRing)).toThrow();
  });
});
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd web && npx jest tests/entitlements/signing.test.ts --no-coverage
```
Expected: FAIL — modules do not exist yet.

- [ ] **Step 4: Implement signing.ts**

```typescript
// web/lib/entitlements/signing.ts
import crypto from 'node:crypto';
import type { SignedEnvelope, SnapshotPayload } from './types';

function canonicalBytes(envelope: Omit<SignedEnvelope, 'signature'>): Buffer {
  const obj = {
    schema_version: envelope.schema_version,
    signature_key_id: envelope.signature_key_id,
    signature_algorithm: envelope.signature_algorithm,
    payload: envelope.payload,
  };
  return Buffer.from(JSON.stringify(obj), 'utf-8');
}

export function signEnvelope(
  payload: SnapshotPayload,
  keyId: string,
  key: Buffer,
): SignedEnvelope {
  const unsigned: Omit<SignedEnvelope, 'signature'> = {
    schema_version: 2,
    signature_key_id: keyId,
    signature_algorithm: 'hmac-sha256',
    payload,
  };
  const mac = crypto
    .createHmac('sha256', key)
    .update(canonicalBytes(unsigned))
    .digest('hex');
  return { ...unsigned, signature: mac };
}

export function verifyEnvelope(
  envelope: SignedEnvelope,
  keyRing: Map<string, Buffer>,
): SnapshotPayload {
  const key = keyRing.get(envelope.signature_key_id);
  if (!key) {
    throw new Error(`Unknown signing key: ${envelope.signature_key_id}`);
  }
  const { signature, ...unsigned } = envelope;
  const expected = crypto
    .createHmac('sha256', key)
    .update(canonicalBytes(unsigned))
    .digest('hex');
  if (
    signature.length !== expected.length ||
    !crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected))
  ) {
    throw new Error('Envelope signature verification failed');
  }
  return envelope.payload;
}
```

- [ ] **Step 5: Write resolution tests**

```typescript
// web/tests/entitlements/resolution.test.ts
import {
  resolveEntitlements,
  computeSemanticHash,
} from '@/lib/entitlements/resolution';
import type { RestrictionInput } from '@/lib/entitlements/types';

describe('resolveEntitlements', () => {
  it('returns base entitlements when no addons or restrictions', () => {
    const base = { 'module.generate.enabled': true, 'members.max': 5 };
    const result = resolveEntitlements(base, [], []);
    expect(result.entitlements).toEqual(base);
  });

  it('combines boolean addons with OR', () => {
    const base = { 'module.scale.enabled': false };
    const addon = { 'module.scale.enabled': true };
    const result = resolveEntitlements(base, [addon], []);
    expect(result.entitlements['module.scale.enabled']).toBe(true);
  });

  it('combines numeric addons with MAX', () => {
    const base = { 'members.max': 5 };
    const addon = { 'members.max': 10 };
    const result = resolveEntitlements(base, [addon], []);
    expect(result.entitlements['members.max']).toBe(10);
  });

  it('applies DISABLE restriction after combination', () => {
    const base = { 'module.scale.enabled': true };
    const restriction: RestrictionInput = {
      id: 'r1', kind: 'ABUSE', entitlementKey: 'module.scale.enabled',
      effect: 'DISABLE', restrictedValue: false, reason: 'abuse',
    };
    const result = resolveEntitlements(base, [], [restriction]);
    expect(result.entitlements['module.scale.enabled']).toBe(false);
  });

  it('applies REDUCE_CAP restriction after combination', () => {
    const base = { 'members.max': 10 };
    const restriction: RestrictionInput = {
      id: 'r1', kind: 'BILLING', entitlementKey: 'members.max',
      effect: 'REDUCE_CAP', restrictedValue: 3, reason: 'billing issue',
    };
    const result = resolveEntitlements(base, [], [restriction]);
    expect(result.entitlements['members.max']).toBe(3);
  });
});

describe('computeSemanticHash', () => {
  it('produces stable hash for same inputs', () => {
    const inputs = {
      planVersionId: 'pv_1',
      sourceRefs: [],
      periodStart: '2026-08-01T00:00:00Z',
      periodEnd: '2026-09-01T00:00:00Z',
      periodAnchor: '2026-08-01T00:00:00Z',
      resolvedEntitlements: { 'module.generate.enabled': true },
      restrictionIds: [],
      restrictionEffects: [],
      restrictionValues: [],
      entitlementRegistryVersion: 1,
      resolutionPolicyVersion: 1,
      platformLimitsVersion: 1,
    };
    const h1 = computeSemanticHash(inputs);
    const h2 = computeSemanticHash(inputs);
    expect(h1).toBe(h2);
  });

  it('changes when resolved entitlements change', () => {
    const base = {
      planVersionId: 'pv_1', sourceRefs: [],
      periodStart: '2026-08-01T00:00:00Z', periodEnd: '2026-09-01T00:00:00Z',
      periodAnchor: '2026-08-01T00:00:00Z',
      resolvedEntitlements: { 'members.max': 5 },
      restrictionIds: [], restrictionEffects: [], restrictionValues: [],
      entitlementRegistryVersion: 1, resolutionPolicyVersion: 1,
      platformLimitsVersion: 1,
    };
    const altered = { ...base, resolvedEntitlements: { 'members.max': 10 } };
    expect(computeSemanticHash(base)).not.toBe(computeSemanticHash(altered));
  });
});
```

- [ ] **Step 6: Run resolution tests to verify they fail**

```bash
cd web && npx jest tests/entitlements/resolution.test.ts --no-coverage
```

- [ ] **Step 7: Implement resolution.ts**

```typescript
// web/lib/entitlements/resolution.ts
import crypto from 'node:crypto';
import type {
  PlanVersionEntitlements,
  ResolvedEntitlements,
  RestrictionInput,
  SemanticHashInputs,
} from './types';

export function resolveEntitlements(
  base: PlanVersionEntitlements,
  addOns: PlanVersionEntitlements[],
  restrictions: RestrictionInput[],
): ResolvedEntitlements {
  const combined: Record<string, boolean | number> = { ...base };

  for (const addon of addOns) {
    for (const [key, value] of Object.entries(addon)) {
      const existing = combined[key];
      if (existing === undefined) {
        combined[key] = value;
      } else if (typeof value === 'boolean' && typeof existing === 'boolean') {
        combined[key] = existing || value;
      } else if (typeof value === 'number' && typeof existing === 'number') {
        combined[key] = Math.max(existing, value);
      }
    }
  }

  for (const r of restrictions) {
    if (r.effect === 'DISABLE') {
      combined[r.entitlementKey] = false;
    } else if (r.effect === 'REDUCE_CAP' && typeof r.restrictedValue === 'number') {
      const current = combined[r.entitlementKey];
      if (typeof current === 'number') {
        combined[r.entitlementKey] = Math.min(current, r.restrictedValue);
      }
    }
  }

  return { entitlements: combined };
}

export function computeSemanticHash(inputs: SemanticHashInputs): string {
  const canonical = JSON.stringify({
    plan_version_id: inputs.planVersionId,
    source_refs: inputs.sourceRefs,
    period_start: inputs.periodStart,
    period_end: inputs.periodEnd,
    period_anchor: inputs.periodAnchor,
    resolved_entitlements: inputs.resolvedEntitlements,
    restriction_ids: inputs.restrictionIds,
    restriction_effects: inputs.restrictionEffects,
    restriction_values: inputs.restrictionValues,
    entitlement_registry_version: inputs.entitlementRegistryVersion,
    resolution_policy_version: inputs.resolutionPolicyVersion,
    platform_limits_version: inputs.platformLimitsVersion,
  });
  return crypto.createHash('sha256').update(canonical).digest('hex');
}
```

- [ ] **Step 8: Run all signing and resolution tests**

```bash
cd web && npx jest tests/entitlements/ --no-coverage
```
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add web/lib/entitlements/ web/tests/entitlements/
git commit -m "feat(control-plane): entitlement signing, resolution, and semantic hashing"
```

---

### Task 4: Publication Module and v2 Pull-Repair Endpoint

**Files:**
- Create: `web/lib/entitlements/publication.ts`
- Create: `web/pages/api/internal/domain/workspaces/[workspaceId]/workspace-projection-snapshot.ts`
- Modify: `web/middleware.ts`
- Create: `web/tests/entitlements/publication.test.ts`

**Interfaces:**
- Consumes: `signEnvelope` from Task 3, `resolveEntitlements` and `computeSemanticHash` from Task 3, Prisma models from Task 1
- Produces:
  - `publishSnapshot(prisma: PrismaClient, teamId: string, signingKeyId: string, signingKey: Buffer): Promise<PublicationResult>`
  - `GET /api/internal/domain/workspaces/{workspaceId}/workspace-projection-snapshot` returning `application/octet-stream`

- [ ] **Step 1: Write publication unit tests**

Test the publication logic in isolation using a mock Prisma client. Key cases:
- First publication creates CommercialAccessProjection if missing
- Semantic hash change increments entitlementVersion
- Lease renewal (no semantic change) does not increment entitlementVersion but does increment leaseSequence
- leaseSequence never resets across entitlement version changes
- Publication atomically creates Snapshot, Outbox entry, and updates projection

```typescript
// web/tests/entitlements/publication.test.ts
import { buildSnapshotPayload } from '@/lib/entitlements/publication';

describe('buildSnapshotPayload', () => {
  it('maps lifecycle to lowercase wire values', () => {
    const payload = buildSnapshotPayload({
      snapshotId: 'es_1',
      team: { id: 'ws_1', name: 'Test', lifecycle: 'ACTIVE', suspended: false,
              authorizationEpoch: 1, projectionVersion: 1 },
      entitlementVersion: 1,
      leaseSequence: 1,
      planVersionId: 'pv_1',
      sourceRefs: [],
      periodStart: new Date('2026-08-01'),
      periodEnd: new Date('2026-09-01'),
      periodAnchor: new Date('2026-08-01'),
      resolvedEntitlements: {},
      restrictionSummary: [],
      semanticHash: 'abc',
      registryVersion: 1,
      resolutionPolicyVersion: 1,
      platformLimitsVersion: 1,
      issuedAt: new Date('2026-08-31T10:00:00Z'),
      validUntil: new Date('2026-09-01T00:00:00Z'),
      auditEventId: 'evt_1',
    });
    expect(payload.lifecycle).toBe('active');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd web && npx jest tests/entitlements/publication.test.ts --no-coverage
```

- [ ] **Step 3: Implement publication.ts**

Implement the `buildSnapshotPayload` function and the main `publishSnapshot` function per Spec §1.7 publication transaction (steps 1–11). The function:
1. Locks `CommercialAccessProjection` with `FOR UPDATE`
2. Resolves entitlements via `resolveEntitlements`
3. Computes `semanticHash`
4. Conditionally increments `entitlementVersion`
5. Always increments `leaseSequence` and `projectionVersion`
6. Creates `ControlPlaneAuditEvent`
7. Signs the canonical envelope
8. Creates `EntitlementSnapshot` with `canonicalEnvelope` bytes
9. Creates `EntitlementSnapshotOutbox` PENDING
10. Updates projection pointers
11. Updates Team counters

Use `prisma.$transaction` with `isolationLevel: 'Serializable'` for the publication transaction.

The lifecycle mapping (database uppercase → wire lowercase) is:
```typescript
const LIFECYCLE_WIRE_MAP: Record<string, string> = {
  PROVISIONING: 'provisioning',
  ACTIVE: 'active',
  SUSPENDED: 'suspended',
  DELETION_PENDING: 'deletion_pending',
  DELETED: 'deleted',
};
```

- [ ] **Step 4: Implement workspace-projection-snapshot.ts**

```typescript
// web/pages/api/internal/domain/workspaces/[workspaceId]/workspace-projection-snapshot.ts
import crypto from 'node:crypto';
import type { NextApiRequest, NextApiResponse } from 'next';
import env from '@/lib/env';
import { prisma } from '@/lib/prisma';

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
): Promise<void> {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    res.status(405).end();
    return;
  }

  // Uses the existing projectionSyncServiceToken — same pull direction,
  // now serving v2 signed envelopes alongside the v1 authorization-snapshot.
  const serviceToken = env.projectionSyncServiceToken;
  if (!serviceToken) {
    res.status(500).json({ error: 'Service token not configured' });
    return;
  }

  const authHeader = req.headers.authorization;
  if (!authHeader?.startsWith('Bearer ')) {
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }

  const token = authHeader.slice(7);
  const expected = Buffer.from(serviceToken, 'utf-8');
  const actual = Buffer.from(token, 'utf-8');
  if (expected.length !== actual.length || !crypto.timingSafeEqual(expected, actual)) {
    res.status(403).json({ error: 'Forbidden' });
    return;
  }

  const workspaceId = req.query.workspaceId;
  if (typeof workspaceId !== 'string' || workspaceId.length === 0) {
    res.status(400).json({ error: 'Invalid workspace ID' });
    return;
  }

  const snapshot = await prisma.entitlementSnapshot.findFirst({
    where: { teamId: workspaceId },
    orderBy: [{ entitlementVersion: 'desc' }, { leaseSequence: 'desc' }],
    select: { canonicalEnvelope: true },
  });

  if (!snapshot?.canonicalEnvelope) {
    res.status(404).json({ error: 'No snapshot available' });
    return;
  }

  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Content-Type', 'application/octet-stream');
  res.status(200).send(Buffer.from(snapshot.canonicalEnvelope));
}
```

- [ ] **Step 5: Add new endpoint to middleware service-auth bypass**

Add `workspace-projection-snapshot` to the `apiServiceAuthenticatedRoutes` array in `web/middleware.ts`, following the same pattern as the existing `authorization-snapshot` route.

- [ ] **Step 6: Run all tests**

```bash
cd web && npx jest tests/entitlements/ --no-coverage
```

- [ ] **Step 7: Implement Workspace Provisioning Module**

Per Spec §4.15, provisioning is a two-step sequence. Add `provisionWorkspace()` to `publication.ts`:

```typescript
export async function provisionWorkspace(
  prisma: PrismaClient,
  teamId: string,
  ownerId: string,
  betaPlanVersionId: string,
  signingKeyId: string,
  signingKey: Buffer,
): Promise<void> {
  await prisma.$transaction(async (tx) => {
    // 1. Team already created with lifecycle=PROVISIONING
    // 2. First Owner membership already created
    // 3. Verify Beta PlanVersion exists and is EFFECTIVE
    // 4. Create BASE AccessGrant
    // 5. Create CommercialAccessProjection
    // 6. Publish initial signed Snapshot (lifecycle=provisioning)
    // 7. Write Outbox PENDING
  });
  // Domain receives and applies the provisioning snapshot.
  // On domain confirmation, a second call transitions lifecycle to ACTIVE.
}
```

The domain side (Task 10 `EntitlementProjectionModule.ingest()`) creates the workspace projection and SpendGuardrail when it receives the first snapshot. The control plane then publishes a second `active`-lifecycle snapshot.

- [ ] **Step 8: Commit**

```bash
git add web/lib/entitlements/publication.ts web/pages/api/internal/domain/workspaces/ web/middleware.ts web/tests/entitlements/publication.test.ts
git commit -m "feat(control-plane): publication module, provisioning, and v2 pull-repair endpoint

Publication Module atomically resolves, signs, and publishes
WorkspaceProjectionEnvelopes. Provisioning creates AccessGrant,
initial snapshot, and outbox entry. The v2 pull-repair endpoint
returns the exact canonicalEnvelope bytes for domain repair sync."
```

---

### Task 5: Outbox Dispatcher

**Files:**
- Create: `web/lib/entitlements/dispatcher.ts`
- Create: `web/tests/entitlements/dispatcher.test.ts`

**Interfaces:**
- Consumes: `EntitlementSnapshotOutbox` Prisma model, `CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN` env var, `DOMAIN_INTERNAL_BASE_URL` env var
- Produces:
  - `claimAndDispatch(prisma: PrismaClient, dispatcherId: string, opts: DispatcherOpts): Promise<DispatchResult>`
  - State machine: `PENDING → DELIVERING → DELIVERED` (happy), `DELIVERING → FAILED → DELIVERING` (retry), `FAILED → DEAD_LETTER` (terminal)

- [ ] **Step 1: Write dispatcher tests**

Test the dispatcher's claim-and-deliver cycle in isolation:
- Claiming sets `DELIVERING` state, `deliveryLeaseId`, `deliveryLeaseExpiresAt`
- Successful delivery transitions to `DELIVERED`
- HTTP 5xx/timeout transitions to `FAILED` with backoff
- HTTP 409 transitions to `DEAD_LETTER`
- Expired `DELIVERING` leases can be reclaimed
- Max attempts reached transitions to `DEAD_LETTER`

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd web && npx jest tests/entitlements/dispatcher.test.ts --no-coverage
```

- [ ] **Step 3: Implement dispatcher.ts**

Per Spec §4.2, each dispatch cycle:
1. Short transaction: `FOR UPDATE SKIP LOCKED` on retryable records
2. Set `DELIVERING`, lease ID, lease expiry, increment attempts
3. Commit claim transaction
4. HTTP POST canonical envelope bytes to domain inbox (outside transaction)
5. New transaction: CAS update final state using lease ID

Response handling per spec:
- 2xx → `DELIVERED`
- 409 → `DEAD_LETTER` + alert
- 400/422 → `DEAD_LETTER` + alert
- 408/429/5xx/network → `FAILED` + exponential backoff

- [ ] **Step 4: Run tests**

```bash
cd web && npx jest tests/entitlements/dispatcher.test.ts --no-coverage
```

- [ ] **Step 5: Commit**

```bash
git add web/lib/entitlements/dispatcher.ts web/tests/entitlements/dispatcher.test.ts
git commit -m "feat(control-plane): outbox dispatcher with lease-based delivery"
```

---

## Stage 2: Domain Models + Alembic Migration

### Task 6: Domain SQLAlchemy Models — Entitlement Inbox and Projection

**Files:**
- Create: `app/entitlements/__init__.py`
- Create: `app/entitlements/models.py`
- Create: `tests/test_entitlement_models.py`

**Interfaces:**
- Consumes: `WorkspaceRecord` from `app/adapters/persistence/models.py`
- Produces:
  - `EntitlementSnapshotInboxRecord` (Spec §2.1)
  - `EntitlementProjectionRecord` (Spec §2.2)

- [ ] **Step 1: Write model structure tests**

```python
# tests/test_entitlement_models.py
import pytest
from app.entitlements.models import (
    EntitlementSnapshotInboxRecord,
    EntitlementProjectionRecord,
)

def test_inbox_table_name():
    assert EntitlementSnapshotInboxRecord.__tablename__ == "entitlement_snapshot_inbox"

def test_projection_table_name():
    assert EntitlementProjectionRecord.__tablename__ == "entitlement_projections"

def test_inbox_has_required_columns():
    columns = {c.name for c in EntitlementSnapshotInboxRecord.__table__.columns}
    required = {
        "snapshot_id", "workspace_id", "entitlement_version",
        "lease_sequence", "projection_version", "schema_version",
        "delivery_kind", "canonical_envelope", "envelope_sha256",
        "semantic_hash", "signature", "signature_key_id",
        "signature_algorithm", "outcome", "received_at",
        "processed_at", "projection_applied_at",
    }
    assert required.issubset(columns)

def test_projection_has_required_columns():
    columns = {c.name for c in EntitlementProjectionRecord.__table__.columns}
    required = {
        "workspace_id", "current_snapshot_id", "entitlement_version",
        "lease_sequence", "projection_version", "schema_version",
        "plan_version_id", "resolved_entitlements_json",
        "semantic_hash", "issued_at", "valid_until", "applied_at",
    }
    assert required.issubset(columns)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_entitlement_models.py -v
```

- [ ] **Step 3: Implement models**

Create `app/entitlements/__init__.py` (empty) and `app/entitlements/models.py` with both ORM classes per Spec §2.1–§2.2. Use `Numeric(24, 12)` for financial fields. Include all unique constraints, composite FKs binding `workspace_id`, and CHECK constraints as `__table_args__`.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_entitlement_models.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/entitlements/ tests/test_entitlement_models.py
git commit -m "feat(domain): EntitlementSnapshotInboxRecord and EntitlementProjectionRecord models"
```

---

### Task 7: Domain SQLAlchemy Models — Credentials, Cost, Usage, Notifications

**Files:**
- Create: `app/entitlements/credentials.py`
- Create: `app/entitlements/cost_models.py`
- Create: `app/entitlements/usage_models.py`
- Modify: `app/adapters/persistence/models.py` (extend WorkspaceRecord, OperationRecord, JobRecord)
- Create: `tests/test_cost_usage_models.py`

**Interfaces:**
- Consumes: `WorkspaceRecord`, `OperationRecord`, `JobRecord` from `app/adapters/persistence/models.py`
- Produces:
  - `CredentialVersionRecord`, `CredentialBindingRecord` (Spec §2.4–§2.5)
  - `SpendGuardrailRecord`, `SpendGuardrailEventRecord`, `SpendReservationRecord`, `CapacityReservationRecord` (Spec §2.7–§2.10)
  - `ProviderCostLedgerEntryRecord`, `ProviderCallAttemptRecord` (Spec §2.11, §3.1)
  - `UsageEventRecord`, `CustomerNotificationOutboxRecord`, `GuardrailNotificationStateRecord` (Spec §2.12–§2.13)
  - Extended `WorkspaceRecord` with `lifecycle`, `budget_currency`
  - Extended `OperationRecord` with `semantic_key_hash`, `request_fingerprint_hash`, `admission_contract_version`
  - Extended `JobRecord` with `operation_id`, `entitlement_version_at_enqueue`, `execution_contract_version`

- [ ] **Step 1: Write model structure tests**

Test table names and required columns for all new models. Test that `WorkspaceRecord` has `lifecycle` and `budget_currency` columns. Test that `OperationRecord` has `semantic_key_hash`. Test that `JobRecord` has `operation_id`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_cost_usage_models.py -v
```

- [ ] **Step 3: Extend WorkspaceRecord**

Add to `WorkspaceRecord` in `app/adapters/persistence/models.py`:
```python
lifecycle: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
budget_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
```

- [ ] **Step 4: Extend OperationRecord**

Add to `OperationRecord`:
```python
semantic_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
request_fingerprint_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
admission_contract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

Add a partial unique constraint for new-contract Operations:
```python
# In __table_args__:
UniqueConstraint("workspace_id", "semantic_key_hash", name="uq_operation_semantic_key"),
```

- [ ] **Step 5: Extend JobRecord**

Add to `JobRecord`:
```python
operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
entitlement_version_at_enqueue: Mapped[int | None] = mapped_column(Integer, nullable=True)
execution_contract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

Add composite FK:
```python
ForeignKeyConstraint(
    ["workspace_id", "operation_id"],
    ["operation_records.workspace_id", "operation_records.id"],
    name="fk_jobs_workspace_operation_operation_records",
    ondelete="RESTRICT",
),
```

- [ ] **Step 6: Implement credential models**

Create `app/entitlements/credentials.py` with `CredentialVersionRecord` and `CredentialBindingRecord` per Spec §2.4–§2.5. Key constraints:
- `UNIQUE(workspace_id, credential_key, version_no)`
- `UNIQUE(workspace_id, id)` for composite FK candidate key
- Partial unique index for at-most-one active per credential_key
- Composite FK on CredentialBinding: `(workspace_id, credential_version_id) → credential_versions(workspace_id, id)`

- [ ] **Step 7: Implement cost models**

Create `app/entitlements/cost_models.py` with:
- `SpendGuardrailRecord` (Spec §2.7): primary key on `workspace_id`, all amounts `Numeric(24, 12)`
- `SpendGuardrailEventRecord` (Spec §2.8): append-only
- `SpendReservationRecord` (Spec §2.9): `UNIQUE(workspace_id, semantic_key_hash)`, composite FKs to Operation, Job
- `CapacityReservationRecord` (Spec §2.10): `UNIQUE(workspace_id, semantic_key_hash)`, acquisition_phase CHECK
- `ProviderCallAttemptRecord` (Spec §3.1): `UNIQUE(workspace_id, call_semantic_key_hash, attempt_no)`, all FKs composite
- `ProviderCostLedgerEntryRecord` (Spec §2.11): `UNIQUE(workspace_id, semantic_key_hash)`, amounts non-negative, append-only

- [ ] **Step 8: Implement usage and notification models**

Create `app/entitlements/usage_models.py` with:
- `UsageEventRecord` (Spec §2.12): `UNIQUE(workspace_id, semantic_key_hash)`, append-only
- `CustomerNotificationOutboxRecord` (Spec §2.13): `UNIQUE(workspace_id, deduplication_key)`, delivery state machine
- `GuardrailNotificationStateRecord` (Spec §2.13): `UNIQUE(workspace_id, guardrail_revision, platform_policy_version, dimension, window_key, threshold)`, state CHECK

- [ ] **Step 9: Run all model tests**

```bash
pytest tests/test_entitlement_models.py tests/test_cost_usage_models.py -v
```

- [ ] **Step 10: Commit**

```bash
git add app/entitlements/ app/adapters/persistence/models.py tests/test_cost_usage_models.py
git commit -m "feat(domain): all entitlement, credential, cost, usage, and notification SQLAlchemy models"
```

---

### Task 8: Domain Alembic Migration

**Files:**
- Create: `migrations/versions/<rev>_entitlement_commercial_access.py`
- Modify: `deploy/postgres/domain-tenant-roles.sql`

**Interfaces:**
- Consumes: all SQLAlchemy models from Tasks 6–7
- Produces: complete database schema with tables, composite FKs, partial indexes, CHECK constraints, append-only triggers, `marketai_apply_workspace_projection_v2` function, RLS policies, and role grants

The migration follows the dependency order from Spec §6.2:
1. Extend workspaces (lifecycle, budget_currency with backfill)
2. Extend operation_records (semantic_key_hash, etc.)
3. Extend jobs and operation_state_events (operation_id, workspace_id backfill)
4. Create entitlement inbox and projection
5. Create credentials and bindings
6. Create guardrails and guardrail events
7. Create spend and capacity reservations
8. Create provider call attempts
9. Create provider cost ledger
10. Create usage events
11. Create guardrail notification state and customer notification outbox
12. Create metadata and compatibility views
13. Install functions, triggers, RLS and grants

- [ ] **Step 1: Generate Alembic revision**

```bash
alembic revision --autogenerate -m "entitlement_commercial_access"
```

- [ ] **Step 2: Edit the migration — workspace and operation extensions**

Add lifecycle and budget_currency to workspaces with backfill per Spec §6.2:
```python
# Add nullable columns
op.add_column('workspaces', sa.Column('lifecycle', sa.String(32), nullable=True))
op.add_column('workspaces', sa.Column('budget_currency', sa.String(3), nullable=True))

# Backfill lifecycle from suspended
op.execute("""
    UPDATE workspaces SET lifecycle = CASE WHEN suspended = true THEN 'suspended' ELSE 'active' END
""")

# Backfill budget_currency from default_currency
op.execute("UPDATE workspaces SET budget_currency = default_currency")

# Add constraints and make non-null
op.alter_column('workspaces', 'lifecycle', nullable=False)
op.alter_column('workspaces', 'budget_currency', nullable=False)
```

- [ ] **Step 3: Edit the migration — new tables**

Create all new tables using `op.create_table(...)` for each model. Ensure all `Numeric(24, 12)` types and composite FKs are correct.

- [ ] **Step 4: Edit the migration — append-only triggers**

Install triggers for `entitlement_snapshot_inbox`, `spend_guardrail_events`, `provider_cost_ledger`, `usage_events`:

```python
if dialect_is_postgres:
    op.execute("""
    CREATE FUNCTION reject_update_delete_entitlement_inbox() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'entitlement_snapshot_inbox is append-only'; END;
    $$ LANGUAGE plpgsql SET search_path = '';

    CREATE TRIGGER trg_entitlement_inbox_immutable
      BEFORE UPDATE OR DELETE ON entitlement_snapshot_inbox
      FOR EACH ROW EXECUTE FUNCTION reject_update_delete_entitlement_inbox();
    """)
```

Repeat for each append-only table.

- [ ] **Step 5: Edit the migration — marketai_apply_workspace_projection_v2 function**

```python
op.execute("""
CREATE FUNCTION marketai_apply_workspace_projection_v2(
    p_snapshot_id TEXT,
    p_workspace_id TEXT,
    p_workspace_name TEXT,
    p_authorization_epoch INT,
    p_entitlement_version INT,
    p_lease_sequence INT,
    p_projection_version INT,
    p_lifecycle TEXT,
    p_suspended BOOLEAN,
    p_schema_version INT,
    p_delivery_kind TEXT,
    p_canonical_envelope BYTEA,
    p_envelope_sha256 TEXT,
    p_semantic_hash TEXT,
    p_signature TEXT,
    p_signature_key_id TEXT,
    p_signature_algorithm TEXT,
    p_plan_version_id TEXT,
    p_source_refs_json JSONB,
    p_period_start TIMESTAMPTZ,
    p_period_end TIMESTAMPTZ,
    p_period_anchor TIMESTAMPTZ,
    p_resolved_entitlements_json JSONB,
    p_restriction_summary_json JSONB,
    p_entitlement_registry_version INT,
    p_resolution_policy_version INT,
    p_platform_limits_version INT,
    p_issued_at TIMESTAMPTZ,
    p_valid_until TIMESTAMPTZ,
    p_control_plane_audit_event_id TEXT
) RETURNS TEXT
SECURITY DEFINER
SET search_path = ''
LANGUAGE plpgsql AS $$
DECLARE
    v_current_ev INT;
    v_current_ls INT;
    v_current_pv INT;
    v_existing_sha TEXT;
    v_result TEXT;
BEGIN
    -- Lock current projection
    SELECT entitlement_version, lease_sequence, projection_version
    INTO v_current_ev, v_current_ls, v_current_pv
    FROM public.entitlement_projections
    WHERE workspace_id = p_workspace_id
    FOR UPDATE;

    -- Determine outcome
    IF NOT FOUND THEN
        -- First projection for this workspace
        v_result := 'applied';
    ELSIF p_entitlement_version < v_current_ev THEN
        v_result := 'stale';
    ELSIF p_entitlement_version = v_current_ev AND p_lease_sequence <= v_current_ls THEN
        -- Check for conflict: same identity with different content
        SELECT envelope_sha256 INTO v_existing_sha
        FROM public.entitlement_snapshot_inbox
        WHERE workspace_id = p_workspace_id
          AND entitlement_version = p_entitlement_version
          AND lease_sequence = p_lease_sequence;
        IF FOUND AND v_existing_sha != p_envelope_sha256 THEN
            v_result := 'conflict';
        ELSE
            v_result := 'duplicate';
        END IF;
    ELSIF p_entitlement_version = v_current_ev AND p_lease_sequence > v_current_ls THEN
        -- Lease renewal: semantic_hash must match
        IF p_semantic_hash != (SELECT semantic_hash FROM public.entitlement_projections WHERE workspace_id = p_workspace_id) THEN
            v_result := 'conflict';
        ELSE
            v_result := 'applied';
        END IF;
    ELSIF p_entitlement_version = v_current_ev + 1 THEN
        v_result := 'applied';
    ELSIF p_delivery_kind = 'full_repair' THEN
        v_result := 'applied';
    ELSE
        v_result := 'version_jump_paused';
    END IF;

    -- Always write inbox
    INSERT INTO public.entitlement_snapshot_inbox (
        snapshot_id, workspace_id, entitlement_version, lease_sequence,
        projection_version, schema_version, delivery_kind,
        canonical_envelope, envelope_sha256, semantic_hash,
        signature, signature_key_id, signature_algorithm,
        outcome, received_at, processed_at, projection_applied_at
    ) VALUES (
        p_snapshot_id, p_workspace_id, p_entitlement_version, p_lease_sequence,
        p_projection_version, p_schema_version, p_delivery_kind,
        p_canonical_envelope, p_envelope_sha256, p_semantic_hash,
        p_signature, p_signature_key_id, p_signature_algorithm,
        v_result, now(), now(),
        CASE WHEN v_result = 'applied' THEN now() ELSE NULL END
    ) ON CONFLICT (workspace_id, entitlement_version, lease_sequence) DO NOTHING;

    -- Apply if permitted
    IF v_result = 'applied' THEN
        INSERT INTO public.entitlement_projections (
            workspace_id, current_snapshot_id, entitlement_version,
            lease_sequence, projection_version, schema_version,
            plan_version_id, source_refs_json, period_start, period_end,
            period_anchor, resolved_entitlements_json, restriction_summary_json,
            semantic_hash, entitlement_registry_version, resolution_policy_version,
            platform_limits_version, issued_at, valid_until,
            control_plane_audit_event_id,
            signature, signature_key_id, signature_algorithm, applied_at
        ) VALUES (
            p_workspace_id, p_snapshot_id, p_entitlement_version,
            p_lease_sequence, p_projection_version, p_schema_version,
            p_plan_version_id, p_source_refs_json, p_period_start, p_period_end,
            p_period_anchor, p_resolved_entitlements_json, p_restriction_summary_json,
            p_semantic_hash, p_entitlement_registry_version, p_resolution_policy_version,
            p_platform_limits_version, p_issued_at, p_valid_until,
            p_control_plane_audit_event_id,
            p_signature, p_signature_key_id, p_signature_algorithm, now()
        )
        ON CONFLICT (workspace_id) DO UPDATE SET
            current_snapshot_id = EXCLUDED.current_snapshot_id,
            entitlement_version = EXCLUDED.entitlement_version,
            lease_sequence = EXCLUDED.lease_sequence,
            projection_version = EXCLUDED.projection_version,
            schema_version = EXCLUDED.schema_version,
            plan_version_id = EXCLUDED.plan_version_id,
            source_refs_json = EXCLUDED.source_refs_json,
            period_start = EXCLUDED.period_start,
            period_end = EXCLUDED.period_end,
            period_anchor = EXCLUDED.period_anchor,
            resolved_entitlements_json = EXCLUDED.resolved_entitlements_json,
            restriction_summary_json = EXCLUDED.restriction_summary_json,
            semantic_hash = EXCLUDED.semantic_hash,
            entitlement_registry_version = EXCLUDED.entitlement_registry_version,
            resolution_policy_version = EXCLUDED.resolution_policy_version,
            platform_limits_version = EXCLUDED.platform_limits_version,
            issued_at = EXCLUDED.issued_at,
            valid_until = EXCLUDED.valid_until,
            control_plane_audit_event_id = EXCLUDED.control_plane_audit_event_id,
            signature = EXCLUDED.signature,
            signature_key_id = EXCLUDED.signature_key_id,
            signature_algorithm = EXCLUDED.signature_algorithm,
            applied_at = now();

        -- Update workspace projection fields
        UPDATE public.workspaces SET
            name = p_workspace_name,
            authorization_epoch = GREATEST(authorization_epoch, p_authorization_epoch),
            entitlement_version = p_entitlement_version,
            suspended = p_suspended,
            lifecycle = p_lifecycle,
            projection_version = p_projection_version
        WHERE id = p_workspace_id;
    END IF;

    RETURN v_result;
END;
$$;
""")
```

- [ ] **Step 6: Edit the migration — RLS policies**

Enable and force RLS on every new table. Grant execute on `marketai_apply_workspace_projection_v2` only to the API role. Revoke PUBLIC execute.

- [ ] **Step 7: Edit the migration — role grants**

Update `deploy/postgres/domain-tenant-roles.sql` with the permission matrix from Spec §2.16 and §4.14. Key grants:
- API: execute `marketai_apply_workspace_projection_v2` only, INSERT on credential tables, full on guardrails, INSERT/SELECT on cost/usage
- Worker: SELECT on active credential bindings/envelopes, CAS UPDATE on reservations/attempts, INSERT/SELECT on cost/usage
- Readonly: SELECT on safe views only, no ciphertext access

- [ ] **Step 8: Test migration runs on fresh database**

```bash
alembic upgrade head
```

- [ ] **Step 9: Test migration runs on existing database (upgrade path)**

```bash
alembic downgrade base && alembic upgrade head
```

- [ ] **Step 10: Commit**

```bash
git add migrations/versions/ deploy/postgres/domain-tenant-roles.sql
git commit -m "feat(domain): Alembic migration for entitlement, credential, cost, usage tables

Creates 14 new tables, extends workspaces/operations/jobs,
installs append-only triggers, marketai_apply_workspace_projection_v2,
RLS policies, and least-privilege grants."
```

---

## Stage 3: Domain Modules

### Task 9: Shared Domain Types and Value Objects

**Files:**
- Create: `app/entitlements/types.py`
- Create: `tests/test_entitlement_types.py`

**Interfaces:**
- Consumes: nothing
- Produces: `ExecutionMode`, `ProviderSelection`, `EntitlementRef`, `EntitlementRequirements`, `FrozenQuote`, `QuoteBundle`, `CapacityRequest`, `ReservationPlan`, `ReservationBundle`, `AdmissionRequest`, `AdmissionResult`, `InboxOutcome`, `ExternalCallOutcome`, `BillableCallSpec`, `JobSpec`, `OperationPayload`, `CustomerNotificationIntent`, `UsageFact`, `CredentialMetadata`, `EncryptedEnvelope`, `CredentialEncryptionContext`

All types are frozen dataclasses per Spec §3.4–§3.14.

- [ ] **Step 1: Write type instantiation tests**

```python
# tests/test_entitlement_types.py
from decimal import Decimal
from app.entitlements.types import (
    ExecutionMode, FrozenQuote, EntitlementRef, AdmissionRequest,
    CapacityRequest, ReservationBundle,
)

def test_execution_mode_values():
    assert ExecutionMode.BYOK == "byok"
    assert ExecutionMode.SANDBOX == "sandbox"
    assert ExecutionMode.MOCK == "mock"

def test_frozen_quote_is_immutable():
    q = FrozenQuote(
        call_semantic_key_hash="abc",
        provider="gemini", model="imagen-3",
        execution_mode=ExecutionMode.BYOK, quantity=1,
        provider_amount=Decimal("0.04"), provider_currency="USD",
        budget_amount=Decimal("0.04"), budget_currency="USD",
        exchange_rate=Decimal("1"), exchange_rate_source="identity",
        exchange_rate_observed_at=None, fx_buffer_bps=0,
        price_rule_version="v1", pricing_input_hash="xyz",
        simulated_amount=None, simulated_currency=None,
    )
    import pytest
    with pytest.raises(AttributeError):
        q.provider_amount = Decimal("999")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_entitlement_types.py -v
```

- [ ] **Step 3: Implement types.py**

Create all frozen dataclasses. Use `Literal["byok", "sandbox", "mock"]` for `ExecutionMode` or a `StrEnum`. All financial fields are `Decimal`. All types are frozen and documented per their spec section.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_entitlement_types.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/entitlements/types.py tests/test_entitlement_types.py
git commit -m "feat(domain): shared entitlement value objects and domain types"
```

---

### Task 10: EntitlementProjectionModule

**Files:**
- Create: `app/entitlements/projection.py`
- Create: `tests/test_entitlement_projection.py`

**Interfaces:**
- Consumes: `marketai_apply_workspace_projection_v2` SQL function (Task 8), `EntitlementSnapshotInboxRecord` and `EntitlementProjectionRecord` (Task 6)
- Produces:
  - `EntitlementProjectionModule.ingest(signed_envelope: bytes, *, delivery_kind: Literal["push", "full_repair"]) -> InboxOutcome`
  - `EntitlementProjectionModule.repair(workspace_id: str) -> InboxOutcome`
  - `InboxOutcome = Literal["applied", "duplicate", "stale", "version_jump_paused", "conflict"]`

- [ ] **Step 1: Write projection module tests**

Test cases per Spec §2.2 application rules:
- Valid signed envelope → `applied`, projection updated
- Same snapshot_id resubmitted → `duplicate`
- Older entitlement_version → `stale`
- Same version, higher lease_sequence with matching semantic_hash → `applied` (lease renewal)
- Same version, higher lease_sequence with different semantic_hash → `conflict`
- Version jump on ordinary push → `version_jump_paused`
- `full_repair` delivery can apply cross-version snapshot → `applied`
- Invalid signature → raises, no inbox row written
- Same snapshot_id with different envelope_sha256 → `conflict`

Use a test fixture that creates a workspace with an existing projection.

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_entitlement_projection.py -v
```

- [ ] **Step 3: Implement projection.py**

```python
class EntitlementProjectionModule:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        verification_key_ring: dict[str, bytes],
        max_lease_seconds: int = 86400,
    ):
        ...

    async def ingest(
        self,
        signed_envelope: bytes,
        *,
        delivery_kind: Literal["push", "full_repair"],
    ) -> InboxOutcome:
        # 1. Parse envelope JSON
        # 2. Verify signature against key ring
        # 3. Validate schema_version == 2
        # 4. Validate workspace_id, lease bounds
        # 5. Call marketai_apply_workspace_projection_v2 via session.execute()
        # 6. Return outcome
        ...

    async def repair(self, workspace_id: str) -> InboxOutcome:
        # Pull from control plane and call ingest with delivery_kind="full_repair"
        ...
```

The signature verification reuses the same HMAC-SHA256 algorithm as the control plane (Spec §1.9). Parse the envelope bytes as JSON, extract `{schema_version, signature_key_id, signature_algorithm, payload}` as the signed content, and verify against the key ring.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_entitlement_projection.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/entitlements/projection.py tests/test_entitlement_projection.py
git commit -m "feat(domain): EntitlementProjectionModule with signature verification and v2 application"
```

---

### Task 11: CredentialProtector and CredentialAdmin

**Files:**
- Create: `app/entitlements/credential_protector.py`
- Create: `app/entitlements/credential_admin.py`
- Create: `tests/test_credential_admin.py`

**Interfaces:**
- Consumes: `CredentialVersionRecord`, `CredentialBindingRecord` (Task 7), `TenantContext` from `app/platform/tenancy.py`
- Produces:
  - `CredentialProtector` Protocol with `seal(plaintext, context) -> EncryptedEnvelope` and `open(envelope, context) -> Iterator[memoryview]` (Spec §2.6)
  - `LocalKmsAdapter` implementing `CredentialProtector` for dev/test
  - `CredentialAdmin.create(...) -> CredentialMetadata`
  - `CredentialAdmin.rotate(...) -> CredentialMetadata`
  - `CredentialAdmin.revoke(...) -> None`
  - `CredentialAdmin.bind(...) -> CredentialBindingView`
  - `CredentialAdmin.list_metadata(...) -> list[CredentialMetadata]`

- [ ] **Step 1: Write credential protector tests**

```python
# tests/test_credential_admin.py
import pytest
from app.entitlements.credential_protector import LocalKmsAdapter, CredentialEncryptionContext

def test_seal_and_open_roundtrip():
    kms = LocalKmsAdapter(master_secret=b"test-secret-key-32-bytes-long!!")
    ctx = CredentialEncryptionContext(
        workspace_id="ws_1", credential_version_id="cv_1",
        credential_key="openai_api_key", provider="openai", version_no=1,
    )
    plaintext = b"sk-test-secret-value"
    envelope = kms.seal(plaintext, ctx)
    with kms.open(envelope, ctx) as view:
        assert bytes(view) == plaintext

def test_different_context_fails_decryption():
    kms = LocalKmsAdapter(master_secret=b"test-secret-key-32-bytes-long!!")
    ctx1 = CredentialEncryptionContext(
        workspace_id="ws_1", credential_version_id="cv_1",
        credential_key="openai_api_key", provider="openai", version_no=1,
    )
    ctx2 = CredentialEncryptionContext(
        workspace_id="ws_2", credential_version_id="cv_1",
        credential_key="openai_api_key", provider="openai", version_no=1,
    )
    envelope = kms.seal(b"secret", ctx1)
    with pytest.raises(Exception):
        with kms.open(envelope, ctx2):
            pass

def test_each_seal_produces_unique_nonce():
    kms = LocalKmsAdapter(master_secret=b"test-secret-key-32-bytes-long!!")
    ctx = CredentialEncryptionContext(
        workspace_id="ws_1", credential_version_id="cv_1",
        credential_key="key", provider="p", version_no=1,
    )
    e1 = kms.seal(b"secret", ctx)
    e2 = kms.seal(b"secret", ctx)
    assert e1.nonce != e2.nonce
    assert e1.encrypted_data_key != e2.encrypted_data_key
```

- [ ] **Step 2: Write credential admin tests**

Test cases per Spec §3.8:
- Create with `expected_absent=True` succeeds when no active credential exists
- Create when active credential exists → conflict
- Rotate atomically creates new version and marks old as `rotated`
- Revoke marks credential as `revoked`
- Bind associates a credential version with a scope
- `list_metadata` returns only metadata, never secrets
- Member/system contexts cannot create, rotate, revoke, bind, or list

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_credential_admin.py -v
```

- [ ] **Step 4: Implement credential_protector.py**

Implement `CredentialProtector` protocol and `LocalKmsAdapter`:
- Generate random 256-bit data key per encryption
- Generate unique AES-GCM nonce per encryption
- Use serialized `CredentialEncryptionContext` as AAD
- KMS wrap/unwrap via deterministic key derivation from master secret (local adapter only)
- Verify GCM auth tag on open
- Context manager clears plaintext from memory on exit (best effort)

- [ ] **Step 5: Implement credential_admin.py**

Implement all five commands per Spec §3.8. Each command:
- Opens a tenant transaction
- Validates `TenantContext.initiator_role == "owner"`
- Performs the operation with CAS/revision checks
- Appends an AuditEventRecord
- Commits

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_credential_admin.py -v
```

- [ ] **Step 7: Commit**

```bash
git add app/entitlements/credential_protector.py app/entitlements/credential_admin.py tests/test_credential_admin.py
git commit -m "feat(domain): CredentialProtector with LocalKMS and CredentialAdmin commands"
```

---

### Task 12: CostQuoteEngine and ReservationBook

**Files:**
- Create: `app/entitlements/quote_engine.py`
- Create: `app/entitlements/reservation_book.py`
- Create: `tests/test_reservation_book.py`

**Interfaces:**
- Consumes: `SpendGuardrailRecord`, `SpendReservationRecord`, `CapacityReservationRecord`, `ProviderCostLedgerEntryRecord` (Task 7), `EntitlementRef` (Task 9), `FrozenQuote`, `QuoteBundle`, `CapacityRequest`, `ReservationPlan`, `ReservationBundle` (Task 9)
- Produces:
  - `CostQuoteEngine.quote(provider, model, execution_mode, quantity, budget_currency, ...) -> FrozenQuote`
  - `CostQuoteEngine.bundle(call_specs) -> QuoteBundle`
  - `ReservationBook.reserve(session, context, plan: ReservationPlan) -> ReservationBundle`

- [ ] **Step 1: Write quote engine tests**

```python
from decimal import Decimal
from app.entitlements.quote_engine import CostQuoteEngine

def test_byok_quote_uses_price_registry():
    engine = CostQuoteEngine(price_registry={"gemini": {"imagen-3": Decimal("0.04")}}, price_rule_version="v1")
    quote = engine.quote(
        call_semantic_key_hash="abc",
        provider="gemini", model="imagen-3",
        execution_mode="byok", quantity=1,
        budget_currency="USD",
    )
    assert quote.provider_amount == Decimal("0.04")
    assert quote.budget_amount == Decimal("0.04")

def test_mock_quote_has_zero_amounts():
    engine = CostQuoteEngine(price_registry={"mock": {"default": Decimal("0.04")}}, price_rule_version="v1")
    quote = engine.quote(
        call_semantic_key_hash="abc",
        provider="mock", model="default",
        execution_mode="mock", quantity=1,
        budget_currency="USD",
    )
    assert quote.provider_amount == Decimal("0")
    assert quote.budget_amount == Decimal("0")
    assert quote.simulated_amount == Decimal("0.04")
```

- [ ] **Step 2: Write reservation book tests**

Test the internal fixed order from Spec §3.6:
- Guardrail per-operation limit blocks when exceeded
- Rolling 24h limit blocks when exceeded
- Period limit blocks when exceeded
- Outstanding exposure limit blocks when exceeded
- Successful reservation creates SpendReservation, CapacityReservation, and Ledger entries atomically
- Concurrent reservations under the same Guardrail are serialized by `FOR UPDATE` on the guardrail row
- Mock does not create SpendReservation
- Admission-phase capacity requests create capacity rows; execution-phase requests are deferred

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_reservation_book.py -v
```

- [ ] **Step 4: Implement quote_engine.py**

Price registry is seeded from `config.COST_PER_IMAGE` as the initial version. Each quote freezes: provider, model, execution mode, amount, currency, exchange rate (1.0 for USD-to-USD), price rule version, and pricing input hash.

- [ ] **Step 5: Implement reservation_book.py**

Internal fixed order per Spec §3.6 steps 1–10:
1. `SELECT ... FOR UPDATE` on SpendGuardrailRecord
2. Lock capacity in stable key order
3. Calculate all four guardrail formulas
4. Check admission-time capacity
5. Create Jobs with preallocated IDs
6. Create per-call SpendReservations
7. Write Quote and Reservation Ledger Entries
8. Create `reserve_on_admission` CapacityReservations
9. Persist notification state transitions
10. Return `ReservationBundle`

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_reservation_book.py -v
```

- [ ] **Step 7: Commit**

```bash
git add app/entitlements/quote_engine.py app/entitlements/reservation_book.py tests/test_reservation_book.py
git commit -m "feat(domain): CostQuoteEngine and ReservationBook with atomic guardrail enforcement"
```

---

### Task 13: EntitlementPolicy and PaidOperationAdmission

**Files:**
- Create: `app/entitlements/policy.py`
- Create: `app/entitlements/admission.py`
- Create: `tests/test_admission.py`

**Interfaces:**
- Consumes: `EntitlementProjectionRecord` (Task 6), `ReservationBook` (Task 12), `CostQuoteEngine` (Task 12), all domain types (Task 9)
- Produces:
  - `EntitlementPolicy.authorize(session, context, requirements, now) -> EntitlementRef`
  - `PaidOperationAdmission.admit_in_transaction(session, context, request: AdmissionRequest) -> AdmissionResult`

- [ ] **Step 1: Write entitlement policy tests**

```python
from app.entitlements.policy import EntitlementPolicy, authorize

def test_authorize_rejects_expired_lease(workspace_with_expired_lease):
    # workspace has valid_until in the past
    with pytest.raises(LeaseExpiredError):
        authorize(session, context, requirements, now=datetime.now(timezone.utc))

def test_authorize_rejects_suspended_workspace(suspended_workspace):
    with pytest.raises(WorkspaceSuspendedError):
        authorize(session, context, requirements, now=datetime.now(timezone.utc))

def test_authorize_rejects_missing_feature(workspace_without_scale):
    requirements = EntitlementRequirements(
        feature_keys=("module.scale.enabled",),
        numeric_requirements=(),
    )
    with pytest.raises(FeatureNotEntitledError):
        authorize(session, context, requirements, now=datetime.now(timezone.utc))

def test_authorize_succeeds_with_valid_lease_and_features(active_workspace):
    ref = authorize(session, context, requirements, now=datetime.now(timezone.utc))
    assert ref.entitlement_version > 0
    assert ref.lease_sequence > 0
```

- [ ] **Step 2: Write admission tests**

Test the 10-step admission flow from Spec §3.7:
- Happy path: lifecycle valid, lease valid, intent claimed, entitlements checked, provider resolved, quoted, reserved, finalized → `AdmissionResult` with operation_id, job_ids, reservation_ids
- Replay: same semantic_key_hash with same fingerprint → return original `AdmissionResult`
- Conflict: same semantic_key_hash with different fingerprint → `SemanticIntentConflictError`
- Lease expired → rejection before quoting
- Feature not entitled → rejection after intent claim
- Guardrail exceeded → rejection after quoting
- Mock does not create SpendReservation

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_admission.py -v
```

- [ ] **Step 4: Implement policy.py**

```python
def authorize(
    session: Session,
    context: TenantContext,
    requirements: EntitlementRequirements,
    now: datetime,
) -> EntitlementRef:
    projection = session.execute(
        select(EntitlementProjectionRecord)
        .where(EntitlementProjectionRecord.workspace_id == context.workspace_id)
    ).scalar_one_or_none()

    if projection is None:
        raise NoEntitlementProjectionError(context.workspace_id)
    if projection.valid_until < now:
        raise LeaseExpiredError(context.workspace_id, projection.valid_until)

    # Check workspace lifecycle
    workspace = session.get(WorkspaceRecord, context.workspace_id)
    if workspace.lifecycle not in ("active",):
        raise WorkspaceLifecycleError(workspace.lifecycle)

    # Check required features
    entitlements = projection.resolved_entitlements_json
    for key in requirements.feature_keys:
        if not entitlements.get(key, False):
            raise FeatureNotEntitledError(key)

    # Check numeric requirements
    for nr in requirements.numeric_requirements:
        current = entitlements.get(nr.key)
        if current is None or current < nr.minimum:
            raise NumericEntitlementExceededError(nr.key, nr.minimum, current)

    return EntitlementRef(
        entitlement_version=projection.entitlement_version,
        lease_sequence=projection.lease_sequence,
        period_start=projection.period_start,
        period_end=projection.period_end,
        semantic_hash=projection.semantic_hash,
    )
```

- [ ] **Step 5: Implement admission.py**

```python
class PaidOperationAdmission:
    def __init__(
        self,
        quote_engine: CostQuoteEngine,
        reservation_book: ReservationBook,
    ):
        ...

    def admit_in_transaction(
        self,
        session: Session,
        context: TenantContext,
        request: AdmissionRequest,
    ) -> AdmissionResult:
        # 1. Validate lifecycle, actor, role
        # 2. Validate lease
        # 3. Claim semantic intent (insert or replay Operation)
        # 4. If replay: return original result immediately
        # 5. Validate feature entitlements
        # 6. Validate explicit provider mode
        # 7. Generate per-call frozen quotes
        # 8. Atomically check and occupy via ReservationBook
        # 9. Finalize operation admission state
        # 10. Return result
        ...
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_admission.py -v
```

- [ ] **Step 7: Commit**

```bash
git add app/entitlements/policy.py app/entitlements/admission.py tests/test_admission.py
git commit -m "feat(domain): EntitlementPolicy and PaidOperationAdmission with 10-step flow"
```

---

### Task 14: ExternalCallCoordinator

**Files:**
- Create: `app/entitlements/coordinator.py`
- Create: `tests/test_coordinator.py`

**Interfaces:**
- Consumes: `ProviderCallAttemptRecord` (Task 7), `SpendReservationRecord` (Task 7), `CredentialAdmin` (Task 11), `CredentialProtector` (Task 11), `EntitlementProjectionModule` (Task 10)
- Defines: `ProviderAdapterProtocol` — the abstract Protocol that Task 16 (Unified Provider Adapter) implements. This avoids a Stage 3→4 forward dependency; Task 14 codes to the Protocol, Task 16 provides the concrete adapter.
- Produces:
  - `ExternalCallCoordinator.execute(context, *, job_id, call_semantic_key_hash) -> ExternalCallOutcome`
  - `ExternalCallOutcome` with typed instruction: `PROCEED`, `WAIT_FOR_CAPACITY`, `PAUSE_FOR_ENTITLEMENT_REPAIR`, `CANCEL_BEFORE_DISPATCH`, `SETTLED`, `UNCERTAIN_RECONCILIATION_REQUIRED`

- [ ] **Step 1: Write coordinator tests**

Test the three-phase flow from Spec §3.9:

**Prepare transaction tests:**
- Lock job and spend reservation
- Re-check lifecycle and lease
- Acquire execution-time concurrency capacity
- Resolve active credential binding
- Create `ProviderCallAttempt(state="prepared")`
- Transition to `dispatching` in same transaction
- Commit

**Provider call tests:**
- Successful call → `succeeded` attempt
- Definite failure → `failed` attempt
- Network ambiguity → `uncertain` attempt

**Complete transaction tests:**
- Success: write Reported Cost, update reservation, release capacity, write usage event
- Failure: write failure evidence, release reservations
- Uncertain: preserve exposure, no re-call

**Fault injection tests (Spec §5.5):**
- Before prepare commit → no provider call
- After prepare commit but before bytes sent → uncertain
- After success but before finalization → recoverable

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_coordinator.py -v
```

- [ ] **Step 3: Implement coordinator.py**

The core invariant: once `dispatching` commits, the system assumes the remote side-effect may have occurred. The coordinator never holds a DB transaction across a network call.

```python
class ExternalCallCoordinator:
    async def execute(
        self,
        context: TenantContext,
        *,
        job_id: str,
        call_semantic_key_hash: str,
    ) -> ExternalCallOutcome:
        # PREPARE TRANSACTION
        attempt = await self._prepare(context, job_id, call_semantic_key_hash)
        if attempt.instruction != "PROCEED":
            return ExternalCallOutcome(instruction=attempt.instruction)

        # PROVIDER CALL (no DB transaction open)
        try:
            result = await self._call_provider(attempt)
        except ProviderDispatchError as e:
            return await self._handle_dispatch_error(context, attempt, e)

        # COMPLETE TRANSACTION
        return await self._finalize(context, attempt, result)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_coordinator.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/entitlements/coordinator.py tests/test_coordinator.py
git commit -m "feat(domain): ExternalCallCoordinator with durable dispatch fence"
```

---

### Task 15: CostReconciler, UsageRecorder, UsageQuery, GuardrailAdmin, CustomerNotificationWriter

**Files:**
- Create: `app/entitlements/cost_reconciler.py`
- Create: `app/entitlements/usage.py`
- Create: `app/entitlements/guardrail_admin.py`
- Create: `app/entitlements/notification_writer.py`
- Create: `tests/test_reconciler.py`
- Create: `tests/test_usage.py`
- Create: `tests/test_guardrail_admin.py`

**Interfaces:**
- Consumes: `ProviderCallAttemptRecord`, `SpendReservationRecord`, `ProviderCostLedgerEntryRecord`, `UsageEventRecord`, `SpendGuardrailRecord`, `SpendGuardrailEventRecord`, `CustomerNotificationOutboxRecord`, `GuardrailNotificationStateRecord` (Tasks 6–7), `TenantContext`
- Produces:
  - `CostReconciler.reconcile_attempt(context, attempt_id, evidence) -> ReconciliationResult`
  - `CostReconciler.record_adjustment(context, *, target_entry_id, direction, ...) -> None`
  - `UsageRecorder.record_many(session, context, events: Sequence[UsageFact]) -> None`
  - `UsageQuery.aggregate(session, context, request) -> UsageAggregate`
  - `GuardrailAdmin.get(context) -> SpendGuardrailView`
  - `GuardrailAdmin.preview_update(context, request, *, expected_revision, mfa_evidence_ref) -> GuardrailUpdatePreview`
  - `GuardrailAdmin.confirm_update(context, *, preview_token) -> SpendGuardrailView`
  - `CustomerNotificationWriter.enqueue(session, context, intent) -> NotificationOutboxRef`

- [ ] **Step 1: Write reconciler tests**

Per Spec §3.10:
- Provider confirms non-execution → release reservation
- Provider confirms execution → write Reported/Estimated Settlement
- First settlement above reservation → record full amount, emit breach delta, no Adjustment
- Later correction to existing settlement → Adjustment
- Duplicate evidence → idempotent

- [ ] **Step 2: Write usage tests**

Per Spec §3.11:
- Same semantic key + same payload → idempotent
- Same key + different payload → conflict
- Delta/gauge measurement kinds match only legal direction values

- [ ] **Step 3: Write guardrail admin tests**

Per Spec §3.12:
- Lowering limits does not require MFA
- Raising limits requires valid step-up assertion
- Values above platform maximum → rejection
- Currency change blocked with outstanding reservations
- Confirm atomically updates guardrail and appends event

- [ ] **Step 4: Run tests to verify they fail**

```bash
pytest tests/test_reconciler.py tests/test_usage.py tests/test_guardrail_admin.py -v
```

- [ ] **Step 5: Implement all four modules**

Follow Spec §3.10–§3.13 for each module.

`CustomerNotificationWriter` (Spec §3.13):
- Lock `GuardrailNotificationStateRecord` row
- Determine if threshold crossing qualifies for notification
- Write state transition with `crossing_sequence`
- Insert `CustomerNotificationOutboxRecord` with deterministic `deduplication_key`
- 80% crossing in admission transaction; 100% rejection in independent post-rejection transaction

- [ ] **Step 6: Run all tests**

```bash
pytest tests/test_reconciler.py tests/test_usage.py tests/test_guardrail_admin.py -v
```

- [ ] **Step 7: Commit**

```bash
git add app/entitlements/cost_reconciler.py app/entitlements/usage.py app/entitlements/guardrail_admin.py app/entitlements/notification_writer.py tests/test_reconciler.py tests/test_usage.py tests/test_guardrail_admin.py
git commit -m "feat(domain): CostReconciler, UsageRecorder/Query, GuardrailAdmin, CustomerNotificationWriter"
```

---

## Stage 4: Integration

### Task 16: Unified Provider Adapter Interface

**Files:**
- Create: `app/providers/adapter.py`
- Modify: `app/providers/mock.py`
- Modify: `app/providers/gemini.py`
- Modify: `app/providers/openai_img.py`
- Modify: `app/providers/seedream.py`
- Create: `tests/test_provider_adapter.py`

**Interfaces:**
- Consumes: existing `ImageProvider` class from `app/providers/base.py`
- Produces:
  - `ImageProvider` Protocol with `capabilities: ProviderCapabilities`, `execute(request: ProviderCallRequest) -> ProviderCallResult`, `lookup(request: ProviderLookupRequest) -> ProviderLookupResult` (Spec §4.11)
  - `ProviderDispatchError` with `dispatch_certainty: DispatchCertainty`
  - Updated mock/gemini/openai/seedream adapters implementing the new interface

- [ ] **Step 1: Write adapter protocol tests**

```python
from app.providers.adapter import ImageProvider, ProviderCapabilities, ProviderCallRequest

def test_mock_adapter_capabilities():
    from app.providers.mock import MockProvider
    adapter = MockProvider()
    assert adapter.capabilities.accepts_idempotency_key is True
    assert adapter.capabilities.supports_result_lookup_by_request_id is False

async def test_mock_execute_returns_result():
    from app.providers.mock import MockProvider
    adapter = MockProvider()
    request = ProviderCallRequest(
        prompt="test", reference_inputs=(), aspect_ratio="1:1",
        image_size="1024x1024", model="default",
        idempotency_key="idem_1", credential=None,
    )
    result = await adapter.execute(request)
    assert result.output_bytes is not None
    assert result.idempotency_key_accepted is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_provider_adapter.py -v
```

- [ ] **Step 3: Create adapter.py with protocol and types**

Define the unified protocol per Spec §4.11. All types are frozen dataclasses. `ProviderDispatchError` carries typed `dispatch_certainty`.

- [ ] **Step 4: Update each provider to implement new interface**

Each adapter gets an `execute()` method alongside the existing `generate()` (which remains for legacy path). The old `generate_with_retry` is not used for new paid calls.

- Mock: always `not_dispatched` on failure, accepts idempotency key
- Gemini: real HTTP call, `may_have_dispatched` on timeout/network
- OpenAI: real HTTP call, `may_have_dispatched` on timeout/network
- Seedream: same pattern

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_provider_adapter.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/providers/adapter.py app/providers/mock.py app/providers/gemini.py app/providers/openai_img.py app/providers/seedream.py tests/test_provider_adapter.py
git commit -m "feat(providers): unified Provider Adapter interface with typed dispatch certainty"
```

---

### Task 17: Rewire Generation Command with Atomic Reservation

**Files:**
- Modify: `app/store.py`
- Create: `tests/test_generation_admission.py`

**Interfaces:**
- Consumes: `PaidOperationAdmission` (Task 13), `CostQuoteEngine` (Task 12), `ReservationBook` (Task 12), existing `create_run()` in `app/store.py:467`
- Produces: Updated `create_run()` that uses `PaidOperationAdmission.admit_in_transaction()` per Spec §4.9

- [ ] **Step 1: Write integration test for new create_run flow**

```python
def test_create_run_with_admission_creates_operation_and_reservations(
    active_workspace, workspace_guardrail,
):
    """New admission path: create_run creates Operation, Jobs, and
    SpendReservations in a single transaction."""
    run = make_test_run(provider="gemini", model="imagen-3", execution_mode="byok")
    create_run(context, run)

    # Verify Operation was created
    with tenant_session(context) as session:
        op = session.execute(
            select(OperationRecord).where(OperationRecord.workspace_id == context.workspace_id)
        ).scalar_one()
        assert op.admission_contract_version == 1

        # Verify SpendReservation was created per planned call
        reservations = session.execute(
            select(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == context.workspace_id
            )
        ).scalars().all()
        assert len(reservations) == len(run.assets)

def test_create_run_mock_skips_spend_reservation(active_workspace):
    run = make_test_run(provider="mock", model="default", execution_mode="mock")
    create_run(context, run)

    with tenant_session(context) as session:
        reservations = session.execute(
            select(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == context.workspace_id
            )
        ).scalars().all()
        assert len(reservations) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_generation_admission.py -v
```

- [ ] **Step 3: Rewire create_run**

Transform `create_run` per Spec §4.9 ordering:
1. Open tenant transaction
2. Validate lifecycle and lease
3. Claim semantic intent
4. Check for replay
5. Write Run/Asset pending records
6. Construct per-call `BillableCallSpec`
7. Freeze per-call Quotes
8. Lock and check Guardrails
9. Create Jobs with `execution_contract_version`
10. Create per-call SpendReservations
11. Create admission-time Capacity Reservations
12. Finalize Operation
13. Single commit

Remove `_daily_cost_exposure()` and direct `CostLedgerRecord` writes from the new path. Retain `MAX_COST_PER_RUN` and `MAX_COST_PER_DAY` only as platform absolute limits fed into the Guardrail platform policy.

Add deployment flag `MARKETAI_ADMISSION_CONTRACT_VERSION` (default `0` = legacy, `1` = new). When `1`, `create_run` uses the new path.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_generation_admission.py -v
```

- [ ] **Step 5: Run existing test suite to verify no regressions**

```bash
make verify
```

- [ ] **Step 6: Commit**

```bash
git add app/store.py tests/test_generation_admission.py
git commit -m "feat(domain): rewire create_run with PaidOperationAdmission

Generation commands now use atomic admission: semantic intent claim,
entitlement validation, per-call quoting, guardrail check, and
spend/capacity reservation in a single transaction."
```

---

### Task 18: Worker Dispatch Fence and Entitlement API Routes

**Files:**
- Modify: `app/worker_runtime.py`
- Create: `app/entitlements/api.py`
- Modify: `app/main.py`
- Create: `tests/test_worker_dispatch.py`

**Interfaces:**
- Consumes: `ExternalCallCoordinator` (Task 14), `CredentialAdmin` (Task 11), `GuardrailAdmin` (Task 15), `UsageQuery` (Task 15), `EntitlementProjectionModule` (Task 10)
- Produces:
  - Worker `_generate_asset` uses `ExternalCallCoordinator.execute()` instead of `generate_with_retry`
  - `POST /api/internal/domain/workspaces/{workspace_id}/entitlement-inbox` (Spec §4.4)
  - `POST /api/v1/credentials` (Spec §4.5)
  - `POST /api/v1/credentials/{credential_key}/rotate`
  - `POST /api/v1/credentials/{credential_version_id}/revoke`
  - `GET /api/v1/credentials`
  - `PUT /api/v1/credentials/{credential_version_id}/bindings/{scope}`
  - `GET /api/v1/spend-guardrail` (Spec §4.6)
  - `POST /api/v1/spend-guardrail/preview`
  - `POST /api/v1/spend-guardrail/confirm`
  - `GET /api/v1/usage` (Spec §4.7)
  - `GET /api/v1/cost-summary`

- [ ] **Step 1: Write worker dispatch test**

```python
async def test_generate_asset_uses_coordinator(
    active_workspace, claimed_job_with_reservation,
):
    """Worker uses ExternalCallCoordinator instead of generate_with_retry."""
    runtime = WorkerRuntime()
    await runtime._generate_asset(claimed_job_with_reservation)

    # Verify ProviderCallAttempt was created
    with tenant_session(context) as session:
        attempt = session.execute(
            select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.job_id == claimed_job_with_reservation.job.id
            )
        ).scalar_one()
        assert attempt.state in ("succeeded", "failed", "uncertain")
```

- [ ] **Step 2: Write entitlement inbox endpoint test**

```python
async def test_inbox_endpoint_applies_valid_envelope(client, signed_envelope_bytes):
    response = await client.post(
        f"/api/internal/domain/workspaces/{workspace_id}/entitlement-inbox",
        content=signed_envelope_bytes,
        headers={
            "Authorization": f"Bearer {CONTROL_PLANE_TO_DOMAIN_TOKEN}",
            "Content-Type": "application/octet-stream",
        },
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "applied"

async def test_inbox_endpoint_rejects_wrong_token(client, signed_envelope_bytes):
    response = await client.post(
        f"/api/internal/domain/workspaces/{workspace_id}/entitlement-inbox",
        content=signed_envelope_bytes,
        headers={
            "Authorization": "Bearer wrong-token",
            "Content-Type": "application/octet-stream",
        },
    )
    assert response.status_code == 403
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_worker_dispatch.py -v
```

- [ ] **Step 4: Update worker_runtime.py**

Modify `_generate_asset` to use `ExternalCallCoordinator.execute()` when `execution_contract_version >= 1`. The old `generate_with_retry` path remains for `execution_contract_version == 0` (legacy jobs).

- [ ] **Step 5: Create entitlements/api.py**

Create a FastAPI `APIRouter` with all routes from Spec §4.4–§4.7:
- Inbox endpoint: constant-time token comparison, size limit, raw bytes to `EntitlementProjectionModule.ingest()`
- Credential routes: Owner-only via `TenantContext`, `Idempotency-Key` required, `Cache-Control: no-store`
- Guardrail routes: get/preview/confirm pattern
- Usage/cost routes: cursor pagination, execution-mode separation

- [ ] **Step 6: Create mutation policy registry**

Per Spec §4.12, create a registry mapping operations to policies:
```python
# In app/entitlements/api.py
MUTATION_POLICY = {
    "create_run": "LEASE_REQUIRED",
    "upload": "LEASE_REQUIRED",
    "create_credential": "LEASE_REQUIRED",
    "rotate_credential": "LEASE_REQUIRED",
    "bind_credential": "LEASE_REQUIRED",
    "raise_guardrail": "LEASE_REQUIRED",
    "read": "READ_ONLY",
    "export": "READ_ONLY",
    "cost_query": "READ_ONLY",
    "revoke_credential": "RECOVERY_ALLOWED",
    "lower_guardrail": "RECOVERY_ALLOWED",
    "cancel_undispatched": "RECOVERY_ALLOWED",
    "reconcile": "RECOVERY_ALLOWED",
}
```

Add a FastAPI dependency that checks the policy before each endpoint executes. `LEASE_REQUIRED` verifies the entitlement projection's `valid_until > now`. `READ_ONLY` permits regardless of lease. `RECOVERY_ALLOWED` permits even with expired lease.

- [ ] **Step 7: Register routes in main.py**

Add the internal inbox route outside `DelegatedIdentityMiddleware`. Add the v1 API routes inside the authenticated router.

- [ ] **Step 8: Run OpenAPI generation and commit generated files**

```bash
scripts/generate_all.sh
```

Commit updated: `domain-v1.d.ts`, `operation-map.d.ts`, `operation-registry.ts`, `allowlist-binding.ts`, `allowlist.ts`, `validators.ts`, `paths.ts` per Spec §4.8.

- [ ] **Step 9: Run tests**

```bash
pytest tests/test_worker_dispatch.py -v
```

- [ ] **Step 10: Commit**

```bash
git add app/worker_runtime.py app/entitlements/api.py app/main.py tests/test_worker_dispatch.py
git commit -m "feat(integration): Worker dispatch fence, mutation policy, and entitlement API routes

Worker uses ExternalCallCoordinator for new-contract jobs. Adds inbox,
credential, guardrail, usage, and cost API endpoints. Mutation policy
registry enforces LEASE_REQUIRED / READ_ONLY / RECOVERY_ALLOWED."
```

---

### Task 19: Upgrade Projection Sync to v2 Envelope

**Files:**
- Modify: `app/platform/projection_sync.py`
- Modify: `app/config.py`
- Create: `tests/test_projection_sync_v2.py`

**Interfaces:**
- Consumes: `EntitlementProjectionModule` (Task 10), new v2 pull-repair endpoint (Task 4)
- Produces: `WorkspaceProjectionSyncer` upgraded to pull and apply v2 signed envelopes via `EntitlementProjectionModule.ingest()`

- [ ] **Step 1: Write v2 projection sync tests**

```python
async def test_v2_syncer_pulls_and_applies_envelope(
    mock_control_plane_v2_endpoint, active_workspace,
):
    syncer = WorkspaceProjectionSyncer(
        fetcher=v2_fetcher,
        projection_module=projection_module,
    )
    result = await syncer.sync(workspace_id)
    assert result.outcome == "applied"
    assert result.entitlement_version > 0

async def test_v2_syncer_does_not_advance_entitlements_from_v1(
    mock_control_plane_v1_endpoint, active_workspace,
):
    """v1 endpoint cannot advance entitlement contents."""
    syncer = WorkspaceProjectionSyncer(fetcher=v1_fetcher, ...)
    # Should only apply authorization fields, not entitlement contents
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_projection_sync_v2.py -v
```

- [ ] **Step 3: Add new config vars**

Add to `app/config.py`:
```python
DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN = os.getenv("DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN", "")
CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN = os.getenv("CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN", "")
ENTITLEMENT_VERIFICATION_KEY_RING = os.getenv("ENTITLEMENT_VERIFICATION_KEY_RING", "")
ENTITLEMENT_MAX_LEASE_SECONDS = int(os.getenv("ENTITLEMENT_MAX_LEASE_SECONDS", "86400"))
MARKETAI_CREDENTIAL_KMS_ADAPTER = os.getenv("MARKETAI_CREDENTIAL_KMS_ADAPTER", "local")
MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET = os.getenv("MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET", "")
MARKETAI_PLATFORM_MAX_PER_OPERATION = Decimal(os.getenv("MARKETAI_PLATFORM_MAX_PER_OPERATION", "5"))
MARKETAI_PLATFORM_MAX_ROLLING_24H = Decimal(os.getenv("MARKETAI_PLATFORM_MAX_ROLLING_24H", "20"))
MARKETAI_PLATFORM_MAX_PERIOD = Decimal(os.getenv("MARKETAI_PLATFORM_MAX_PERIOD", "100"))
MARKETAI_PLATFORM_MAX_OUTSTANDING = Decimal(os.getenv("MARKETAI_PLATFORM_MAX_OUTSTANDING", "10"))
MARKETAI_PLATFORM_POLICY_VERSION = int(os.getenv("MARKETAI_PLATFORM_POLICY_VERSION", "1"))
MARKETAI_PRICE_REGISTRY_VERSION = os.getenv("MARKETAI_PRICE_REGISTRY_VERSION", "v1")
MARKETAI_ADMISSION_CONTRACT_VERSION = int(os.getenv("MARKETAI_ADMISSION_CONTRACT_VERSION", "0"))
```

- [ ] **Step 4: Upgrade projection_sync.py**

Add a `WorkspaceProjectionV2Fetcher` that calls the new `/workspace-projection-snapshot` endpoint with `DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN`. The fetcher returns raw bytes. The syncer passes those bytes to `EntitlementProjectionModule.ingest(delivery_kind="full_repair")`.

Retain the v1 fetcher and store for backward compatibility during rollout.

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_projection_sync_v2.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/platform/projection_sync.py app/config.py tests/test_projection_sync_v2.py
git commit -m "feat(integration): upgrade projection sync to v2 signed envelope with dual service identities"
```

---

## Stage 5: Legacy Bridge + End-to-End Tests

### Task 20: Legacy Cost Evidence Compatibility View

**Files:**
- Create: `app/entitlements/legacy_bridge.py`
- Create: `tests/test_legacy_bridge.py`

**Interfaces:**
- Consumes: existing `CostLedgerRecord` (`app/adapters/persistence/models.py:400`), `GenerationRunRecord`, `JobRecord`
- Produces: `create_legacy_compatibility_view()` that creates a read-only SQL view mapping old evidence to new schema (Spec §6.3)

- [ ] **Step 1: Write legacy bridge tests**

```python
def test_compatibility_view_maps_reserved_to_reservation(legacy_cost_row):
    results = query_compatibility_view(session, workspace_id)
    entry = next(r for r in results if r.original_id == legacy_cost_row.id)
    assert entry.entry_type == "reservation"

def test_compatibility_view_maps_actual_to_estimated_settlement(legacy_cost_row):
    results = query_compatibility_view(session, workspace_id)
    entry = next(r for r in results if r.original_id == legacy_cost_row.id)
    assert entry.entry_type == "estimated_settlement"

def test_compatibility_view_assigns_correct_execution_mode(legacy_mock_run):
    results = query_compatibility_view(session, workspace_id)
    entry = next(r for r in results if r.run_provider == "mock")
    assert entry.execution_mode == "mock"

def test_compatibility_view_uses_legacy_platform_for_non_mock(legacy_real_run):
    results = query_compatibility_view(session, workspace_id)
    entry = next(r for r in results if r.run_provider == "gemini")
    assert entry.execution_mode == "legacy_platform"

def test_compatibility_view_does_not_duplicate_rows(legacy_cost_row):
    results = query_compatibility_view(session, workspace_id)
    assert len([r for r in results if r.original_id == legacy_cost_row.id]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_legacy_bridge.py -v
```

- [ ] **Step 3: Implement legacy_bridge.py**

Create the SQL view per Spec §6.3:
- Prefix public evidence ID with `legacy_` to prevent collision
- Map `reserved` → `reservation`, `released` → `release`, `actual` → `estimated_settlement`
- Join to run for provider to determine execution_mode: `mock` → `mock`, non-mock → `legacy_platform`, missing → `legacy_unknown`
- Single currency: expose same amount for both provider and budget, null exchange rate with `legacy_single_currency` provenance

The view is SELECT-only and excluded from new Guardrail admission aggregates.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_legacy_bridge.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/entitlements/legacy_bridge.py tests/test_legacy_bridge.py
git commit -m "feat(domain): read-only legacy cost evidence compatibility view"
```

---

### Task 21: Cross-Tenant Isolation and RLS Tests

**Files:**
- Create: `tests/test_entitlement_isolation.py`

**Interfaces:**
- Consumes: all new tables from Tasks 6–8, RLS policies, role grants
- Produces: PostgreSQL integration tests proving cross-tenant isolation for all new tables

- [ ] **Step 1: Write cross-tenant isolation tests**

Per Spec §5.2, test with two independently provisioned Workspaces:

```python
@pytest.mark.postgres
def test_entitlement_projection_rls_isolation(two_workspaces):
    """Workspace A cannot read Workspace B's entitlement projection."""
    ws_a, ws_b = two_workspaces
    with tenant_session(ws_a.context) as session:
        projections = session.execute(
            select(EntitlementProjectionRecord)
        ).scalars().all()
        assert all(p.workspace_id == ws_a.id for p in projections)

@pytest.mark.postgres
def test_credential_rls_isolation(two_workspaces_with_credentials):
    """Workspace A cannot read Workspace B's credentials."""
    ws_a, ws_b = two_workspaces_with_credentials
    with tenant_session(ws_a.context) as session:
        creds = session.execute(
            select(CredentialVersionRecord)
        ).scalars().all()
        assert all(c.workspace_id == ws_a.id for c in creds)

@pytest.mark.postgres
def test_composite_fk_rejects_cross_workspace_reference(two_workspaces):
    """SpendReservation cannot reference an Operation from another Workspace."""
    ws_a, ws_b = two_workspaces
    with tenant_session(ws_a.context) as session:
        with pytest.raises(IntegrityError):
            session.add(SpendReservationRecord(
                workspace_id=ws_a.id,
                operation_id=ws_b.operation_id,  # cross-tenant!
                ...
            ))
            session.flush()
```

Repeat for every new table: spend_reservations, capacity_reservations, provider_cost_ledger, usage_events, provider_call_attempts, customer_notification_outbox, guardrail_notification_states.

- [ ] **Step 2: Write append-only trigger tests**

```python
@pytest.mark.postgres
def test_cost_ledger_rejects_update(cost_ledger_entry):
    with pytest.raises(Exception, match="append-only"):
        session.execute(
            update(ProviderCostLedgerEntryRecord)
            .where(ProviderCostLedgerEntryRecord.id == cost_ledger_entry.id)
            .values(provider_amount=Decimal("999"))
        )

@pytest.mark.postgres
def test_cost_ledger_rejects_delete(cost_ledger_entry):
    with pytest.raises(Exception, match="append-only"):
        session.execute(
            delete(ProviderCostLedgerEntryRecord)
            .where(ProviderCostLedgerEntryRecord.id == cost_ledger_entry.id)
        )
```

- [ ] **Step 3: Write role permission tests**

```python
@pytest.mark.postgres
def test_readonly_role_cannot_read_credential_ciphertext(readonly_session):
    with pytest.raises(Exception):
        readonly_session.execute(
            select(CredentialVersionRecord.encrypted_secret)
        )

@pytest.mark.postgres
def test_worker_role_cannot_modify_guardrails(worker_session):
    with pytest.raises(Exception):
        worker_session.execute(
            update(SpendGuardrailRecord).values(per_operation_limit=Decimal("999"))
        )
```

- [ ] **Step 4: Run all isolation tests**

```bash
pytest tests/test_entitlement_isolation.py -v --tb=short
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_entitlement_isolation.py
git commit -m "test: cross-tenant RLS, append-only triggers, and role permission isolation"
```

---

### Task 22: Admission Concurrency and Guardrail Contention Tests

**Files:**
- Create: `tests/test_admission_concurrency.py`

**Interfaces:**
- Consumes: `PaidOperationAdmission` (Task 13), `ReservationBook` (Task 12), `SpendGuardrailRecord` (Task 7)
- Produces: PostgreSQL concurrency tests proving atomic guardrail contention

- [ ] **Step 1: Write concurrency tests**

Per Spec §5.3, use barriers and independent database connections:

```python
@pytest.mark.postgres
async def test_concurrent_reservations_respect_guardrail(active_workspace):
    """Two concurrent admissions under a $5 per-operation limit.
    Each tries to reserve $3. Only one should succeed."""
    barrier = asyncio.Barrier(2)
    results = []

    async def attempt(i: int):
        await barrier.wait()
        try:
            result = admission.admit_in_transaction(session, context, request)
            results.append(("success", i))
        except GuardrailExceededError:
            results.append(("rejected", i))

    await asyncio.gather(attempt(0), attempt(1))
    assert results.count(("success",)) <= 1  # at most one succeeds

@pytest.mark.postgres
def test_rolling_24h_aggregation_uses_controlled_clock(active_workspace, clock):
    """Reservations just outside the 24h window are excluded."""
    clock.set(datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc))
    # Create a $15 settlement 25 hours ago
    create_settlement(workspace, amount=Decimal("15"), at=clock.now() - timedelta(hours=25))

    # New $15 reservation should succeed (old one outside window)
    result = admission.admit_in_transaction(session, context, request_for(Decimal("15")))
    assert result.operation_id is not None
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_admission_concurrency.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_admission_concurrency.py
git commit -m "test: admission concurrency and guardrail contention with barriers"
```

---

### Task 23: End-to-End Admission → Dispatch → Settlement Test

**Files:**
- Create: `tests/test_entitlements_e2e.py`

**Interfaces:**
- Consumes: all modules from Tasks 10–18
- Produces: process-level E2E tests covering the full lifecycle

- [ ] **Step 1: Write E2E happy-path test**

```python
@pytest.mark.postgres
async def test_full_byok_lifecycle(
    provisioned_workspace,
    workspace_with_entitlements,
    workspace_with_credentials,
    workspace_with_guardrail,
):
    """Full lifecycle: admit → dispatch → settle → usage recorded."""
    # 1. Admit a generation command
    run = make_test_run(execution_mode="byok", provider="gemini")
    create_run(context, run)

    # 2. Verify admission created Operation + Reservations
    with tenant_session(context) as session:
        op = session.execute(select(OperationRecord).where(...)).scalar_one()
        assert op.state == "accepted"
        reservations = session.execute(select(SpendReservationRecord).where(...)).scalars().all()
        assert all(r.state == "reserved" for r in reservations)

    # 3. Worker processes the job
    worker = WorkerRuntime()
    await worker.run_once()

    # 4. Verify settlement
    with tenant_session(context) as session:
        ledger = session.execute(
            select(ProviderCostLedgerEntryRecord)
            .where(ProviderCostLedgerEntryRecord.workspace_id == context.workspace_id)
            .where(ProviderCostLedgerEntryRecord.entry_type.in_(["reported_cost", "estimated_settlement"]))
        ).scalars().all()
        assert len(ledger) > 0

        # Reservations are settled
        reservations = session.execute(select(SpendReservationRecord).where(...)).scalars().all()
        assert all(r.state in ("settled", "released") for r in reservations)

        # Usage recorded
        usage = session.execute(select(UsageEventRecord).where(...)).scalars().all()
        assert len(usage) > 0
```

- [ ] **Step 2: Write E2E mock isolation test**

```python
@pytest.mark.postgres
async def test_mock_produces_zero_real_cost(provisioned_workspace):
    run = make_test_run(execution_mode="mock", provider="mock")
    create_run(context, run)
    await WorkerRuntime().run_once()

    with tenant_session(context) as session:
        reservations = session.execute(select(SpendReservationRecord).where(...)).scalars().all()
        assert len(reservations) == 0  # Mock creates no spend reservations
```

- [ ] **Step 3: Write E2E uncertain outcome test**

```python
@pytest.mark.postgres
async def test_uncertain_call_preserves_exposure(
    provisioned_workspace, fault_injecting_provider,
):
    """Provider returns ambiguous error → uncertain state, exposure preserved."""
    fault_injecting_provider.set_fault("may_have_dispatched")

    run = make_test_run(execution_mode="byok", provider="gemini")
    create_run(context, run)
    await WorkerRuntime().run_once()

    with tenant_session(context) as session:
        attempt = session.execute(select(ProviderCallAttemptRecord).where(...)).scalar_one()
        assert attempt.state == "uncertain"

        reservation = session.execute(select(SpendReservationRecord).where(...)).scalar_one()
        assert reservation.state == "uncertain"
        assert reservation.outstanding_budget_amount > 0  # exposure preserved
```

- [ ] **Step 4: Write E2E lease expiry test**

```python
@pytest.mark.postgres
async def test_expired_lease_blocks_new_operations(provisioned_workspace, clock):
    """Operations are rejected when the entitlement lease has expired."""
    clock.advance(hours=25)  # past 24h lease
    run = make_test_run(execution_mode="byok", provider="gemini")
    with pytest.raises(LeaseExpiredError):
        create_run(context, run)
```

- [ ] **Step 5: Run all E2E tests**

```bash
pytest tests/test_entitlements_e2e.py -v --tb=short
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_entitlements_e2e.py
git commit -m "test: end-to-end admission→dispatch→settlement lifecycle tests"
```

---

### Task 24: Credential Safety and Canary Tests

**Files:**
- Create: `tests/test_credential_safety.py`

**Interfaces:**
- Consumes: `CredentialAdmin` (Task 11), `CredentialProtector` (Task 11), `ExternalCallCoordinator` (Task 14)
- Produces: Security tests per Spec §5.4

- [ ] **Step 1: Write canary secret tests**

```python
CANARY_SECRET = "CANARY-sk-DEADBEEF-credential-leak-detector"

def test_canary_absent_from_api_responses(client, workspace_with_canary_credential):
    response = client.get("/api/v1/credentials", headers=owner_headers)
    assert CANARY_SECRET not in response.text

def test_canary_absent_from_error_bodies(client, workspace_with_canary_credential):
    response = client.post("/api/v1/credentials", json={
        "credential_key": "openai_api_key",  # already exists
        "secret": CANARY_SECRET,
        "provider": "openai",
    }, headers=owner_headers)
    assert response.status_code == 409
    assert CANARY_SECRET not in response.text

@pytest.mark.postgres
def test_readonly_role_cannot_see_ciphertext(readonly_session, workspace_with_canary_credential):
    results = readonly_session.execute(text("SELECT * FROM credential_versions")).fetchall()
    for row in results:
        assert CANARY_SECRET not in str(row)

def test_cross_workspace_aad_fails_decryption():
    kms = LocalKmsAdapter(master_secret=b"test-key-32-bytes-padding!!!!")
    ctx_a = CredentialEncryptionContext(workspace_id="ws_a", ...)
    ctx_b = CredentialEncryptionContext(workspace_id="ws_b", ...)
    envelope = kms.seal(b"secret", ctx_a)
    with pytest.raises(Exception):
        with kms.open(envelope, ctx_b):
            pass
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_credential_safety.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_credential_safety.py
git commit -m "test: credential safety canary and cross-workspace decryption tests"
```

---

### Task 25: Cost Evidence Replay and Mode Isolation Tests

**Files:**
- Create: `tests/test_cost_evidence.py`

**Interfaces:**
- Consumes: `ProviderCostLedgerEntryRecord` (Task 7), `SpendReservationRecord` (Task 7), legacy bridge (Task 20)
- Produces: Tests per Spec §5.6

- [ ] **Step 1: Write ledger replay tests**

```python
def test_ledger_replays_to_same_balance(settled_workspace):
    """Replaying all ledger entries produces the same outstanding balance."""
    entries = session.execute(
        select(ProviderCostLedgerEntryRecord)
        .where(ProviderCostLedgerEntryRecord.workspace_id == workspace_id)
        .order_by(ProviderCostLedgerEntryRecord.created_at)
    ).scalars().all()

    balance = Decimal("0")
    for e in entries:
        if e.direction == "increase":
            balance += e.budget_amount
        else:
            balance -= e.budget_amount
    assert balance == expected_outstanding

def test_adjustment_references_original_entry(adjustment_entry):
    assert adjustment_entry.adjusts_entry_id is not None
    original = session.get(ProviderCostLedgerEntryRecord, adjustment_entry.adjusts_entry_id)
    assert original is not None
    assert original.workspace_id == adjustment_entry.workspace_id
```

- [ ] **Step 2: Write mode isolation tests**

```python
def test_mock_amounts_are_zero(mock_ledger_entry):
    assert mock_ledger_entry.provider_amount == Decimal("0")
    assert mock_ledger_entry.budget_amount == Decimal("0")

def test_mock_simulated_amount_in_separate_field(mock_ledger_entry):
    assert mock_ledger_entry.simulated_amount is not None
    assert mock_ledger_entry.simulated_amount > Decimal("0")

def test_legacy_evidence_stays_in_legacy_buckets(legacy_bridge_results):
    for entry in legacy_bridge_results:
        assert entry.execution_mode in ("mock", "legacy_platform", "legacy_unknown")
        assert entry.execution_mode not in ("byok", "sandbox")
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_cost_evidence.py -v
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_cost_evidence.py
git commit -m "test: cost evidence replay, mode isolation, and legacy bridge determinism"
```

---

### Task 26: Migration Verification and Full Test Suite

**Files:**
- Modify: (no new files — verification task)

**Interfaces:**
- Consumes: everything from Tasks 1–25
- Produces: passing `make verify` plus all ticket-specific test suites

- [ ] **Step 1: Run full test suite**

```bash
make verify
```

- [ ] **Step 2: Run PostgreSQL-specific tests**

```bash
pytest -m postgres -v --tb=short
```

- [ ] **Step 3: Run migration check**

```bash
make migration-check
```

- [ ] **Step 4: Run web tests and build**

```bash
cd web && npm run build && npm test
```

- [ ] **Step 5: Verify OpenAPI generation is clean**

```bash
scripts/generate_all.sh
git diff --exit-code
```

- [ ] **Step 6: Check for credential canary leaks in all outputs**

```bash
grep -rn "CANARY" tests/ app/ web/ --include="*.log" --include="*.json" | grep -v "test_" | grep -v ".py:" || echo "No canary leaks found"
```

- [ ] **Step 7: Final commit — stage 5**

Squash stage 5 tests into the final commit:
```bash
git add -A
git commit -m "test: complete acceptance evidence for Issue #18

Cross-tenant isolation, admission concurrency, E2E lifecycle,
credential safety, cost evidence replay, mode isolation, legacy bridge,
and migration verification tests."
```

---

## Dependency Map

```
Task 1 (Prisma Schema)
  └─ Task 2 (Prisma Migration)
      └─ Task 4 (Publication + Pull Endpoint)
          └─ Task 5 (Dispatcher)

Task 3 (Signing + Resolution)
  └─ Task 4

Task 6 (Domain Entitlement Models)
  └─ Task 7 (Domain Cost/Usage/Cred Models)
      └─ Task 8 (Alembic Migration)
          └─ Task 10 (Projection Module)
          └─ Task 11 (Credential Admin)
          └─ Task 12 (Quote + Reservation)
              └─ Task 13 (Admission)
                  └─ Task 17 (Rewire create_run)
          └─ Task 14 (Coordinator)
              └─ Task 18 (Worker + API Routes)
          └─ Task 15 (Reconciler + Usage + Guardrail + Notification)

Task 9 (Types) ← used by Tasks 10–18

Task 16 (Provider Adapter) ← implements ProviderAdapterProtocol defined by Task 14

Task 19 (Projection Sync v2) ← depends on Tasks 4, 10

Task 20 (Legacy Bridge) ← depends on Task 8
Task 21 (Isolation Tests) ← depends on Task 8
Task 22 (Concurrency Tests) ← depends on Tasks 12–13
Task 23 (E2E Tests) ← depends on Tasks 10–18
Task 24 (Credential Safety) ← depends on Task 11
Task 25 (Cost Evidence) ← depends on Tasks 7, 20
Task 26 (Final Verification) ← depends on all
```

## Observability Coverage (Spec §7)

Section 7 defines structured events, metric instruments, alerts, and tracing conventions. These are implemented **within each module's task**, not as a separate task:

- **Authoritative audit:** Each domain command module (Tasks 11, 13, 14, 15) writes `AuditEventRecord` in the same transaction as the state change. Control-plane Publication (Task 4) writes `ControlPlaneAuditEvent`.
- **Structured operational events:** Each module emits typed events per the §7.3 catalog after commit (not before). Event emission failure does not roll back business state.
- **Metric instruments:** Task 18 (API routes) registers the metric instruments from §7.4 using bounded label registries. No per-Workspace labels.
- **Canary scans:** Task 24 covers credential canary leak detection.
- **Trace propagation:** W3C `traceparent` propagation through BFF → FastAPI → Worker is validated in Task 23 E2E tests.
- **Alert predicates:** Alert SLOs and paging ownership are production release gates (Spec §8.2 D6), not merge gates. CI-level alert-query tests run against controlled clocks and seeded failure states in Task 26.

---

## Contract §14 Traceability

| §14 | Criterion | Tasks |
|-----|-----------|-------|
| 1 | Cross-tenant isolation | 21 |
| 2 | Billing Webhook replay | Deferred |
| 3 | Checkout cannot elevate | Deferred |
| 4 | Outbox, version jumps, lease expiry | 5, 10, 23 |
| 5 | Commercial lifecycle (Beta subset) | 10, 13, 23 |
| 6 | Atomic Guardrail contention | 12, 22 |
| 7 | Worker crash and uncertain-call safety | 14, 23 |
| 8 | Cost evidence replay | 25 |
| 9 | Mock isolation | 23, 25 |
| 10 | Owner/Member API permissions | 11, 18 (Playwright deferred) |
| 11 | Workspace deletion and retention | Deferred |
