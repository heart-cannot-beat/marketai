# Secret-Safe Cross-Area Golden-Path Browser E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a single serial Playwright test proving the complete v9 Beta golden path on PostgreSQL — from two-phase provisioning through all six areas to canonical snapshot equality after restart — with zero business-table seeding, three-layer evidence, deterministic Worker SIGKILL recovery, and secret-safe CI artifacts.

**Architecture:** Four workstreams converging on a single `golden-path.spec.ts`. Workstream A refactors the ExternalCallCoordinator to support controlled-mock dispatch without credentials and adds atomic Workspace activation. Workstream B extracts the two-phase provisioning deep module. Workstream C builds test infrastructure (controlled-provider sidecar, lifecycle broker, mail sink, Compose overlay, topology validation). Workstream D assembles the E2E harness — fixtures, evidence types, area drivers, schemas, CI workflows, and the spec itself.

**Tech Stack:** Playwright (browser E2E), TypeScript 5.9, Next.js 15, Prisma 6.10, FastAPI, SQLAlchemy 2, PostgreSQL 16, Docker Compose v2, Python 3.12, Node.js 22, Ed25519 (topology attestation), HMAC-SHA256 (opaque aliases).

**Spec:** `docs/superpowers/specs/2026-09-17-cross-area-golden-path-e2e-design.md` (locked at commit c7baa4f)

## Global Constraints

- `storageState: undefined` — real invitation/signup/MFA from raw browser
- Zero business-table seeding — every domain entity from UI actions
- `META_WRITE_ENABLED=false`, `META_WRITE_ACCESS_TOKEN=` — no Meta writes
- `META_ADAPTER=fake_meta`, `GENERATION_ADAPTER=controlled_mock` — no real providers
- Three-layer evidence: NetworkSpy (fail-closed), persisted call evidence (non-vacuous), effective runtime config (boolean-only probes)
- Secret-safe CI: no traces, no video, no raw credentials in logs, no shell tracing
- Raw invitation capability traverses E2E harness only in memory — never logs/reports/screenshots/traces/videos/command args/artifacts
- TOTP secret remains in onboarding module's private closure
- Per-run Ed25519 keypair: private key never in attestation/containers/env dumps/logs/artifacts; deleted during teardown
- Mail-sink control token via Docker secrets: never in env dumps/logs/reports/screenshots/artifacts
- HMAC run key generated at test start, never persisted
- Verifier failure never uploads artifacts (may contain secrets)
- CI never enables shell tracing
- `retries: 0`, `workers: 1`, `globalTimeout: 40min`
- All worktrees start from c7baa4f

---

## Workstream A: ExternalCallCoordinator Recovery Chain (Prerequisite)

### Task 1: Refactor Generation handler onto ExternalCallCoordinator three-phase seam

The current `_generation_run_item` in `app/worker_runtime.py:491` calls `handle_run_item(session, context, run_item_id)` inside a single `tenant_session`. Killing the Worker after remote dispatch rolls back the attempt record needed for reconciliation. This task refactors the Generation per-item handler to use the existing three-phase `ExternalCallCoordinator` from `app/providers/dispatch.py` (which already accepts `credential: memoryview | None` and has a `reconcile()` method), so the attempt record is committed before dispatch and survives SIGKILL.

The `app/providers/dispatch.py` coordinator already handles three phases: `_prepare_in_transaction` → `_dispatch_and_complete` → committed outcome. It also already handles `credential=None`. The work here is to adapt `app/generation/worker.py:handle_run_item` to use this coordinator instead of running everything inside one session.

**Files:**
- Modify: `app/generation/worker.py` (refactor `handle_run_item` to use `ExternalCallCoordinator`)
- Modify: `app/worker_runtime.py:491-525` (update `_generation_run_item` dispatch)
- Create: `tests/generation/test_worker_coordinator_integration.py`

**Interfaces:**
- Consumes: `ExternalCallCoordinator` from `app/providers/dispatch.py` (existing), `ExternalCallAdapter` protocol, `TenantContext`
- Produces: Generation per-item handler that commits the attempt and idempotency key BEFORE external dispatch, enabling reconciliation after SIGKILL. The controlled-mock adapter path skips credential resolution (already supported by the coordinator's `credential=None` parameter).

- [ ] **Step 1: Read the current `handle_run_item` implementation**

```bash
grep -n "def handle_run_item" app/generation/worker.py
# Read the full function to understand what it does inside one tenant_session
```

Understand what preparation (attempt record, idempotency key, reservation), dispatch (provider HTTP call), and completion (asset update, settlement) steps currently happen inside the single session.

- [ ] **Step 2: Write the failing test for three-phase dispatch**

```python
# tests/generation/test_worker_coordinator_integration.py
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from app.platform.tenancy import TenantContext


class TestGenerationCoordinatorIntegration:
    """The Generation per-item handler must use ExternalCallCoordinator's
    three-phase seam so the attempt record survives SIGKILL."""

    @pytest.mark.asyncio
    async def test_attempt_record_committed_before_dispatch(self):
        """Phase 1 commits the attempt record. Phase 2 dispatches externally.
        If the process dies between Phase 2 and Phase 3, the attempt record
        is recoverable via reconcile()."""
        # Set up: mock coordinator that records phase transitions
        # Verify: prepare committed before dispatch started
        pass  # Fill with actual mocks against generation/worker.py
```

- [ ] **Step 3: Run test to verify it fails**

```bash
python -m pytest tests/generation/test_worker_coordinator_integration.py -v
```
Expected: FAIL — current `handle_run_item` doesn't use the coordinator.

- [ ] **Step 4: Create a Generation-specific `ExternalCallAdapter` implementation**

In `app/generation/worker.py`, implement the `ExternalCallAdapter` protocol from `app/providers/dispatch.py` for the controlled-provider sidecar:

```python
class ControlledGenerationAdapter:
    """ExternalCallAdapter for controlled_mock generation via the sidecar."""
    
    def __init__(self, sidecar_url: str):
        self._sidecar_url = sidecar_url
    
    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_idempotency=True,
            supports_result_lookup_by_request_id=False,
            supports_reconciliation=True,
        )
    
    async def execute(self, request, *, idempotency_key: str, credential=None):
        # POST /provider/generate on the sidecar
        ...
    
    async def reconcile(self, *, idempotency_key: str, credential=None):
        # POST /provider/reconcile on the sidecar
        ...
```

- [ ] **Step 5: Refactor `handle_run_item` to use the coordinator**

Split the single-session `handle_run_item` into coordinator phases:
- Phase 1 (`_prepare_in_transaction`): commit attempt + idempotency key + reservation inside a session
- Phase 2 (`_dispatch_and_complete`): call provider outside session, then complete inside a new session
- The coordinator from `app/providers/dispatch.py` already handles this flow

Update `_generation_run_item` in `worker_runtime.py` to use the `ProviderCallCoordinator` (which is `app/providers/dispatch.ExternalCallCoordinator`):

```python
def _generation_run_item(self, job: jobs.ClaimedJob) -> None:
    from .generation.worker import build_generation_request, GenerationEvidenceRecorder
    
    run_item_id = str(job.payload.get("run_item_id") or "")
    # ... validation ...
    
    coordinator = self._provider_coordinator  # ProviderCallCoordinator
    result = await coordinator.execute(
        job.context,
        job_id=job.id,
        operation_id=job.payload["operation_id"],
        call_semantic_key_hash=...,
        step_kind="generation_run_item",
        execution_mode=job.payload.get("execution_mode", "controlled_mock"),
        chargeability=...,
        provider=...,
        request=build_generation_request(job),
        adapter=self._generation_adapter,
        evidence_recorder=GenerationEvidenceRecorder(),
        credential=None,  # controlled_mock: no credentials
    )
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/generation/test_worker_coordinator_integration.py -v
python -m pytest tests/ -k "generation" -v
```
Expected: All PASS

- [ ] **Step 7: Verify existing generation tests still pass**

```bash
python -m pytest tests/ -v --timeout=60
```

- [ ] **Step 8: Commit**

```bash
git add app/generation/worker.py app/worker_runtime.py \
  tests/generation/test_worker_coordinator_integration.py
git commit -m "$(cat <<'EOF'
feat(generation): refactor per-item handler onto ExternalCallCoordinator

Generation run items now use the three-phase dispatch seam: committed
prepare (attempt + idempotency key) → external dispatch → committed
completion. The attempt record survives SIGKILL and enables reconciliation
without re-dispatching.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 2: Idempotency key persistence before dispatch

The spec requires that the reconciliation idempotency key is persisted *before* dispatch so that after SIGKILL the Worker can reconcile without re-dispatching. The current `_prepare_in_transaction` already persists `call_semantic_key_hash` on the `ProviderCallAttemptRecord`. This task adds an explicit `idempotency_key_hash` field so the recovery path can look it up.

**Files:**
- Create: `migrations/versions/xxxx_add_idempotency_key_hash.py` (Alembic)
- Modify: `app/entitlements/cost_models.py` (add `idempotency_key_hash` column to `ProviderCallAttemptRecord`)
- Modify: `app/entitlements/coordinator.py` (persist hash in Phase 1)
- Create: `tests/entitlements/test_coordinator_recovery.py`

**Interfaces:**
- Consumes: `ExternalCallCoordinator` from Task 1
- Produces: `ProviderCallAttemptRecord.idempotency_key_hash` — a stable hash of the idempotency key persisted before dispatch. Recovery code can query by this hash to find the original attempt and reconcile.

- [ ] **Step 1: Write the failing test for idempotency key persistence**

```python
# tests/entitlements/test_coordinator_recovery.py
import hashlib
import pytest
from unittest.mock import MagicMock
from contextlib import contextmanager

from app.entitlements.coordinator import ExternalCallCoordinator
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    SpendReservationRecord,
)
from app.platform.tenancy import TenantContext


class TestIdempotencyKeyPersistence:
    def test_prepare_persists_idempotency_key_hash(self):
        """Phase 1 must persist the idempotency key hash on the attempt record."""
        reservation = MagicMock(spec=SpendReservationRecord)
        reservation.id = "sr_test"
        reservation.workspace_id = "ws_test"
        reservation.job_id = "job_test"
        reservation.provider = "controlled_mock"
        reservation.state = "reserved"
        reservation.operation_id = "op_test"
        reservation.execution_mode = "controlled_mock"

        added_records = []
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = reservation
        mock_session.query.return_value.filter_by.return_value.count.return_value = 0
        mock_session.add.side_effect = lambda r: added_records.append(r)
        mock_session.flush = MagicMock()

        @contextmanager
        def session_factory(ctx):
            yield mock_session

        coordinator = ExternalCallCoordinator(
            session_factory=session_factory,
            protector=MagicMock(),
            adapter=MagicMock(),
        )
        ctx = TenantContext(workspace_id="ws_test", actor_id="actor_test")
        prepared = coordinator._prepare_in_transaction(
            ctx, job_id="job_test", call_semantic_key_hash="hash_test"
        )

        attempt = added_records[0]
        expected_hash = hashlib.sha256(
            prepared["idempotency_key"].encode()
        ).hexdigest()
        assert attempt.idempotency_key_hash == expected_hash
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/entitlements/test_coordinator_recovery.py::TestIdempotencyKeyPersistence -v
```
Expected: FAIL — `ProviderCallAttemptRecord` has no `idempotency_key_hash` attribute.

- [ ] **Step 3: Add the column to the model**

In `app/entitlements/cost_models.py`, add to `ProviderCallAttemptRecord`:

```python
idempotency_key_hash = Column(String, nullable=True, index=True)
```

- [ ] **Step 4: Create the Alembic migration**

```bash
cd /Users/happy/orca/workspaces/marketai/implement-the-secret-safe-cross-area-golden-path
alembic revision --autogenerate -m "add idempotency_key_hash to provider_call_attempts"
```

Review the generated migration and verify it adds a single nullable column with an index.

- [ ] **Step 5: Persist the hash in prepare**

In `app/entitlements/coordinator.py`, in both the controlled-mock branch and the real-credential branch of `_prepare_in_transaction`, add after computing `idempotency_key`:

```python
import hashlib

