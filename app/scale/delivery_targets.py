"""``ScaleDeliveryTargetRecord`` population and lookup (spec §4.3, Task 7).

This module owns the deep-module boundary around
:class:`app.scale.models.ScaleDeliveryTargetRecord`: it is the only place
that computes ``target_fingerprint``/``source_manifest_hash`` and the only
place that decides whether a ``(source_type, scenario_version_id,
source_sync_run_id, source_publication_id, source_attestation_id)``
combination is legal, mirroring the database CHECK constraints as an
application-level fail-fast (a caller gets a typed exception, not a raw
``IntegrityError``, when it hands this module a malformed source shape).

Scope: this module takes each delivery-slot field as an already-resolved
value. Actually *resolving* those values -- reading ``FakeMetaObjectRecord``
hierarchy state for ``curated_scenario``, or evidence pages and
``ObservationContractGenerationRecord.attribution_window`` for
``evidence_observation`` (spec §4.3's Population table), including "exactly
one AssetVersion" creative-mapping validation -- is future population-path
work, not this task's file list. :func:`create_delivery_target` is the
narrow-waist boundary that work will call through.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id
from app.platform.tenancy import TenantContext

from .canonical_lineage import canonical_lineage_hash
from .models import ScaleDeliveryTargetRecord

__all__ = [
    "DeliveryTargetRequest",
    "DeliveryTargetError",
    "InvalidSourceProvenanceError",
    "DeliveryTargetNotFoundError",
    "DeliveryTargetExpiredError",
    "create_delivery_target",
    "validate_delivery_target",
]

# Schema version folded into target_fingerprint's canonical_lineage_hash
# envelope. Bump when the field set below changes. Corresponds to the
# spec's "target-v1" label (§4.3); canonical_lineage_hash's version
# parameter is a plain int, not that ASCII string.
_TARGET_FINGERPRINT_VERSION = 1

# Schema version for source_manifest_hash. Corresponds to the spec's
# "delivery-target-source-manifest-v1" label (§4.3).
_SOURCE_MANIFEST_HASH_VERSION = 1

_CURATED_SCENARIO_MODES = ("customer_sandbox", "controlled_mock")
_EVIDENCE_OBSERVATION_MODE = "internal_validation"

# Source-type biconditional (spec §4.3): exactly one provenance shape per
# row. Mirrors ck_sdt_source_type_biconditional so a malformed request fails
# in Python before it ever reaches the database CHECK.
_REQUIRED_REFS_BY_SOURCE_TYPE = {
    "curated_scenario": frozenset({"scenario_version_id"}),
    "evidence_observation": frozenset(
        {"source_sync_run_id", "source_publication_id", "source_attestation_id"}
    ),
}
_ALL_PROVENANCE_REFS = frozenset(
    {"scenario_version_id", "source_sync_run_id", "source_publication_id", "source_attestation_id"}
)


class DeliveryTargetError(RuntimeError):
    """Deterministic, stably-coded rejection raised by this module."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class InvalidSourceProvenanceError(DeliveryTargetError):
    """The (source_type, provenance reference) combination is illegal."""


class DeliveryTargetNotFoundError(DeliveryTargetError):
    def __init__(self, target_id: str) -> None:
        self.target_id = target_id
        super().__init__("delivery_target_not_found", target_id)


class DeliveryTargetExpiredError(DeliveryTargetError):
    def __init__(self, target_id: str) -> None:
        self.target_id = target_id
        super().__init__("delivery_target_expired", target_id)


@dataclass(frozen=True)
class DeliveryTargetRequest:
    """One delivery slot's caller-resolved, ready-to-persist fields.

    The caller (a future population path) is responsible for resolving
    these from ``FakeMetaObjectRecord``/evidence pages and validating the
    creative-to-AssetVersion mapping is exact (spec §4.3 Population);
    this module trusts the values and only re-validates what it can
    determine from the request shape itself.
    """

    meta_connection_id: str
    ad_account_id: str
    scenario_scope_key: str
    campaign_remote_id: str
    adset_remote_id: str
    ad_remote_id: str

    ad_creative_id: str
    creative_asset_version_id: str
    creative_binding_evidence_hash: str
    market_code: str
    placement: str

    attribution_setting: str
    contract_generation_id: str

    offer_version_id: str
    landing_page_version_id: str

    source_type: str
    source_manifest: dict

    observed_at: datetime
    expires_at: datetime

    scenario_version_id: str | None = None
    source_sync_run_id: str | None = None
    source_publication_id: str | None = None
    source_attestation_id: str | None = None


def _require_meta_mode(context: TenantContext) -> str:
    if context.meta_mode is None:
        raise DeliveryTargetError("meta_mode_required")
    return context.meta_mode


def _validate_source_provenance(meta_mode: str, request: DeliveryTargetRequest) -> None:
    required = _REQUIRED_REFS_BY_SOURCE_TYPE.get(request.source_type)
    if required is None:
        raise InvalidSourceProvenanceError(
            "unknown_source_type", request.source_type,
        )

    provided = {
        ref for ref in _ALL_PROVENANCE_REFS if getattr(request, ref) is not None
    }
    if provided != required:
        raise InvalidSourceProvenanceError(
            "source_provenance_mismatch",
            f"source_type={request.source_type!r} requires exactly "
            f"{sorted(required)}, got {sorted(provided)}",
        )

    if request.source_type == "curated_scenario" and meta_mode not in _CURATED_SCENARIO_MODES:
        raise InvalidSourceProvenanceError(
            "curated_scenario_requires_sandbox_or_mock", meta_mode,
        )
    if request.source_type == "evidence_observation" and meta_mode != _EVIDENCE_OBSERVATION_MODE:
        raise InvalidSourceProvenanceError(
            "evidence_observation_requires_internal_validation", meta_mode,
        )


