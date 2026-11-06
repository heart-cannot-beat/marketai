"""SQL-backed repository facade used by the existing HTTP and pipeline modules.

The facade preserves the current function interface while moving business state
from JSON files to append-only/history-aware SQL records.  Local image files are
kept temporarily until the object-store adapter lands.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import case, func, select

from . import config
from .adapters.persistence.database import tenant_session
from .adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    CostLedgerRecord,
    DimensionRecord,
    DimensionSchemaRecord,
    DimensionValueRecord,
    ExperimentDecisionRecord,
    ExperimentEntryRecord,
    GenerationRunRecord,
    JobRecord,
    PlanEntryRecord,
    PlanRecord,
    ProductRecord,
    QualityReviewRecord,
    VerdictDecisionRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from .entitlements.admission import PaidOperationAdmission
from .entitlements.policy import EntitlementPolicy
from .entitlements.quote_engine import CostQuoteEngine
from .entitlements.reservation_book import ReservationBook
from .entitlements.types import (
    AdmissionRequest,
    BillableCallSpec,
    CapacityRequest,
    EntitlementRequirements,
    ExecutionMode,
    JobSpec,
    OperationPayload,
    ProviderSelection,
)
from .platform.jobs import cancel_resource, enqueue_in_session
from .platform.audit import append_event
from .platform.tenancy import TenantContext, TenantScopeError, current_context
from .schemas import (
    AngleSchema,
    Asset,
    AssetVersion,
    Combo,
    Dimension,
    DimensionValue,
    Plan,
    Product,
    Run,
)


PRODUCTS_DIR = config.DATA_DIR / "products"
PRODUCTS_DIR.mkdir(parents=True, exist_ok=True)


class CostLimitExceeded(ValueError):
    def __init__(self, *, scope: str, attempted: Decimal, limit: Decimal, currency: str) -> None:
        self.scope = scope
        self.attempted = attempted
        self.limit = limit
        self.currency = currency
        super().__init__(f"{scope} 成本 {attempted} {currency} 超过硬上限 {limit} {currency}")


def _canonical_hash(value: object) -> str:
    from .platform.canonical import canonical_hash
    return canonical_hash(value)


def combo_fingerprint(values: dict[str, str]) -> str:
    return _canonical_hash(sorted(values.items()))


def _as_dt(value: float | int | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return utcnow()
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _as_ts(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _ensure_workspace(session) -> None:
    context = current_context(session)
    workspace = session.get(WorkspaceRecord, context.workspace_id)
    if workspace is not None:
        if workspace.authorization_epoch != context.authorization_epoch:
            raise TenantScopeError("the TenantContext authorization epoch is stale")
        return
    if session.get_bind().dialect.name != "sqlite":
        raise TenantScopeError("the Workspace projection has not been provisioned")
    session.add(
        WorkspaceRecord(
            id=context.workspace_id,
            name="Default Workspace",
            default_currency=config.DEFAULT_CURRENCY,
            authorization_epoch=context.authorization_epoch,
        )
    )
    session.flush()


# ---------- local object paths (temporary adapter) ----------
def product_dir(pid: str) -> Path:
    return PRODUCTS_DIR / pid


def refs_dir(pid: str) -> Path:
    path = product_dir(pid) / "refs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_dir(pid: str, run_id: str) -> Path:
    path = product_dir(pid) / "runs" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------- product ----------
def _product_from_record(row: ProductRecord) -> Product:
    payload = {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "selling_points": row.selling_points or [],
        "audience": row.audience,
        "brand_notes": row.brand_notes,
        "ref_images": row.ref_images or [],
        "economics": row.economics or {},
        "created_at": _as_ts(row.created_at),
    }
    return Product(**payload)


def list_products(context: TenantContext) -> list[Product]:
    with tenant_session(context) as session:
        rows = session.scalars(
            select(ProductRecord)
            .where(ProductRecord.workspace_id == context.workspace_id)
            .order_by(ProductRecord.created_at.desc())
        ).all()
        return [_product_from_record(row) for row in rows]


def get_product(context: TenantContext, pid: str) -> Optional[Product]:
    with tenant_session(context) as session:
        row = session.get(ProductRecord, pid)
        if not row or row.workspace_id != context.workspace_id:
            return None
        return _product_from_record(row)


def save_product(context: TenantContext, product: Product) -> None:
    with tenant_session(context) as session:
        _ensure_workspace(session)
        row = session.get(ProductRecord, product.id)
        if row is not None and row.workspace_id != context.workspace_id:
            raise KeyError("产品不存在")
        created = row is None
        economics = product.economics.model_dump(mode="json")
        if row is None:
            row = ProductRecord(
                id=product.id,
                workspace_id=context.workspace_id,
                name=product.name,
                description=product.description,
                selling_points=list(product.selling_points),
                audience=product.audience,
                brand_notes=product.brand_notes,
                ref_images=list(product.ref_images),
                economics=economics,
                created_at=_as_dt(product.created_at),
                updated_at=utcnow(),
            )
            session.add(row)
        else:
            row.name = product.name
            row.description = product.description
            row.selling_points = list(product.selling_points)
            row.audience = product.audience
            row.brand_notes = product.brand_notes
            row.ref_images = list(product.ref_images)
            row.economics = economics
            row.revision += 1
            row.updated_at = utcnow()
        append_event(
            session,
            event_type="product.created" if created else "product.updated",
            resource_type="product",
            resource_id=product.id,
            details={"revision": 1 if created else row.revision},
        )


# ---------- angles ----------
def _latest_schema_record(session, pid: str) -> DimensionSchemaRecord | None:
    context = current_context(session)
    return session.scalar(
        select(DimensionSchemaRecord)
        .where(
            DimensionSchemaRecord.workspace_id == context.workspace_id,
            DimensionSchemaRecord.product_id == pid,
        )
        .order_by(DimensionSchemaRecord.version.desc())
        .limit(1)
    )


def get_angles(context: TenantContext, pid: str) -> Optional[AngleSchema]:
    with tenant_session(context) as session:
        schema = _latest_schema_record(session, pid)
        if not schema:
            return None
        dims = session.scalars(
            select(DimensionRecord)
            .where(DimensionRecord.schema_id == schema.id)
            .order_by(DimensionRecord.position)
        ).all()
        result: list[Dimension] = []
        for dim in dims:
            values = session.scalars(
                select(DimensionValueRecord)
                .where(DimensionValueRecord.dimension_id == dim.id)
                .order_by(DimensionValueRecord.position)
            ).all()
            result.append(
                Dimension(
                    id=dim.stable_dimension_id,
                    label=dim.label,
                    values=[
                        DimensionValue(
                            id=value.stable_value_id,
                            label=value.label,
                            prompt_fragment=value.prompt_fragment,
                        )
                        for value in values
                    ],
                )
            )
        return AngleSchema(
            product_id=pid,
            dimensions=result,
            style_notes=schema.style_notes,
            source=schema.source,
            updated_at=_as_ts(schema.created_at),
        )


def save_angles(context: TenantContext, angles: AngleSchema) -> None:
    snapshot = angles.model_dump(mode="json", exclude={"updated_at"})
    snapshot_hash = _canonical_hash(snapshot)
    with tenant_session(context) as session:
        _ensure_workspace(session)
        product = session.get(ProductRecord, angles.product_id)
        if product is None or product.workspace_id != context.workspace_id:
            raise KeyError("产品不存在")
        latest = _latest_schema_record(session, angles.product_id)
        if latest and latest.snapshot_hash == snapshot_hash:
            return
        version = (latest.version + 1) if latest else 1
        schema = DimensionSchemaRecord(
            id=new_id("dsv"),
            workspace_id=context.workspace_id,
            product_id=angles.product_id,
            version=version,
            style_notes=angles.style_notes,
            source=angles.source,
            snapshot_hash=snapshot_hash,
            created_at=_as_dt(angles.updated_at),
        )
        session.add(schema)
        session.flush()
        for dim_pos, dim in enumerate(angles.dimensions):
            dim_row = DimensionRecord(
                id=new_id("dimr"),
                schema_id=schema.id,
                stable_dimension_id=dim.id,
                label=dim.label,
                position=dim_pos,
            )
            session.add(dim_row)
            session.flush()
            for value_pos, value in enumerate(dim.values):
                session.add(
                    DimensionValueRecord(
                        id=new_id("dvr"),
                        dimension_id=dim_row.id,
                        stable_value_id=value.id,
                        label=value.label,
                        prompt_fragment=value.prompt_fragment,
                        position=value_pos,
                    )
                )
        append_event(
            session,
            event_type="dimension_schema.created",
            resource_type="product",
            resource_id=angles.product_id,
            details={"schema_id": schema.id, "version": version},
        )


# ---------- plans ----------
def save_plan(context: TenantContext, plan: Plan) -> None:
    snapshot = plan.model_dump(mode="json", exclude={"created_at"})
    with tenant_session(context) as session:
        _ensure_workspace(session)
        existing = session.get(PlanRecord, plan.id)
        if existing is not None and existing.workspace_id != context.workspace_id:
            raise KeyError("Plan 不存在")
        if existing:
            if existing.snapshot_hash != _canonical_hash(snapshot):
                raise ValueError("Plan 已冻结，不能原地修改")
            return
        product = session.get(ProductRecord, plan.product_id)
        if product is None or product.workspace_id != context.workspace_id:
            raise KeyError("产品不存在")
        schema = _latest_schema_record(session, plan.product_id)
        row = PlanRecord(
            id=plan.id,
            workspace_id=context.workspace_id,
            product_id=plan.product_id,
            schema_id=schema.id if schema else None,
            mode=plan.mode,
            cap=plan.cap,
            total_full=plan.total_full,
            snapshot_hash=_canonical_hash(snapshot),
            created_at=_as_dt(plan.created_at),
        )
        session.add(row)
        session.flush()
        for position, combo in enumerate(plan.combos):
            session.add(
                PlanEntryRecord(
                    id=new_id("pe"),
                    plan_id=plan.id,
                    combo_id=combo.id,
                    combo_code=combo.code,
                    combo_fingerprint=combo_fingerprint(combo.values),
                    values_json=dict(combo.values),
                    tags_json=dict(combo.tags),
                    position=position,
                )
            )
        append_event(
            session,
            event_type="plan.created",
            resource_type="plan",
            resource_id=plan.id,
            details={"product_id": plan.product_id, "entries": len(plan.combos)},
        )


def get_plan(context: TenantContext, pid: str) -> Optional[Plan]:
    with tenant_session(context) as session:
        row = session.scalar(
            select(PlanRecord)
            .where(
                PlanRecord.workspace_id == context.workspace_id,
                PlanRecord.product_id == pid,
            )
            .order_by(PlanRecord.created_at.desc(), PlanRecord.id.desc())
            .limit(1)
        )
        if not row:
            return None
        entries = session.scalars(
            select(PlanEntryRecord)
            .where(PlanEntryRecord.plan_id == row.id)
            .order_by(PlanEntryRecord.position)
        ).all()
        return Plan(
            id=row.id,
            product_id=row.product_id,
            mode=row.mode,
            cap=row.cap,
            total_full=row.total_full,
            combos=[
                Combo(
                    id=entry.combo_id,
                    code=entry.combo_code,
                    values=dict(entry.values_json),
                    tags=dict(entry.tags_json),
                )
                for entry in entries
            ],
            created_at=_as_ts(row.created_at),
        )


# ---------- generation runs ----------
def _asset_from_record(row: AssetRecord) -> Asset:
    return Asset(
        id=row.id,
        run_id=row.run_id,
        combo_id=row.combo_id,
        combo_code=row.combo_code,
        tags=dict(row.tags_json),
        filename=row.filename,
        provider=row.provider,
        model=row.model,
        prompt=row.prompt,
        fingerprint=row.fingerprint,
        reused_from_asset_id=row.reused_from_asset_id,
        status=row.status if row.status in ("pending", "ok", "failed") else "failed",
        error=row.error_detail,
        created_at=_as_ts(row.created_at),
    )


def _run_from_record(session, row: GenerationRunRecord) -> Run:
    assets = session.scalars(
        select(AssetRecord).where(AssetRecord.run_id == row.id).order_by(AssetRecord.created_at, AssetRecord.id)
    ).all()
    return Run(
        id=row.id,
        product_id=row.product_id,
        plan_id=row.plan_id,
        provider=row.provider,
        model=row.model,
        aspect_ratio=row.aspect_ratio,
        image_size=row.image_size,
        variants=row.variants,
        use_ref=row.use_ref,
        status=row.status,
        total=row.total,
        done=row.done,
        failed=row.failed,
        est_cost=row.est_cost,
        currency=row.currency,
        assets=[_asset_from_record(asset) for asset in assets],
        created_at=_as_ts(row.created_at),
    )


def _daily_cost_exposure(session, day: datetime) -> Decimal:
    context = current_context(session)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    # Entries are append-only; releases reduce exposure.
    signed = case(
        (CostLedgerRecord.entry_type == "released", -CostLedgerRecord.amount),
        else_=CostLedgerRecord.amount,
    )
    value = session.scalar(
        select(func.coalesce(func.sum(signed), 0)).where(
            CostLedgerRecord.workspace_id == context.workspace_id,
            CostLedgerRecord.created_at >= start,
        )
    )
    return Decimal(str(value or 0))


def _build_admission_request(run: Run, pending_assets: list) -> AdmissionRequest:
    """Construct an AdmissionRequest from a generation Run and its pending assets."""
    execution_mode = ExecutionMode.MOCK if run.provider == "mock" else ExecutionMode.BYOK

    semantic_key_hash = hashlib.sha256(run.id.encode()).hexdigest()[:16]
    run_params = json.dumps(
        {
            "product_id": run.product_id,
            "plan_id": run.plan_id,
            "provider": run.provider,
            "model": run.model,
            "aspect_ratio": run.aspect_ratio,
            "image_size": run.image_size,
            "variants": run.variants,
            "use_ref": run.use_ref,
            "asset_ids": sorted(a.id for a in pending_assets),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    request_fingerprint_hash = hashlib.sha256(run_params.encode()).hexdigest()[:16]

    billable_calls = tuple(
        BillableCallSpec(
            call_semantic_key_hash=hashlib.sha256(asset.id.encode()).hexdigest()[:16],
            provider=run.provider,
            model=run.model,
            quantity=1,
        )
        for asset in pending_assets
    )

    job_specs = tuple(
        JobSpec(
            job_type="generate_asset",
            resource_type="run",
            resource_id=run.id,
            payload_json={
                "product_id": run.product_id,
                "run_id": run.id,
                "asset_id": asset.id,
            },
            call_semantic_key_hash=hashlib.sha256(asset.id.encode()).hexdigest()[:16],
        )
        for asset in pending_assets
    )

    return AdmissionRequest(
        command_type="generate_image",
        semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=request_fingerprint_hash,
        entitlement_requirements=EntitlementRequirements(
            feature_keys=("module.generate.enabled",),
            numeric_requirements=(),
        ),
        provider_selection=ProviderSelection(
            execution_mode=execution_mode,
            provider=run.provider,
            model=run.model,
            binding_scope="workspace",
            credential_binding_id=None,
        ),
        billable_calls=billable_calls,
        capacity_requests=(),
        operation_payload=OperationPayload(
            command_type="generate_image",
            result_resource_type="generation_run",
            result_resource_id=run.id,
            payload_version=1,
        ),
        job_specs=job_specs,
    )


def _build_price_registry() -> dict[str, dict[str, Decimal]]:
    """Build a CostQuoteEngine price registry from config.COST_PER_IMAGE.

    The config maps provider -> flat price.  The quote engine requires
    provider -> model -> price.  We use a wildcard model key ``"*"`` so
    that any model lookup for a known provider succeeds via the
    ``_WildcardCostQuoteEngine`` below.
    """
    return {
        provider: {"*": price}
        for provider, price in config.COST_PER_IMAGE.items()
    }


class _WildcardCostQuoteEngine(CostQuoteEngine):
    """A CostQuoteEngine that falls back to a ``*`` model key."""

    def _lookup(self, provider: str, model: str) -> Decimal:
        provider_prices = self._registry.get(provider, {})
        if model in provider_prices:
            return provider_prices[model]
        if "*" in provider_prices:
            return provider_prices["*"]
        raise KeyError(f"no price for provider={provider!r}, model={model!r}")


def create_run(context: TenantContext, run: Run) -> None:
    """Atomically persist a run, reserve cost, and enqueue one job per pending asset."""
    if config.MARKETAI_ADMISSION_CONTRACT_VERSION >= 1:
        _create_run_admission(context, run)
    else:
        _create_run_legacy(context, run)


def _create_run_admission(context: TenantContext, run: Run) -> None:
    """New admission-gated path: entitlement check, quote, reserve, then persist."""
    with tenant_session(context) as session:
        _ensure_workspace(session)
        # Idempotency: if the Run already exists, return immediately.
        existing_run = session.get(GenerationRunRecord, run.id)
        if existing_run is not None and existing_run.workspace_id != context.workspace_id:
            raise KeyError("Run 不存在")
        if existing_run is not None:
            return
        # Validate product and plan ownership.
        product = session.get(ProductRecord, run.product_id)
        plan_rec = session.get(PlanRecord, run.plan_id)
        if (
            product is None
            or product.workspace_id != context.workspace_id
            or plan_rec is None
            or plan_rec.workspace_id != context.workspace_id
        ):
            raise KeyError("产品或 Plan 不存在")

        # Collect pending assets that need generation jobs.
        pending_assets = [a for a in run.assets if a.status == "pending"]

        # Build and submit admission request.
        admission_request = _build_admission_request(run, pending_assets)

        quote_engine = _WildcardCostQuoteEngine(
            price_registry=_build_price_registry(),
            price_rule_version="v1",
        )
        admission = PaidOperationAdmission(
            policy=EntitlementPolicy(),
            quote_engine=quote_engine,
            reservation_book=ReservationBook(),
        )
        admission_result = admission.admit_in_transaction(
            session, context, admission_request,
        )

        # Create GenerationRunRecord.
        row = GenerationRunRecord(
            id=run.id,
            workspace_id=context.workspace_id,
            product_id=run.product_id,
            plan_id=run.plan_id,
            provider=run.provider,
            model=run.model,
            aspect_ratio=run.aspect_ratio,
            image_size=run.image_size,
            variants=run.variants,
            use_ref=run.use_ref,
            status=run.status,
            total=run.total,
            done=run.done,
            failed=run.failed,
            est_cost=run.est_cost,
            currency=run.currency,
            created_at=_as_dt(run.created_at),
            updated_at=utcnow(),
        )
        session.add(row)
        session.flush()

        # Create AssetRecords (but do NOT enqueue -- admission created JobRecords).
        plan_entries = {
            entry.combo_id: entry
            for entry in session.scalars(
                select(PlanEntryRecord).where(PlanEntryRecord.plan_id == run.plan_id)
            ).all()
        }
        for asset in run.assets:
            plan_entry = plan_entries.get(asset.combo_id)
            asset_row = AssetRecord(
                id=asset.id,
                workspace_id=context.workspace_id,
                product_id=run.product_id,
                run_id=run.id,
                plan_entry_id=plan_entry.id if plan_entry else None,
                combo_id=asset.combo_id,
                combo_code=asset.combo_code,
                tags_json=dict(asset.tags),
                filename=asset.filename,
                provider=asset.provider,
                model=asset.model,
                prompt=asset.prompt,
                fingerprint=asset.fingerprint,
                reused_from_asset_id=asset.reused_from_asset_id,
                status=asset.status,
                error_detail=asset.error,
                created_at=_as_dt(asset.created_at),
                updated_at=utcnow(),
            )
            session.add(asset_row)
            session.flush()

        # No CostLedgerRecord -- the new admission path uses SpendReservationRecords.

        # Audit event.
        append_event(
            session,
            event_type="generation_run.created",
            resource_type="generation_run",
            resource_id=run.id,
            details={
                "product_id": run.product_id,
                "provider": run.provider,
                "asset_count": len(run.assets),
                "estimated_cost": str(run.est_cost),
                "currency": run.currency,
                "admission_operation_id": admission_result.operation_id,
                "admission_replayed": admission_result.replayed,
            },
        )


def _create_run_legacy(context: TenantContext, run: Run) -> None:
    """Original legacy path: daily cost check, CostLedgerRecord, enqueue_in_session."""
    with tenant_session(context) as session:
        _ensure_workspace(session)
        existing_run = session.get(GenerationRunRecord, run.id)
        if existing_run is not None and existing_run.workspace_id != context.workspace_id:
            raise KeyError("Run 不存在")
        if existing_run is not None:
            return
        product = session.get(ProductRecord, run.product_id)
        plan = session.get(PlanRecord, run.plan_id)
        if (
            product is None
            or product.workspace_id != context.workspace_id
            or plan is None
            or plan.workspace_id != context.workspace_id
        ):
            raise KeyError("产品或 Plan 不存在")
        if run.est_cost > config.MAX_COST_PER_RUN:
            raise CostLimitExceeded(
                scope="单次 Run", attempted=run.est_cost, limit=config.MAX_COST_PER_RUN, currency=run.currency
            )
        daily = _daily_cost_exposure(session, utcnow())
        if daily + run.est_cost > config.MAX_COST_PER_DAY:
            raise CostLimitExceeded(
                scope="当日", attempted=daily + run.est_cost, limit=config.MAX_COST_PER_DAY, currency=run.currency
            )
        row = GenerationRunRecord(
            id=run.id,
            workspace_id=context.workspace_id,
            product_id=run.product_id,
            plan_id=run.plan_id,
            provider=run.provider,
            model=run.model,
            aspect_ratio=run.aspect_ratio,
            image_size=run.image_size,
            variants=run.variants,
            use_ref=run.use_ref,
            status=run.status,
            total=run.total,
            done=run.done,
            failed=run.failed,
            est_cost=run.est_cost,
            currency=run.currency,
            created_at=_as_dt(run.created_at),
            updated_at=utcnow(),
        )
        session.add(row)
        # Keep the aggregate creation order explicit.  Jobs may flush the
        # session while being enqueued, so the Run FK must already exist.
        session.flush()
        plan_entries = {
            entry.combo_id: entry
            for entry in session.scalars(select(PlanEntryRecord).where(PlanEntryRecord.plan_id == run.plan_id)).all()
        }
        for asset in run.assets:
            plan_entry = plan_entries.get(asset.combo_id)
            asset_row = AssetRecord(
                    id=asset.id,
                    workspace_id=context.workspace_id,
                    product_id=run.product_id,
                    run_id=run.id,
                    plan_entry_id=plan_entry.id if plan_entry else None,
                    combo_id=asset.combo_id,
                    combo_code=asset.combo_code,
                    tags_json=dict(asset.tags),
                    filename=asset.filename,
                    provider=asset.provider,
                    model=asset.model,
                    prompt=asset.prompt,
                    fingerprint=asset.fingerprint,
                    reused_from_asset_id=asset.reused_from_asset_id,
                    status=asset.status,
                    error_detail=asset.error,
                    created_at=_as_dt(asset.created_at),
                    updated_at=utcnow(),
                )
            session.add(asset_row)
            session.flush()
            if asset.status == "pending":
                enqueue_in_session(
                    session,
                    context=context,
                    job_type="generate_asset",
                    payload={"product_id": run.product_id, "run_id": run.id, "asset_id": asset.id},
                    idempotency_key=f"generate:{asset.id}:{asset.fingerprint}",
                    resource_type="run",
                    resource_id=run.id,
                    safe_to_retry_after_crash=(run.provider == "mock"),
                    max_attempts=1,
                )
        if run.est_cost:
            session.add(
                CostLedgerRecord(
                    id=new_id("cost"),
                    workspace_id=context.workspace_id,
                    run_id=run.id,
                    entry_type="reserved",
                    amount=run.est_cost,
                    currency=run.currency,
                    note="生成任务成本预留",
                )
            )
        append_event(
            session,
            event_type="generation_run.created",
            resource_type="generation_run",
            resource_id=run.id,
            details={
                "product_id": run.product_id,
                "provider": run.provider,
                "asset_count": len(run.assets),
                "estimated_cost": str(run.est_cost),
                "currency": run.currency,
            },
        )


def save_run(context: TenantContext, run: Run) -> None:
    """Compatibility upsert used by the importer; normal starts use create_run."""
    with tenant_session(context) as session:
        _ensure_workspace(session)
        row = session.get(GenerationRunRecord, run.id)
        if row is not None and row.workspace_id != context.workspace_id:
            raise KeyError("Run 不存在")
        if row is None:
            row = GenerationRunRecord(
                id=run.id,
                workspace_id=context.workspace_id,
                product_id=run.product_id,
                plan_id=run.plan_id,
                provider=run.provider,
                model=run.model,
                aspect_ratio=run.aspect_ratio,
                image_size=run.image_size,
                variants=run.variants,
                use_ref=run.use_ref,
                status=run.status,
                total=run.total,
                done=run.done,
                failed=run.failed,
                est_cost=run.est_cost,
                currency=run.currency,
                created_at=_as_dt(run.created_at),
                updated_at=utcnow(),
            )
            session.add(row)
        else:
            row.status = run.status
            row.done = run.done
            row.failed = run.failed
            row.updated_at = utcnow()
        session.flush()
        for asset in run.assets:
            asset_row = session.get(AssetRecord, asset.id)
            if asset_row is not None and asset_row.workspace_id != context.workspace_id:
                raise KeyError("素材不存在")
            if asset_row is None:
                session.add(
                    AssetRecord(
                        id=asset.id,
                        workspace_id=context.workspace_id,
                        product_id=run.product_id,
                        run_id=run.id,
                        combo_id=asset.combo_id,
                        combo_code=asset.combo_code,
                        tags_json=dict(asset.tags),
                        filename=asset.filename,
                        provider=asset.provider,
                        model=asset.model,
                        prompt=asset.prompt,
                        fingerprint=asset.fingerprint or _canonical_hash(asset.model_dump(mode="json")),
                        reused_from_asset_id=asset.reused_from_asset_id,
                        status=asset.status,
                        error_detail=asset.error,
                        created_at=_as_dt(asset.created_at),
                        updated_at=utcnow(),
                    )
                )
            else:
                asset_row.status = asset.status
                asset_row.error_detail = asset.error
                asset_row.updated_at = utcnow()


def get_run(context: TenantContext, pid: str, run_id: str) -> Optional[Run]:
    with tenant_session(context) as session:
        row = session.get(GenerationRunRecord, run_id)
        if not row or row.product_id != pid or row.workspace_id != context.workspace_id:
            return None
        return _run_from_record(session, row)


def list_runs(context: TenantContext, pid: str) -> list[Run]:
    with tenant_session(context) as session:
        rows = session.scalars(
            select(GenerationRunRecord)
            .where(
                GenerationRunRecord.workspace_id == context.workspace_id,
                GenerationRunRecord.product_id == pid,
            )
            .order_by(GenerationRunRecord.created_at.desc())
        ).all()
        return [_run_from_record(session, row) for row in rows]


def get_asset(context: TenantContext, asset_id: str) -> AssetRecord | None:
    with tenant_session(context) as session:
        row = session.get(AssetRecord, asset_id)
        if not row or row.workspace_id != context.workspace_id:
            return None
        session.expunge(row)
        return row


def find_reusable_asset(context: TenantContext, fingerprint: str) -> AssetRecord | None:
    if not fingerprint:
        return None
    with tenant_session(context) as session:
        row = session.scalar(
            select(AssetRecord)
            .where(
                AssetRecord.workspace_id == context.workspace_id,
                AssetRecord.fingerprint == fingerprint,
                AssetRecord.status == "ok",
            )
            .order_by(AssetRecord.created_at.desc())
            .limit(1)
        )
        if row:
            session.expunge(row)
        return row


def materialize_reuse(
    context: TenantContext, pid: str, run_id: str, filename: str, source: AssetRecord
) -> bool:
    if source.workspace_id != context.workspace_id:
        return False
    source_path = run_dir(pid, source.run_id) / source.filename
    target_path = run_dir(pid, run_id) / filename
    if not source_path.is_file():
        return False
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        target_path.hardlink_to(source_path)
    except OSError:
        shutil.copy2(source_path, target_path)
    return True


def mark_asset_running(context: TenantContext, asset_id: str) -> None:
    with tenant_session(context) as session:
        row = session.get(AssetRecord, asset_id)
        if not row or row.workspace_id != context.workspace_id:
            raise KeyError("素材不存在")
        row.status = "pending"
        row.error_code = ""
        row.error_detail = ""
        row.updated_at = utcnow()
        session.flush()
        run = session.get(GenerationRunRecord, row.run_id)
        if run and run.status == "queued":
            run.status = "running"
            run.updated_at = utcnow()


def _refresh_run_counts(session, run_id: str) -> None:
    run = session.get(GenerationRunRecord, run_id)
    if not run:
        return
    statuses = session.execute(
        select(AssetRecord.status, func.count()).where(AssetRecord.run_id == run_id).group_by(AssetRecord.status)
    ).all()
    counts = {status: count for status, count in statuses}
    run.done = int(counts.get("ok", 0))
    run.failed = int(counts.get("failed", 0))
    terminal = run.done + run.failed
    if run.status != "cancelled":
        run.status = "done" if terminal >= run.total else "running"
    run.updated_at = utcnow()


def _settle_generation_cost(session, run: GenerationRunRecord, job_id: str, *, success: bool) -> None:
    """Release one reservation and, on success, commit the estimated actual cost."""
    already_released = session.scalar(
        select(func.count()).select_from(CostLedgerRecord).where(
            CostLedgerRecord.job_id == job_id,
            CostLedgerRecord.entry_type == "released",
        )
    )
    if already_released:
        return
    unit_cost = config.COST_PER_IMAGE.get(run.provider, Decimal("0.05"))
    if unit_cost:
        session.add(
            CostLedgerRecord(
                id=new_id("cost"),
                workspace_id=current_context(session).workspace_id,
                run_id=run.id,
                job_id=job_id,
                entry_type="released",
                amount=unit_cost,
                currency=run.currency,
                note="生成项结束，释放成本预留",
            )
        )
        if success:
            session.add(
                CostLedgerRecord(
                    id=new_id("cost"),
                    workspace_id=current_context(session).workspace_id,
                    run_id=run.id,
                    job_id=job_id,
                    entry_type="actual",
                    amount=unit_cost,
                    currency=run.currency,
                    note="Provider 未返回 usage，按配置单价记录实际成本",
                )
            )


def complete_asset(
    context: TenantContext, asset_id: str, file_path: Path, *, job_id: str | None = None
) -> AssetVersion:
    raw = file_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    with tenant_session(context) as session:
        row = session.get(AssetRecord, asset_id)
        if not row or row.workspace_id != context.workspace_id:
            raise KeyError("素材不存在")
        row.status = "ok"
        row.error_code = ""
        row.error_detail = ""
        row.updated_at = utcnow()
        session.flush()
        existing = session.scalar(
            select(AssetVersionRecord).where(
                AssetVersionRecord.asset_id == asset_id,
                AssetVersionRecord.content_sha256 == digest,
            )
        )
        run = session.get(GenerationRunRecord, row.run_id)
        if existing is None:
            try:
                object_key = str(file_path.relative_to(config.DATA_DIR))
            except ValueError:
                object_key = str(file_path)
            existing = AssetVersionRecord(
                id=new_id("ver"),
                workspace_id=context.workspace_id,
                asset_id=asset_id,
                kind="generated",
                aspect_ratio=run.aspect_ratio,
                language="",
                object_key=object_key,
                content_sha256=digest,
                byte_size=len(raw),
                mime_type="image/png",
            )
            session.add(existing)
        if job_id:
            _settle_generation_cost(session, run, job_id, success=True)
        _refresh_run_counts(session, row.run_id)
        append_event(
            session,
            event_type="asset.completed",
            resource_type="asset",
            resource_id=asset_id,
            details={"job_id": job_id or "", "content_sha256": digest},
        )
        session.flush()
        return AssetVersion(
            id=existing.id,
            asset_id=existing.asset_id,
            parent_version_id=existing.parent_version_id,
            kind=existing.kind,
            aspect_ratio=existing.aspect_ratio,
            language=existing.language,
            object_key=existing.object_key,
            content_sha256=existing.content_sha256,
            byte_size=existing.byte_size,
            mime_type=existing.mime_type,
            currency=existing.currency,
            truth_package_id=existing.truth_package_id,
            created_at=_as_ts(existing.created_at),
        )


def fail_asset(
    context: TenantContext,
    asset_id: str,
    *,
    code: str,
    detail: str,
    job_id: str | None = None,
) -> None:
    with tenant_session(context) as session:
        row = session.get(AssetRecord, asset_id)
        if not row or row.workspace_id != context.workspace_id:
            raise KeyError("素材不存在")
        row.status = "failed"
        row.error_code = code
        row.error_detail = detail[:2000]
        row.updated_at = utcnow()
        session.flush()
        if job_id:
            run = session.get(GenerationRunRecord, row.run_id)
            _settle_generation_cost(session, run, job_id, success=False)
        _refresh_run_counts(session, row.run_id)
        append_event(
            session,
            event_type="asset.failed",
            resource_type="asset",
            resource_id=asset_id,
            details={"job_id": job_id or "", "error_code": code},
        )


def cancel_run(context: TenantContext, run_id: str) -> bool:
    with tenant_session(context) as session:
        row = session.get(GenerationRunRecord, run_id)
        if (
            not row
            or row.workspace_id != context.workspace_id
            or row.status not in ("queued", "running")
        ):
            return False
        row.status = "cancelled"
        row.revision += 1
        row.updated_at = utcnow()
        reserved = session.scalar(
            select(func.coalesce(func.sum(CostLedgerRecord.amount), 0)).where(
                CostLedgerRecord.run_id == run_id,
                CostLedgerRecord.entry_type == "reserved",
            )
        )
        released = session.scalar(
            select(func.coalesce(func.sum(CostLedgerRecord.amount), 0)).where(
                CostLedgerRecord.run_id == run_id,
                CostLedgerRecord.entry_type == "released",
            )
        )
        outstanding = Decimal(str(reserved or 0)) - Decimal(str(released or 0))
        cancellable_jobs = session.scalar(
            select(func.count()).select_from(JobRecord).where(
                JobRecord.workspace_id == context.workspace_id,
                JobRecord.resource_type == "run",
                JobRecord.resource_id == run_id,
                JobRecord.state.in_(("queued", "retry_wait", "leased")),
            )
        )
        unit_cost = config.COST_PER_IMAGE.get(row.provider, Decimal("0.05"))
        remaining = min(outstanding, Decimal(int(cancellable_jobs or 0)) * unit_cost)
        if remaining > 0:
            session.add(
                CostLedgerRecord(
                    id=new_id("cost"),
                    workspace_id=context.workspace_id,
                    run_id=run_id,
                    entry_type="released",
                    amount=remaining,
                    currency=row.currency,
                    note="Run 取消，释放未执行成本预留",
                )
            )
        append_event(
            session,
            event_type="generation_run.cancelled",
            resource_type="generation_run",
            resource_id=run_id,
            details={"released_cost": str(max(remaining, Decimal("0")))},
        )
    cancel_resource(context, "run", run_id)
    return True


# ---------- history + current projections ----------
_QUALITY_FROM_RATING = {1: "approved", 2: "needs_changes", -1: "rejected"}
_RATING_FROM_QUALITY = {"approved": 1, "needs_changes": 2, "rejected": -1}


def get_quality(context: TenantContext, pid: str) -> dict[str, str]:
    with tenant_session(context) as session:
        rows = session.scalars(
            select(QualityReviewRecord)
            .where(
                QualityReviewRecord.workspace_id == context.workspace_id,
                QualityReviewRecord.product_id == pid,
            )
            .order_by(QualityReviewRecord.created_at.desc(), QualityReviewRecord.id.desc())
        ).all()
        projection: dict[str, str | None] = {}
        for row in rows:
            projection.setdefault(row.asset_id, row.quality)
        return {asset_id: value for asset_id, value in projection.items() if value}


def set_quality(
    context: TenantContext, pid: str, asset_id: str, quality: Optional[str]
) -> dict[str, str]:
    with tenant_session(context) as session:
        asset = session.get(AssetRecord, asset_id)
        if (
            not asset
            or asset.workspace_id != context.workspace_id
            or asset.product_id != pid
        ):
            raise KeyError("素材不存在")
        session.add(
            QualityReviewRecord(
                id=new_id("qr"),
                workspace_id=context.workspace_id,
                product_id=pid,
                asset_id=asset_id,
                quality=quality,
                actor_id=context.initiator_id or context.executor,
            )
        )
        append_event(
            session,
            event_type="asset.quality_reviewed",
            resource_type="asset",
            resource_id=asset_id,
            details={"product_id": pid, "quality": quality},
        )
    return get_quality(context, pid)


def get_verdicts(context: TenantContext, pid: str) -> dict[str, dict]:
    with tenant_session(context) as session:
        experiment_rows = session.execute(
            select(ExperimentDecisionRecord, ExperimentEntryRecord, AssetVersionRecord)
            .join(ExperimentEntryRecord, ExperimentDecisionRecord.experiment_entry_id == ExperimentEntryRecord.id)
            .join(AssetVersionRecord, ExperimentEntryRecord.asset_version_id == AssetVersionRecord.id)
            .join(AssetRecord, AssetVersionRecord.asset_id == AssetRecord.id)
            .where(AssetRecord.product_id == pid, AssetRecord.workspace_id == context.workspace_id,
                   ExperimentEntryRecord.removed_at.is_(None))
            .order_by(ExperimentDecisionRecord.created_at.desc(), ExperimentDecisionRecord.id.desc())
        ).all()
        projected: dict[str, dict] = {}
        for decision, _entry, version in experiment_rows:
            projected.setdefault(version.asset_id, {
                "verdict": decision.verdict,
                "context": {
                    "source": "experiment_judge",
                    "experiment_id": decision.experiment_id,
                    "judgement_id": decision.judgement_id,
                    "metric": decision.metric,
                    "estimate": str(decision.estimate) if decision.estimate is not None else None,
                    "interval_low": str(decision.interval_low) if decision.interval_low is not None else None,
                    "interval_high": str(decision.interval_high) if decision.interval_high is not None else None,
                    "reason_codes": list(decision.reason_codes_json),
                },
                "set_at": _as_ts(decision.created_at),
            })
        rows = session.scalars(
            select(VerdictDecisionRecord)
            .where(
                VerdictDecisionRecord.workspace_id == context.workspace_id,
                VerdictDecisionRecord.product_id == pid,
            )
            .order_by(VerdictDecisionRecord.created_at.desc(), VerdictDecisionRecord.id.desc())
        ).all()
        projection: dict[str, VerdictDecisionRecord] = {}
        for row in rows:
            projection.setdefault(row.asset_id, row)
        legacy = {
            asset_id: {
                "verdict": row.verdict,
                "context": dict(row.context_json),
                "set_at": _as_ts(row.created_at),
            }
            for asset_id, row in projection.items()
            if row.verdict
        }
        for asset_id, value in legacy.items():
            projected.setdefault(asset_id, value)
        return projected


def set_verdict(
    tenant: TenantContext,
    pid: str,
    asset_id: str,
    verdict: Optional[str],
    context: Optional[dict] = None,
) -> dict[str, dict]:
    with tenant_session(tenant) as session:
        asset = session.get(AssetRecord, asset_id)
        if (
            not asset
            or asset.workspace_id != tenant.workspace_id
            or asset.product_id != pid
        ):
            raise KeyError("素材不存在")
        session.add(
            VerdictDecisionRecord(
                id=new_id("vd"),
                workspace_id=tenant.workspace_id,
                product_id=pid,
                asset_id=asset_id,
                verdict=verdict,
                context_json=context or {},
                source="legacy_endpoint",
                actor_id=tenant.initiator_id or tenant.executor,
            )
        )
        append_event(
            session,
            event_type="asset.verdict_recorded",
            resource_type="asset",
            resource_id=asset_id,
            details={"product_id": pid, "verdict": verdict, "context": context or {}},
        )
    return get_verdicts(tenant, pid)


def get_ratings(context: TenantContext, pid: str) -> dict[str, int]:
    return {
        asset_id: _RATING_FROM_QUALITY[value]
        for asset_id, value in get_quality(context, pid).items()
        if value in _RATING_FROM_QUALITY
    }


def set_rating(context: TenantContext, pid: str, asset_id: str, rating: int) -> dict[str, int]:
    set_quality(context, pid, asset_id, _QUALITY_FROM_RATING.get(rating))
    return get_ratings(context, pid)