idempotency_key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()

# Then in the ProviderCallAttemptRecord constructor:
attempt = ProviderCallAttemptRecord(
    # ... existing fields ...
    idempotency_key_hash=idempotency_key_hash,
)
```

Also add `"idempotency_key_hash": idempotency_key_hash` to the returned prepared dict.

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/entitlements/test_coordinator_recovery.py -v
python -m pytest tests/entitlements/test_coordinator_controlled_mock.py -v
```
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add app/entitlements/cost_models.py app/entitlements/coordinator.py \
  migrations/versions/*idempotency_key_hash* \
  tests/entitlements/test_coordinator_recovery.py
git commit -m "$(cat <<'EOF'
feat(coordinator): persist idempotency key hash before dispatch

The ProviderCallAttemptRecord now stores a SHA-256 hash of the
idempotency key, persisted in Phase 1 before external dispatch.
Recovery code can query by this hash to reconcile without
re-dispatching after SIGKILL.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 2.5: Recovery interface for unfinished attempts

The spec (Section 13.1) requires "an interface for recovering an unfinished attempt." The coordinator's existing `reconcile()` takes an `attempt_id`, but after SIGKILL the Worker needs to *find* the unfinished attempt by its idempotency key hash.

**Files:**
- Modify: `app/providers/dispatch.py` (add `find_uncertain_attempts`)
- Create: `tests/providers/test_dispatch_recovery.py`

**Interfaces:**
- Consumes: `ExternalCallCoordinator` from Task 1, `ProviderCallAttemptRecord` with `idempotency_key_hash` from Task 2
- Produces: `ExternalCallCoordinator.find_uncertain_attempts(ctx, call_semantic_key_hash)` → list of attempt IDs with `dispatch_state='uncertain'` and their idempotency key hashes. The Worker calls this on startup to find and reconcile unfinished work.

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/test_dispatch_recovery.py
import pytest
from contextlib import contextmanager
from unittest.mock import MagicMock

from app.providers.dispatch import ExternalCallCoordinator
from app.platform.tenancy import TenantContext


class TestFindUncertainAttempts:
    def test_finds_uncertain_attempts_by_semantic_key(self):
        """After SIGKILL, Worker must find attempts stuck in 'uncertain' state."""
        mock_session = MagicMock()
        uncertain_attempt = MagicMock()
        uncertain_attempt.id = "att_1"
        uncertain_attempt.dispatch_state = "uncertain"
        uncertain_attempt.provider_idempotency_key = "key_hash_1"
        uncertain_attempt.call_semantic_key_hash = "semantic_1"
        mock_session.query.return_value.filter_by.return_value.all.return_value = [
            uncertain_attempt
        ]

        @contextmanager
        def session_factory(ctx):
            yield mock_session

        coordinator = ExternalCallCoordinator(session_factory)
        ctx = TenantContext(workspace_id="ws_test", actor_id="actor_test")
        results = coordinator.find_uncertain_attempts(ctx, "semantic_1")

        assert len(results) == 1
        assert results[0]["attempt_id"] == "att_1"
        assert results[0]["dispatch_state"] == "uncertain"

    def test_returns_empty_when_no_uncertain_attempts(self):
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = []

        @contextmanager
        def session_factory(ctx):
            yield mock_session

        coordinator = ExternalCallCoordinator(session_factory)
        ctx = TenantContext(workspace_id="ws_test", actor_id="actor_test")
        results = coordinator.find_uncertain_attempts(ctx, "semantic_1")
        assert results == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/providers/test_dispatch_recovery.py -v
```
Expected: FAIL — `find_uncertain_attempts` doesn't exist.

- [ ] **Step 3: Implement `find_uncertain_attempts`**

In `app/providers/dispatch.py`, add to `ExternalCallCoordinator`:

```python
def find_uncertain_attempts(
    self,
    ctx: TenantContext,
    call_semantic_key_hash: str,
) -> list[dict[str, Any]]:
    """Find attempts stuck in 'uncertain' state for recovery."""
    with self._session_factory(ctx) as session:
        attempts = (
            session.query(ProviderCallAttemptRecord)
            .filter_by(
                workspace_id=ctx.workspace_id,
                call_semantic_key_hash=call_semantic_key_hash,
                dispatch_state="uncertain",
            )
            .all()
        )
        return [
            {
                "attempt_id": a.id,
                "dispatch_state": a.dispatch_state,
                "provider_idempotency_key": a.provider_idempotency_key,
                "call_semantic_key_hash": a.call_semantic_key_hash,
                "run_item_id": a.run_item_id,
            }
            for a in attempts
        ]
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/providers/test_dispatch_recovery.py -v
```
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/providers/dispatch.py tests/providers/test_dispatch_recovery.py
git commit -m "$(cat <<'EOF'
feat(coordinator): add find_uncertain_attempts for SIGKILL recovery

After Worker restart, find_uncertain_attempts locates attempts stuck
in 'uncertain' state by semantic key hash, enabling reconciliation
without re-dispatching.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 3: Atomic Workspace activation with snapshot publication

The spec requires `activateWorkspaceAndPublish(...)` — an atomic operation that transitions Team to ACTIVE and enqueues the active entitlement snapshot in one serializable transaction. A crash cannot leave an ACTIVE Team whose active snapshot was never published.

**Files:**
- Modify: `web/lib/onboarding.ts` (or wherever `provisionWorkspace` lives — check the import in `provision-workspace.ts`)
- Create: `web/lib/__tests__/activate-workspace.test.ts`

**Interfaces:**
- Consumes: Prisma client, Team in PROVISIONING state with published provisioning snapshot
- Produces: `activateWorkspaceAndPublish(prisma, { teamId, signingKeyId, signingKey })` — atomically sets Team.status = ACTIVE, creates the active entitlement snapshot, and publishes it to the outbox. Returns `{ team, activeSnapshot }`.

- [ ] **Step 1: Locate the existing provisioning code**

```bash
grep -rn "provisionFirstOwnerWorkspace\|provisionWorkspace\|PROVISIONING\|ACTIVE" web/lib/onboarding.ts web/lib/onboarding/ --include="*.ts" | head -40
```

Read the current implementation to understand the Team lifecycle and snapshot publication.

- [ ] **Step 2: Write the failing test**

Create `web/lib/__tests__/activate-workspace.test.ts` with a test that calls `activateWorkspaceAndPublish` and asserts: (1) Team status is ACTIVE, (2) an active snapshot exists, (3) outbox entry exists — all within one transaction.

The exact test depends on the Prisma schema and existing patterns found in Step 1. Use Prisma's interactive transactions (`$transaction`) to verify atomicity.

- [ ] **Step 3: Implement `activateWorkspaceAndPublish`**

Add the function to the onboarding module. It must use `prisma.$transaction` with serializable isolation:

```typescript
export async function activateWorkspaceAndPublish(
  prisma: PrismaClient,
  input: {
    teamId: string;
    signingKeyId: string;
    signingKey: Buffer;
  },
): Promise<{ team: Team; activeSnapshotVersion: number }> {
  return prisma.$transaction(async (tx) => {
    const team = await tx.team.update({
      where: { id: input.teamId, status: 'PROVISIONING' },
      data: { status: 'ACTIVE' },
    });
    const snapshot = await createAndSignSnapshot(tx, {
      teamId: team.id,
      signingKeyId: input.signingKeyId,
      signingKey: input.signingKey,
      phase: 'active',
    });
    await enqueueOutboxEntry(tx, {
      teamId: team.id,
      snapshotVersion: snapshot.version,
      eventType: 'workspace_activated',
    });
    return { team, activeSnapshotVersion: snapshot.version };
  }, { isolationLevel: Prisma.TransactionIsolationLevel.Serializable });
}
```

Adapt to match existing snapshot creation and outbox patterns discovered in Step 1.

- [ ] **Step 4: Run tests**

```bash
cd web && npx jest lib/__tests__/activate-workspace.test.ts --verbose
```

- [ ] **Step 5: Commit**

```bash
git add web/lib/onboarding.ts web/lib/__tests__/activate-workspace.test.ts
git commit -m "$(cat <<'EOF'
feat(control-plane): atomic Workspace activation with snapshot publication

activateWorkspaceAndPublish() transitions Team to ACTIVE and enqueues
the active entitlement snapshot in one serializable transaction. A crash
cannot leave an ACTIVE Team whose snapshot was never published.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Workstream B: Two-Phase Provisioning Module Extraction

### Task 4: Extract deep module `web/lib/control-plane-provisioning/`

Extract the provisioning logic from `web/scripts/provision-workspace.ts` into a reusable deep module with explicit two-phase interface.

**Files:**
- Create: `web/lib/control-plane-provisioning/types.ts`
- Create: `web/lib/control-plane-provisioning/begin.ts`
- Create: `web/lib/control-plane-provisioning/complete.ts`
- Create: `web/lib/control-plane-provisioning/index.ts`
- Modify: `web/scripts/provision-workspace.ts` (refactor to use deep module)
- Create: `web/lib/control-plane-provisioning/__tests__/provisioning.test.ts`

**Interfaces:**
- Consumes: Prisma client, `provisionFirstOwnerWorkspace` from `web/lib/onboarding`, `activateWorkspaceAndPublish` from Task 3
- Produces:
  - `beginProvisioning(prisma, input)` → `BeginProvisioningResult` with `provisioningRef`, `workspaceRef`, `workspaceSlug`, `ownerInvitation`
  - `completeProvisioning(prisma, { provisioningRef, signingKeyId, signingKey })` → `CompleteProvisioningResult` with snapshot versions

- [ ] **Step 1: Create types**

```typescript
// web/lib/control-plane-provisioning/types.ts

export interface BeginProvisioningInput {
  idempotencyKey: string;
  workspaceName: string;
  workspaceSlug?: string;
  ownerEmail: string;
  operatorIdentifier: string;
  betaPlanVersionId: string;
  namespace: string;
}

export interface SecretInvitationCapability {
  readonly token: string;
  readonly invitationPath: string;
}

export interface BeginProvisioningResult {
  provisioningRef: string;
  workspaceRef: string;
  workspaceSlug: string;
  ownerInvitation: SecretInvitationCapability;
}

export interface CompleteProvisioningResult {
  provisioningRef: string;
  workspaceRef: string;
  workspaceSlug: string;
  ownerMembershipRef: string;
  provisioningSnapshotVersion: number;
  activeSnapshotVersion: number;
}
```

- [ ] **Step 2: Implement `beginProvisioning`**

```typescript
// web/lib/control-plane-provisioning/begin.ts
import type { PrismaClient } from '@prisma/client';
import type { BeginProvisioningInput, BeginProvisioningResult } from './types';

export async function beginProvisioning(
  prisma: PrismaClient,
  input: BeginProvisioningInput,
): Promise<BeginProvisioningResult> {
  // 1. Idempotently ensure platform operator
  // 2. Idempotently ensure Beta PlanVersion (exact match on plan_key + version + entitlement_hash)
  // 3. Create Team in PROVISIONING state
  // 4. Create Invitation through existing invitation model
  // 5. Record in provisioning journal
  // Return result with SecretInvitationCapability
  // ... (adapt from existing provisionFirstOwnerWorkspace logic)
}
```

Implementation must be adapted from the logic currently inside `provisionFirstOwnerWorkspace` in `web/lib/onboarding`. Read that function first. The key change: this now returns *before* the Owner has enrolled, holding the Team in `PROVISIONING` state with an unconsumed invitation.

- [ ] **Step 3: Implement `completeProvisioning`**

```typescript
// web/lib/control-plane-provisioning/complete.ts
import type { PrismaClient } from '@prisma/client';
import type { CompleteProvisioningResult } from './types';

export async function completeProvisioning(
  prisma: PrismaClient,
  input: {
    provisioningRef: string;
    signingKeyId: string;
    signingKey: Buffer;
  },
): Promise<CompleteProvisioningResult> {
  // 1. Look up provisioning journal entry
  // 2. Verify consumed invitation → exactly one matching Owner membership
  // 3. Call existing provisionWorkspace(...)
  // 4. Wait for provisioning outbox delivery + domain ack
  // 5. Wait for Spend Guardrail
  // 6. activateWorkspaceAndPublish (Task 3) — atomic ACTIVE + snapshot
  // 7. Wait for active outbox delivery + domain projection
  // 8. Update provisioning journal
}
```

- [ ] **Step 4: Create public index**

```typescript
// web/lib/control-plane-provisioning/index.ts
export { beginProvisioning } from './begin';
export { completeProvisioning } from './complete';
export type {
  BeginProvisioningInput,
  BeginProvisioningResult,
  CompleteProvisioningResult,
  SecretInvitationCapability,
} from './types';
```

- [ ] **Step 5: Write integration test**

Create `web/lib/control-plane-provisioning/__tests__/provisioning.test.ts` testing the two-phase flow with a real Prisma client against the test database.

- [ ] **Step 6: Refactor `provision-workspace.ts` to use the deep module**

```typescript
// web/scripts/provision-workspace.ts — now explicitly two-phase
import { beginProvisioning, completeProvisioning } from '../lib/control-plane-provisioning';

// Subcommand: begin
// Subcommand: complete
```

The script accepts `begin` and `complete` subcommands. `begin` calls `beginProvisioning` and outputs the provisioning ref + invitation URL. `complete` takes `--provisioning-ref` and calls `completeProvisioning`.

- [ ] **Step 7: Run tests**

```bash
cd web && npx jest lib/control-plane-provisioning/__tests__/provisioning.test.ts --verbose
```

- [ ] **Step 8: Commit**

```bash
git add web/lib/control-plane-provisioning/ web/scripts/provision-workspace.ts
git commit -m "$(cat <<'EOF'
feat(control-plane): extract two-phase provisioning deep module

beginProvisioning() creates Team/Invitation in PROVISIONING state.
completeProvisioning() resolves consumed invitation, provisions,
activates atomically, and publishes snapshots. Both the production CLI
and E2E adapter call the same interface.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 5: Golden-path provisioning command adapter

Create the E2E command adapter that `provisioning.ts` fixture will invoke.

**Files:**
- Create: `web/scripts/golden-path-provisioning.ts`
- Create: `web/scripts/__tests__/golden-path-provisioning.test.ts`

**Interfaces:**
- Consumes: `beginProvisioning`, `completeProvisioning` from Task 4
- Produces: CLI command accepting `begin`/`complete` subcommands. Input via JSON on stdin. Results via JSON on stdout. Invitation capability via fd 3. Sanitized stderr with bounded error codes only.

- [ ] **Step 1: Implement the command adapter**

```typescript
// web/scripts/golden-path-provisioning.ts
import { loadRuntimeEnv, projectRoot } from './loadEnv';

async function main() {
  loadRuntimeEnv(projectRoot(__dirname));
  const { prisma } = await import('../lib/prisma');
  const {
    beginProvisioning,
    completeProvisioning,
  } = await import('../lib/control-plane-provisioning');

  const subcommand = process.argv[2];
  const input = await readJsonStdin();

  try {
    if (subcommand === 'begin') {
      const result = await beginProvisioning(prisma, input);
      // Write safe result to stdout
      process.stdout.write(JSON.stringify({
        provisioningRef: result.provisioningRef,
        workspaceRef: result.workspaceRef,
        workspaceSlug: result.workspaceSlug,
      }));
      // Write invitation capability to fd 3 (dedicated secret channel)
      const fd3 = createWriteStream('', { fd: 3 });
      fd3.write(JSON.stringify(result.ownerInvitation));
      fd3.end();
    } else if (subcommand === 'complete') {
      const result = await completeProvisioning(prisma, {
        provisioningRef: input.provisioningRef,
        signingKeyId: input.signingKeyId,
        signingKey: Buffer.from(input.signingKey, 'base64'),
      });
      process.stdout.write(JSON.stringify(result));
    } else {
      process.stderr.write('error_code:unknown_subcommand\n');
      process.exitCode = 2;
    }
  } catch (error) {
    const code = (error as any).code ?? 'internal_error';
    process.stderr.write(`error_code:${code}\n`);
    process.exitCode = 1;
  } finally {
    await prisma.$disconnect();
  }
}

async function readJsonStdin(): Promise<any> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

void main();
```

- [ ] **Step 2: Write test verifying fd 3 secret transport**

Test that invitation tokens are written to fd 3, not stdout or stderr.

- [ ] **Step 3: Run tests**

```bash
cd web && npx jest scripts/__tests__/golden-path-provisioning.test.ts --verbose
```

- [ ] **Step 4: Commit**

```bash
git add web/scripts/golden-path-provisioning.ts web/scripts/__tests__/golden-path-provisioning.test.ts
git commit -m "$(cat <<'EOF'
feat(scripts): add golden-path provisioning command adapter

Thin CLI adapter for E2E use. JSON stdin, safe JSON stdout, invitation
capability on fd 3, bounded error codes on stderr. Calls the same
two-phase provisioning module as the production CLI.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Workstream C: Infrastructure — Sidecar, Broker, Mail Sink, Topology

### Task 6: Controlled-provider HTTP sidecar

**Files:**
- Create: `test-support/controlled-provider/Dockerfile`
- Create: `test-support/controlled-provider/server.ts`
- Create: `test-support/controlled-provider/ledger.ts`
- Create: `test-support/controlled-provider/schemas.ts`
- Create: `test-support/controlled-provider/__tests__/sidecar.test.ts`

**Interfaces:**
- Consumes: Nothing — standalone HTTP server
- Produces:
  - Provider interface: `POST /provider/generate`, `POST /provider/reconcile`
  - Test-control interface: `POST /test/runs/:namespace/barriers`, `POST /test/runs/:namespace/barriers/:name/arrive`, `GET /test/runs/:namespace/checkpoints/:name`, `DELETE /test/runs/:namespace/barriers/:name`, `GET /test/runs/:namespace/effects`, `GET /test/runs/:namespace/request_attempts`
  - Health: `GET /test/health`

- [ ] **Step 1: Create the SQLite ledger module**

```typescript
// test-support/controlled-provider/ledger.ts
import Database from 'better-sqlite3';

export function createLedger(dbPath: string) {
  const db = new Database(dbPath);
  db.pragma('journal_mode = WAL');
  db.exec(`
    CREATE TABLE IF NOT EXISTS effects (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      namespace TEXT NOT NULL,
      idempotency_key TEXT NOT NULL,
      request_fingerprint TEXT NOT NULL,
      run_item TEXT,
      effect_kind TEXT,
      effect_data TEXT,
      success INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE (namespace, idempotency_key),
      UNIQUE (namespace, run_item, effect_kind, request_fingerprint)
        WHERE success = 1
    );

    CREATE TABLE IF NOT EXISTS request_attempts (
      sequence INTEGER PRIMARY KEY AUTOINCREMENT,
      namespace TEXT NOT NULL,
      idempotency_key_hash TEXT NOT NULL,
      request_kind TEXT NOT NULL CHECK (request_kind IN ('generate', 'reconcile')),
      request_fingerprint TEXT NOT NULL,
      received_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_request_attempts_key_kind
      ON request_attempts (namespace, idempotency_key_hash, request_kind);
  `);
  return db;
}
```

- [ ] **Step 2: Create the HTTP server**

Implement `test-support/controlled-provider/server.ts` with all endpoints from spec Section 4. Use Node.js built-in `http` module or a minimal framework. Key behaviors:
- `POST /provider/generate`: Record request attempt (request_kind='generate') BEFORE idempotency check. Check `(namespace, idempotency_key)` uniqueness. Same key + same fingerprint → return original. Same key + different fingerprint → 409.
- `POST /provider/reconcile`: Record request attempt (request_kind='reconcile') BEFORE lookup. Return committed effect or 404.
- Barrier: atomic first-match binding by `matchOrdinal`. Arrive blocks. Checkpoint polling.
- Effects: bounded, namespaced, returns only hashes and opaque refs.
- Request attempts: requires both `idempotency_key_hash` and `request_kind` filters.

- [ ] **Step 3: Create the Dockerfile**

```dockerfile
# test-support/controlled-provider/Dockerfile
FROM node:22-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --production
COPY . .
HEALTHCHECK CMD wget -q -O /dev/null http://localhost:${PORT:-9090}/test/health || exit 1
CMD ["node", "server.js"]
```

- [ ] **Step 4: Write tests for idempotency, barriers, and request-attempt separation**

```typescript
// test-support/controlled-provider/__tests__/sidecar.test.ts
// Test: same key + same fingerprint → same effect
// Test: same key + different fingerprint → 409
// Test: request_attempts records both generate and reconcile separately
// Test: barrier blocks arrive and checkpoint reports terminal state
// Test: request_kind is required filter (missing → 400)
// Test: effects returns only hashes, not raw keys
```

- [ ] **Step 5: Run tests**

```bash
cd test-support/controlled-provider && npm test
```

- [ ] **Step 6: Commit**

```bash
git add test-support/controlled-provider/
git commit -m "$(cat <<'EOF'
feat(test-support): add controlled-provider HTTP sidecar

Lightweight mock provider with transactional SQLite ledger. Supports
idempotent generation, reconciliation, barrier-based SIGKILL sequencing,
and separate request-attempt logging for recovery assertions.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 7: Lifecycle broker

**Files:**
- Create: `test-support/lifecycle-broker/broker.ts`
- Create: `test-support/lifecycle-broker/protocol.ts`
- Create: `test-support/lifecycle-broker/__tests__/broker.test.ts`

**Interfaces:**
- Consumes: Docker CLI on the host
- Produces: UDS-based JSON protocol with closed action/service unions:
  - `stop(web|api|worker)`, `start(web|api|worker)`, `kill(worker, SIGKILL)`, `restart(web|api|worker)`, `status(web|api|worker)`
  - `inspect_network(web|api|worker)` → `NetworkAttestation`
  - `probe_egress(web|api|worker|controlled-provider)` → DNS/IPv4/IPv6 booleans

- [ ] **Step 1: Define the protocol types**

```typescript
// test-support/lifecycle-broker/protocol.ts
export type RuntimeService = 'web' | 'api' | 'worker';
export type EgressProbeService = RuntimeService | 'controlled-provider';

export interface BrokerRequest {
  action: 'stop' | 'start' | 'kill' | 'restart' | 'status' | 'inspect_network' | 'probe_egress';
  service: string;
  signal?: 'SIGKILL';
  projectName: string;
}

export interface NetworkAttestation {
  service: string;
  networkName: string;
  networkHash: string;
  attachmentCount: number;
  singleNetwork: boolean;
  internalOnly: boolean;
  noPublishedPorts: boolean;
}

export interface EgressAttestation {
  service: string;
  dnsResolved: boolean;
  ipv4Connected: boolean;
  ipv6Connected: boolean;
}

export interface BrokerResponse {
  ok: boolean;
  service: string;
  state?: string;
  network_attestation?: NetworkAttestation;
  egress_attestation?: EgressAttestation;
  error_code?: string;
}
```

- [ ] **Step 2: Implement the broker**

```typescript
// test-support/lifecycle-broker/broker.ts
// - Listens on UDS path
// - Validates projectName against gp-<run_id>-<run_attempt> pattern
// - Validates service against closed union
// - Uses `docker compose ps -q <service>` for container resolution
// - inspect_network: `docker inspect` → closed projection only
// - probe_egress: fixed image-local probe command, returns only booleans
```

- [ ] **Step 3: Write tests**

Test service validation (rejects unknown services), project name validation, and the closed response projections.

- [ ] **Step 4: Commit**

```bash
git add test-support/lifecycle-broker/
git commit -m "$(cat <<'EOF'
feat(test-support): add lifecycle broker for container management

Host-side broker with UDS transport. Closed action/service unions,
project name validation. Provides inspect_network and probe_egress
without exposing Docker socket to Playwright.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 8: Mail sink

**Files:**
- Create: `test-support/mail-sink/server.ts`
- Create: `test-support/mail-sink/Dockerfile`
- Create: `test-support/mail-sink/__tests__/mail-sink.test.ts`

**Interfaces:**
- Consumes: Nothing — standalone SMTP + HTTP server
- Produces:
  - SMTP on port 2525 (no TLS, no auth — internal-only network)
  - `GET /health` → 200
  - `POST /api/messages/query` with `Authorization: Bearer <token>` — long-polls for matching message, returns `{ to, invitationPath }`

- [ ] **Step 1: Implement the mail sink**

Use `smtp-server` npm package for SMTP and Node.js `http` for the control endpoint. Key behaviors:
- Accept all SMTP mail, parse with `mailparser`, store in-memory Map keyed by recipient
- `POST /api/messages/query`: constant-time token comparison, long-poll with timeout, return only parsed invitation path from the email body
- Health endpoint

- [ ] **Step 2: Create Dockerfile**

```dockerfile
FROM node:22-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --production
COPY . .
HEALTHCHECK CMD wget -q -O /dev/null http://localhost:${HTTP_PORT:-8025}/health || exit 1
CMD ["node", "server.js"]
```

- [ ] **Step 3: Write tests**

Test: token validation returns 401 on bad token. Test: long-poll returns invitation path after SMTP delivery. Test: recipient data stays out of request URLs.

- [ ] **Step 4: Commit**

```bash
git add test-support/mail-sink/
git commit -m "$(cat <<'EOF'
feat(test-support): add mail sink for invitation capture

In-memory SMTP server + authenticated HTTP query endpoint. Per-run
Docker-secret token for access control. Returns only parsed invitation
path, never raw email content.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 9: Compose overlay and topology validation

**Files:**
- Create: `docker-compose.golden-path.yml`
- Create: `scripts/ci/validate_topology.py`
- Create: `scripts/ci/__tests__/test_validate_topology.py`

**Interfaces:**
- Consumes: `docker-compose.yml` (existing), sidecar/broker/mail-sink images from Tasks 6-8
- Produces:
  - `docker-compose.golden-path.yml` overlay with controlled-provider, mail-sink, playwright, internal network
  - `validate_topology.py --phase config` (pre-up) and `--phase live` (post-up) with `--write-signed-attestation`

- [ ] **Step 1: Create the Compose overlay**

```yaml
# docker-compose.golden-path.yml
# Extends docker-compose.yml
# Adds: controlled-provider, mail-sink, playwright services
# Single named network with internal: true
# No published ports
# Overrides API/Worker env: GENERATION_ADAPTER, CONTROLLED_PROVIDER_URL, META_ADAPTER, META_WRITE_ENABLED, META_WRITE_ACCESS_TOKEN
# Lifecycle broker UDS bind-mount into playwright only
# Topology attestation read-only bind-mount into playwright
# Mail-sink control token as Docker secret for mail-sink + playwright only
```

See spec Section 3 for exact configuration. Services: postgres, init-domain, init-control, web, api, worker, controlled-provider, mail-sink, playwright.

- [ ] **Step 2: Implement topology validator**

```python
# scripts/ci/validate_topology.py
# --phase config: runs `docker compose config`, rejects:
#   network_mode: host, published host ports, multiple networks per service,
#   networks without internal: true, Docker-socket mounts, host-gateway aliases,
#   privileged containers, unapproved host-path mounts
# --phase live: runs `docker inspect`, confirms same invariants on running containers
#   --compose-started-at: validates freshness
#   --write-signed-attestation: writes Ed25519-signed JSON attestation to shared volume
```

- [ ] **Step 3: Write tests for topology validation**

Test each rejection condition. Test attestation signature verification.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.golden-path.yml scripts/ci/validate_topology.py scripts/ci/__tests__/
git commit -m "$(cat <<'EOF'
feat(infra): add golden-path Compose overlay and topology validator

Overlay extends docker-compose.yml with controlled-provider, mail-sink,
playwright. Internal-only network. Two-phase host-side topology
validation with Ed25519-signed attestation delivery.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 10: Log redactor and artifact verifier

**Files:**
- Create: `scripts/ci/redact_compose_logs.py`
- Create: `scripts/ci/verify_golden_artifacts.py`
- Create: `scripts/ci/__tests__/test_redact_logs.py`
- Create: `scripts/ci/__tests__/test_verify_artifacts.py`

**Interfaces:**
- Consumes: Docker Compose logs, golden-path artifact directory
- Produces:
  - `redact_compose_logs.py`: pattern-based redacted logs, atomic rename, second-pass validation
  - `verify_golden_artifacts.py`: allowlist check, schema validation, secret scan, size bounds, completeness check

- [ ] **Step 1: Implement log redactor**

Per spec Section 10.3. Key: collect only web/api/worker logs from run start. Pattern-based redaction for tokens, secrets, passwords, API keys, invitation URLs, TOTP secrets, recovery codes. On failure: delete candidate, emit constant marker. Atomic rename. Second-pass validation scan.

- [ ] **Step 2: Implement artifact verifier**

Per spec Section 10.4. Allowlist check, JSON schema validation against `contracts/e2e/` schemas, secret scan, size bounds, completeness, structural checks (reject symlinks, hidden files, path traversal).

- [ ] **Step 3: Write tests**

Test redactor catches: plain tokens, URL-encoded, JSON-escaped, multiline secrets. Test verifier rejects: unlisted files, oversized files, files containing secret patterns, missing required artifacts.

- [ ] **Step 4: Commit**

```bash
git add scripts/ci/redact_compose_logs.py scripts/ci/verify_golden_artifacts.py scripts/ci/__tests__/
git commit -m "$(cat <<'EOF'
feat(ci): add log redactor and artifact verifier for golden path

Redactor: pattern-based, atomic rename, second-pass validation, fails
closed. Verifier: allowlist, schema validation, secret scan, size
bounds, completeness check. Verifier failure never uploads artifacts.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Workstream D: E2E Test Harness

### Task 11: Evidence type system and JSON schemas

**Files:**
- Create: `web/e2e/golden-path/artifacts/schemas.ts`
- Create: `contracts/e2e/golden-path-report.schema.json`
- Create: `contracts/e2e/golden-path-evidence.schema.json`
- Create: `contracts/e2e/acceptance-manifest.schema.json`
- Create: `contracts/e2e/artifact-allowlist.schema.json`

**Interfaces:**
- Consumes: Nothing
- Produces: TypeScript types (`Area`, `EntityKind`, `EntityRef<K>`, `EvidenceEnvelope`, all area-specific evidence types, `BrandedOpaqueId<K>`) and canonical JSON Schemas consumed by both TypeScript and Python

- [ ] **Step 1: Create TypeScript evidence types**

Implement `web/e2e/golden-path/artifacts/schemas.ts` with all types from spec Section 7. Use branded types for `BrandedOpaqueId<K>`:

```typescript
declare const brand: unique symbol;
type BrandedOpaqueId<K extends EntityKind> = string & { readonly [brand]: K };
```

Include: `Area`, `EntityKind`, `EntityRef<K>`, `BrandedOpaqueId<K>`, `EvidenceEnvelope<A, C>`, all area-specific evidence types (`LibraryEvidence`, `GenerationSubmittedEvidence`, `GenerationCompletedEvidence`, `AssetsEvidence`, `ExperimentFrozenEvidence`, `ExperimentLaunchedEvidence`, `ExperimentInconclusiveEvidence`, `ExperimentWinnerEvidence`, `LocalizationBatchEvidence`, `LocalizationOutcomesEvidence`, `ScaleEnrollmentEvidence`, `ScaleEvidence`), and fixture-support types:

```typescript
// Fixture-support types (also in schemas.ts)

export type StepUpAction = 'experiment_launch' | 'workspace_admin';
export interface OpaqueStepUpGrant {
  readonly action: StepUpAction;
  readonly grantedAt: number;
}

export interface MemberSessionCapability {
  page(): Page;
  close(): Promise<void>;
}

export interface BarrierRef {
  readonly name: string;
  readonly namespace: string;
}

export interface TerminalCheckpointState {
  phase: 'after_remote_commit';
  remote_effect_committed: true;
  response_received_by_adapter: true;
  worker_barrier_waiting: true;
}

export interface SanitizedEffectSummary {
  idempotencyKeyHash: string;
  effectRef: string;
  effectKind: string;
  namespace: string;
}

export interface RequestAttemptSummary {
  sequence: number;
  idempotencyKeyHash: string;
  requestKind: 'generate' | 'reconcile';
  requestFingerprint: string;
  receivedAt: string;
}

export interface MetaObservationAdvancedResult {
  scenarioApplied: MetaScenarioId;
  observationCount: number;
  provenance: 'customer_sandbox';
}

export type MetaScenarioId = 'insufficient_metrics' | 'decisive_winner' | 'scale_warming' | 'scale_declining';
```

- [ ] **Step 2: Create JSON schemas**

Create all four schemas from spec Sections 9.2 and 11.

- [ ] **Step 3: Commit**

```bash
git add web/e2e/golden-path/artifacts/schemas.ts contracts/e2e/
git commit -m "$(cat <<'EOF'
feat(e2e): add evidence type system and canonical JSON schemas

Branded EntityRef<K>, area-specific evidence types, and four canonical
JSON schemas: report, evidence, acceptance manifest, artifact allowlist.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 12: SafeCapture and evidence recorder

**Files:**
- Create: `web/e2e/golden-path/artifacts/safe-capture.ts`
- Create: `web/e2e/golden-path/artifacts/evidence-recorder.ts`

**Interfaces:**
- Consumes: Evidence types from Task 11, Playwright `Page`
- Produces:
  - `SafeCapture`: positive-allowlist screenshot with masking, viewport-only, EXIF stripped
  - `EvidenceRecorder`: cumulative evidence collection, HMAC opaque aliases, report finalization

- [ ] **Step 1: Implement SafeCapture**

Per spec Section 9.1. Positive allowlist of checkpoint codes and route classifications. Both conditions required. Playwright screenshot masking + injected capture-only stylesheet. Viewport-only. Bounded dimensions/bytes. No capture from secret-bearing contexts. No entity IDs in filenames. EXIF/GPS stripped. Output to allowlisted directory only.

- [ ] **Step 2: Implement EvidenceRecorder**

Per spec Section 6.10 and 9.3-9.4. Initialize `interrupted` report before Compose starts. Record area evidence, timings, assertion outcomes. HMAC opaque alias mapping with per-run key. SafeCapture on failure. Finalize reports in teardown.

```typescript
export class EvidenceRecorder {
  private readonly runKey: Buffer; // generated at start, never persisted
  private readonly runId: string;

  constructor(runId: string) {
    this.runId = runId;
    this.runKey = crypto.randomBytes(32);
  }

  opaqueAlias(kind: EntityKind, rawId: string): string {
    return crypto.createHmac('sha256', this.runKey)
      .update(kind + rawId)
      .digest('hex');
  }

  // ... recording, report finalization
}
```

- [ ] **Step 3: Commit**

```bash
git add web/e2e/golden-path/artifacts/
git commit -m "$(cat <<'EOF'
feat(e2e): add SafeCapture and EvidenceRecorder

SafeCapture: positive-allowlist, failure-only, viewport-only, masking,
EXIF stripped. EvidenceRecorder: HMAC opaque aliases, interrupted report
initialization, cumulative evidence, report finalization.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 13: Playwright config and fixture composition

**Files:**
- Create: `web/e2e/golden-path/playwright.config.golden.ts`
- Create: `web/e2e/golden-path/fixtures/index.ts`
- Create: `web/e2e/golden-path/fixtures/runtime-safety.ts`
- Create: `web/e2e/golden-path/fixtures/service-control.ts`
- Create: `web/e2e/golden-path/fixtures/evidence-inspector.ts`

**Interfaces:**
- Consumes: Lifecycle broker protocol (Task 7), topology attestation (Task 9), `inspect_db.py` (to be created)
- Produces: Composed `test` extending `base.test` and `expect`. Runtime safety fixture with NetworkSpy, topology validation, egress probes, config probes. Service control fixture. Evidence inspector fixture.

- [ ] **Step 1: Create Playwright config**

```typescript
// web/e2e/golden-path/playwright.config.golden.ts
import { defineConfig } from '@playwright/test';

export default defineConfig({
  globalTimeout: 40 * 60_000,
  timeout: 40 * 60_000,
  workers: 1,
  retries: 0,
  use: {
    trace: 'off',
    video: 'off',
    screenshot: 'off',
    // The default page fixture is NOT used.
    // Fixtures depend on `browser`, create contexts manually,
    // and install NetworkSpy before creating any page.
    storageState: undefined,
    serviceWorkers: 'block',
  },
  testDir: '.',
  testMatch: 'golden-path.spec.ts',
});
```

**Important**: The default Playwright `page` fixture must NOT be used. The composed `test` in `fixtures/index.ts` does not extend `page`. All contexts are created manually from `browser` with `storageState: undefined` and NetworkSpy installed before the first page.

- [ ] **Step 2: Implement runtime-safety fixture**

Per spec Section 6.9. `validateTopologyAttestation()`, `validateRestartedServiceNetwork()`, `probeNoEgress()`, `probeEffectiveConfig()`, `installNetworkSpy()`, `assertNetworkSpyClean()`.

`probeNoEgress(project)` runs multi-service, multi-protocol probes:
- Playwright probes its OWN container directly (DNS, IPv4, IPv6 — no broker needed)
- For web, api, worker, controlled-provider: uses broker's `probe_egress` operation
- Any successful external connection fails the test

```typescript
async probeNoEgress(project: string): Promise<void> {
  // Playwright self-probe (direct, no broker)
  const selfProbe = await this.probeEgressDirect();
  if (selfProbe.dnsResolved || selfProbe.ipv4Connected || selfProbe.ipv6Connected) {
    throw new Error('Playwright container has external egress');
  }
  // Other services via broker
  for (const service of ['web', 'api', 'worker', 'controlled-provider'] as const) {
    const attestation = await this.broker.probeEgress(project, service);
    if (attestation.dnsResolved || attestation.ipv4Connected || attestation.ipv6Connected) {
      throw new Error(`Service ${service} has external egress`);
    }
  }
}
```

NetworkSpy: installed on `BrowserContext` before first page. Allowlists only web and BlobStore origins. Aborts and records non-allowlisted requests. Uses `serviceWorkers: 'block'`. Sanitizes URLs (strip query params, credentials).

- [ ] **Step 3: Implement service-control fixture**

Per spec Section 6.6. Closed `RuntimeService` union. Methods: `stop`, `start`, `kill`, `restart`, `status`, `inspectNetwork`. Communicates with lifecycle broker over UDS.

- [ ] **Step 4: Implement evidence-inspector fixture**

Per spec Section 6.8. Wraps `inspect_db.py` calls. Methods: `assertZeroBusinessEntities`, `queryCallEvidence`, `queryProviderCostLedger`, `captureCanonicalSnapshot`, `compareSnapshots`, `assertNoWinnerDecision`, `assertZeroMetaWrites`, `assertWorkspaceMetaProvenance`, `assertNonVacuousMetaEvidence`, `assertNoRunnableOrInFlightJobs`, `assertOperationsSettled`, `readJobLease`, `assertCallEvidenceModeConsistency`.

- [ ] **Step 5: Create inspect_db.py**

```python
# web/e2e/golden-path/scripts/inspect_db.py
# Read-only domain evidence queries.
# Accepts JSON command on stdin, returns JSON on stdout.
# Each command validated against closed schema.
```

- [ ] **Step 6: Create fixture composition index**

```typescript
// web/e2e/golden-path/fixtures/index.ts
import { test as base, expect } from '@playwright/test';
// Compose all fixtures into a single `test` export
```

- [ ] **Step 7: Commit**

```bash
git add web/e2e/golden-path/playwright.config.golden.ts \
  web/e2e/golden-path/fixtures/ \
  web/e2e/golden-path/scripts/
git commit -m "$(cat <<'EOF'
feat(e2e): add Playwright config, runtime safety, service control, evidence inspector

Composed fixture with NetworkSpy (fail-closed), topology attestation
validation, egress probes, config probes, container lifecycle via
broker, and read-only domain evidence queries.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 14: Provisioning and onboarding fixtures

**Files:**
- Create: `web/e2e/golden-path/fixtures/provisioning.ts`
- Create: `web/e2e/golden-path/fixtures/onboarding-harness.ts`
- Create: `web/e2e/golden-path/fixtures/workspace-setup.ts`

**Interfaces:**
- Consumes: Golden-path provisioning command (Task 5), mail sink (Task 8), `OwnerSessionCapability` type
- Produces:
  - `provisioning` fixture: wraps `golden-path-provisioning.ts` with `execFile`, fd 3 capture, bounded buffers
  - `onboarding` fixture: `enrollOwner(invitation)` → `OwnerSessionCapability`, `enrollMember(invitation)` → `MemberSessionCapability`, `captureInvitation(mailSink, opts)` → `SecretInvitationCapability`
  - `workspaceSetup` fixture: `createMetaSandboxConnection(session, { mode })` via Workspace settings UI

- [ ] **Step 1: Implement provisioning fixture**

Per spec Section 5.3. `execFile` with no shell, bounded buffers, explicit timeout, JSON input on stdin, safe result on stdout, invitation on fd 3, sanitized stderr. Never includes raw child output in thrown errors. Never passes secrets as CLI arguments.

```typescript
// web/e2e/golden-path/fixtures/provisioning.ts
import { execFile } from 'node:child_process';

export class ProvisioningFixture {
  async begin(input: BeginProvisioningInput): Promise<{
    result: Omit<BeginProvisioningResult, 'ownerInvitation'>;
    ownerInvitation: SecretInvitationCapability;
  }> {
    // execFile golden-path-provisioning.ts begin
    // Read result from stdout (safe)
    // Read invitation from fd 3 (secret)
  }

  async complete(input: { provisioningRef: string }): Promise<CompleteProvisioningResult> {
    // execFile golden-path-provisioning.ts complete
  }
}
```

- [ ] **Step 2: Implement onboarding harness**

Per spec Section 6.5. Two interfaces: `enrollOwner`, `enrollMember`. Creates disposable secret-bearing contexts with NetworkSpy. TOTP secret stays in closure. Returns opaque `OwnerSessionCapability`:

```typescript
export interface OwnerSessionCapability {
  page(): Page;
  performStepUp(action: StepUpAction): Promise<OpaqueStepUpGrant>;
  createFreshContext(): Promise<OwnerSessionCapability>;
  close(): Promise<void>;
}
```

Mail-sink integration via `captureInvitation`:
```typescript
async captureInvitation(
  mailSinkUrl: string,
  mailSinkToken: string,
  opts: { recipientEmail: string; timeout: number },
): Promise<SecretInvitationCapability>
```

- [ ] **Step 3: Implement workspace-setup fixture**

Per spec Section 6.5.2. `createMetaSandboxConnection(session, { mode: 'customer_sandbox' })` — drives the Workspace settings UI to create the Meta Sandbox connection.

- [ ] **Step 4: Commit**

```bash
git add web/e2e/golden-path/fixtures/provisioning.ts \
  web/e2e/golden-path/fixtures/onboarding-harness.ts \
  web/e2e/golden-path/fixtures/workspace-setup.ts
git commit -m "$(cat <<'EOF'
feat(e2e): add provisioning, onboarding, and workspace-setup fixtures

Provisioning wraps the command adapter with fd 3 secret transport.
Onboarding creates disposable contexts with NetworkSpy, TOTP in
closure, opaque OwnerSessionCapability. Workspace setup creates
customer_sandbox Meta connection via SaaS UI.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 15: Adapter-control fixtures

**Files:**
- Create: `web/e2e/golden-path/fixtures/controlled-provider-fixtures.ts`
- Create: `web/e2e/golden-path/fixtures/meta-scenario-fixtures.ts`
- Create: `web/e2e/golden-path/scripts/wait_adapter_checkpoint.py`

**Interfaces:**
- Consumes: Controlled-provider sidecar HTTP API (Task 6), BFF meta-observation endpoint, `OwnerSessionCapability`
- Produces:
  - `providerControl`: barriers, checkpoints, effects, request attempts for Generation sidecar
  - `metaScenarios`: FakeMetaAdapter scenario control through same-origin BFF

- [ ] **Step 1: Implement controlled-provider fixtures**

Per spec Section 6.7. Uses sidecar HTTP test-control interface via `fetch`:

```typescript
// web/e2e/golden-path/fixtures/controlled-provider-fixtures.ts
export class ProviderControlFixture {
  registerBarrier(namespace: string, opts: { runRef: EntityRef<'generation_run'>; matchOrdinal: number }): Promise<BarrierRef>
  awaitCheckpoint(namespace: string, barrierName: string, opts: { expected: TerminalCheckpointState }): Promise<void>
  releaseBarrier(namespace: string, barrierName: string): Promise<void>
  inspectEffects(namespace: string, opts: { limit: number }): Promise<SanitizedEffectSummary[]>
  inspectRequestAttempts(namespace: string, opts: {
    idempotencyKeyHash: string;
    requestKind: 'generate' | 'reconcile';
    limit: number;
  }): Promise<RequestAttemptSummary[]>
  cleanup(namespace: string): Promise<void>
}
```

`awaitCheckpoint` delegates to `wait_adapter_checkpoint.py` and validates output against a closed TypeScript schema.

- [ ] **Step 2: Implement meta-scenario fixtures**

Per spec Section 6.7. Uses same-origin BFF endpoint with authenticated Owner session:

```typescript
// web/e2e/golden-path/fixtures/meta-scenario-fixtures.ts
export type MetaScenarioId = 'insufficient_metrics' | 'decisive_winner' | 'scale_warming' | 'scale_declining';

export class MetaScenarioFixture {
  async advanceMetaObservation(
    session: OwnerSessionCapability,
    namespace: string,
    opts: {
      scenario: MetaScenarioId;
      experiment?: EntityRef<'experiment'>;
      enrollment?: EntityRef<'scale_enrollment'>;
      candidateRefs?: EntityRef<'asset_version'>[];
    },
  ): Promise<MetaObservationAdvancedResult> {
    // POST /api/domain/v1/w/{slug}/test-support/meta-observation/advance
    // Uses session.page() for same-origin fetch
  }
}
```

- [ ] **Step 3: Create wait_adapter_checkpoint.py**

```python
# web/e2e/golden-path/scripts/wait_adapter_checkpoint.py
# Polls GET /test/runs/:namespace/checkpoints/:name
# Exits 0 with JSON terminal state on stdout
# Exits 1 with bounded error code on timeout
```

- [ ] **Step 4: Commit**

```bash
git add web/e2e/golden-path/fixtures/controlled-provider-fixtures.ts \
  web/e2e/golden-path/fixtures/meta-scenario-fixtures.ts \
  web/e2e/golden-path/scripts/wait_adapter_checkpoint.py
git commit -m "$(cat <<'EOF'
feat(e2e): add controlled-provider and meta-scenario fixtures

Provider control: barriers, checkpoints, effects, request-attempt
inspection via sidecar HTTP. Meta scenarios: FakeMetaAdapter scenario
control through same-origin BFF with customer_sandbox provenance.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 16: Six area drivers

**Files:**
- Create: `web/e2e/golden-path/fixtures/area-drivers/library.ts`
- Create: `web/e2e/golden-path/fixtures/area-drivers/generation.ts`
- Create: `web/e2e/golden-path/fixtures/area-drivers/assets.ts`
- Create: `web/e2e/golden-path/fixtures/area-drivers/experiments.ts`
- Create: `web/e2e/golden-path/fixtures/area-drivers/localization.ts`
- Create: `web/e2e/golden-path/fixtures/area-drivers/scale.ts`

**Interfaces:**
- Consumes: `OwnerSessionCapability`, evidence types from Task 11, narrow typed inputs from preceding areas
- Produces: Each driver returns typed `AreaEvidence` and provides `verifyPersistedState(session, evidence)` for post-restart verification. See spec Section 7.4 for all signatures.

- [ ] **Step 1: Implement Library driver**

`setup(session, config)` → `LibraryEvidence`. Creates: Product with reference image, Offer, Persona, Dimension Template, Prompt Policy, Data Source. All through UI.

- [ ] **Step 2: Implement Generation driver**

`submit(session, opts)` → `GenerationSubmittedEvidence`. `observeCompletion(session, opts)` → `GenerationCompletedEvidence`.

- [ ] **Step 3: Implement Assets driver**

`reviewAndExport(session, opts)` → `AssetsEvidence`. Keyboard review (A/C/X/space/left). Regeneration. Re-approval of regenerated asset. ZIP export.

- [ ] **Step 4: Implement Experiments driver**

`createAndFreeze(session, opts)` → `ExperimentFrozenEvidence`. `launchPaused(session, opts)` → `ExperimentLaunchedEvidence` with `candidateDeliveries`. `recordInconclusive(session, opts)` → `ExperimentInconclusiveEvidence`. `confirmWinner(session, opts)` → `ExperimentWinnerEvidence`.

- [ ] **Step 5: Implement Localization driver**

`createAndConfirmBatch(session, opts)` → `LocalizationBatchEvidence`. `observeOutcomesAndRetry(session, opts)` → `LocalizationOutcomesEvidence`. Expected outcomes: DE→succeeded, CA→skipped, JP→failed→retry→succeeded.

- [ ] **Step 6: Implement Scale driver**

`createAndActivateEnrollment(session, opts)` → `ScaleEnrollmentEvidence`. `observeAdviceAndConvertDraft(session, opts)` → `ScaleEvidence`.

- [ ] **Step 7: Add `verifyPersistedState` to each driver**

Read-only verification method for post-restart state confirmation.

- [ ] **Step 8: Commit**

```bash
git add web/e2e/golden-path/fixtures/area-drivers/
git commit -m "$(cat <<'EOF'
feat(e2e): add six area drivers with typed evidence

Library, Generation, Assets, Experiments, Localization, Scale. Each
returns branded EntityRef evidence and provides verifyPersistedState
for post-restart verification. Drivers are UI-only — no DB queries.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 17: Suite contracts

**Files:**
- Create: `web/e2e/library/suite-contract.ts`
- Create: `web/e2e/generation/suite-contract.ts`
- Create: `web/e2e/assets/suite-contract.ts`
- Create: `web/e2e/experiments/suite-contract.ts`
- Create: `web/e2e/localization/suite-contract.ts`
- Create: `web/e2e/scale/suite-contract.ts`

**Interfaces:**
- Consumes: Nothing
- Produces: Machine-readable suite metadata per area. CI validates these. Not imported by golden-path runtime.

- [ ] **Step 1: Create all six suite contracts**

Per spec Section 12. Each contract is area-specific. Below are all six:

**Library** (`web/e2e/library/suite-contract.ts`):
```typescript
export const suiteContract = {
  schemaVersion: '1.0',
  area: 'library',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: { command: 'seed_prerequisites.py', creates: [], mechanism: 'application-command-path' },
  adapterModes: ['customer_sandbox'],
  shortcuts: [],
  goldenPathSteps: [1],
  contractRefs: ['docs/product/creative-library-ownership-and-reuse-contract.md'],
  specs: ['golden-path.spec.ts', 'reference-transfer.spec.ts', 'cross-workspace-security.spec.ts', 'owner-member.spec.ts', 'template-datasource.spec.ts', 'version-history.spec.ts', 'command-recovery.spec.ts', 'accessibility-responsive.spec.ts'],
} as const;
```

**Generation** (`web/e2e/generation/suite-contract.ts`):
```typescript
export const suiteContract = {
  schemaVersion: '1.0',
  area: 'generation',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: { command: 'seed_prerequisites.py', creates: ['library-chain'], mechanism: 'application-command-path' },
  adapterModes: ['controlled_mock'],
  shortcuts: ['library'],
  goldenPathSteps: [2, 3],
  contractRefs: ['docs/product/v9-functional-alignment-acceptance.md'],
  specs: ['golden-path.spec.ts', 'draft-preview.spec.ts', 'recovery.spec.ts', 'submission-progress.spec.ts'],
} as const;
```

**Assets** (`web/e2e/assets/suite-contract.ts`):
```typescript
export const suiteContract = {
  schemaVersion: '1.0',
  area: 'assets',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: { command: 'seed_prerequisites.py', creates: ['library-chain', 'generation-run'], mechanism: 'application-command-path' },
  adapterModes: ['controlled_mock'],
  shortcuts: ['library', 'generation'],
  goldenPathSteps: [4],
  contractRefs: ['docs/product/v9-functional-alignment-acceptance.md'],
  specs: ['cross-workspace.spec.ts', 'gallery-navigation.spec.ts', 'review-mode.spec.ts', 'export.spec.ts'],
} as const;
```

**Experiments** (`web/e2e/experiments/suite-contract.ts`):
```typescript
export const suiteContract = {
  schemaVersion: '1.0',
  area: 'experiments',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: { command: 'seed_prerequisites.py', creates: ['library-chain', 'generation-run', 'approved-assets'], mechanism: 'application-command-path' },
  adapterModes: ['customer_sandbox', 'controlled_mock'],
  shortcuts: ['library', 'generation', 'assets'],
  goldenPathSteps: [5, 6],
  contractRefs: ['docs/product/v9-functional-alignment-acceptance.md'],
  specs: ['judgement-winner.spec.ts', 'authorization-mode.spec.ts', 'duplicate-resolution.spec.ts', 'closed-state-evidence.spec.ts', 'launch-recovery.spec.ts'],
} as const;
```

**Localization** (`web/e2e/localization/suite-contract.ts`):
```typescript
export const suiteContract = {
  schemaVersion: '1.0',
  area: 'localization',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: { command: 'seed_prerequisites.py', creates: ['confirmed-winner', 'delivery-target'], mechanism: 'application-command-path' },
  adapterModes: ['customer_sandbox', 'controlled_mock'],
  shortcuts: ['library', 'generation', 'assets', 'experiments'],
  goldenPathSteps: [7],
  contractRefs: ['docs/product/localization-truth-package-contract.md'],
  specs: ['golden-path.spec.ts', 'partial-retry.spec.ts', 'isolated-recovery.spec.ts', 'authorization.spec.ts', 'recovery.spec.ts'],
} as const;
```

**Scale** (`web/e2e/scale/suite-contract.ts`):
```typescript
export const suiteContract = {
  schemaVersion: '1.0',
  area: 'scale',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: { command: 'seed_prerequisites.py', creates: ['confirmed-winner', 'delivery-target', 'policy-chain'], mechanism: 'application-command-path' },
  adapterModes: ['customer_sandbox', 'controlled_mock'],
  shortcuts: ['library', 'generation', 'assets', 'experiments'],
  goldenPathSteps: [8, 10],
  contractRefs: ['docs/product/scale-advice-semantics-contract.md'],
  specs: ['controlled-mock-journey.spec.ts', 'customer-sandbox-smoke.spec.ts'],
} as const;
```

- [ ] **Step 2: Commit**

```bash
git add web/e2e/library/suite-contract.ts \
  web/e2e/generation/suite-contract.ts \
  web/e2e/assets/suite-contract.ts \
  web/e2e/experiments/suite-contract.ts \
  web/e2e/localization/suite-contract.ts \
  web/e2e/scale/suite-contract.ts
git commit -m "$(cat <<'EOF'
feat(e2e): add machine-readable suite contracts for all six areas

CI metadata and acceptance gates per area. Declares adapter modes,
prerequisites, golden-path steps, and governing contract references.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 18: Golden-path spec and CI workflows

**Files:**
- Create: `web/e2e/golden-path/golden-path.spec.ts`
- Create: `.github/workflows/golden-path-pr.yml`
- Create: `.github/workflows/golden-path-acceptance.yml`

**Interfaces:**
- Consumes: All fixtures (Tasks 11-16), all infrastructure (Tasks 6-10), all prerequisite refactors (Tasks 1-5)
- Produces: The single serial Playwright test proving the v9 golden path. Two CI workflows: PR validation and formal acceptance.

- [ ] **Step 1: Write `golden-path.spec.ts`**

Per spec Section 8 (Steps 0–10). Single `test()` with `test.step()` for each step. Uses the composed `test` from `fixtures/index.ts`.

```typescript
import { test, expect } from './fixtures';

test('v9 Beta golden path', async ({
  provisioning, onboarding, workspaceSetup,
  serviceControl, providerControl, metaScenarios,
  evidence, safety, artifacts,
  library, generation, assets, experiments, localization, scale,
}) => {
  const namespace = `gp-${Date.now()}`;

  // ── Step 0 — Safety, Provisioning, Owner, Member, Emptiness ──
  await test.step('Step 0.1 — Validate topology and safety', async () => {
    await safety.validateTopologyAttestation();
    await safety.probeNoEgress(namespace);
    await safety.probeEffectiveConfig(namespace);
  });

  const { provisioningRef, workspaceRef, workspaceSlug } =
    await test.step('Step 0.2 — Begin provisioning', async () => {
      return provisioning.begin({
        idempotencyKey: `gp-${namespace}`,
        workspaceName: 'Golden Path Workspace',
        ownerEmail: 'owner@golden-path.test',
        operatorIdentifier: 'golden-path-operator',
        betaPlanVersionId: 'beta-v1',
        namespace,
      });
    });

  const ownerSession = await test.step('Step 0.3 — Enroll Owner', async () => {
    return onboarding.enrollOwner(provisioning.ownerInvitation);
  });

  const memberInvitation = await test.step('Step 0.4 — Create Member invitation', async () => {
    await ownerSession.page().goto(`/${workspaceSlug}/settings/members`);
    await ownerSession.page().getByRole('button', { name: /invite/i }).click();
    await ownerSession.page().getByLabel(/email/i).fill('member@golden-path.test');
    await ownerSession.page().getByRole('button', { name: /send/i }).click();
    return onboarding.captureInvitation({ recipientEmail: 'member@golden-path.test', timeout: 30_000 });
  });

  await test.step('Step 0.5 — Enroll Member and verify restrictions', async () => {
    const memberSession = await onboarding.enrollMember(memberInvitation);
    await expect(memberSession.page().getByText(workspaceSlug)).toBeVisible();
    await memberSession.page().goto(`/${workspaceSlug}/settings`);
    await expect(memberSession.page().getByText(/forbidden|not authorized/i)).toBeVisible();
    await memberSession.close();
  });

  await test.step('Step 0.6 — Close temporary Owner context', async () => {
    await ownerSession.close();
  });

  const session = await test.step('Step 0.7 — Create main Owner context', async () => {
    return ownerSession.createFreshContext();
  });

  const { provisioningSnapshotVersion, activeSnapshotVersion } =
    await test.step('Step 0.8 — Complete provisioning', async () => {
      return provisioning.complete({ provisioningRef });
    });

  await test.step('Step 0.9 — Create Meta Sandbox connection', async () => {
    await workspaceSetup.createMetaSandboxConnection(session, { mode: 'customer_sandbox' });
  });

  await test.step('Step 0.10 — Verify Meta provenance', async () => {
    await evidence.assertWorkspaceMetaProvenance(workspaceRef, 'customer_sandbox');
  });

  await test.step('Step 0.11 — Verify zero business entities', async () => {
    await evidence.assertZeroBusinessEntities(workspaceRef);
  });

  // ── Step 1 — Creative Library ──
  const libraryEv = await test.step('Step 1 — Creative Library', async () => {
    return library.setup(session, {
      productName: 'Golden Path Product',
      offerName: 'Golden Path Offer',
      personaName: 'Golden Path Persona',
      templateName: 'Golden Path Template',
      policyName: 'Golden Path Policy',
      dataSourceName: 'Golden Path DataSource',
    });
  });

  // ── Step 2 — Generation Submission ──
  await test.step('Step 2.1 — Stop Worker', async () => {
    await serviceControl.stop('worker');
  });

  const submitted = await test.step('Step 2.2 — Submit generation', async () => {
    return generation.submit(session, {
      productVersion: libraryEv.productVersion,
      templateVersion: libraryEv.templateVersion,
      policyVersion: libraryEv.promptPolicyVersion,
      dataSourceVersion: libraryEv.dataSourceVersion,
    });
  });

  // ── Step 3 — Deterministic Worker SIGKILL and Recovery ──
  const barrierRef = await test.step('Step 3.1 — Register barrier', async () => {
    return providerControl.registerBarrier(namespace, {
      runRef: submitted.run,
      matchOrdinal: 0,
    });
  });

  await test.step('Step 3.2 — Start Worker', async () => {
    await serviceControl.start('worker');
  });

  await test.step('Step 3.3 — Await checkpoint', async () => {
    await providerControl.awaitCheckpoint(namespace, barrierRef.name, {
      expected: {
        phase: 'after_remote_commit',
        remote_effect_committed: true,
        response_received_by_adapter: true,
        worker_barrier_waiting: true,
      },
    });
  });

  await test.step('Step 3.4 — SIGKILL Worker', async () => {
    await serviceControl.kill('worker', 'SIGKILL');
  });

  const lease = await test.step('Step 3.5 — Read job lease', async () => {
    return evidence.readJobLease(submitted.run);
  });

  await test.step('Step 3.6 — Wait for lease deadline', async () => {
    const waitMs = Math.max(0, lease.deadlineMs - Date.now()) + 1000;
    await new Promise(r => setTimeout(r, waitMs));
  });

  await test.step('Step 3.7 — Restart Worker', async () => {
    await serviceControl.start('worker');
  });

  const completed = await test.step('Step 3.8 — Observe completion', async () => {
    return generation.observeCompletion(session, {
      run: submitted.run,
      expectedItemCount: submitted.expectedItemCount,
    });
  });

  await test.step('Step 3.9-3.10 — Recovery assertions', async () => {
    const targetKeyHash = completed.assetVersions[0].runtimeId;
    const generateAttempts = await providerControl.inspectRequestAttempts(namespace, {
      idempotencyKeyHash: targetKeyHash,
      requestKind: 'generate',
      limit: 10,
    });
    expect(generateAttempts).toHaveLength(1);

    const reconcileAttempts = await providerControl.inspectRequestAttempts(namespace, {
      idempotencyKeyHash: targetKeyHash,
      requestKind: 'reconcile',
      limit: 10,
    });
    expect(reconcileAttempts).toHaveLength(1);
  });

  await test.step('Step 3.11 — Re-probe config after Worker restart', async () => {
    await safety.probeEffectiveConfig(namespace);
  });

  // ── Step 4 — Assets Review ──
  const assetsEv = await test.step('Step 4 — Assets review and export', async () => {
    return assets.reviewAndExport(session, {
      approvedAssets: completed.assetVersions.slice(0, 2),
      needsChangesAsset: completed.assetVersions[2],
      rejectedAsset: completed.assetVersions[3],
      skipAsset: completed.assetVersions[4],
    });
  });

  // ── Step 5 — Experiment Freeze and PAUSED Launch ──
  const frozen = await test.step('Step 5.1 — Create and freeze experiment', async () => {
    return experiments.createAndFreeze(session, {
      approvedAssets: assetsEv.approvedRefs,
      reApprovedAsset: assetsEv.reApprovedRef,
      offerVersion: libraryEv.offerVersion,
    });
  });

  const launched = await test.step('Step 5.2 — Launch PAUSED', async () => {
    return experiments.launchPaused(session, { experiment: frozen.experiment });
  });

  // ── Step 6 — Metrics, Judgment, Winner ──
  await test.step('Step 6.1 — Insufficient metrics → inconclusive', async () => {
    await metaScenarios.advanceMetaObservation(session, namespace, {
      scenario: 'insufficient_metrics',
      experiment: frozen.experiment,
    });
    await experiments.recordInconclusive(session, { experiment: frozen.experiment });
    await evidence.assertNoWinnerDecision(workspaceRef, frozen.experiment);
  });

  const winner = await test.step('Step 6.2 — Decisive metrics → confirm Winner', async () => {
    await metaScenarios.advanceMetaObservation(session, namespace, {
      scenario: 'decisive_winner',
      experiment: frozen.experiment,
      candidateRefs: frozen.candidateRefs,
    });
    return experiments.confirmWinner(session, { experiment: frozen.experiment });
  });

  // ── Step 7 — Localization ──
  const localizationBatch = await test.step('Step 7.1-7.2 — Create localization batch', async () => {
    return localization.createAndConfirmBatch(session, {
      winnerAssetVersion: winner.winnerAssetVersion,
      winnerDecision: winner.winnerDecision,
      offerVersion: libraryEv.offerVersion,
      targetMarkets: { DE: 'succeeded', CA: 'skipped_no_change_required', JP: 'failed' },
    });
  });

  const localizationEv = await test.step('Step 7.3-7.4 — Observe outcomes and retry JP', async () => {
    return localization.observeOutcomesAndRetry(session, {
      batch: localizationBatch.batch,
      expectedOutcomes: { DE: 'succeeded', CA: 'skipped_no_change_required', JP: 'failed' },
      retryMarket: 'JP',
      retryExpectedOutcome: 'succeeded',
    });
  });

  // ── Step 8 — Scale ──
  const winnerDelivery = launched.candidateDeliveries.find(
    d => d.candidateRef.runtimeId === winner.winnerAssetVersion.runtimeId
  );
  expect(winnerDelivery).toBeDefined();

  const enrollmentEv = await test.step('Step 8.1 — Create Scale enrollment', async () => {
    return scale.createAndActivateEnrollment(session, {
      winnerDecision: winner.winnerDecision,
      winnerAssetVersion: winner.winnerAssetVersion,
      offerVersion: libraryEv.offerVersion,
      market: 'US',
      placement: 'feed',
      landingPage: 'https://example.test/landing',
      campaign: launched.campaignRef,
      adSet: winnerDelivery!.adSetRef,
      ad: winnerDelivery!.adRef,
    });
  });

  await test.step('Step 8.2 — Scale warming', async () => {
    await metaScenarios.advanceMetaObservation(session, namespace, {
      scenario: 'scale_warming',
      enrollment: enrollmentEv.enrollment,
    });
  });

  await test.step('Step 8.3 — Scale declining', async () => {
    await metaScenarios.advanceMetaObservation(session, namespace, {
      scenario: 'scale_declining',
      enrollment: enrollmentEv.enrollment,
    });
  });

  const scaleEv = await test.step('Step 8.4 — Observe advice and convert draft', async () => {
    return scale.observeAdviceAndConvertDraft(session, {
      enrollment: enrollmentEv.enrollment,
    });
  });

  await test.step('Step 8.5-8.6 — Scale evidence assertions', async () => {
    await evidence.assertZeroMetaWrites(workspaceRef);
  });

  // ── Step 9 — Pre-Restart Canonical Snapshot ──
  await test.step('Step 9.1 — Quiescence', async () => {
    await evidence.assertNoRunnableOrInFlightJobs(workspaceRef);
    await evidence.assertOperationsSettled(workspaceRef);
  });

  const preRestartSnapshot = await test.step('Step 9.2 — Capture snapshot', async () => {
    return evidence.captureCanonicalSnapshot(workspaceRef);
  });

  // ── Step 10 — Restart and Final Verification ──
  for (const svc of ['web', 'api', 'worker'] as const) {
    await test.step(`Step 10.1 — Restart ${svc}`, async () => {
      await serviceControl.restart(svc);
    });
    await test.step(`Step 10.2 — Verify ${svc} network`, async () => {
      const netAttestation = await serviceControl.inspectNetwork(svc);
      await safety.validateRestartedServiceNetwork(svc, netAttestation);
    });
  }

  await test.step('Step 10.3 — Re-probe config', async () => {
    await safety.probeEffectiveConfig(namespace);
  });

  const freshSession = await test.step('Step 10.4 — Fresh Owner context', async () => {
    return session.createFreshContext();
  });

  await test.step('Step 10.5 — Verify all six areas', async () => {
    await library.verifyPersistedState(freshSession, libraryEv);
    await generation.verifyPersistedState(freshSession, completed);
    await assets.verifyPersistedState(freshSession, assetsEv);
    await experiments.verifyPersistedState(freshSession, winner);
    await localization.verifyPersistedState(freshSession, localizationEv);
    await scale.verifyPersistedState(freshSession, scaleEv);
  });

  await test.step('Step 10.6 — Post-restart quiescence', async () => {
    await evidence.assertNoRunnableOrInFlightJobs(workspaceRef);
    await evidence.assertOperationsSettled(workspaceRef);
  });

  const postRestartSnapshot = await test.step('Step 10.7 — Post-restart snapshot', async () => {
    return evidence.captureCanonicalSnapshot(workspaceRef);
  });

  await test.step('Step 10.8 — Canonical snapshot equality', async () => {
    const diff = await evidence.compareSnapshots(preRestartSnapshot, postRestartSnapshot);
    expect(diff).toEqual({});
  });

  await test.step('Step 10.9 — Final three-layer assertions', async () => {
    await safety.assertNetworkSpyClean();
    await evidence.assertZeroMetaWrites(workspaceRef);
    await evidence.assertNonVacuousMetaEvidence(workspaceRef, {
      expectedProvenance: 'customer_sandbox',
      expectedScenarios: ['insufficient_metrics', 'decisive_winner', 'scale_warming', 'scale_declining'],
      frozenCandidateCount: frozen.candidateRefs.length,
    });
    await evidence.assertWorkspaceMetaProvenance(workspaceRef, 'customer_sandbox');
    await evidence.assertCallEvidenceModeConsistency(workspaceRef);
  });

  await test.step('Step 10.10 — Mark complete', async () => {
    await artifacts.markScenarioComplete();
  });
});
```

- [ ] **Step 2: Create PR validation workflow**

Per spec Section 10.1. `golden-path-pr.yml`:
- Trigger: PR on affected paths
- Steps: checkout, keypair/token generation, broker start, image build, Pass 1 topology, compose up, readiness wait, Pass 2 topology with attestation, playwright run, always: redact logs, verify artifacts, conditional upload, teardown + cleanup

- [ ] **Step 3: Create formal acceptance workflow**

Per spec Section 10.2. `golden-path-acceptance.yml`:
- Trigger: workflow_dispatch with release commit SHA
- DAG: resolve-inputs → validate-contracts → area suites (parallel) → golden-path → assemble → publish
- Protected environment, minimal permissions

- [ ] **Step 4: Add Makefile targets**

```makefile
golden-path-up:
	docker compose -p gp-local-0 -f docker-compose.yml -f docker-compose.golden-path.yml up -d

golden-path-run:
	docker compose -p gp-local-0 -f docker-compose.yml -f docker-compose.golden-path.yml \
	  run --rm playwright npx playwright test --config=e2e/golden-path/playwright.config.golden.ts

golden-path-down:
	docker compose -p gp-local-0 -f docker-compose.yml -f docker-compose.golden-path.yml down -v --remove-orphans
```

- [ ] **Step 5: Run `make verify` and golden-path locally**

```bash
make verify
make golden-path-up
make golden-path-run
make golden-path-down
```

- [ ] **Step 6: Commit**

```bash
git add web/e2e/golden-path/golden-path.spec.ts \
  .github/workflows/golden-path-pr.yml \
  .github/workflows/golden-path-acceptance.yml \
  Makefile
git commit -m "$(cat <<'EOF'
feat(e2e): add golden-path spec and CI workflows

Single serial Playwright test proving the complete v9 Beta golden path.
PR validation and formal acceptance workflows with secret-safe artifact
handling and durable AcceptanceManifest.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 19: BFF test-support route for Meta observation

The `metaScenarios` fixture needs a same-origin BFF endpoint to advance FakeMetaAdapter scenarios. This endpoint exists only in the E2E build with `META_ADAPTER=fake_meta`.

**Files:**
- Create: `web/pages/api/domain/v1/w/[slug]/test-support/meta-observation/advance.ts` (or equivalent Next.js API route)
- Create: `web/pages/api/domain/v1/w/[slug]/test-support/__tests__/meta-observation.test.ts`

**Interfaces:**
- Consumes: Next.js API route, Prisma for membership verification, FastAPI domain API for forwarding
- Produces: `POST /api/domain/v1/w/{slug}/test-support/meta-observation/advance` — resolves slug, verifies active Owner membership, mints delegated-user token, forwards to private FastAPI test-support operation. Absent from production builds.

- [ ] **Step 1: Implement the BFF route**

```typescript
// Conditional on META_ADAPTER=fake_meta
// Resolves workspace from slug and membership
// Verifies Owner authorization
// Mints delegated-user token
// Forwards to FastAPI: POST /api/domain/v1/workspaces/{workspace_id}/test-support/meta-observation/advance
// Returns { scenarioApplied, observationCount, provenance }
```

- [ ] **Step 2: Implement the FastAPI test-support operation**

```python
# app/meta/test_support.py (conditional on META_ADAPTER=fake_meta)
# Derives workspace_id from verified claims
# Verifies experiment/enrollment/candidate refs belong to workspace + customer_sandbox connection
# Loads curated scenario data into FakeMetaAdapter's database tables
# Returns { scenario_applied, observation_count, provenance }
```

- [ ] **Step 3: Write tests**

Test: endpoint absent when META_ADAPTER != fake_meta. Test: rejects non-Owner. Test: rejects cross-workspace refs.

- [ ] **Step 4: Commit**

```bash
git add web/pages/api/domain/v1/w/\[slug\]/test-support/ app/meta/test_support.py
git commit -m "$(cat <<'EOF'
feat(e2e): add BFF and FastAPI test-support routes for Meta observation

Same-origin BFF endpoint for FakeMetaAdapter scenario control. Routes
through normal authentication and workspace resolution. Present only
when META_ADAPTER=fake_meta. customer_sandbox provenance enforced.

Refs #98

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Task 20: Integration verification and PR

**Files:**
- None new — verification of all prior tasks

**Interfaces:**
- Consumes: All tasks 1-19
- Produces: Passing `make verify`, passing golden-path test, opened PR

- [ ] **Step 1: Run `make verify`**

```bash
make verify
```

- [ ] **Step 2: Run full golden-path locally**

```bash
make golden-path-up
# Wait for readiness
make golden-path-run
make golden-path-down
```

- [ ] **Step 3: Fix any failures**

Iterate until the golden path passes end-to-end.

- [ ] **Step 4: Open PR**

```bash
gh pr create --title "feat: secret-safe cross-area golden-path E2E (#98)" --body "$(cat <<'EOF'
## Summary

- Refactors ExternalCallCoordinator for controlled-mock dispatch and idempotency key persistence
- Extracts two-phase provisioning deep module with atomic Workspace activation
- Adds controlled-provider sidecar, lifecycle broker, mail sink, Compose overlay
- Implements topology validation with Ed25519-signed attestation
- Delivers golden-path.spec.ts proving the complete v9 Beta path
- Adds SafeCapture, EvidenceRecorder, HMAC opaque aliases, suite contracts
- Adds PR validation and formal acceptance CI workflows
- Secret-safe: no traces, no video, no raw credentials in artifacts

## Test plan

- [ ] `make verify` passes
- [ ] Golden-path spec passes locally against Compose stack
- [ ] CI golden-path-pr workflow runs and uploads verified artifacts
- [ ] Log redactor catches all secret patterns
- [ ] Artifact verifier rejects files with secret patterns
- [ ] Topology validator rejects non-internal networks
- [ ] NetworkSpy aborts non-allowlisted requests
- [ ] SIGKILL recovery produces exactly 1 generate + 1 reconcile request attempt
- [ ] Canonical snapshot matches after full restart
- [ ] All Meta evidence carries customer_sandbox provenance
- [ ] Zero Meta writes confirmed by three independent layers

Closes #98

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Record any remaining risks in the PR description**

Document: migrations, operational impact, known limitations, risks not covered by automated tests.