def _compute_remote_binding_key(
    meta_connection_id: str, ad_account_id: str,
    campaign_remote_id: str, adset_remote_id: str, ad_remote_id: str,
) -> str:
    return "/".join(
        (meta_connection_id, ad_account_id, campaign_remote_id, adset_remote_id, ad_remote_id)
    )


def _target_fingerprint(request: DeliveryTargetRequest, source_manifest_hash: str) -> str:
    """spec §4.3 ``target_fingerprint``: proves the exact delivery slot, not
    just the row that happens to reference it."""
    return canonical_lineage_hash(
        _TARGET_FINGERPRINT_VERSION,
        {
            "meta_connection_id": request.meta_connection_id,
            "ad_account_id": request.ad_account_id,
            "scenario_scope_key": request.scenario_scope_key,
            "campaign_remote_id": request.campaign_remote_id,
            "adset_remote_id": request.adset_remote_id,
            "ad_remote_id": request.ad_remote_id,
            "ad_creative_id": request.ad_creative_id,
            "creative_asset_version_id": request.creative_asset_version_id,
            "creative_binding_evidence_hash": request.creative_binding_evidence_hash,
            "market_code": request.market_code,
            "placement": request.placement,
            "attribution_setting": request.attribution_setting,
            "contract_generation_id": request.contract_generation_id,
            "offer_version_id": request.offer_version_id,
            "landing_page_version_id": request.landing_page_version_id,
            "source_manifest_hash": source_manifest_hash,
        },
    )


def create_delivery_target(
    session: Session,
    context: TenantContext,
    request: DeliveryTargetRequest,
) -> ScaleDeliveryTargetRecord:
    """Construct, validate and persist one ``ScaleDeliveryTargetRecord``.

    Raises :class:`InvalidSourceProvenanceError` before touching the
    database when ``request``'s source-type/provenance-reference/meta_mode
    combination cannot satisfy the table's CHECK constraints -- the
    database is still the authority (this is fail-fast, not a replacement).
    """
    meta_mode = _require_meta_mode(context)
    _validate_source_provenance(meta_mode, request)

    source_manifest_hash = canonical_lineage_hash(
        _SOURCE_MANIFEST_HASH_VERSION, request.source_manifest,
    )
    fingerprint = _target_fingerprint(request, source_manifest_hash)
    remote_binding_key = _compute_remote_binding_key(
        request.meta_connection_id, request.ad_account_id,
        request.campaign_remote_id, request.adset_remote_id, request.ad_remote_id,
    )

    target = ScaleDeliveryTargetRecord(
        id=new_id("sdt"),
        workspace_id=context.workspace_id,
        meta_mode=meta_mode,
        meta_connection_id=request.meta_connection_id,
        ad_account_id=request.ad_account_id,
        scenario_scope_key=request.scenario_scope_key,
        campaign_remote_id=request.campaign_remote_id,
        adset_remote_id=request.adset_remote_id,
        ad_remote_id=request.ad_remote_id,
        ad_creative_id=request.ad_creative_id,
        creative_asset_version_id=request.creative_asset_version_id,
        creative_binding_evidence_hash=request.creative_binding_evidence_hash,
        market_code=request.market_code,
        placement=request.placement,
        attribution_setting=request.attribution_setting,
        contract_generation_id=request.contract_generation_id,
        offer_version_id=request.offer_version_id,
        landing_page_version_id=request.landing_page_version_id,
        source_type=request.source_type,
        scenario_version_id=request.scenario_version_id,
        source_sync_run_id=request.source_sync_run_id,
        source_publication_id=request.source_publication_id,
        source_attestation_id=request.source_attestation_id,
        source_manifest=request.source_manifest,
        source_manifest_hash=source_manifest_hash,
        target_fingerprint=fingerprint,
        remote_binding_key=remote_binding_key,
        observed_at=request.observed_at,
        expires_at=request.expires_at,
    )
    session.add(target)
    session.flush()
    return target


def validate_delivery_target(
    session: Session,
    context: TenantContext,
    *,
    target_id: str,
) -> ScaleDeliveryTargetRecord:
    """Load one delivery target scoped to this tenant and Meta mode, and
    prove it is still usable (exists, not expired).

    Shared by any authoritative gate that needs a proven delivery target --
    e.g. Task 9's ``lock_and_validate_enrollment_lineage`` -- so "not found"
    and "expired" are decided in exactly one place.
    """
    meta_mode = _require_meta_mode(context)
    target = session.execute(
        select(ScaleDeliveryTargetRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=meta_mode,
            id=target_id,
        )
    ).scalar_one_or_none()
    if target is None:
        raise DeliveryTargetNotFoundError(target_id)
    if target.expires_at < datetime.now(timezone.utc):
        raise DeliveryTargetExpiredError(target_id)
    return target
