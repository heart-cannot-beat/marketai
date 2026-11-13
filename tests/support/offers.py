"""Publish a Product and its Offers through the Library service (#54).

Manual fact rows name an Offer entry, and publish checks that the entry has
a published OfferVersion whose ``market_scope`` covers the row's market.
Tests publish both through ``app.library.service`` instead of inserting
LibraryVersion rows, so each Offer has passed the same validation as an
Owner's. Offers publish only for an Owner. Call these outside any open
``tenant_session``: the service opens its own transaction.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.library import service
from app.library.contracts import ENTRY_OFFER, ENTRY_PRODUCT
from app.platform.tenancy import TenantContext


def _publish(ctx: TenantContext, entry_type: str, display_name: str, content: dict) -> tuple[str, str]:
    entry = service.create_entry(ctx, entry_type=entry_type, display_name=display_name, content=content)
    version = service.publish(ctx, entry["id"], entry["draft"]["revision"])
    return entry["id"], version["id"]


def publish_product(ctx: TenantContext, name: str = "Acme Charge 65W charger") -> tuple[str, str]:
    """A published Product with the fewest fields publish accepts: ``(entry_id, version_id)``."""
    return _publish(ctx, ENTRY_PRODUCT, name, {
        "name": name,
        "description": "Compact 65W GaN charger",
        "audience": "Commuters who charge a laptop and a phone",
    })


def offer_content(
    product_version_id: str, *, markets: Sequence[str], sku: str = "ACME-65W", **overrides: object
) -> dict:
    content: dict = {
        "product_version_id": product_version_id,
        "item": {"sku": sku, "quantity": 1},
        "landing_page_url": f"https://acme-charge.example/offers/{sku.lower()}",
        "market_scope": list(markets),
        "valid_from": "2026-01-01",
    }
    content.update(overrides)
    return content


def publish_offer(
    owner: TenantContext,
    product_version_id: str,
    *,
    markets: Sequence[str],
    sku: str = "ACME-65W",
    **overrides: object,
) -> tuple[str, str]:
    """A published Offer of the Product: ``(entry_id, version_id)``."""
    return _publish(
        owner, ENTRY_OFFER, f"Offer {sku}",
        offer_content(product_version_id, markets=markets, sku=sku, **overrides),
    )


def republish_offer(owner: TenantContext, entry_id: str, content: dict) -> str:
    """Save new Offer content and publish it; the old version is superseded."""
    revision = int(service.get_entry(owner, entry_id)["draft"]["revision"])
    draft = service.save_draft(owner, entry_id, content, expected_revision=revision)
    return service.publish(owner, entry_id, draft["revision"])["id"]


def publish_offers(
    owner: TenantContext, *, markets: Sequence[str], skus: Sequence[str] = ("ACME-65W",)
) -> list[tuple[str, str]]:
    """One Product and one published Offer per SKU, all covering ``markets``."""
    _product_entry, product_version = publish_product(owner)
    return [publish_offer(owner, product_version, markets=markets, sku=sku) for sku in skus]
