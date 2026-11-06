"""Typed downstream evidence options; Library item extensions remain JSON."""
from typing import Literal
from pydantic import JsonValue
from .contracts import Document


class EvidenceAssetOption(Document):
    asset_version_id: str
    label: str
    localization: bool
    scale: bool
    scope: Literal['repeatable_combination', 'exact_asset_only']


class EvidenceSourceContext(Document):
    market: str
    placement: str
    locale: str
    country: str
    currency: str


class EvidenceOfferOption(Document):
    id: str
    label: str
    product: str | None
    context: EvidenceSourceContext
    item: dict[str, JsonValue] | None
    landing_page: str | None


class ExistingEvidenceHandoff(Document):
    decision_id: str
    asset_version_id: str
    evaluation_id: str
    current: bool
    scope: Literal['repeatable_combination', 'exact_asset_only']


class EvidenceHandoffOptions(Document):
    evaluation_id: str | None
    assets: list[EvidenceAssetOption]
    offers: list[EvidenceOfferOption]
    existing: list[ExistingEvidenceHandoff]
    evidence_source: Literal['user_supplied_delivery']
    no_meta_writes: Literal[True]


class EvidenceHandoffEnvelope(Document):
    id: str
    data: EvidenceHandoffOptions


class CreatedEvidenceHandoff(Document):
    decision_id: str
    evidence_origin: Literal['marketing_business_target']
    no_meta_writes: Literal[True]


class CreatedEvidenceHandoffEnvelope(Document):
    id: str
    data: CreatedEvidenceHandoff
