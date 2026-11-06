from app.localization.claim_facts import (
    candidate_fact_types,
    is_market_sensitive_claim,
    required_fact_types,
)


def test_claim_aliases_share_one_server_mapping() -> None:
    assert candidate_fact_types("price_claim") == ("price",)
    assert candidate_fact_types("delivery_claim") == ("delivery",)
    assert candidate_fact_types("shipping_availability") == (
        "shipping",
        "availability",
    )
    assert candidate_fact_types("certification_claim") == ("product_claim",)


def test_only_mapped_claims_are_market_sensitive() -> None:
    assert is_market_sensitive_claim("bundle_price") is True
    assert is_market_sensitive_claim("headline") is False
    assert candidate_fact_types("unknown") == ()


def test_required_fact_types_deduplicates_aliases() -> None:
    assert required_fact_types(
        ["price", "price_claim", "shipping_availability", "headline"]
    ) == ("availability", "price", "shipping")
