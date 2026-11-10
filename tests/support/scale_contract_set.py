"""Fixtures for the Scale metric contract set and its scheduled reads (#108, #101 S2).

The contract set itself is always built by production code
(``app.scale.delivery_targets.ensure_metric_contract_set``). What these
helpers make is everything around it the product does not create yet:
the sandbox connection (through the API), the rows a DeliveryTarget refers to
(an AssetVersion, an Offer and a Landing Page version -- their product entries
and the DeliveryTarget entry itself are #63), and a DeliveryTarget request on
the connection. The request names a sandbox ad with the campaign, ad set and
creative the sandbox's configuration read returns for it
(:func:`sandbox_ad_hierarchy`): Scale's collector checks them against that
read, and made-up parents would block every manifest as
``source_provenance_mismatch`` (#110). Fictional brand only: Acme Charge.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    PlanRecord,
    ProductRecord,
    new_id,
)
from app.adapters.meta.fake_adapter import FakeMetaAdapter
from app.meta.models import ScenarioVersionRecord
from app.platform.canonical import canonical_hash
from app.scale.delivery_targets import DeliveryTargetRequest
from tests.support.meta_observation import AD_ACCOUNT

OBSERVED_AT = datetime(2026, 11, 1, tzinfo=timezone.utc)


def create_connection_through_api(test, client, meta_mode: str = "customer_sandbox") -> dict:
    """A connection and its binding, each through its route -- no contract yet."""
    connection = client.post("/api/v1/meta/connections", json={"ad_account_id": AD_ACCOUNT, "meta_mode": meta_mode})
    test.assertEqual(connection.status_code, 201, connection.text)
    connection = connection.json()
    binding = client.post(f"/api/v1/meta/connections/{connection['id']}/bindings",
                          json={"ad_account_id": AD_ACCOUNT, "meta_mode": meta_mode})
    test.assertEqual(binding.status_code, 201, binding.text)
    return connection


def seed_target_references(session, workspace_id: str, tag: str) -> dict[str, str]:
    """An AssetVersion, an Offer version and a Landing Page version (#63 makes them in the product)."""
    product = ProductRecord(id=new_id("prod"), workspace_id=workspace_id, name=f"Acme Charge Dock {tag}")
    session.add(product)
    session.flush()
    plan = PlanRecord(id=new_id("plan"), workspace_id=workspace_id, product_id=product.id, mode="full",
                      snapshot_hash="p" * 64)
    session.add(plan)
    session.flush()
    run = GenerationRunRecord(
        id=new_id("run"), workspace_id=workspace_id, product_id=product.id, plan_id=plan.id,
        provider="mock-vision", model="mock-vision-1", aspect_ratio="1:1", image_size="1024x1024", variants=1,
        use_ref=False, status="succeeded", currency="USD",
    )
    session.add(run)
    session.flush()
    asset = AssetRecord(
        id=new_id("asset"), workspace_id=workspace_id, product_id=product.id, run_id=run.id,
        combo_id=f"combo_{tag}", combo_code=f"combo-{tag}", tags_json={}, filename=f"acme_{tag}.png",
        provider="mock-vision", model="mock-vision-1", prompt="Acme Charge dock on a desk",
        fingerprint=f"fp_{tag}", status="succeeded",
    )
    session.add(asset)
    session.flush()
    version = AssetVersionRecord(
        id=new_id("astv"), workspace_id=workspace_id, asset_id=asset.id, kind="image", aspect_ratio="1:1",
        object_key=f"assets/acme_{tag}.png", content_sha256="a" * 64, byte_size=1024, mime_type="image/png",
    )
    session.add(version)
    session.flush()
    library = {}
    for entry_type in ("offer", "landing_page"):
        entry = LibraryEntryRecord(id=new_id("lent"), workspace_id=workspace_id, entry_type=entry_type,
                                   display_name=f"Acme Charge {entry_type} {tag}")
        session.add(entry)
        session.flush()
        content = {"brand": "Acme Charge", "kind": entry_type, "tag": tag}
        row = LibraryVersionRecord(
            id=new_id("lver"), workspace_id=workspace_id, entry_id=entry.id, entry_type=entry_type, version_no=1,
            content_json=content, content_hash=canonical_hash(content), published_by="test-fixture",
        )
        session.add(row)
        session.flush()
        library[entry_type] = row.id
    return {"asset_version_id": version.id, "offer_version_id": library["offer"],
            "landing_page_version_id": library["landing_page"]}


@dataclass(frozen=True)
class AdHierarchy:
    """A sandbox ad and the parents its configuration read returns."""

    ad_id: str
    adset_id: str
    campaign_id: str
    creative_id: str


def sandbox_ad_hierarchy(session, *, workspace_id: str, meta_mode: str, connection_id: str, ad_id: str,
                         as_of: date) -> AdHierarchy:
    """The ad's campaign, ad set and creative, as the connection's default
    scenario answers the ``ads`` configuration edge on ``as_of``. Reads only."""
    scenario_id = session.scalar(select(ScenarioVersionRecord.id).where(
        ScenarioVersionRecord.workspace_id == workspace_id,
        ScenarioVersionRecord.meta_mode == meta_mode,
        ScenarioVersionRecord.meta_connection_id == connection_id,
        ScenarioVersionRecord.is_default == True))  # noqa: E712
    page = FakeMetaAdapter(session, workspace_id, meta_mode, scenario_id).read_delivery_configuration(
        ad_account_id=AD_ACCOUNT, edge="ads", fields=["id", "adset_id", "campaign_id", "creative"], as_of=as_of,
        limit=100)
    row = next(item for item in page.payload["data"] if item["id"] == ad_id)
    return AdHierarchy(ad_id=ad_id, adset_id=row["adset_id"], campaign_id=row["campaign_id"],
                       creative_id=row["creative"]["id"])


def delivery_target_request(*, connection_id: str, scenario_version_id: str, meta_mode: str,
                            hierarchy: AdHierarchy, references: dict[str, str], contract_generation_id: str = "",
                            market_code: str = "US", attribution_setting: str = "7d_click_1d_view",
                            observed_at: datetime = OBSERVED_AT) -> DeliveryTargetRequest:
    """A curated-scenario DeliveryTarget on a sandbox ad, bound later to the connection's contract set."""
    return DeliveryTargetRequest(
        meta_connection_id=connection_id,
        ad_account_id=AD_ACCOUNT,
        scenario_scope_key=scenario_version_id if meta_mode == "controlled_mock" else "__no_scenario__",
        campaign_remote_id=hierarchy.campaign_id,
        adset_remote_id=hierarchy.adset_id,
        ad_remote_id=hierarchy.ad_id,
        ad_creative_id=hierarchy.creative_id,
        creative_asset_version_id=references["asset_version_id"],
        creative_binding_evidence_hash="e" * 64,
        market_code=market_code,
        placement="feed",
        attribution_setting=attribution_setting,
        contract_generation_id=contract_generation_id,
        offer_version_id=references["offer_version_id"],
        landing_page_version_id=references["landing_page_version_id"],
        source_type="curated_scenario",
        source_manifest={"schema_version": 1, "source_type": "curated_scenario"},
        observed_at=observed_at,
        expires_at=OBSERVED_AT + timedelta(days=365),
        scenario_version_id=scenario_version_id,
    )
