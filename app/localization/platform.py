"""Platform-scoped Market Policy seed for the v9 Beta (Issue #24).

Market Policy is a platform-global catalog, not Workspace data: it is
seeded once per environment (via ``platform_session``, once the migration
that creates these tables lands) rather than per tenant.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    MarketPolicyVersionEventRecord,
    new_id,
    utcnow,
)

# (market_id, locale, country, currency) for each of the nine v9 Beta markets.
_BETA_CATALOG = (
    ("en-GB", "en-GB", "GB", "GBP"),
    ("en-AU", "en-AU", "AU", "AUD"),
    ("en-CA", "en-CA", "CA", "CAD"),
    ("de-DE", "de-DE", "DE", "EUR"),
    ("fr-FR", "fr-FR", "FR", "EUR"),
    ("es-ES", "es-ES", "ES", "EUR"),
    ("it-IT", "it-IT", "IT", "EUR"),
    ("ja-JP", "ja-JP", "JP", "JPY"),
    ("ko-KR", "ko-KR", "KR", "KRW"),
)


def _default_digit_format(locale: str) -> dict:
    if locale.startswith(("de", "fr", "es", "it")):
        return {
            "decimal_separator": ",",
            "grouping_separator": ".",
            "currency_position": "suffix",
        }
    if locale.startswith(("ja", "ko")):
        return {
            "decimal_separator": ".",
            "grouping_separator": ",",
            "currency_position": "prefix",
        }
    return {
        "decimal_separator": ".",
        "grouping_separator": ",",
        "currency_position": "prefix",
    }


def seed_beta_market_catalog(session: Session) -> list[MarketPolicyRecord]:
    """Idempotent seed for the nine v9 Beta markets.

    Creates, for each market not already present, a ``MarketPolicyRecord``,
    its first (and, at seed time, only) effective
    ``MarketPolicyVersionRecord``, and the ``published``
    ``MarketPolicyVersionEventRecord`` that records that version becoming
    effective. Markets that already exist are left untouched -- re-running
    the seed is a no-op for them.
    """
    existing = {r.market_id for r in session.query(MarketPolicyRecord).all()}
    policies: list[MarketPolicyRecord] = []
    now = utcnow()
    for market_id, locale, country, currency in _BETA_CATALOG:
        if market_id in existing:
            continue

        policy = MarketPolicyRecord(
            id=new_id("mp"),
            market_id=market_id,
            locale=locale,
            country=country,
            currency=currency,
            created_at=now,
        )
        session.add(policy)
        session.flush()

        version = MarketPolicyVersionRecord(
            id=new_id("mpv"),
            market_policy_id=policy.id,
            version_number=1,
            version_state="effective",
            locale=locale,
            country=country,
            currency=currency,
            pricing_context_json={},
            digit_format_json=_default_digit_format(locale),
            base_tone="neutral",
            reserved_words_json=[],
            restricted_claim_types_json=[],
            requires_local_copy=not locale.startswith("en"),
            shipping_address_granularity="country",
            max_fact_staleness_seconds=86400,
            qc_rules_json={},
            created_at=now,
        )
        session.add(version)
        session.flush()

        event = MarketPolicyVersionEventRecord(
            id=new_id("mpve"),
            market_policy_id=policy.id,
            market_policy_version_id=version.id,
            sequence=1,
            event_type="published",
            previous_state=None,
            current_state="effective",
            deduplication_key=f"mpve-{version.id}-published",
            actor_id="system",
            created_at=now,
        )
        session.add(event)
        session.flush()

        policies.append(policy)
    return policies
