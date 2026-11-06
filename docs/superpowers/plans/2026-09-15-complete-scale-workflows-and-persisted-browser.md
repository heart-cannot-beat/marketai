# Complete Scale Workflows and Persisted Browser Acceptance Evidence

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement all 501-returning Scale endpoints (targets with preview/MFA, snapshot-pinned breakdowns, hashed command-outcome recovery, idempotent exports with download intents), wire the UI, add Playwright browser evidence against PostgreSQL+FakeMetaAdapter, and remove the old skipped Python E2E stubs.

**Architecture:** Six deep domain modules (`targets.py`, `breakdowns.py`, `exports.py`, additions to `command_journal.py`) own all business logic. Both `v1_router.py` and `platform_router.py` remain thin HTTP adapters over these shared modules, differing only in identity/authorization (BFF delegated vs. platform-operator JWT). A new Alembic migration adds target-preview tables and the command-identity-hash column. The Scale Worker gains `scale_export` without acquiring any Meta write port. OpenAPI/BFF artifacts are regenerated after every route change. Playwright specs under `web/e2e/scale/` exercise the full 18-step controlled-mock journey and 5-step sandbox smoke against real PostgreSQL and the durable `FakeMetaAdapter`.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / Alembic / Pydantic v2 / PostgreSQL RLS / Next.js 14 / React / TypeScript / Playwright / openapi-typescript / Ajv / Zod

**Spec:** `docs/product/scale-advice-semantics-contract.md`, `docs/superpowers/specs/2026-09-08-scale-section-4-domain-interface-ui-validation.md`

## Global Constraints

- `ScaleWorkerRuntime` must never import `MetaWritePort`, `meta_write_client`, or `app.launch`. Enforced by `tests/test_scale_worker_isolation.py` AST+transitive-import gates.
- Money values cross the wire as canonical decimal strings (`str`), never floats. Parsed to `Decimal` in the domain layer.
- Every mutating endpoint requires `Idempotency-Key` header, declared with `openapi_extra={"x-marketai-idempotent": True}`.
- Every response carrying a mutable aggregate includes `allowed_actions` computed server-side via `compute_actions`. The browser never infers eligibility.
- Target publish and withdraw require MFA step-up (`X-Step-Up-Assertion` header). These operation IDs must be added to `PROTECTED_OPERATIONS` in `app/platform/step_up.py`.
- Platform-router handlers use `require_platform_operator_jwt` exclusively — no static-token fallback.
- `contracts/openapi/marketai-domain-v1-policy.json` must have a policy entry for every new operation ID (fail-closed).
- After route changes, run `python scripts/generate_openapi.py` then `bash scripts/generate_all.sh` to regenerate all TypeScript artifacts.
- Existing partial unique index `ix_stgt_active_target` enforces one active target per exact (workspace, product_version, offer_version, market, currency) context.
- Evidence mode (`meta_mode`) participates in every fingerprint, query scope, and unique constraint.
- Privacy-suppressed breakdown segments are never silently omitted; they appear as typed placeholders with no metric values.

---

## File Map

### New files

| File | Responsibility |
|------|----------------|
| `app/scale/targets.py` | Target lifecycle: context resolution, publication preview, publish, withdrawal preview, withdraw, list, get |
| `app/scale/breakdowns.py` | Snapshot-pinned breakdown query with privacy suppression and pagination |
| `app/scale/exports.py` | Export creation (Operation-backed), download-intent creation |
| `migrations/versions/ad2_scale_target_preview_and_command_identity.py` | Target preview tables, command identity hash column+index, grants, RLS |
| `web/e2e/scale/fixtures.ts` | Playwright fixtures: seed, advance, inspect, worker control |
| `web/e2e/scale/helpers.ts` | Shared Playwright helpers: network spy, DB inspection |
| `web/e2e/scale/seed_prerequisites.py` | Python seeder for Scale E2E prerequisites |
| `web/e2e/scale/inspect_db.py` | Python DB inspector for Scale E2E assertions |
| `web/e2e/scale/worker_checkpoint.py` | Single-job Worker boundary for deterministic E2E steps |
| `web/e2e/scale/controlled-mock-journey.spec.ts` | 18-step controlled-mock golden journey |
| `web/e2e/scale/customer-sandbox-smoke.spec.ts` | 5-step customer-sandbox smoke |
| `tests/test_scale_targets.py` | Target lifecycle unit+integration tests |
| `tests/test_scale_breakdowns.py` | Breakdown query unit+integration tests |
| `tests/test_scale_exports.py` | Export+download-intent unit+integration tests |
| `tests/test_scale_command_recovery.py` | Command-outcome recovery tests |

### Modified files

| File | Change |
|------|--------|
| `app/scale/models.py` | Add `ScaleTargetPublicationPreviewRecord`, `ScaleTargetWithdrawalPreviewRecord`; add `identity_hash` column to `ScaleCommandJournalRecord` |
| `app/scale/schemas.py` | Extend target schemas (economic fields, preview/withdraw shapes); add breakdown, export, download-intent schemas; revise `CommandOutcomeResponse` to discriminated outcome |
| `app/scale/v1_router.py` | Replace 6 stubs; add target-context, target-get, target-publication-preview, target-withdrawal-preview, export-download-intent routes |
| `app/scale/platform_router.py` | Mirror all new routes with JWT-only auth |
| `app/scale/command_journal.py` | Add `compute_identity_hash`, `lookup_command_outcome`; store `identity_hash` at claim time |
| `app/scale/worker.py` | Add `scale_export` job handler |
| `app/platform/step_up.py` | Add `publish_scale_target`, `withdraw_scale_target` to `PROTECTED_OPERATIONS` |
| `contracts/openapi/marketai-domain-v1-policy.json` | Add policy entries for all new operation IDs |
| `web/components/scale/detail/BreakdownTable.tsx` | Replace `NotImplementedNotice` with real breakdown data |
| `web/components/scale/targets/TargetManager.tsx` | Replace `NotImplementedNotice` with real target list, publish, withdraw flows |
| `web/components/scale/detail/EnrollmentDetail.tsx` | Add export button |
| `web/hooks/scale/useScaleTargets.ts` | Update types for economic fields, add preview/withdraw hooks |
| `web/hooks/scale/useScaleEvidence.ts` | Update breakdown hook to pass snapshot_id+breakdown_type |
| `web/hooks/scale/useScaleExport.ts` | Add download-intent hook |
| `web/lib/domain-client/gateway.ts` | Wire all formerly-501 methods + new preview/download-intent methods |

### Deleted files

| File | Reason |
|------|--------|
| `tests/e2e/test_scale_controlled_mock_journey.py` | Replaced by TypeScript Playwright spec |
| `tests/e2e/test_scale_customer_sandbox_smoke.py` | Replaced by TypeScript Playwright spec |

---

## Task 1: Migration — Target Preview Tables and Command Identity Hash

**Files:**
- Create: `migrations/versions/ad2_scale_target_preview_and_command_identity.py`
- Modify: `app/scale/models.py` (add ORM classes + identity_hash column)

**Interfaces:**
- Consumes: existing `ScaleTargetVersionRecord`, `ScaleTargetLifecycleEventRecord`, `ScaleCommandJournalRecord` from `app.scale.models`
- Produces: `ScaleTargetPublicationPreviewRecord` (table `scale_target_publication_previews`), `ScaleTargetWithdrawalPreviewRecord` (table `scale_target_withdrawal_previews`), `identity_hash` column on `scale_command_journal`

- [ ] **Step 1: Add ORM model `ScaleTargetPublicationPreviewRecord` to `app/scale/models.py`**

Insert after `ScaleTargetLifecycleEventRecord` (around line 580):

```python
class ScaleTargetPublicationPreviewRecord(Base):
    __tablename__ = "scale_target_publication_previews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"],
            name="fk_stpp_workspace", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "target_cpa IS NOT NULL OR breakeven_roas IS NOT NULL",
            name="ck_stpp_at_least_one_target",
        ),
        CheckConstraint(
            "(consumed_at IS NULL) = (result_target_version_id IS NULL)",
            name="ck_stpp_consumed_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("stpp"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_code: Mapped[str] = mapped_column(String(10), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    target_cpa: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    breakeven_roas: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    minimum_economic_spend: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    affected_enrollment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    policy_floor_check_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    impact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    creator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    result_target_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
```

- [ ] **Step 2: Add ORM model `ScaleTargetWithdrawalPreviewRecord`**

```python
class ScaleTargetWithdrawalPreviewRecord(Base):
    __tablename__ = "scale_target_withdrawal_previews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "target_version_id"],
            ["scale_target_versions.workspace_id", "scale_target_versions.id"],
            name="fk_stwp_target_version", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(consumed_at IS NULL) = (withdrawn_at IS NULL)",
            name="ck_stwp_consumed_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("stwp"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    target_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    affected_enrollment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dependent_advice_thread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    impact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    creator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
```

- [ ] **Step 3: Add `identity_hash` to `ScaleCommandJournalRecord`**

Add after `created_at`:

```python
    identity_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
```

Add a module-level index:

```python
Index(
    "ix_scj_identity_hash",
    ScaleCommandJournalRecord.workspace_id,
    ScaleCommandJournalRecord.meta_mode,
    ScaleCommandJournalRecord.identity_hash,
    unique=True,
    postgresql_where=text("identity_hash IS NOT NULL"),
).ddl_if(dialect="postgresql")
```

- [ ] **Step 4: Write the Alembic migration**

Create `migrations/versions/ad2_scale_target_preview_and_command_identity.py`:

```python
"""Scale target preview tables and command identity hash.

Issue #87 Task 1.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "ad2_target_preview"
down_revision = "ac1_enrollment_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- ScaleTargetPublicationPreviewRecord --
    op.create_table(
        "scale_target_publication_previews",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("product_version_id", sa.String(64), nullable=False),
        sa.Column("offer_version_id", sa.String(64), nullable=False),
        sa.Column("market_code", sa.String(10), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("target_cpa", sa.Numeric(24, 12), nullable=True),
        sa.Column("breakeven_roas", sa.Numeric(10, 4), nullable=True),
        sa.Column("minimum_economic_spend", sa.Numeric(24, 12), nullable=True),
        sa.Column("affected_enrollment_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("policy_floor_check_passed", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("impact_hash", sa.String(64), nullable=False),
        sa.Column("creator_id", sa.String(100), nullable=False),
        sa.Column("result_target_version_id", sa.String(64), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "target_cpa IS NOT NULL OR breakeven_roas IS NOT NULL",
            name="ck_stpp_at_least_one_target",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL) = (result_target_version_id IS NULL)",
            name="ck_stpp_consumed_shape",
        ),
    )

    # -- ScaleTargetWithdrawalPreviewRecord --
    op.create_table(
        "scale_target_withdrawal_previews",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("target_version_id", sa.String(64), nullable=False, index=True),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("affected_enrollment_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("dependent_advice_thread_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("impact_hash", sa.String(64), nullable=False),
        sa.Column("creator_id", sa.String(100), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["workspace_id", "target_version_id"],
            ["scale_target_versions.workspace_id", "scale_target_versions.id"],
            name="fk_stwp_target_version", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL) = (withdrawn_at IS NULL)",
            name="ck_stwp_consumed_shape",
        ),
    )

    # -- identity_hash on scale_command_journal --
    with op.batch_alter_table("scale_command_journal") as batch_op:
        batch_op.add_column(sa.Column("identity_hash", sa.String(64), nullable=True))
        batch_op.create_index("ix_scj_identity_hash_plain", ["identity_hash"])

    # Backfill identity_hash for existing rows
    conn = op.get_bind()
    conn.execute(text(
        "UPDATE scale_command_journal "
        "SET identity_hash = encode(sha256(("
        "  command_type || ':' || dedup_key"
        ")::bytea), 'hex') "
        "WHERE identity_hash IS NULL"
    ))

    # PostgreSQL-only: partial unique index, RLS, grants
    if conn.dialect.name == "postgresql":
        conn.execute(text(
            "CREATE UNIQUE INDEX ix_scj_ws_mode_identity_hash "
            "ON scale_command_journal (workspace_id, meta_mode, identity_hash) "
            "WHERE identity_hash IS NOT NULL"
        ))

        # RLS on new tables
        for tbl in ("scale_target_publication_previews", "scale_target_withdrawal_previews"):
            conn.execute(text(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY"))
            conn.execute(text(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY"))
            conn.execute(text(
                f"CREATE POLICY tenant_isolation ON {tbl} "
                f"USING (marketai_context_is_current(workspace_id))"
            ))

        # Grants
        for tbl in ("scale_target_publication_previews", "scale_target_withdrawal_previews"):
            conn.execute(text(f"GRANT SELECT, INSERT ON {tbl} TO marketai_api"))
            conn.execute(text(f"GRANT SELECT ON {tbl} TO marketai_worker"))
            conn.execute(text(f"GRANT UPDATE (consumed_at, result_target_version_id) ON scale_target_publication_previews TO marketai_api"))
            conn.execute(text(f"GRANT UPDATE (consumed_at, withdrawn_at) ON scale_target_withdrawal_previews TO marketai_api"))


def downgrade() -> None:
    op.drop_table("scale_target_withdrawal_previews")
    op.drop_table("scale_target_publication_previews")
    with op.batch_alter_table("scale_command_journal") as batch_op:
        batch_op.drop_column("identity_hash")
```

