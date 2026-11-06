"""Authoritative source-market context for Localization.

The value is deliberately atomic: callers may use the immutable copy frozen
on an ExperimentVersion or the immutable OfferVersion source object, but may
never compose a synthetic context from unrelated columns.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from app.adapters.persistence.models import ExperimentVersionRecord


SOURCE_MARKET_FIELDS = ("market", "placement", "locale", "country", "currency")
_FULL_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{4})?-[A-Za-z]{2}$")


def resolve_source_market_snapshot(
    version: ExperimentVersionRecord,
    offer_content: Mapping[str, Any],
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """Resolve and validate one complete source context, or fail closed."""
    frozen = version.frozen_snapshot_json or {}
    candidates = [
        frozen.get("source_market_snapshot"),
        offer_content.get("source_market_snapshot"),
    ]
    present = [candidate for candidate in candidates if candidate is not None]
    if not present:
        return None, {
            "code": "missing_source_context",
            "missing_fields": list(SOURCE_MARKET_FIELDS),
        }

    normalized: list[dict[str, str]] = []
    for candidate in present:
        if not isinstance(candidate, dict):
            return None, {
                "code": "invalid_source_context",
                "invalid_fields": list(SOURCE_MARKET_FIELDS),
            }
        missing_fields = [
            field
            for field in SOURCE_MARKET_FIELDS
            if not isinstance(candidate.get(field), str)
            or not candidate[field].strip()
        ]
        if missing_fields:
            return None, {
                "code": "missing_source_context",
                "missing_fields": missing_fields,
            }
        value = {
            field: candidate[field].strip()
            for field in SOURCE_MARKET_FIELDS
        }
        invalid_fields: list[str] = []
        if value["market"] != version.market:
            invalid_fields.append("market")
        if value["placement"] != version.placement:
            invalid_fields.append("placement")
        if _FULL_LOCALE_RE.fullmatch(value["locale"]) is None:
            invalid_fields.append("locale")
        if not (
            len(value["country"]) == 2
            and value["country"].isalpha()
            and value["country"].isupper()
        ):
            invalid_fields.append("country")
        if not (
            len(value["currency"]) == 3
            and value["currency"].isalpha()
            and value["currency"].isupper()
        ):
            invalid_fields.append("currency")
        if invalid_fields:
            return None, {
                "code": "invalid_source_context",
                "invalid_fields": sorted(set(invalid_fields)),
            }
        normalized.append(value)

    if len(normalized) == 2 and normalized[0] != normalized[1]:
        return None, {
            "code": "conflicting_source_context",
            "conflicting_fields": [
                field
                for field in SOURCE_MARKET_FIELDS
                if normalized[0][field] != normalized[1][field]
            ],
        }
    return normalized[0], None
