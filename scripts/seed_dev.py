"""Idempotently seed one realistic product fixture into the local dev workspace."""
from __future__ import annotations

import json

from app import config, store
from app.schemas import (
    AngleSchema,
    Combo,
    Dimension,
    DimensionValue,
    Plan,
    Product,
    ProductEconomics,
)
from app.platform.tenancy import TenantContext


PRODUCT_ID = "prod_dev_cordless_vacuum"
PLAN_ID = "plan_dev_cordless_vacuum"


DEV_CONTEXT = TenantContext(
    workspace_id=config.DEFAULT_WORKSPACE_ID,
    role="owner",
    initiator_id="user:local-dev",
    request_id="seed-dev",
)


def seed() -> dict:
    if not config.DATABASE_URL.startswith("sqlite"):
        raise RuntimeError("Development seed refuses to run against a non-SQLite database")

    product = Product(
        id=PRODUCT_ID,
        name="[DEV] Cordless Car Vacuum",
        description="Portable cordless vacuum fixture for exercising MarketAI creative workflows.",
        selling_points=[
            "12000Pa suction",
            "USB-C fast charging",
            "Washable HEPA filter",
            "Compact storage for car interiors",
        ],
        audience="Urban car owners who care about quick interior cleanup",
        brand_notes="Clean graphite product, electric-blue accent, no unsupported performance claims",
        economics=ProductEconomics(
            price="59.90",
            discount="10.00",
            cogs="18.50",
            payment_fee="1.80",
            fulfilment_cost="4.50",
            shipping_subsidy="3.00",
            expected_refund_rate="0.05",
            target_contribution_margin="15.00",
            currency=config.DEFAULT_CURRENCY,
        ),
    )
    store.save_product(DEV_CONTEXT, product)

    hook = Dimension(
        id="dim_dev_hook",
        label="钩子",
        values=[
            DimensionValue(
                id="hook_dev_problem",
                label="缝隙灰尘痛点",
                prompt_fragment="close-up of crumbs and dust trapped in a car-seat gap",
            ),
            DimensionValue(
                id="hook_dev_speed",
                label="三分钟快速清洁",
                prompt_fragment="three-minute rapid car interior cleanup demonstration",
            ),
        ],
    )
    scene = Dimension(
        id="dim_dev_scene",
        label="场景",
        values=[
            DimensionValue(
                id="scene_dev_garage",
                label="家庭车库",
                prompt_fragment="bright organized home garage",
            ),
            DimensionValue(
                id="scene_dev_parking",
                label="城市停车场",
                prompt_fragment="modern urban parking structure",
            ),
        ],
    )
    angles = AngleSchema(
        product_id=product.id,
        dimensions=[hook, scene],
        style_notes="premium direct-response product photography, truthful scale, legible composition",
        source="manual",
    )
    store.save_angles(DEV_CONTEXT, angles)

    combos = [
        Combo(
            id="C01",
            code="A1-B1",
            values={hook.id: hook.values[0].id, scene.id: scene.values[0].id},
            tags={hook.label: hook.values[0].label, scene.label: scene.values[0].label},
        ),
        Combo(
            id="C02",
            code="A2-B2",
            values={hook.id: hook.values[1].id, scene.id: scene.values[1].id},
            tags={hook.label: hook.values[1].label, scene.label: scene.values[1].label},
        ),
    ]
    plan = Plan(
        id=PLAN_ID,
        product_id=product.id,
        mode="coverage",
        cap=2,
        total_full=4,
        combos=combos,
    )
    store.save_plan(DEV_CONTEXT, plan)
    return {
        "ok": True,
        "workspace_id": DEV_CONTEXT.workspace_id,
        "database": "sqlite",
        "product_id": product.id,
        "plan_id": plan.id,
        "dimensions": len(angles.dimensions),
        "combos": len(plan.combos),
        "remote_write": False,
    }


def main() -> None:
    print(json.dumps(seed(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