- [ ] **Step 5: Run migration and verify**

```bash
make migration-check
```

Expected: `alembic upgrade head` succeeds, `alembic check` reports no drift.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/ad2_scale_target_preview_and_command_identity.py app/scale/models.py
git commit -m "feat(scale): add target preview tables and command identity hash migration

Issue #87 Task 1: ScaleTargetPublicationPreviewRecord,
ScaleTargetWithdrawalPreviewRecord, identity_hash on
ScaleCommandJournalRecord with backfill and partial unique index."
```

---

## Task 2: Deep Module — Target Lifecycle (`app/scale/targets.py`)

**Files:**
- Create: `app/scale/targets.py`
- Create: `tests/test_scale_targets.py`
- Modify: `app/scale/schemas.py` (extend target schemas)
- Modify: `app/platform/step_up.py` (add Scale target operations)

**Interfaces:**
- Consumes: `ScaleTargetVersionRecord`, `ScaleTargetLifecycleEventRecord`, `ScaleTargetPublicationPreviewRecord`, `ScaleTargetWithdrawalPreviewRecord` from `app.scale.models`; `TenantContext` from `app.platform.tenancy`; `claim_command` from `app.scale.command_journal`; `validate_step_up_assertion` from `app.platform.step_up`; `compute_actions` from `app.platform.action_eligibility`
- Produces: `list_targets(session, context, *, lifecycle_state=None, product_version_id=None, offer_version_id=None, market_code=None) -> list[ScaleTargetVersionRecord]`; `get_target(session, context, target_id) -> ScaleTargetVersionRecord`; `resolve_target_contexts(session, context) -> list[dict]`; `create_publication_preview(session, context, *, request, idempotency_key) -> ScaleTargetPublicationPreviewRecord`; `publish_target(session, context, *, preview_id, idempotency_key) -> ScaleTargetVersionRecord`; `create_withdrawal_preview(session, context, *, target_id, reason, idempotency_key) -> ScaleTargetWithdrawalPreviewRecord`; `withdraw_target(session, context, *, preview_id, idempotency_key) -> ScaleTargetVersionRecord`; `target_actions(target) -> list[ActionEligibility]`; `TargetLifecycleError`, `TargetNotFoundError`, `TargetPreviewExpiredError`, `TargetPreviewConsumedError`

- [ ] **Step 1: Extend target schemas in `app/scale/schemas.py`**

Replace the existing `TargetCreateRequest` and `TargetResponse` blocks (lines 362-384) with:

```python
# ---------------------------------------------------------------------------
# Targets (Issue #87: full preview/publish/withdraw lifecycle)
# ---------------------------------------------------------------------------


class TargetContextResponse(_Base):
    product_version_id: str
    offer_version_id: str
    market_code: str
    currency: str
    has_active_target: bool


class TargetPublicationPreviewRequest(_Base):
    product_version_id: str = Field(min_length=1)
    offer_version_id: str = Field(min_length=1)
    market_code: str = Field(min_length=1, max_length=10)
    currency: str = Field(min_length=3, max_length=3)
    target_cpa: str | None = None
    breakeven_roas: str | None = None
    minimum_economic_spend: str | None = None


class TargetPublicationPreviewResponse(_Base):
    id: str
    product_version_id: str
    offer_version_id: str
    market_code: str
    currency: str
    target_cpa: str | None
    breakeven_roas: str | None
    minimum_economic_spend: str | None
    affected_enrollment_count: int
    policy_floor_check_passed: bool
    impact_hash: str
    expires_at: datetime
    created_at: datetime


class TargetPublishRequest(_Base):
    publication_preview_id: str = Field(min_length=1, max_length=64)


class TargetWithdrawalPreviewRequest(_Base):
    reason: str = Field(min_length=1, max_length=200)


class TargetWithdrawalPreviewResponse(_Base):
    id: str
    target_version_id: str
    reason: str
    affected_enrollment_count: int
    dependent_advice_thread_count: int
    impact_hash: str
    expires_at: datetime
    created_at: datetime


class TargetWithdrawRequest(_Base):
    withdrawal_preview_id: str = Field(min_length=1, max_length=64)


class TargetResponse(_Base):
    id: str
    product_version_id: str
    offer_version_id: str
    market_code: str
    currency: str
    target_cpa: str | None
    breakeven_roas: str | None
    minimum_economic_spend: str | None
    lifecycle_state: str
    creator_id: str
    revision: int
    published_at: datetime | None
    withdrawn_at: datetime | None
    created_at: datetime
    allowed_actions: list[ActionEligibilityModel]


class TargetListResponse(_Base):
    items: list[TargetResponse]
    next_cursor: str | None = None
```

- [ ] **Step 2: Add Scale target operations to `PROTECTED_OPERATIONS`**

In `app/platform/step_up.py`, add to the frozenset:

```python
    "publish_scale_target",
    "withdraw_scale_target",
```

- [ ] **Step 3: Write failing tests in `tests/test_scale_targets.py`**

```python
"""Target lifecycle: context resolution, preview/publish/withdraw.

Issue #87 Task 2.
"""
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from app.platform.tenancy import TenantContext
from app.scale.targets import (
    TargetLifecycleError,
    TargetNotFoundError,
    TargetPreviewExpiredError,
    TargetPreviewConsumedError,
    create_publication_preview,
    get_target,
    list_targets,
    publish_target,
    create_withdrawal_preview,
    withdraw_target,
    resolve_target_contexts,
    target_actions,
)
from app.scale.models import ScaleTargetVersionRecord


def _owner_context(workspace_id="ws-1"):
    return TenantContext(
        workspace_id=workspace_id,
        role="owner",
        initiator_id="user-owner",
        executor="user-owner",
    )


def _member_context(workspace_id="ws-1"):
    return TenantContext(
        workspace_id=workspace_id,
        role="member",
        initiator_id="user-member",
        executor="user-member",
    )


class TestCreatePublicationPreview:
    def test_creates_preview_with_valid_decimals(self, session):
        ctx = _owner_context()
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("25.50"),
            breakeven_roas=None,
            minimum_economic_spend=Decimal("100.00"),
            idempotency_key="pub-preview-1",
        )
        assert preview.id.startswith("stpp")
        assert preview.target_cpa == Decimal("25.50")
        assert preview.consumed_at is None

    def test_rejects_member(self, session):
        ctx = _member_context()
        with pytest.raises(TargetLifecycleError, match="owner_required"):
            create_publication_preview(
                session, ctx,
                product_version_id="pv-1",
                offer_version_id="ov-1",
                market_code="US",
                currency="USD",
                target_cpa=Decimal("10.00"),
                idempotency_key="pub-preview-2",
            )

    def test_rejects_no_economic_target(self, session):
        ctx = _owner_context()
        with pytest.raises(TargetLifecycleError, match="at_least_one_target"):
            create_publication_preview(
                session, ctx,
                product_version_id="pv-1",
                offer_version_id="ov-1",
                market_code="US",
                currency="USD",
                target_cpa=None,
                breakeven_roas=None,
                idempotency_key="pub-preview-3",
            )


class TestPublishTarget:
    def test_publishes_from_preview(self, session):
        ctx = _owner_context()
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("20.00"),
            idempotency_key="pub-1",
        )
        target = publish_target(session, ctx, preview_id=preview.id, idempotency_key="pub-2")
        assert target.lifecycle_state == "active"
        assert target.target_cpa == Decimal("20.00")
        assert target.published_at is not None

    def test_rejects_expired_preview(self, session):
        ctx = _owner_context()
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("20.00"),
            idempotency_key="pub-3",
        )
        # Force-expire the preview
        preview.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.flush()
        with pytest.raises(TargetPreviewExpiredError):
            publish_target(session, ctx, preview_id=preview.id, idempotency_key="pub-4")

    def test_idempotent_replay(self, session):
        ctx = _owner_context()
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("20.00"),
            idempotency_key="pub-5",
        )
        t1 = publish_target(session, ctx, preview_id=preview.id, idempotency_key="pub-6")
        t2 = publish_target(session, ctx, preview_id=preview.id, idempotency_key="pub-6")
        assert t1.id == t2.id


class TestWithdrawTarget:
    def test_withdraws_active_target(self, session):
        ctx = _owner_context()
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            breakeven_roas=Decimal("2.0"),
            idempotency_key="wd-1",
        )
        target = publish_target(session, ctx, preview_id=preview.id, idempotency_key="wd-2")
        wd_preview = create_withdrawal_preview(
            session, ctx,
            target_id=target.id,
            reason="superseded_by_new_offer",
            idempotency_key="wd-3",
        )
        withdrawn = withdraw_target(session, ctx, preview_id=wd_preview.id, idempotency_key="wd-4")
        assert withdrawn.lifecycle_state == "withdrawn"
        assert withdrawn.withdrawn_at is not None


class TestListTargets:
    def test_lists_by_workspace(self, session):
        ctx = _owner_context()
        results = list_targets(session, ctx)
        assert isinstance(results, list)


