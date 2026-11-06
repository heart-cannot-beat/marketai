"""Run one isolated, zero-cost business flow against the real local database.

The smoke tenant is deliberately separate from the default workspace. Generation
uses MockProvider and launch uses FakeMetaWriteAdapter, so the script cannot make
remote Meta writes or pollute the production workspace's ad bindings.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import timedelta


os.environ["MARKETAI_WORKSPACE_ID"] = os.getenv("MARKETAI_SMOKE_WORKSPACE_ID", "ws_smoke")

from sqlalchemy import select  # noqa: E402

from app import config, store  # noqa: E402
from app.adapters.meta.fake_write import FakeMetaWriteAdapter  # noqa: E402
from app.adapters.persistence.database import tenant_session  # noqa: E402
from app.adapters.persistence.models import (  # noqa: E402
    AssetRecord,
    AssetVersionRecord,
    MetaAccountRecord,
    utcnow,
)
from app.experiments import service as experiment_service  # noqa: E402
from app.launch import service as launch_service  # noqa: E402
from app.meta.contracts import contract_hash  # noqa: E402
from app.pipeline import manager  # noqa: E402
from app.schemas import (  # noqa: E402
    AngleSchema,
    Combo,
    Dimension,
    DimensionValue,
    GenRequest,
    Plan,
    Product,
)
from app.worker_runtime import WorkerRuntime  # noqa: E402
from app.platform.tenancy import TenantContext  # noqa: E402


SMOKE_CONTEXT = TenantContext(
    workspace_id=config.DEFAULT_WORKSPACE_ID,
    role="owner",
    initiator_id="smoke-runner",
    request_id="smoke-business-flow",
)


def _ensure_smoke_account() -> MetaAccountRecord:
    with tenant_session(SMOKE_CONTEXT) as session:
        existing = session.scalar(select(MetaAccountRecord).where(
            MetaAccountRecord.workspace_id == SMOKE_CONTEXT.workspace_id,
            MetaAccountRecord.ad_account_id == config.META_AD_ACCOUNT_ID,
        ))
        if existing is not None:
            return existing
        account = MetaAccountRecord(
            id=f"mact_smoke_{uuid.uuid4().hex[:10]}",
            workspace_id=SMOKE_CONTEXT.workspace_id,
            ad_account_id=config.META_AD_ACCOUNT_ID,
            name="MarketAI isolated smoke account",
            currency=config.DEFAULT_CURRENCY,
            timezone_name=config.META_TIMEZONE,
            api_version=config.META_API_VERSION,
        )
        session.add(account)
        session.flush()
        return account


async def run_smoke() -> dict:
    if SMOKE_CONTEXT.workspace_id == "ws_default":
        raise RuntimeError("Smoke flow refuses to run in the default workspace")
    if config.META_WRITE_ENABLED:
        raise RuntimeError("Close META_WRITE_ENABLED before running the isolated smoke flow")
    if not config.META_AD_ACCOUNT_ID:
        raise RuntimeError("META_AD_ACCOUNT_ID is required for launch preview validation")
    if not config.LAUNCH_CONFIRMATION_SECRET:
        raise RuntimeError("Launch confirmation secret is required")

    suffix = uuid.uuid4().hex[:10]
    product = Product(
        id=f"prod_smoke_{suffix}",
        name=f"MarketAI Pipeline Smoke {suffix}",
        description="Isolated zero-cost fixture for generation-to-decision verification.",
        selling_points=["deterministic pipeline fixture"],
        audience="internal QA only",
    )
    store.save_product(SMOKE_CONTEXT, product)

    dimension = Dimension(
        id=f"dim_hook_{suffix}",
        label="钩子",
        values=[DimensionValue(
            id=f"value_hook_{suffix}",
            label="生产链路验证",
            prompt_fragment="production pipeline verification card",
        )],
    )
    angles = AngleSchema(
        product_id=product.id,
        dimensions=[dimension],
        style_notes="clean QA fixture, legible product card",
        source="manual",
    )
    store.save_angles(SMOKE_CONTEXT, angles)
    plan = Plan(
        id=f"plan_smoke_{suffix}",
        product_id=product.id,
        mode="full",
        cap=0,
        total_full=1,
        combos=[Combo(
            id="C01",
            code="A1",
            values={dimension.id: dimension.values[0].id},
            tags={dimension.label: dimension.values[0].label},
        )],
    )
    store.save_plan(SMOKE_CONTEXT, plan)

    run = manager.start(
        SMOKE_CONTEXT,
        product,
        angles,
        plan,
        GenRequest(product_id=product.id, provider="mock", variants=2, use_ref=False),
    )
    runtime = WorkerRuntime(owner=f"smoke-generation-{suffix}")
    handled = [await runtime.run_once(), await runtime.run_once()]
    persisted_run = store.get_run(SMOKE_CONTEXT, product.id, run.id)
    if not all(handled) or persisted_run is None or persisted_run.status != "done" or persisted_run.done != 2:
        raise RuntimeError("Mock generation did not complete exactly two assets")
    for asset in persisted_run.assets:
        store.set_quality(SMOKE_CONTEXT, product.id, asset.id, "approved")

    with tenant_session(SMOKE_CONTEXT) as session:
        versions = session.scalars(select(AssetVersionRecord).join(AssetRecord).where(
            AssetRecord.product_id == product.id,
        ).order_by(AssetVersionRecord.created_at)).all()
        if len(versions) != 2:
            raise RuntimeError("Generated assets do not have exactly two immutable AssetVersions")
        version_ids = [version.id for version in versions]

    account = _ensure_smoke_account()
    experiment_id = experiment_service.create_experiment(
        SMOKE_CONTEXT,
        product_id=product.id,
        name=f"Isolated mock experiment {suffix}",
        metric_contract_hash=contract_hash(),
        config_snapshot={
            "primary_metric": "ctr",
            "ctr_threshold": "0.02",
            "currency": account.currency,
            "min_days": 1,
            "min_impressions": 1,
            "min_clicks": 0,
            "min_purchases": "0",
            "alpha": "0.05",
            "bootstrap_samples": 500,
            "require_fresh_data": False,
        },
        entries=[{
            "asset_version_id": version_id,
            "marketing_vars": {
                "persona": "internal QA",
                "angle": "pipeline integrity",
                "hook": f"production path smoke {index + 1}",
                "offer": "none",
                "landing_page": "https://example.com/marketai-smoke",
            },
        } for index, version_id in enumerate(version_ids)],
    )
    frozen = experiment_service.freeze_experiment(
        SMOKE_CONTEXT, experiment_id, 1
    )
    entry_id = frozen["entries"][0]["id"]

    preview = launch_service.create_preview(
        SMOKE_CONTEXT,
        experiment_id=experiment_id,
        entry_id=entry_id,
        ad_account_id=config.META_AD_ACCOUNT_ID,
        page_id="page_isolated_smoke",
        landing_page_url="https://example.com/marketai-smoke",
        message="MarketAI isolated pipeline smoke",
        headline="Pipeline integrity check",
        country="US",
        daily_budget_minor=1,
    )
    confirmation = launch_service.confirm_preview(
        SMOKE_CONTEXT, preview["id"], preview["preview_hash"],
    )
    remote_state: dict = {}
    fake_adapter = FakeMetaWriteAdapter(remote_state)
    operation_id = launch_service.start_operation(
        SMOKE_CONTEXT,
        preview["id"],
        confirmation["confirmation_token"],
        test_adapter=fake_adapter,
    )
    launch_handled = await WorkerRuntime(
        owner=f"smoke-launch-{suffix}", launch_adapter=fake_adapter,
    ).run_once()
    operation = launch_service.get_operation(SMOKE_CONTEXT, operation_id)
    if not launch_handled or operation["state"] != "completed":
        raise RuntimeError("Fake PAUSED launch did not complete")
    delivery_statuses = {
        item["configured_status"] for item in remote_state.values()
        if item["configured_status"] != "N/A"
    }
    if delivery_statuses != {"PAUSED"}:
        raise RuntimeError("Smoke launch created a non-PAUSED delivery object")

    judgement = experiment_service.run_judgement(
        SMOKE_CONTEXT, experiment_id, as_of=utcnow().date() - timedelta(days=1),
    )
    closed = experiment_service.close_experiment(
        SMOKE_CONTEXT, experiment_id, frozen["revision"],
    )
    return {
        "ok": True,
        "workspace_id": SMOKE_CONTEXT.workspace_id,
        "remote_write": False,
        "product_id": product.id,
        "plan_id": plan.id,
        "run_id": run.id,
        "asset_ids": [asset.id for asset in persisted_run.assets],
        "asset_version_ids": version_ids,
        "quality": store.get_quality(SMOKE_CONTEXT, product.id),
        "experiment_id": experiment_id,
        "experiment_state": closed["state"],
        "judgement_summary": judgement["result"]["summary"],
        "launch_operation_id": operation_id,
        "launch_state": operation["state"],
        "launch_steps": [item["step"] for item in operation["steps"]],
        "all_delivery_objects_paused": True,
    }


def main() -> None:
    print(json.dumps(asyncio.run(run_smoke()), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
