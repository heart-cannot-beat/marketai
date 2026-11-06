"""Canonical mapping from observed source claims to Truth fact families.

Source analysis emits a broader vocabulary than the Truth resolver.  This
module is the one translation boundary used by preparation validation,
decision validation, and read projections.  Browser code must consume the
projected candidates instead of recreating this mapping.
"""
from __future__ import annotations

from collections.abc import Iterable


_CLAIM_FACT_TYPES: dict[str, tuple[str, ...]] = {
    "price": ("price",),
    "price_claim": ("price",),
    "component_price": ("price", "product_claim"),
    "component_price_claim": ("price", "product_claim"),
    "bundle_price": ("price", "product_claim"),
    "bundle_price_claim": ("price", "product_claim"),
    "discount": ("discount",),
    "discount_claim": ("discount",),
    "component": ("product_claim",),
    "component_claim": ("product_claim",),
    "bundle": ("product_claim",),
    "bundle_claim": ("product_claim",),
    "product": ("product_claim",),
    "product_claim": ("product_claim",),
    "compliance": ("product_claim",),
    "compliance_claim": ("product_claim",),
    "certification": ("product_claim",),
    "certification_claim": ("product_claim",),
    "health": ("product_claim",),
    "health_claim": ("product_claim",),
    "shipping": ("shipping",),
    "shipping_claim": ("shipping",),
    "shipping_availability": ("shipping", "availability"),
    "shipping_availability_claim": ("shipping", "availability"),
    "availability": ("availability",),
    "availability_claim": ("availability",),
    "delivery": ("delivery",),
    "delivery_time": ("delivery",),
    "delivery_claim": ("delivery",),
}


def candidate_fact_types(claim_type: object) -> tuple[str, ...]:
    """Return the ordered Truth fact families accepted for ``claim_type``."""

    if not isinstance(claim_type, str):
        return ()
    return _CLAIM_FACT_TYPES.get(claim_type.strip().lower(), ())


def is_market_sensitive_claim(claim_type: object) -> bool:
    """Mapped claim families are market-sensitive and cannot be preserved."""

    return bool(candidate_fact_types(claim_type))


def required_fact_types(claim_types: Iterable[object]) -> tuple[str, ...]:
    """Return every Truth family required by a set of observed claim types."""

    required = {
        fact_type
        for claim_type in claim_types
        for fact_type in candidate_fact_types(claim_type)
    }
    return tuple(sorted(required))