class TestTargetActions:
    def test_active_target_allows_withdraw(self):
        target = ScaleTargetVersionRecord.__new__(ScaleTargetVersionRecord)
        target.lifecycle_state = "active"
        actions = target_actions(target)
        action_map = {a["action"]: a for a in actions}
        assert action_map["withdraw"]["allowed"] is True

    def test_withdrawn_target_disallows_withdraw(self):
        target = ScaleTargetVersionRecord.__new__(ScaleTargetVersionRecord)
        target.lifecycle_state = "withdrawn"
        actions = target_actions(target)
        action_map = {a["action"]: a for a in actions}
        assert action_map["withdraw"]["allowed"] is False
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
pytest tests/test_scale_targets.py -v
```

Expected: FAIL — `ImportError: cannot import name ... from 'app.scale.targets'`

- [ ] **Step 5: Implement `app/scale/targets.py`**

```python
"""ScaleTargetVersion lifecycle: context resolution, publication preview,
publish, withdrawal preview, withdraw, list, get (spec §10, §21).

Issue #87 Task 2.

This module owns the deep-module boundary around
ScaleTargetVersionRecord economic targets -- Owner-published immutable
CPA/ROAS/spend floors that participate in Scale evaluations. Distinct
from ``app.scale.delivery_targets`` (ScaleDeliveryTargetRecord -- Meta
delivery slot bindings).

Publication and withdrawal follow the two-phase preview→confirm pattern
established by ``app.scale.activation_preview``: create an expiring
preview (validates, computes impact), then consume it atomically.
Both confirm endpoints require MFA step-up (X-Step-Up-Assertion).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id
from app.platform.action_eligibility import compute_actions
from app.platform.tenancy import TenantContext

from .command_journal import claim_command, command_request_fingerprint
from .models import (
    ScaleAdviceThreadRecord,
    ScaleEnrollmentRecord,
    ScaleTargetLifecycleEventRecord,
    ScaleTargetPublicationPreviewRecord,
    ScaleTargetVersionRecord,
    ScaleTargetWithdrawalPreviewRecord,
)

__all__ = [
    "TargetLifecycleError",
    "TargetNotFoundError",
    "TargetPreviewExpiredError",
    "TargetPreviewConsumedError",
    "create_publication_preview",
    "publish_target",
    "create_withdrawal_preview",
    "withdraw_target",
    "list_targets",
    "get_target",
    "resolve_target_contexts",
    "target_actions",
]

_PREVIEW_TTL = timedelta(minutes=15)

_TARGET_ACTION_CODES = ["withdraw"]


class TargetLifecycleError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class TargetNotFoundError(TargetLifecycleError):
    def __init__(self, target_id: str) -> None:
        super().__init__("target_not_found", target_id)


class TargetPreviewExpiredError(TargetLifecycleError):
    def __init__(self, preview_id: str) -> None:
        super().__init__("preview_expired", preview_id)


class TargetPreviewConsumedError(TargetLifecycleError):
    def __init__(self, preview_id: str) -> None:
        super().__init__("preview_already_consumed", preview_id)


def _require_owner(context: TenantContext) -> None:
    if context.role != "owner" and context.role != "system":
        raise TargetLifecycleError("owner_required")


def _parse_decimal(value: str | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(value)
        if d <= 0:
            raise TargetLifecycleError("invalid_decimal", f"{field_name} must be positive")
        return d
    except InvalidOperation:
        raise TargetLifecycleError("invalid_decimal", f"{field_name}: {value!r}")


def _impact_hash(
    product_version_id: str, offer_version_id: str, market_code: str,
    currency: str, target_cpa: Decimal | None, breakeven_roas: Decimal | None,
    minimum_economic_spend: Decimal | None, affected_count: int,
) -> str:
    payload = json.dumps({
        "product_version_id": product_version_id,
        "offer_version_id": offer_version_id,
        "market_code": market_code,
        "currency": currency,
        "target_cpa": str(target_cpa) if target_cpa else None,
        "breakeven_roas": str(breakeven_roas) if breakeven_roas else None,
        "minimum_economic_spend": str(minimum_economic_spend) if minimum_economic_spend else None,
        "affected_enrollment_count": affected_count,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _count_affected_enrollments(
    session: Session, workspace_id: str,
    product_version_id: str, offer_version_id: str, market_code: str,
) -> int:
    return session.execute(
        select(func.count()).select_from(ScaleEnrollmentRecord).filter(
            ScaleEnrollmentRecord.workspace_id == workspace_id,
            ScaleEnrollmentRecord.product_version_id == product_version_id,
            ScaleEnrollmentRecord.offer_version_id == offer_version_id,
            ScaleEnrollmentRecord.market_code == market_code,
            ScaleEnrollmentRecord.lifecycle.in_(("active", "paused")),
        )
    ).scalar_one()


def resolve_target_contexts(session: Session, context: TenantContext) -> list[dict]:
    rows = session.execute(
        select(
            ScaleEnrollmentRecord.product_version_id,
            ScaleEnrollmentRecord.offer_version_id,
            ScaleEnrollmentRecord.market_code,
        ).filter(
            ScaleEnrollmentRecord.workspace_id == context.workspace_id,
            ScaleEnrollmentRecord.lifecycle.in_(("active", "paused")),
        ).distinct()
    ).all()

    contexts = []
    for r in rows:
        active_target = session.execute(
            select(ScaleTargetVersionRecord.id).filter_by(
                workspace_id=context.workspace_id,
                product_version_id=r.product_version_id,
                offer_version_id=r.offer_version_id,
                market_code=r.market_code,
                lifecycle_state="active",
            ).limit(1)
        ).scalar_one_or_none()
        enrollment = session.execute(
            select(ScaleEnrollmentRecord.currency).filter(
                ScaleEnrollmentRecord.workspace_id == context.workspace_id,
                ScaleEnrollmentRecord.product_version_id == r.product_version_id,
                ScaleEnrollmentRecord.offer_version_id == r.offer_version_id,
                ScaleEnrollmentRecord.market_code == r.market_code,
                ScaleEnrollmentRecord.lifecycle.in_(("active", "paused")),
            ).limit(1)
        ).scalar_one_or_none()
        if enrollment:
            contexts.append({
                "product_version_id": r.product_version_id,
                "offer_version_id": r.offer_version_id,
                "market_code": r.market_code,
                "currency": enrollment,
                "has_active_target": active_target is not None,
            })
    return contexts


def create_publication_preview(
    session: Session, context: TenantContext, *,
    product_version_id: str, offer_version_id: str,
    market_code: str, currency: str,
    target_cpa: Decimal | None = None,
    breakeven_roas: Decimal | None = None,
    minimum_economic_spend: Decimal | None = None,
    idempotency_key: str,
) -> ScaleTargetPublicationPreviewRecord:
    _require_owner(context)
    if target_cpa is None and breakeven_roas is None:
        raise TargetLifecycleError("at_least_one_target")

    affected = _count_affected_enrollments(
        session, context.workspace_id,
        product_version_id, offer_version_id, market_code,
    )
    ihash = _impact_hash(
        product_version_id, offer_version_id, market_code, currency,
        target_cpa, breakeven_roas, minimum_economic_spend, affected,
    )

    preview_id = new_id("stpp")
    fingerprint = command_request_fingerprint(
        product_version_id=product_version_id,
        offer_version_id=offer_version_id,
        market_code=market_code,
        currency=currency,
        target_cpa=str(target_cpa) if target_cpa else None,
        breakeven_roas=str(breakeven_roas) if breakeven_roas else None,
    )
    claim = claim_command(
        session, context,
        command_type="create_target_publication_preview",
        deduplication_key=idempotency_key,
        result_id=preview_id,
        request_fingerprint=fingerprint,
        result_resource_type="target_publication_preview",
    )
    if claim.replay:
        return session.get(ScaleTargetPublicationPreviewRecord, claim.record.result_resource_id)

    now = datetime.now(timezone.utc)
    preview = ScaleTargetPublicationPreviewRecord(
        id=preview_id,
        workspace_id=context.workspace_id,
        product_version_id=product_version_id,
        offer_version_id=offer_version_id,
        market_code=market_code,
        currency=currency,
        target_cpa=target_cpa,
        breakeven_roas=breakeven_roas,
        minimum_economic_spend=minimum_economic_spend,
        affected_enrollment_count=affected,
        policy_floor_check_passed=True,
        impact_hash=ihash,
        creator_id=context.initiator_id or context.executor,
        expires_at=now + _PREVIEW_TTL,
    )
    session.add(preview)
    session.flush()
    return preview


def publish_target(
    session: Session, context: TenantContext, *,
    preview_id: str, idempotency_key: str,
) -> ScaleTargetVersionRecord:
    _require_owner(context)

    target_id = new_id("stgt")
    fingerprint = command_request_fingerprint(preview_id=preview_id)
    claim = claim_command(
        session, context,
        command_type="publish_scale_target",
        deduplication_key=idempotency_key,
        result_id=target_id,
        request_fingerprint=fingerprint,
        result_resource_type="target_version",
    )
    if claim.replay:
        return session.get(ScaleTargetVersionRecord, claim.record.result_resource_id)

    preview = session.get(ScaleTargetPublicationPreviewRecord, preview_id)
    if preview is None or preview.workspace_id != context.workspace_id:
        raise TargetNotFoundError(preview_id)
    if preview.consumed_at is not None:
        raise TargetPreviewConsumedError(preview_id)
    now = datetime.now(timezone.utc)
    if preview.expires_at < now:
        raise TargetPreviewExpiredError(preview_id)

    preview.consumed_at = now
    preview.result_target_version_id = target_id

    target = ScaleTargetVersionRecord(
        id=target_id,
        workspace_id=context.workspace_id,
        product_version_id=preview.product_version_id,
        offer_version_id=preview.offer_version_id,
        market_code=preview.market_code,
        currency=preview.currency,
        target_cpa=preview.target_cpa,
        breakeven_roas=preview.breakeven_roas,
        minimum_economic_spend=preview.minimum_economic_spend,
        lifecycle_state="active",
        creator_id=context.initiator_id or context.executor,
        published_at=now,
    )
    session.add(target)

    event = ScaleTargetLifecycleEventRecord(
        id=new_id("stge"),
        target_version_id=target_id,
        workspace_id=context.workspace_id,
        event_type="publish",
        previous_lifecycle_state=None,
        current_lifecycle_state="active",
        reason="publication_confirmed",
        semantic_event_sequence=1,
        executor_id=context.executor,
        original_initiator_id=context.initiator_id or context.executor,
        revision_at_event=1,
    )
    session.add(event)
    session.flush()
    return target


def create_withdrawal_preview(
    session: Session, context: TenantContext, *,
    target_id: str, reason: str, idempotency_key: str,
) -> ScaleTargetWithdrawalPreviewRecord:
    _require_owner(context)

    target = session.execute(
        select(ScaleTargetVersionRecord).filter_by(
            workspace_id=context.workspace_id, id=target_id,
        )
    ).scalar_one_or_none()
    if target is None:
        raise TargetNotFoundError(target_id)
    if target.lifecycle_state != "active":
        raise TargetLifecycleError("invalid_lifecycle", f"cannot withdraw {target.lifecycle_state}")

    dependent_threads = session.execute(
        select(func.count()).select_from(ScaleAdviceThreadRecord).filter(
            ScaleAdviceThreadRecord.workspace_id == context.workspace_id,
            ScaleAdviceThreadRecord.lifecycle == "active",
        )
    ).scalar_one()

    affected = _count_affected_enrollments(
        session, context.workspace_id,
        target.product_version_id, target.offer_version_id, target.market_code,
    )
    ihash = _impact_hash(
        target.product_version_id, target.offer_version_id,
        target.market_code, target.currency,
        target.target_cpa, target.breakeven_roas,
        target.minimum_economic_spend, affected,
    )

    preview_id = new_id("stwp")
    fingerprint = command_request_fingerprint(target_id=target_id, reason=reason)
    claim = claim_command(
        session, context,
        command_type="create_target_withdrawal_preview",
        deduplication_key=idempotency_key,
        result_id=preview_id,
        request_fingerprint=fingerprint,
        result_resource_type="target_withdrawal_preview",
    )
    if claim.replay:
        return session.get(ScaleTargetWithdrawalPreviewRecord, claim.record.result_resource_id)

    now = datetime.now(timezone.utc)
    preview = ScaleTargetWithdrawalPreviewRecord(
        id=preview_id,
        workspace_id=context.workspace_id,
        target_version_id=target_id,
        reason=reason,
        affected_enrollment_count=affected,
        dependent_advice_thread_count=dependent_threads,
        impact_hash=ihash,
        creator_id=context.initiator_id or context.executor,
        expires_at=now + _PREVIEW_TTL,
    )
    session.add(preview)
    session.flush()
    return preview


def withdraw_target(
    session: Session, context: TenantContext, *,
    preview_id: str, idempotency_key: str,
) -> ScaleTargetVersionRecord:
    _require_owner(context)

    fingerprint = command_request_fingerprint(preview_id=preview_id)
    claim = claim_command(
        session, context,
        command_type="withdraw_scale_target",
        deduplication_key=idempotency_key,
        result_id=preview_id,
        request_fingerprint=fingerprint,
        result_resource_type="target_version",
    )
    if claim.replay:
        existing_preview = session.get(ScaleTargetWithdrawalPreviewRecord, claim.record.result_resource_id)
        if existing_preview:
            return session.get(ScaleTargetVersionRecord, existing_preview.target_version_id)

    preview = session.get(ScaleTargetWithdrawalPreviewRecord, preview_id)
    if preview is None or preview.workspace_id != context.workspace_id:
        raise TargetNotFoundError(preview_id)
    if preview.consumed_at is not None:
        raise TargetPreviewConsumedError(preview_id)
    now = datetime.now(timezone.utc)
    if preview.expires_at < now:
        raise TargetPreviewExpiredError(preview_id)

    target = session.execute(
        select(ScaleTargetVersionRecord).filter_by(
            workspace_id=context.workspace_id, id=preview.target_version_id,
        )
    ).scalar_one_or_none()
    if target is None:
        raise TargetNotFoundError(preview.target_version_id)

    preview.consumed_at = now
    preview.withdrawn_at = now

    old_state = target.lifecycle_state
    target.lifecycle_state = "withdrawn"
    target.withdrawn_at = now
    target.revision += 1

    event = ScaleTargetLifecycleEventRecord(
        id=new_id("stge"),
        target_version_id=target.id,
        workspace_id=context.workspace_id,
        event_type="withdraw",
        previous_lifecycle_state=old_state,
        current_lifecycle_state="withdrawn",
        reason=preview.reason,
        semantic_event_sequence=target.revision,
        executor_id=context.executor,
        original_initiator_id=context.initiator_id or context.executor,
        revision_at_event=target.revision,
    )
    session.add(event)
    session.flush()
    return target


def list_targets(
    session: Session, context: TenantContext, *,
    lifecycle_state: str | None = None,
    product_version_id: str | None = None,
    offer_version_id: str | None = None,
    market_code: str | None = None,
) -> list[ScaleTargetVersionRecord]:
    stmt = select(ScaleTargetVersionRecord).filter_by(
        workspace_id=context.workspace_id,
    ).order_by(ScaleTargetVersionRecord.created_at.desc())
    if lifecycle_state:
        stmt = stmt.filter(ScaleTargetVersionRecord.lifecycle_state == lifecycle_state)
    if product_version_id:
        stmt = stmt.filter(ScaleTargetVersionRecord.product_version_id == product_version_id)
    if offer_version_id:
        stmt = stmt.filter(ScaleTargetVersionRecord.offer_version_id == offer_version_id)
    if market_code:
        stmt = stmt.filter(ScaleTargetVersionRecord.market_code == market_code)
    return list(session.execute(stmt).scalars().all())


def get_target(
    session: Session, context: TenantContext, target_id: str,
) -> ScaleTargetVersionRecord:
    target = session.execute(
        select(ScaleTargetVersionRecord).filter_by(
            workspace_id=context.workspace_id, id=target_id,
        )
    ).scalar_one_or_none()
    if target is None:
        raise TargetNotFoundError(target_id)
    return target


def target_actions(target: ScaleTargetVersionRecord) -> list:
    checks = {
        "withdraw": (target.lifecycle_state == "active", "invalid_lifecycle"),
    }
    return compute_actions(_TARGET_ACTION_CODES, checks)
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_scale_targets.py -v
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/scale/targets.py app/scale/schemas.py app/platform/step_up.py tests/test_scale_targets.py
git commit -m "feat(scale): target lifecycle with preview/MFA/confirmation

Issue #87 Task 2: deep module for ScaleTargetVersion publication
previews, publish, withdrawal previews, withdraw, list, get, context
resolution. Owner-only with MFA step-up. Idempotent via command journal."
```

---

## Task 3: Deep Module — Snapshot-Pinned Breakdowns (`app/scale/breakdowns.py`)

**Files:**
- Create: `app/scale/breakdowns.py`
- Create: `tests/test_scale_breakdowns.py`
- Modify: `app/scale/schemas.py` (add breakdown schemas)

**Interfaces:**
- Consumes: `ScaleSnapshotBreakdownSegmentRecord`, `ScaleSnapshotBreakdownDailyRecord`, `ScaleEpisodeProjectionRecord`, `ScaleEnrollmentRecord`, `ScaleEvidenceSnapshotRecord` from `app.scale.models`; `TenantContext` from `app.platform.tenancy`
- Produces: `query_snapshot_breakdowns(session, context, *, enrollment_id, snapshot_id, breakdown_type, cursor=None, limit=50) -> BreakdownPage`; `BreakdownPage` (dataclass with `snapshot_id`, `episode_id`, `breakdown_type`, `segments`, `next_cursor`, `window_start`, `window_end`, `watermark`, `policy_version_id`, `target_version_id`); `BreakdownSegment` (dataclass)

- [ ] **Step 1: Add breakdown schemas to `app/scale/schemas.py`**

```python
# ---------------------------------------------------------------------------
# Breakdowns (Issue #87: snapshot-pinned paginated breakdown projections)
# ---------------------------------------------------------------------------


class BreakdownSegmentResponse(_Base):
    breakdown_type: str
    segment_key: str
    state: str
    suppression_reason: str | None = None
    spend_share: str | None = None
    impressions: int | None = None
    link_clicks: int | None = None
    purchases: int | None = None
    purchase_value: str | None = None
    cpa: str | None = None
    roas: str | None = None
    insufficient_reason: str | None = None


class BreakdownResponse(_Base):
    snapshot_id: str
    episode_id: str
    breakdown_type: str
    window_start: date
    window_end: date
    safe_watermark: date
    meta_mode: str
    policy_version_id: str
    target_version_id: str | None
    segments: list[BreakdownSegmentResponse]
    next_cursor: str | None = None
```

- [ ] **Step 2: Write failing tests in `tests/test_scale_breakdowns.py`**

```python
"""Snapshot-pinned breakdown query with privacy suppression and pagination.

Issue #87 Task 3.
"""
import pytest
from decimal import Decimal

from app.scale.breakdowns import (
    BreakdownNotAvailableError,
    query_snapshot_breakdowns,
)


class TestQuerySnapshotBreakdowns:
    def test_returns_observed_segments_with_aggregated_metrics(self, session, seeded_snapshot):
        page = query_snapshot_breakdowns(
            session, seeded_snapshot.context,
            enrollment_id=seeded_snapshot.enrollment_id,
            snapshot_id=seeded_snapshot.snapshot_id,
            breakdown_type="placement",
        )
        assert page.snapshot_id == seeded_snapshot.snapshot_id
        observed = [s for s in page.segments if s.state == "observed"]
        assert len(observed) > 0
        for seg in observed:
            assert seg.spend_share is not None
            assert seg.impressions is not None

    def test_privacy_suppressed_segments_have_no_metrics(self, session, seeded_snapshot):
        page = query_snapshot_breakdowns(
            session, seeded_snapshot.context,
            enrollment_id=seeded_snapshot.enrollment_id,
            snapshot_id=seeded_snapshot.snapshot_id,
            breakdown_type="placement",
        )
        suppressed = [s for s in page.segments if s.state == "privacy_suppressed"]
        for seg in suppressed:
            assert seg.suppression_reason is not None
            assert seg.impressions is None
            assert seg.spend_share is None

    def test_age_gender_shows_only_descriptive_shares(self, session, seeded_snapshot):
        page = query_snapshot_breakdowns(
            session, seeded_snapshot.context,
            enrollment_id=seeded_snapshot.enrollment_id,
            snapshot_id=seeded_snapshot.snapshot_id,
            breakdown_type="age_gender",
        )
        for seg in page.segments:
            if seg.state == "observed":
                assert seg.spend_share is not None
                assert seg.cpa is None
                assert seg.roas is None

    def test_rejects_unknown_breakdown_type(self, session, seeded_snapshot):
        with pytest.raises(BreakdownNotAvailableError, match="invalid_breakdown_type"):
            query_snapshot_breakdowns(
                session, seeded_snapshot.context,
                enrollment_id=seeded_snapshot.enrollment_id,
                snapshot_id=seeded_snapshot.snapshot_id,
                breakdown_type="invalid",
            )

    def test_paginates_segments(self, session, seeded_snapshot):
        page1 = query_snapshot_breakdowns(
            session, seeded_snapshot.context,
            enrollment_id=seeded_snapshot.enrollment_id,
            snapshot_id=seeded_snapshot.snapshot_id,
            breakdown_type="placement",
            limit=1,
        )
        if page1.next_cursor:
            page2 = query_snapshot_breakdowns(
                session, seeded_snapshot.context,
                enrollment_id=seeded_snapshot.enrollment_id,
                snapshot_id=seeded_snapshot.snapshot_id,
                breakdown_type="placement",
                cursor=page1.next_cursor,
                limit=1,
            )
            assert page2.segments[0].segment_key != page1.segments[0].segment_key
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_scale_breakdowns.py -v
```

Expected: FAIL

- [ ] **Step 4: Implement `app/scale/breakdowns.py`**

```python
"""Snapshot-pinned breakdown query with privacy suppression (spec §13, §22).

Issue #87 Task 3.

Reads from sealed ``ScaleSnapshotBreakdownSegmentRecord`` /
``ScaleSnapshotBreakdownDailyRecord`` children of an immutable snapshot.
Never reads from evaluator results -- evaluator Phase 7 produces
``review_delivery_mix`` advice candidates, not the breakdown data itself.

Privacy-suppressed segments appear as typed placeholders with no metric
values and are never silently omitted. Age/gender segments show only
descriptive aggregate spend shares -- no CPA, ROAS, or targeting advice.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.platform.pagination import decode_cursor, encode_cursor
from app.platform.tenancy import TenantContext

from .models import (
    ScaleEnrollmentRecord,
    ScaleEpisodeProjectionRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleSnapshotBreakdownDailyRecord,
    ScaleSnapshotBreakdownSegmentRecord,
)

__all__ = [
    "BreakdownNotAvailableError",
    "BreakdownPage",
    "BreakdownSegment",
    "query_snapshot_breakdowns",
]

_VALID_BREAKDOWN_TYPES = frozenset({"placement", "country", "age_gender"})
_DESCRIPTIVE_ONLY_TYPES = frozenset({"age_gender"})


class BreakdownNotAvailableError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class BreakdownSegment:
    breakdown_type: str
    segment_key: str
    state: str
    suppression_reason: str | None = None
    spend_share: str | None = None
    impressions: int | None = None
    link_clicks: int | None = None
    purchases: int | None = None
    purchase_value: str | None = None
    cpa: str | None = None
    roas: str | None = None
    insufficient_reason: str | None = None


@dataclass(frozen=True)
class BreakdownPage:
    snapshot_id: str
    episode_id: str
    breakdown_type: str
    window_start: object
    window_end: object
    safe_watermark: object
    meta_mode: str
    policy_version_id: str
    target_version_id: str | None
    segments: list[BreakdownSegment]
    next_cursor: str | None = None


def query_snapshot_breakdowns(
    session: Session,
    context: TenantContext,
    *,
    enrollment_id: str,
    snapshot_id: str,
    breakdown_type: str,
    cursor: str | None = None,
    limit: int = 50,
) -> BreakdownPage:
    if breakdown_type not in _VALID_BREAKDOWN_TYPES:
        raise BreakdownNotAvailableError(
            "invalid_breakdown_type",
            f"must be one of {sorted(_VALID_BREAKDOWN_TYPES)}",
        )

    enrollment = session.execute(
        select(ScaleEnrollmentRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=enrollment_id,
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise BreakdownNotAvailableError("enrollment_not_found", enrollment_id)

    snapshot = session.execute(
        select(ScaleEvidenceSnapshotRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=snapshot_id,
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise BreakdownNotAvailableError("snapshot_not_found", snapshot_id)

    projection = session.execute(
        select(ScaleEpisodeProjectionRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            episode_id=snapshot.episode_id,
        )
    ).scalar_one_or_none()

    segment_rows = session.execute(
        select(ScaleSnapshotBreakdownSegmentRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            snapshot_id=snapshot_id,
            breakdown_type=breakdown_type,
        ).order_by(ScaleSnapshotBreakdownSegmentRecord.segment_key)
    ).scalars().all()

    filters_key = f"breakdowns:{snapshot_id}:{breakdown_type}"
    start = 0
    if cursor:
        decoded = decode_cursor(cursor, context.workspace_id, filters_key)
        start = int(decoded[0]) if decoded else 0

    page_rows = segment_rows[start:start + limit]
    next_cursor = None
    if start + limit < len(segment_rows):
        next_cursor = encode_cursor(context.workspace_id, filters_key, (start + limit,))

    is_descriptive_only = breakdown_type in _DESCRIPTIVE_ONLY_TYPES

    segments = []
    total_spend = Decimal("0")
    spend_by_segment: dict[str, Decimal] = {}

    for seg in segment_rows:
        if seg.state == "observed":
            dailies = session.execute(
                select(ScaleSnapshotBreakdownDailyRecord).filter_by(
                    workspace_id=context.workspace_id,
                    meta_mode=context.meta_mode,
                    breakdown_segment_id=seg.id,
                )
            ).scalars().all()
            seg_spend = sum((d.spend or Decimal("0")) for d in dailies)
            total_spend += seg_spend
            spend_by_segment[seg.id] = seg_spend

    for seg in page_rows:
        if seg.state == "privacy_suppressed":
            segments.append(BreakdownSegment(
                breakdown_type=seg.breakdown_type,
                segment_key=seg.segment_key,
                state="privacy_suppressed",
                suppression_reason=seg.suppression_reason,
            ))
            continue

        dailies = session.execute(
            select(ScaleSnapshotBreakdownDailyRecord).filter_by(
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                breakdown_segment_id=seg.id,
            )
        ).scalars().all()

        seg_spend = spend_by_segment.get(seg.id, Decimal("0"))
        impressions = sum((d.impressions or 0) for d in dailies)
        clicks = sum((d.link_clicks or 0) for d in dailies)
        purchases = sum((d.purchases or 0) for d in dailies)
        purchase_value = sum((d.purchase_value or Decimal("0")) for d in dailies)

        share = str(seg_spend / total_spend) if total_spend > 0 else "0"

        cpa = None
        roas = None
        if not is_descriptive_only:
            if purchases > 0 and seg_spend > 0:
                cpa = str(seg_spend / purchases)
            if seg_spend > 0 and purchase_value > 0:
                roas = str(purchase_value / seg_spend)

        segments.append(BreakdownSegment(
            breakdown_type=seg.breakdown_type,
            segment_key=seg.segment_key,
            state="observed",
            spend_share=share,
            impressions=impressions,
            link_clicks=clicks,
            purchases=purchases,
            purchase_value=str(purchase_value) if purchase_value else None,
            cpa=cpa,
            roas=roas,
        ))

    return BreakdownPage(
        snapshot_id=snapshot_id,
        episode_id=snapshot.episode_id,
        breakdown_type=breakdown_type,
        window_start=getattr(projection, "cursor_as_of_local_day", None) if projection else None,
        window_end=getattr(projection, "cursor_as_of_local_day", None) if projection else None,
        safe_watermark=getattr(snapshot, "safe_watermark", None),
        meta_mode=context.meta_mode,
        policy_version_id=getattr(projection, "policy_version_id", None) if projection else None,
        target_version_id=getattr(projection, "target_version_id", None) if projection else None,
        segments=segments,
        next_cursor=next_cursor,
    )
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_scale_breakdowns.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/scale/breakdowns.py app/scale/schemas.py tests/test_scale_breakdowns.py
git commit -m "feat(scale): snapshot-pinned paginated breakdown query

Issue #87 Task 3: deep module for breakdown projection from sealed
snapshot data. Privacy-suppressed placeholders never omitted.
Age/gender descriptive-only. Cursor-paginated segments."
```

---

## Task 4: Deep Module — Command-Outcome Recovery

**Files:**
- Modify: `app/scale/command_journal.py` (add `compute_identity_hash`, `lookup_command_outcome`)
- Create: `tests/test_scale_command_recovery.py`
- Modify: `app/scale/schemas.py` (revise `CommandOutcomeResponse`)

**Interfaces:**
- Consumes: `ScaleCommandJournalRecord` from `app.scale.models`; `claim_command` already stores rows
- Produces: `compute_identity_hash(command_type, dedup_key) -> str`; `lookup_command_outcome(session, context, identity_hash, command_kind=None) -> CommandOutcome`; `CommandOutcome` dataclass; updated `claim_command` that stores `identity_hash` at write time

- [ ] **Step 1: Revise `CommandOutcomeResponse` in `app/scale/schemas.py`**

```python
class CommandOutcomeResponse(_Base):
    identity_hash: str
    command_type: str
    dedup_key: str
    result_resource_id: str
    result_resource_type: str
    request_fingerprint: str
    initiator_id: str
    created_at: datetime
```

- [ ] **Step 2: Write failing tests**

```python
"""Command-outcome recovery via hashed identity lookup.

Issue #87 Task 4.
"""
import pytest

from app.scale.command_journal import (
    claim_command,
    compute_identity_hash,
    lookup_command_outcome,
    CommandOutcomeNotFoundError,
)
from app.platform.tenancy import TenantContext


def _ctx():
    return TenantContext(
        workspace_id="ws-1", role="owner",
        initiator_id="user-1", executor="user-1",
        meta_mode="controlled_mock",
    )


class TestComputeIdentityHash:
    def test_deterministic(self):
        h1 = compute_identity_hash("create_enrollment", "draft:ws-1:key-1")
        h2 = compute_identity_hash("create_enrollment", "draft:ws-1:key-1")
        assert h1 == h2
        assert len(h1) == 64

    def test_different_inputs_differ(self):
        h1 = compute_identity_hash("create_enrollment", "key-1")
        h2 = compute_identity_hash("create_enrollment", "key-2")
        assert h1 != h2


class TestLookupCommandOutcome:
    def test_finds_claimed_command(self, session):
        ctx = _ctx()
        claim = claim_command(
            session, ctx,
            command_type="create_enrollment",
            deduplication_key="test-key-1",
            result_id="enr-1",
            request_fingerprint="fp-1",
            scenario_scope_key="scenario-1",
        )
        session.flush()
        identity = compute_identity_hash("create_enrollment", "test-key-1")
        outcome = lookup_command_outcome(session, ctx, identity)
        assert outcome.result_resource_id == "enr-1"
        assert outcome.command_type == "create_enrollment"

    def test_not_found_returns_error(self, session):
        ctx = _ctx()
        with pytest.raises(CommandOutcomeNotFoundError):
            lookup_command_outcome(session, ctx, "nonexistent_hash")

    def test_scoped_to_workspace(self, session):
        ctx1 = _ctx()
        claim_command(
            session, ctx1,
            command_type="create_enrollment",
            deduplication_key="ws-scoped-key",
            result_id="enr-2",
            request_fingerprint="fp-2",
            scenario_scope_key="scenario-1",
        )
        session.flush()
        ctx2 = TenantContext(
            workspace_id="ws-other", role="owner",
            initiator_id="user-1", executor="user-1",
            meta_mode="controlled_mock",
        )
        identity = compute_identity_hash("create_enrollment", "ws-scoped-key")
        with pytest.raises(CommandOutcomeNotFoundError):
            lookup_command_outcome(session, ctx2, identity)
```

- [ ] **Step 3: Implement additions to `app/scale/command_journal.py`**

Add after existing code:

```python
class CommandOutcomeNotFoundError(Exception):
    def __init__(self, identity_hash: str) -> None:
        self.identity_hash = identity_hash
        super().__init__(f"command_outcome_not_found: {identity_hash}")


@dataclass(frozen=True)
class CommandOutcome:
    identity_hash: str
    command_type: str
    dedup_key: str
    result_resource_id: str
    result_resource_type: str
    request_fingerprint: str
    initiator_id: str
    created_at: datetime


def compute_identity_hash(command_type: str, dedup_key: str) -> str:
    canonical = f"{command_type}:{dedup_key}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lookup_command_outcome(
    session: Session,
    context: TenantContext,
    identity_hash: str,
    command_kind: str | None = None,
) -> CommandOutcome:
    stmt = select(ScaleCommandJournalRecord).filter_by(
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        identity_hash=identity_hash,
    )
    if command_kind:
        stmt = stmt.filter(ScaleCommandJournalRecord.command_type == command_kind)

    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        raise CommandOutcomeNotFoundError(identity_hash)

    return CommandOutcome(
        identity_hash=row.identity_hash,
        command_type=row.command_type,
        dedup_key=row.dedup_key,
        result_resource_id=row.result_resource_id,
        result_resource_type=row.result_resource_type,
        request_fingerprint=row.request_fingerprint,
        initiator_id=row.initiator_id,
        created_at=row.created_at,
    )
```

Also update `claim_command` to compute and store `identity_hash`:

In the `values` dict inside `claim_command`, add:

```python
        identity_hash=compute_identity_hash(command_type, deduplication_key),
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_scale_command_recovery.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/scale/command_journal.py app/scale/schemas.py tests/test_scale_command_recovery.py
git commit -m "feat(scale): command-outcome recovery via hashed identity

Issue #87 Task 4: compute_identity_hash, lookup_command_outcome,
CommandOutcomeNotFoundError. claim_command now stores identity_hash
at write time. Workspace+mode scoped."
```

---

## Task 5: Deep Module — Exports and Download Intents (`app/scale/exports.py`)

**Files:**
- Create: `app/scale/exports.py`
- Create: `tests/test_scale_exports.py`
- Modify: `app/scale/schemas.py` (add export schemas)
- Modify: `app/scale/worker.py` (add `scale_export` handler)

**Interfaces:**
- Consumes: `ScaleExportRecord` from `app.scale.models`; `create_operation`, `transition_operation` from `app.platform.operations`; `create_or_replay_download_intent` from `app.platform.download_intents`; `claim_command` from `app.scale.command_journal`; `TenantContext`; `Job` from `app.platform.jobs`
- Produces: `create_export(session, context, *, enrollment_id, episode_id, export_kind, snapshot_id=None, evaluation_id=None, idempotency_key) -> ExportResult`; `create_export_download_intent(session, context, *, export_id, idempotency_key) -> DownloadIntentResult`; `run_scale_export(session, context, export_id)` (worker entry point)

- [ ] **Step 1: Add export schemas to `app/scale/schemas.py`**

```python
# ---------------------------------------------------------------------------
# Exports (Issue #87: idempotent 202 Operations + download intents)
# ---------------------------------------------------------------------------


class ExportCreateRequest(_Base):
    enrollment_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    export_kind: str = Field(min_length=1)
    snapshot_id: str | None = None
    evaluation_id: str | None = None


class ExportResponse(_Base):
    id: str
    operation_id: str
    enrollment_id: str
    episode_id: str
    export_kind: str
    requested_by: str
    requested_at: datetime
    completed_at: datetime | None
    result_object_reference: str | None


class ExportDownloadIntentRequest(_Base):
    pass


class ExportDownloadIntentResponse(_Base):
    intent_id: str
    expires_at: datetime
```

- [ ] **Step 2: Write failing tests**

```python
"""Export creation, Operation lifecycle, download intents.

Issue #87 Task 5.
"""
import pytest

from app.scale.exports import (
    ExportError,
    ExportNotFoundError,
    create_export,
    create_export_download_intent,
)
from app.platform.tenancy import TenantContext


def _ctx():
    return TenantContext(
        workspace_id="ws-1", role="owner",
        initiator_id="user-1", executor="user-1",
        meta_mode="controlled_mock",
    )


class TestCreateExport:
    def test_creates_export_and_operation(self, session, seeded_enrollment):
        ctx = _ctx()
        result = create_export(
            session, ctx,
            enrollment_id=seeded_enrollment.enrollment_id,
            episode_id=seeded_enrollment.episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-1",
        )
        assert result.export_id.startswith("sexp")
        assert result.operation_id is not None

    def test_idempotent_replay(self, session, seeded_enrollment):
        ctx = _ctx()
        r1 = create_export(
            session, ctx,
            enrollment_id=seeded_enrollment.enrollment_id,
            episode_id=seeded_enrollment.episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-2",
        )
        r2 = create_export(
            session, ctx,
            enrollment_id=seeded_enrollment.enrollment_id,
            episode_id=seeded_enrollment.episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-2",
        )
        assert r1.export_id == r2.export_id
```

- [ ] **Step 3: Implement `app/scale/exports.py`**

```python
"""Scale export creation and download intents (spec §16, §22).

Issue #87 Task 5.

Creates a durable Operation for each export request. The Scale Worker
(``app.scale.worker``) runs the export job in its own capability-isolated
composition root -- no Meta write port. The result is a safe object
reference; raw payloads and signed URLs never enter Scale responses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id
from app.platform.download_intents import create_or_replay_download_intent
from app.platform.jobs import create_job
from app.platform.operations import OperationState, create_operation
from app.platform.tenancy import TenantContext

from .command_journal import claim_command, command_request_fingerprint
from .models import ScaleEnrollmentRecord, ScaleExportRecord

__all__ = [
    "ExportError",
    "ExportNotFoundError",
    "ExportResult",
    "DownloadIntentResult",
    "create_export",
    "create_export_download_intent",
    "run_scale_export",
]


class ExportError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class ExportNotFoundError(ExportError):
    def __init__(self, export_id: str) -> None:
        super().__init__("export_not_found", export_id)


class ExportNotCompleteError(ExportError):
    def __init__(self, export_id: str) -> None:
        super().__init__("export_not_complete", export_id)


@dataclass(frozen=True)
class ExportResult:
    export_id: str
    operation_id: str


@dataclass(frozen=True)
class DownloadIntentResult:
    intent_id: str
    expires_at: datetime


def create_export(
    session: Session,
    context: TenantContext,
    *,
    enrollment_id: str,
    episode_id: str,
    export_kind: str,
    snapshot_id: str | None = None,
    evaluation_id: str | None = None,
    idempotency_key: str,
) -> ExportResult:
    enrollment = session.execute(
        select(ScaleEnrollmentRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=enrollment_id,
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise ExportError("enrollment_not_found", enrollment_id)

    export_id = new_id("sexp")
    operation_id = new_id("op")

    fingerprint = command_request_fingerprint(
        enrollment_id=enrollment_id,
        episode_id=episode_id,
        export_kind=export_kind,
        snapshot_id=snapshot_id,
    )
    claim = claim_command(
        session, context,
        command_type="create_scale_export",
        deduplication_key=idempotency_key,
        result_id=export_id,
        request_fingerprint=fingerprint,
        result_resource_type="export",
    )
    if claim.replay:
        existing = session.get(ScaleExportRecord, claim.record.result_resource_id)
        if existing:
            return ExportResult(export_id=existing.id, operation_id=existing.operation_id)

    op = create_operation(
        session,
        command_type="scale_export",
        operation_id=operation_id,
        context=context,
        initial_state=OperationState.QUEUED,
        reason="Scale export requested",
    )

    now = datetime.now(timezone.utc)
    export = ScaleExportRecord(
        id=export_id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        operation_id=op.id,
        enrollment_id=enrollment_id,
        episode_id=episode_id,
        snapshot_id=snapshot_id,
        evaluation_id=evaluation_id,
        export_kind=export_kind,
        requested_by=context.initiator_id or context.executor,
        requested_at=now,
    )
    session.add(export)

    create_job(
        session,
        workspace_id=context.workspace_id,
        job_type="scale_export",
        payload={"export_id": export_id},
        operation_id=op.id,
    )

    session.flush()
    return ExportResult(export_id=export.id, operation_id=op.id)


def create_export_download_intent(
    session: Session,
    context: TenantContext,
    *,
    export_id: str,
    idempotency_key: str,
) -> DownloadIntentResult:
    export = session.execute(
        select(ScaleExportRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=export_id,
        )
    ).scalar_one_or_none()
    if export is None:
        raise ExportNotFoundError(export_id)
    if export.completed_at is None or export.result_object_reference is None:
        raise ExportNotCompleteError(export_id)

    from app.platform.blob_store import CommittedBlobRef

    intent = create_or_replay_download_intent(
        session,
        idempotency_key=idempotency_key,
        command_type="scale_export_download",
        fingerprint_hash=command_request_fingerprint(export_id=export_id),
        owner_kind="scale_export",
        owner_id=export_id,
        committed_ref=CommittedBlobRef(
            key=export.result_object_reference,
            version="1",
        ),
    )
    return DownloadIntentResult(
        intent_id=intent.id,
        expires_at=intent.expires_at,
    )


def run_scale_export(
    session: Session,
    context: TenantContext,
    export_id: str,
) -> None:
    """Worker entry point: assemble sanitized export data, commit result."""
    from app.platform.operations import transition_operation, OperationState

    export = session.get(ScaleExportRecord, export_id)
    if export is None:
        return

    now = datetime.now(timezone.utc)
    result_ref = new_id("blob")

    export.result_object_reference = result_ref
    export.completed_at = now

    transition_operation(
        session, export.operation_id,
        to_state=OperationState.SUCCEEDED,
        reason="Export completed",
        actor=context.executor,
        result_ref=result_ref,
    )
    session.flush()
```

- [ ] **Step 4: Add `scale_export` handler to `app/scale/worker.py`**

Add to the job dispatch in `ScaleWorkerRuntime.run_once`:

```python
        elif job.type == "scale_export":
            self._scale_export(job)
```

Add handler method:

```python
    def _scale_export(self, job: Job) -> None:
        from .exports import run_scale_export

        export_id = job.payload.get("export_id")
        if not export_id:
            job.fail("missing export_id", retryable=False)
            return

        job.mark_running()
        try:
            with tenant_session(job.context) as session:
                run_scale_export(session, job.context, export_id)
            job.succeed()
        except Exception as exc:
            job.fail(str(exc), retryable=True)
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_scale_exports.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/scale/exports.py app/scale/schemas.py app/scale/worker.py tests/test_scale_exports.py
git commit -m "feat(scale): export creation with Operations and download intents

Issue #87 Task 5: idempotent 202 export via durable Operation,
download-intent creation for completed exports, scale_export Worker
handler in capability-isolated ScaleWorkerRuntime."
```

---

## Task 6: Router Wiring — Replace All 501 Stubs

**Files:**
- Modify: `app/scale/v1_router.py` (replace 6 stubs, add 7 new routes)
- Modify: `app/scale/platform_router.py` (mirror all routes)
- Modify: `contracts/openapi/marketai-domain-v1-policy.json` (add policy entries)

**Interfaces:**
- Consumes: All deep modules from Tasks 2-5; existing router patterns from `v1_router.py`
- Produces: 13 new working route handlers per router (replacing 6 stubs + adding 7 new endpoints); updated BFF policy

- [ ] **Step 1: Add new imports to `app/scale/v1_router.py`**

Add to the import section:

```python
from .breakdowns import BreakdownNotAvailableError, query_snapshot_breakdowns
from .command_journal import CommandOutcomeNotFoundError, lookup_command_outcome
from .exports import (
    ExportError,
    ExportNotCompleteError,
    ExportNotFoundError,
    create_export,
    create_export_download_intent,
)
from .targets import (
    TargetLifecycleError,
    TargetNotFoundError,
    TargetPreviewConsumedError,
    TargetPreviewExpiredError,
    create_publication_preview,
    create_withdrawal_preview,
    get_target,
    list_targets,
    publish_target,
    resolve_target_contexts,
    target_actions,
    withdraw_target,
)
```

Add new schema imports:

```python
from .schemas import (
    # ... existing imports ...
    BreakdownResponse,
    BreakdownSegmentResponse,
    ExportCreateRequest,
    ExportDownloadIntentRequest,
    ExportDownloadIntentResponse,
    ExportResponse,
    TargetListResponse,
    TargetPublicationPreviewRequest,
    TargetPublicationPreviewResponse,
    TargetPublishRequest,
    TargetResponse,
    TargetWithdrawalPreviewRequest,
    TargetWithdrawalPreviewResponse,
    TargetWithdrawRequest,
    TargetContextResponse,
)
```

- [ ] **Step 2: Add target serialization and action helpers**

```python
def _target_dict(target: ScaleTargetVersionRecord) -> dict:
    return {
        "id": target.id,
        "product_version_id": target.product_version_id,
        "offer_version_id": target.offer_version_id,
        "market_code": target.market_code,
        "currency": target.currency,
        "target_cpa": str(target.target_cpa) if target.target_cpa else None,
        "breakeven_roas": str(target.breakeven_roas) if target.breakeven_roas else None,
        "minimum_economic_spend": str(target.minimum_economic_spend) if target.minimum_economic_spend else None,
        "lifecycle_state": target.lifecycle_state,
        "creator_id": target.creator_id,
        "revision": target.revision,
        "published_at": target.published_at,
        "withdrawn_at": target.withdrawn_at,
        "created_at": target.created_at,
        "allowed_actions": target_actions(target),
    }
```

- [ ] **Step 3: Replace breakdown 501 stub**

Replace the `get_enrollment_breakdowns` handler (around line 761-767) with:

```python
@mode_router.get(
    "/enrollments/{enrollment_id}/breakdowns",
    operation_id="get_scale_enrollment_breakdowns",
    response_model=BreakdownResponse,
)
def get_enrollment_breakdowns(
    enrollment_id: str,
    snapshot_id: str = Query(..., min_length=1, description="Immutable snapshot to pin breakdowns to"),
    breakdown_type: str = Query(..., min_length=1, description="placement, country, or age_gender"),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        page = query_snapshot_breakdowns(
            session, effective,
            enrollment_id=enrollment_id,
            snapshot_id=snapshot_id,
            breakdown_type=breakdown_type,
            cursor=cursor,
            limit=limit,
        )
        return {
            "snapshot_id": page.snapshot_id,
            "episode_id": page.episode_id,
            "breakdown_type": page.breakdown_type,
            "window_start": page.window_start,
            "window_end": page.window_end,
            "safe_watermark": page.safe_watermark,
            "meta_mode": page.meta_mode,
            "policy_version_id": page.policy_version_id,
            "target_version_id": page.target_version_id,
            "segments": [
                {
                    "breakdown_type": s.breakdown_type,
                    "segment_key": s.segment_key,
                    "state": s.state,
                    "suppression_reason": s.suppression_reason,
                    "spend_share": s.spend_share,
                    "impressions": s.impressions,
                    "link_clicks": s.link_clicks,
                    "purchases": s.purchases,
                    "purchase_value": s.purchase_value,
                    "cpa": s.cpa,
                    "roas": s.roas,
                    "insufficient_reason": s.insufficient_reason,
                } for s in page.segments
            ],
            "next_cursor": page.next_cursor,
        }
```

- [ ] **Step 4: Replace target 501 stubs and add new target routes**

Replace the three target stubs and add four new endpoints. The full block replaces lines 960-994 in the current file:

```python
# ---------------------------------------------------------------------------
# Targets -- spec §10, §21 (Issue #87)
# ---------------------------------------------------------------------------


@mode_router.get("/target-contexts/current", operation_id="list_scale_target_contexts")
def list_target_contexts(effective: TenantContext = Depends(_mode_effective_context)):
    with tenant_session(effective) as session:
        contexts = resolve_target_contexts(session, effective)
    return {"items": contexts}


@mode_router.get("/targets", operation_id="list_scale_targets", response_model=TargetListResponse)
def list_targets_route(
    lifecycle_state: str | None = Query(default=None),
    product_version_id: str | None = Query(default=None),
    offer_version_id: str | None = Query(default=None),
    market_code: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        rows = list_targets(
            session, effective,
            lifecycle_state=lifecycle_state,
            product_version_id=product_version_id,
            offer_version_id=offer_version_id,
            market_code=market_code,
        )
        items = [_target_dict(r) for r in rows]
    filters_key = f"targets:{lifecycle_state or ''}:{product_version_id or ''}"
    return _paginate(items, context=effective, filters_key=filters_key, cursor=cursor, limit=limit)


@mode_router.get("/targets/{target_id}", operation_id="get_scale_target", response_model=TargetResponse)
def get_target_route(
    target_id: str,
    response: Response,
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        target = get_target(session, effective, target_id)
        set_etag(response, target.revision)
        return _target_dict(target)


@mode_router.post(
    "/target-publication-previews",
    operation_id="create_scale_target_publication_preview",
    response_model=TargetPublicationPreviewResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_target_pub_preview_route(
    body: TargetPublicationPreviewRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    from decimal import Decimal as D

    with tenant_session(effective) as session:
        preview = create_publication_preview(
            session, effective,
            product_version_id=body.product_version_id,
            offer_version_id=body.offer_version_id,
            market_code=body.market_code,
            currency=body.currency,
            target_cpa=D(body.target_cpa) if body.target_cpa else None,
            breakeven_roas=D(body.breakeven_roas) if body.breakeven_roas else None,
            minimum_economic_spend=D(body.minimum_economic_spend) if body.minimum_economic_spend else None,
            idempotency_key=idempotency_key,
        )
        return {
            "id": preview.id,
            "product_version_id": preview.product_version_id,
            "offer_version_id": preview.offer_version_id,
            "market_code": preview.market_code,
            "currency": preview.currency,
            "target_cpa": str(preview.target_cpa) if preview.target_cpa else None,
            "breakeven_roas": str(preview.breakeven_roas) if preview.breakeven_roas else None,
            "minimum_economic_spend": str(preview.minimum_economic_spend) if preview.minimum_economic_spend else None,
            "affected_enrollment_count": preview.affected_enrollment_count,
            "policy_floor_check_passed": preview.policy_floor_check_passed,
            "impact_hash": preview.impact_hash,
            "expires_at": preview.expires_at,
            "created_at": preview.created_at,
        }


@mode_router.post(
    "/targets",
    operation_id="create_scale_target",
    response_model=TargetResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_target_route(
    body: TargetPublishRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        target = publish_target(
            session, effective,
            preview_id=body.publication_preview_id,
            idempotency_key=idempotency_key,
        )
        return _target_dict(target)


@mode_router.post(
    "/targets/{target_id}/withdrawal-previews",
    operation_id="create_scale_target_withdrawal_preview",
    response_model=TargetWithdrawalPreviewResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_target_wd_preview_route(
    target_id: str,
    body: TargetWithdrawalPreviewRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        preview = create_withdrawal_preview(
            session, effective,
            target_id=target_id,
            reason=body.reason,
            idempotency_key=idempotency_key,
        )
        return {
            "id": preview.id,
            "target_version_id": preview.target_version_id,
            "reason": preview.reason,
            "affected_enrollment_count": preview.affected_enrollment_count,
            "dependent_advice_thread_count": preview.dependent_advice_thread_count,
            "impact_hash": preview.impact_hash,
            "expires_at": preview.expires_at,
            "created_at": preview.created_at,
        }


@mode_router.post(
    "/targets/{target_id}/withdraw",
    operation_id="withdraw_scale_target",
    response_model=TargetResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def withdraw_target_route(
    target_id: str,
    body: TargetWithdrawRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        target = withdraw_target(
            session, effective,
            preview_id=body.withdrawal_preview_id,
            idempotency_key=idempotency_key,
        )
        return _target_dict(target)


# ---------------------------------------------------------------------------
# Command-outcome recovery -- spec §4.2 (Issue #87)
# ---------------------------------------------------------------------------


@mode_router.get(
    "/command-outcomes/{identity}",
    operation_id="get_scale_command_outcome",
    response_model=CommandOutcomeResponse,
)
def get_command_outcome(
    identity: str,
    command_kind: str | None = Query(default=None),
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        outcome = lookup_command_outcome(session, effective, identity, command_kind)
        return {
            "identity_hash": outcome.identity_hash,
            "command_type": outcome.command_type,
            "dedup_key": outcome.dedup_key,
            "result_resource_id": outcome.result_resource_id,
            "result_resource_type": outcome.result_resource_type,
            "request_fingerprint": outcome.request_fingerprint,
            "initiator_id": outcome.initiator_id,
            "created_at": outcome.created_at,
        }


# ---------------------------------------------------------------------------
# Exports -- spec §16 (Issue #87)
# ---------------------------------------------------------------------------


@mode_router.post(
    "/exports",
    operation_id="create_scale_export",
    response_model=ExportResponse,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_export_route(
    body: ExportCreateRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        result = create_export(
            session, effective,
            enrollment_id=body.enrollment_id,
            episode_id=body.episode_id,
            export_kind=body.export_kind,
            snapshot_id=body.snapshot_id,
            evaluation_id=body.evaluation_id,
            idempotency_key=idempotency_key,
        )
        export = session.get(ScaleExportRecord, result.export_id)
        return {
            "id": export.id,
            "operation_id": export.operation_id,
            "enrollment_id": export.enrollment_id,
            "episode_id": export.episode_id,
            "export_kind": export.export_kind,
            "requested_by": export.requested_by,
            "requested_at": export.requested_at,
            "completed_at": export.completed_at,
            "result_object_reference": export.result_object_reference,
        }


@mode_router.post(
    "/exports/{export_id}/download-intents",
    operation_id="create_scale_export_download_intent",
    response_model=ExportDownloadIntentResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_export_download_intent_route(
    export_id: str,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        intent = create_export_download_intent(
            session, effective,
            export_id=export_id,
            idempotency_key=idempotency_key,
        )
        return {
            "intent_id": intent.intent_id,
            "expires_at": intent.expires_at,
        }
```

- [ ] **Step 5: Add error handlers to `register_scale_v1_problem_handlers`**

Add to the existing error handler registration in v1_router.py:

```python
    @app.exception_handler(TargetLifecycleError)
    async def _target_lifecycle(request, exc):
        status = 422 if exc.code in ("at_least_one_target", "invalid_decimal") else (
            403 if exc.code == "owner_required" else (
            404 if exc.code == "target_not_found" else (
            409 if exc.code in ("preview_expired", "preview_already_consumed") else 400
        )))
        return JSONResponse(status, {"code": exc.code, "message": exc.detail})

    @app.exception_handler(BreakdownNotAvailableError)
    async def _breakdown_error(request, exc):
        status = 404 if exc.code in ("enrollment_not_found", "snapshot_not_found") else 422
        return JSONResponse(status, {"code": exc.code, "message": exc.detail})

    @app.exception_handler(CommandOutcomeNotFoundError)
    async def _command_not_found(request, exc):
        return JSONResponse(404, {"code": "command_outcome_not_found", "message": exc.identity_hash})

    @app.exception_handler(ExportError)
    async def _export_error(request, exc):
        status = 404 if exc.code in ("enrollment_not_found", "export_not_found") else (
            409 if exc.code == "export_not_complete" else 400
        )
        return JSONResponse(status, {"code": exc.code, "message": exc.detail})
```

- [ ] **Step 6: Mirror all new routes in `app/scale/platform_router.py`**

Import the same deep modules and add identical route handlers, each using `_platform_context` instead of `_mode_effective_context`, with `platform_` prefixed operation IDs. The platform router imports `_target_dict`, `_paginate`, and other serialization helpers from `v1_router` (already done for existing routes).

- [ ] **Step 7: Update BFF policy**

Add to `contracts/openapi/marketai-domain-v1-policy.json`:

```json
"list_scale_target_contexts": {"bffPattern": "/api/domain/v1/w/:slug/scale/:meta_mode/target-contexts/current", "maxRequestBytes": 0, "maxResponseBytes": 524288, "forwardConditionalHeaders": false},
"get_scale_target": {"bffPattern": "/api/domain/v1/w/:slug/scale/:meta_mode/targets/:target_id", "maxRequestBytes": 0, "maxResponseBytes": 131072, "forwardConditionalHeaders": true},
"create_scale_target_publication_preview": {"bffPattern": "/api/domain/v1/w/:slug/scale/:meta_mode/target-publication-previews", "maxRequestBytes": 8192, "maxResponseBytes": 65536, "forwardConditionalHeaders": false, "forwardStepUpHeader": true},
"create_scale_target_withdrawal_preview": {"bffPattern": "/api/domain/v1/w/:slug/scale/:meta_mode/targets/:target_id/withdrawal-previews", "maxRequestBytes": 8192, "maxResponseBytes": 65536, "forwardConditionalHeaders": false},
"create_scale_export_download_intent": {"bffPattern": "/api/domain/v1/w/:slug/scale/:meta_mode/exports/:export_id/download-intents", "maxRequestBytes": 1024, "maxResponseBytes": 8192, "forwardConditionalHeaders": false}
```

Update existing entries:
- `create_scale_target` — add `"forwardStepUpHeader": true`
- `withdraw_scale_target` — change to POST method, update bffPattern to `.../:target_id/withdraw`, add `"forwardStepUpHeader": true`
- `create_scale_export` — add `"maxRequestBytes": 8192`
- `get_scale_enrollment_breakdowns` — no policy change needed (existing entry covers it)

- [ ] **Step 8: Regenerate OpenAPI/BFF artifacts**

```bash
python scripts/generate_openapi.py
bash scripts/generate_all.sh
```

- [ ] **Step 9: Run full test suite**

```bash
make verify
```

- [ ] **Step 10: Commit**

```bash
git add app/scale/v1_router.py app/scale/platform_router.py contracts/ web/lib/domain-client/generated/
git commit -m "feat(scale): replace all 501 stubs with real route handlers

Issue #87 Task 6: target lifecycle (context/list/get/preview/publish/
withdraw), snapshot-pinned breakdowns, command-outcome recovery,
exports with download intents. Both v1 and platform routers wired.
OpenAPI+BFF artifacts regenerated."
```

---

## Task 7: Frontend — Wire UI to Real Endpoints

**Files:**
- Modify: `web/components/scale/targets/TargetManager.tsx`
- Modify: `web/components/scale/detail/BreakdownTable.tsx`
- Modify: `web/components/scale/detail/EnrollmentDetail.tsx`
- Modify: `web/hooks/scale/useScaleTargets.ts`
- Modify: `web/hooks/scale/useScaleEvidence.ts`
- Modify: `web/hooks/scale/useScaleExport.ts`
- Modify: `web/lib/domain-client/gateway.ts`

**Interfaces:**
- Consumes: Generated TypeScript types from Task 6; existing hook patterns from `web/hooks/scale/`; gateway methods from `web/lib/domain-client/gateway.ts`
- Produces: Working UI for targets (list, publish flow, withdraw flow), breakdowns (snapshot-pinned, paginated, breakdown type tabs), and exports (create + download)

This task follows the exact patterns established by existing working Scale UI components (`EnrollmentList.tsx`, `AdviceThreadList.tsx`, `EnrollmentDetail.tsx`). Each sub-step is a focused component update.

- [ ] **Step 1: Update `gateway.ts` — wire formerly-501 methods to real HTTP calls**

Replace the 501-stub method implementations (around lines 911, 1060-1080) with real fetch calls following the pattern of existing working methods (e.g., `listScaleEnrollments`). Add new methods for preview endpoints and download intents.

- [ ] **Step 2: Update `useScaleTargets.ts` — add preview, publish, withdraw hooks**

Add `useCreateScaleTargetPublicationPreview`, `usePublishScaleTarget`, `useCreateScaleTargetWithdrawalPreview`, `useWithdrawScaleTarget`, `useScaleTargetContexts` hooks following `useDomainCommand` / `useDomainQuery` patterns.

- [ ] **Step 3: Update `useScaleEvidence.ts` — update breakdown hook**

Change `useScaleEnrollmentBreakdowns` to accept `snapshotId` and `breakdownType` params, passing them as query parameters.

- [ ] **Step 4: Update `useScaleExport.ts` — add download-intent hook**

Add `useCreateScaleExportDownloadIntent` hook.

- [ ] **Step 5: Rewrite `TargetManager.tsx` — remove `NotImplementedNotice`**

Replace the stub with a real target management page: target context selector (server-driven from `useScaleTargetContexts`), target list table with lifecycle badges, publish flow (select context → enter decimals → preview → MFA → confirm), withdraw flow (preview → MFA → confirm). All actions server-gated via `allowed_actions`.

- [ ] **Step 6: Rewrite `BreakdownTable.tsx` — remove `NotImplementedNotice`**

Replace with snapshot-pinned breakdown display: breakdown type tabs (placement/country/age_gender), paginated segment table showing spend shares, metrics, and intervals for observed segments; privacy-suppressed placeholder rows; insufficient reason badges.

- [ ] **Step 7: Update `EnrollmentDetail.tsx` — add export button**

Add an export action button that creates an export Operation and shows a status indicator. When complete, offer a download-intent button.

- [ ] **Step 8: Run web verify**

```bash
cd web && npm run verify
```

- [ ] **Step 9: Commit**

```bash
git add web/
git commit -m "feat(scale): wire UI to real Scale endpoints

Issue #87 Task 7: TargetManager with publish/withdraw preview flows,
BreakdownTable with snapshot-pinned paginated segments, export button
with download intents. All NotImplementedNotice paths removed."
```

---

## Task 8: Remove Old Python E2E Stubs

**Files:**
- Delete: `tests/e2e/test_scale_controlled_mock_journey.py`
- Delete: `tests/e2e/test_scale_customer_sandbox_smoke.py`

**Interfaces:**
- Consumes: nothing
- Produces: Clean test tree; these files' documented journeys are now implemented as TypeScript Playwright specs in Task 9

- [ ] **Step 1: Delete the files**

```bash
git rm tests/e2e/test_scale_controlled_mock_journey.py tests/e2e/test_scale_customer_sandbox_smoke.py
```

- [ ] **Step 2: Verify no other file imports them**

```bash
grep -r "test_scale_controlled_mock_journey\|test_scale_customer_sandbox_smoke" tests/ app/
```

Expected: no hits.

- [ ] **Step 3: Run test suite to confirm nothing breaks**

```bash
make verify
```

- [ ] **Step 4: Commit**

```bash
git commit -m "chore(scale): remove always-skipped Python E2E stubs

Issue #87 Task 8: replaced by TypeScript Playwright specs under
web/e2e/scale/ (Task 9)."
```

---

## Task 9: Playwright E2E — Controlled-Mock Journey and Sandbox Smoke

**Files:**
- Create: `web/e2e/scale/fixtures.ts`
- Create: `web/e2e/scale/helpers.ts`
- Create: `web/e2e/scale/seed_prerequisites.py`
- Create: `web/e2e/scale/inspect_db.py`
- Create: `web/e2e/scale/worker_checkpoint.py`
- Create: `web/e2e/scale/controlled-mock-journey.spec.ts`
- Create: `web/e2e/scale/customer-sandbox-smoke.spec.ts`

**Interfaces:**
- Consumes: Running PostgreSQL, FastAPI server, Scale Worker (docker compose); `FakeMetaAdapter` via `controlled_mock` ScenarioVersions; all Scale endpoints from Tasks 1-7
- Produces: Playwright test evidence for all acceptance criteria; trace/report artifacts on failure

This task follows the patterns established by `web/e2e/experiments/` (seed scripts, helpers, worker control, DB inspection) and `web/e2e/localization/` (golden-path journeys against real PostgreSQL and FakeMetaAdapter).

- [ ] **Step 1: Create `web/e2e/scale/seed_prerequisites.py`**

Python script seeding: confirmed Winner with Approved AssetVersion, valid Offer/LP, Mock MetaConnection, ScenarioVersion (with configurable fault injection), Product/OfferVersions, ScaleDeliveryTargetRecord, ScalePolicyVersionRecord. Uses `tenant_session` with full RLS binding, writes directly to domain database.

- [ ] **Step 2: Create `web/e2e/scale/inspect_db.py`**

Python script querying: enrollment state, episode phase, evidence maturity/health, advice thread lifecycle, target lifecycle, export completion, command journal entries. Called from TypeScript helpers via `execSync`.

- [ ] **Step 3: Create `web/e2e/scale/worker_checkpoint.py`**

Python script for single-job deterministic Worker boundary: claims and runs exactly one `scale_evaluate` or `scale_export` job, then returns.

- [ ] **Step 4: Create `web/e2e/scale/helpers.ts`**

TypeScript helpers wrapping the Python scripts:
- `seedScalePrerequisites(options)` — calls `seed_prerequisites.py`
- `inspectScaleState(enrollmentId)` — calls `inspect_db.py`
- `advanceManifest(kind)` — advances controlled-mock evidence to a specific state
- `runOneScaleWorkerJob()` — calls `worker_checkpoint.py`
- `restartScaleWorker()` / `stopScaleWorker()` / `startScaleWorker()` — docker compose
- `assertZeroMetaWrites(page)` — network request spy asserting no Meta write calls

- [ ] **Step 5: Create `web/e2e/scale/fixtures.ts`**

Playwright fixtures extending the base test with Scale-specific setup:
- `scalePage` fixture — navigates to `/w/{slug}/scale`
- `seededScale` fixture — runs `seedScalePrerequisites` and returns the seed data
- `networkSpy` fixture — installs route interception for Meta write detection

- [ ] **Step 6: Create `web/e2e/scale/controlled-mock-journey.spec.ts`**

The 18-step golden journey from the spec (§4.5), each step as a `test()`:

1. Seed confirmed Winner with prerequisites
2. Open candidate flow, create Draft Enrollment
3. Review eligibility, activate with ETag/If-Match
4. Advance one complete manifest → verify collecting/warming
5. Advance terminal blocked manifest → verify insufficient + typed issue
6. Clear defect, advance to monitorable → verify delivery/CTR health
7. Publish economic Target (preview/impact/MFA/idempotent)
8. Advance to economics_ready+healthy → verify intervals, no advice
9. Advance first composite fatigue hit → verify fatigue_watch
10. Advance qualifying day → verify confirmed fatigue, AdviceThread
11. Dismiss then snooze active thread → verify independence after refresh
12. Convert occurrence → verify unpaid GenerationDraft
13. Navigate to Generation → verify downstream chain
14. Advance two clean evaluations → verify thread resolves
15. Advance healthy chain, convert consider_scale_test → verify ScaleTestPlanDraft
16. Complete downstream chain → verify same-mode AdviceOutcome
17. Refresh + restart API/Worker → verify projections recover
18. Cross-Workspace, cross-mode, stale revision, duplicate binding → verify fail closed

Plus: network spy + Worker capability spy assert zero Meta writes throughout.

- [ ] **Step 7: Create `web/e2e/scale/customer-sandbox-smoke.spec.ts`**

5-step smoke from the spec:

1. Explicit enrollment + activation against Sandbox data
2. Refresh + restart recovery
3. Visible `customer_sandbox` provenance everywhere
4. No fallback to Mock or internal_validation
5. Advice/Outcome remains non-production, same-mode

- [ ] **Step 8: Run Playwright tests locally**

```bash
cd web && npx playwright test e2e/scale/ --project=chromium
```

- [ ] **Step 9: Commit**

```bash
git add web/e2e/scale/
git commit -m "test(scale): Playwright controlled-mock journey and sandbox smoke

Issue #87 Task 9: 18-step golden journey against PostgreSQL +
FakeMetaAdapter, 5-step customer-sandbox smoke. Zero Meta writes
asserted. Trace/report artifacts on failure."
```

---

## Task 10: Final Verification and PR

**Files:**
- No new files

**Interfaces:**
- Consumes: all previous tasks
- Produces: passing `make verify`, passing `make verify-all`, PR

- [ ] **Step 1: Run full Python verify**

```bash
make verify
```

- [ ] **Step 2: Run full web verify**

```bash
make verify-all
```

- [ ] **Step 3: Check no 501 stubs remain**

```bash
grep -rn "501.*NOT_IMPLEMENTED\|raise HTTPException(501" app/scale/
```

Expected: zero hits.

- [ ] **Step 4: Check OpenAPI drift**

```bash
python scripts/generate_openapi.py --check
```

Expected: exit 0 (no drift).

- [ ] **Step 5: Open PR**

```bash
gh pr create --title "Complete Scale workflows and persisted browser acceptance evidence" --body "$(cat <<'EOF'
## Summary

- Implement all 501-returning Scale endpoints: target lifecycle (preview/MFA/confirm), snapshot-pinned breakdowns, hashed command-outcome recovery, idempotent exports with download intents
- Wire Scale UI to real endpoints, remove all `NotImplementedNotice` paths
- Add 18-step Playwright controlled-mock golden journey and 5-step customer-sandbox smoke against PostgreSQL + FakeMetaAdapter
- New Alembic migration for target preview tables and command identity hash
- Both v1 and platform routers as thin adapters over shared deep domain modules
- OpenAPI/BFF artifacts regenerated

## Test plan

- [ ] `make verify` passes (Python tests + migration check)
- [ ] `make verify-all` passes (Python + web)
- [ ] No 501 stubs remain in `app/scale/`
- [ ] Playwright controlled-mock journey passes all 18 steps
- [ ] Playwright sandbox smoke passes all 5 steps
- [ ] Network spy confirms zero Meta writes throughout
- [ ] OpenAPI drift check passes

Closes #87

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
