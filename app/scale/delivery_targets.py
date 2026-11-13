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
value. Resolving them from an observed ad -- the sandbox scenario's
hierarchy or published evidence pages, the creative's one AssetVersion, the
source ExperimentVersion's Offer, Landing Page, market and placement, and
the contract set's attribution -- is :mod:`app.scale.observed_ads` (#129),
which calls :func:`create_delivery_target_with_contract_set`.

Metric contract set (#101 S2): :func:`ensure_metric_contract_set` builds,
for the connection a DeliveryTarget lives on, every observation contract
generation Scale's read policy requires -- daily, exact window and delivery
configuration, no breakdown family in the first batch -- from the code
contracts in :mod:`app.meta.scale_read_contracts`, and one
``ScaleMetricSemanticContractSetRecord`` wrapping exactly those generations.
It is idempotent: a second call returns the same generations and set. All
generations of the connection share the account's time zone and the daily
04:00 cron, so the connection's day is one batch. A ``customer_sandbox``
connection's generations are scheduled as they are created.
:func:`create_delivery_target_with_contract_set` is the population path that
binds a new DeliveryTarget to that set's delivery-configuration generation,
the one Enrollment activation resolves the set and the attestation from.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id
from app.meta.models import MetaConnectionRecord, ObservationContractGenerationRecord
from app.meta.observation import ContractError, create_contract_generation, sandbox_account_for_connection
from app.meta.scale_read_contracts import (
    DELIVERY_CONFIGURATION,
    SCALE_READ_POLICY,
    AccountBinding,
    contract_for,
    generation_spec,
)
from app.platform.audit import append_event
from app.platform.tenancy import TenantContext, is_postgres

from .canonical_lineage import canonical_lineage_hash
from .models import (
    ScaleDeliveryTargetRecord,
    ScaleMetricSemanticContractSetMemberRecord,
    ScaleMetricSemanticContractSetRecord,
)

__all__ = [
    "DeliveryTargetRequest",
    "DeliveryTargetError",
    "InvalidSourceProvenanceError",
    "DeliveryTargetNotFoundError",
    "DeliveryTargetExpiredError",
    "create_delivery_target",
    "validate_delivery_target",
    "MetricContractSet",
    "MetricContractSetError",
    "ensure_metric_contract_set",
    "create_delivery_target_with_contract_set",
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


class MetricContractSetError(DeliveryTargetError):
    """The connection's Scale metric contract set cannot be established."""


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

    The caller (:mod:`app.scale.observed_ads`) resolves these from recorded
    evidence and validates the creative-to-AssetVersion mapping is exact;
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
    landing_page_evidence_hash: str

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
            # Historical key name; the value is the frozen evidence hash (#129).
            "landing_page_version_id": request.landing_page_evidence_hash,
            "source_manifest_hash": source_manifest_hash,
        },
    )


def create_delivery_target(
    session: Session,
    context: TenantContext,
    request: DeliveryTargetRequest,
    *,
    target_id: str | None = None,
) -> ScaleDeliveryTargetRecord:
    """Construct, validate and persist one ``ScaleDeliveryTargetRecord``.

    Raises :class:`InvalidSourceProvenanceError` before touching the
    database when ``request``'s source-type/provenance-reference/meta_mode
    combination cannot satisfy the table's CHECK constraints -- the
    database is still the authority (this is fail-fast, not a replacement).
    ``target_id`` is a caller-preallocated id, for a command journal that
    claims its result id before the row exists.
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
        id=target_id or new_id("sdt"),
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
        landing_page_evidence_hash=request.landing_page_evidence_hash,
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


# ---------------------------------------------------------------------------
# Metric contract set (#101 S2)
# ---------------------------------------------------------------------------

#: The generation fields a Scale read contract fixes (``generation_spec``).
_GENERATION_FIELDS = (
    "ad_account_id", "fields", "attribution_window", "breakdowns", "time_window", "delivery_config",
    "local_timezone",
)
_CONTRACT_SET_DESCRIPTION = "Scale read contracts v1"


@dataclass(frozen=True)
class MetricContractSet:
    """A connection's Scale metric contract set and the generations it wraps."""

    contract_set_id: str
    generation_ids: Mapping[str, str]
    created: bool

    @property
    def delivery_configuration_generation_id(self) -> str:
        return self.generation_ids[DELIVERY_CONFIGURATION]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _advisory_lock_key(context: TenantContext, connection_id: str) -> int:
    scope = f"{context.workspace_id}:{context.meta_mode}:{connection_id}:scale_metric_contract_set"
    return int(hashlib.sha256(scope.encode("utf-8")).hexdigest(), 16) % (2**63 - 1)


def _account_binding(
    session: Session, context: TenantContext, connection: MetaConnectionRecord,
    currency: str | None, timezone_name: str | None,
) -> AccountBinding:
    """The account the contracts are frozen to.

    A sandbox connection defaults to the account its scenario simulates;
    a real account's currency and time zone must be given.
    """
    if currency and timezone_name:
        return AccountBinding(connection.ad_account_id, currency, timezone_name)
    if context.meta_mode not in _CURATED_SCENARIO_MODES:
        raise MetricContractSetError(
            "scale_contract_account_required",
            f"{context.meta_mode} contracts need the account's currency and time zone",
        )
    sandbox = sandbox_account_for_connection(
        session, workspace_id=context.workspace_id, meta_mode=context.meta_mode, connection_id=connection.id,
    )
    return AccountBinding(connection.ad_account_id, currency or sandbox.currency,
                          timezone_name or sandbox.timezone_name)


def _required_generation(
    session: Session, context: TenantContext, connection: MetaConnectionRecord,
    contract_type: str, binding: AccountBinding,
) -> ObservationContractGenerationRecord:
    """The connection's active generation of ``contract_type``, created from
    the code contract when there is none. An active generation of the type
    that is anything else is a conflict, not something to replace silently:
    evidence and Episodes may already reference it."""
    spec = generation_spec(contract_for(contract_type), binding)
    existing = session.scalar(
        select(ObservationContractGenerationRecord).where(
            ObservationContractGenerationRecord.workspace_id == context.workspace_id,
            ObservationContractGenerationRecord.meta_mode == context.meta_mode,
            ObservationContractGenerationRecord.meta_connection_id == connection.id,
            ObservationContractGenerationRecord.contract_type == contract_type,
            ObservationContractGenerationRecord.state == "active",
        )
    )
    if existing is not None:
        drifted = [field for field in _GENERATION_FIELDS
                   if _canonical(getattr(existing, field)) != _canonical(spec[field])]
        if drifted:
            raise MetricContractSetError(
                "scale_contract_generation_conflict",
                f"active {contract_type} generation {existing.id} differs from the Scale read contract in {drifted}",
            )
        return existing
    try:
        return create_contract_generation(
            session, workspace_id=context.workspace_id, meta_mode=context.meta_mode,
            connection_id=connection.id, **spec,
        )
    except ContractError as exc:
        raise MetricContractSetError(exc.code, str(exc)) from exc


def ensure_metric_contract_set(
    session: Session,
    context: TenantContext,
    *,
    connection_id: str,
    currency: str | None = None,
    timezone_name: str | None = None,
) -> MetricContractSet:
    """Idempotently build the connection's Scale generations and contract set.

    Runs in the caller's transaction. Only an Owner or the system may set up
    contracts, on an active connection of the context's Workspace and mode.
    On PostgreSQL a transaction-scoped advisory lock serializes concurrent
    calls for one connection, so two callers cannot both create a set for
    the same delivery-configuration generation (activation would find it
    ambiguous). A set that already wraps the connection's
    delivery-configuration generation with other members is drift that a
    caller must resolve; it is never silently joined by a second set.
    """
    meta_mode = _require_meta_mode(context)
    if context.role not in ("owner", "system"):
        raise MetricContractSetError("forbidden", f"role {context.role!r} cannot set up Scale metric contracts")
    connection = session.scalar(
        select(MetaConnectionRecord).where(
            MetaConnectionRecord.workspace_id == context.workspace_id,
            MetaConnectionRecord.meta_mode == meta_mode,
            MetaConnectionRecord.id == connection_id,
        )
    )
    if connection is None or connection.state != "active":
        raise MetricContractSetError("connection_not_found", connection_id)
    if is_postgres(session):
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": _advisory_lock_key(context, connection_id)})
    binding = _account_binding(session, context, connection, currency, timezone_name)

    generations = {
        contract_type: _required_generation(session, context, connection, contract_type, binding)
        for contract_type in SCALE_READ_POLICY.required_contract_types()
    }
    wanted = {kind: (row.id, row.contract_hash) for kind, row in generations.items()}
    generation_ids = {kind: row.id for kind, row in generations.items()}

    member = ScaleMetricSemanticContractSetMemberRecord
    contract_set = ScaleMetricSemanticContractSetRecord
    candidates = sorted(set(session.scalars(
        select(member.contract_set_id)
        .join(contract_set, contract_set.id == member.contract_set_id)
        .where(
            contract_set.workspace_id == context.workspace_id,
            contract_set.meta_mode == meta_mode,
            member.contract_type == DELIVERY_CONFIGURATION,
            member.observation_contract_generation == generation_ids[DELIVERY_CONFIGURATION],
        )
    )))
    for set_id in candidates:
        members = {
            row.contract_type: (row.observation_contract_generation, row.contract_hash)
            for row in session.scalars(select(member).where(member.contract_set_id == set_id))
        }
        if members == wanted:
            return MetricContractSet(set_id, generation_ids, created=False)
    if candidates:
        raise MetricContractSetError(
            "scale_contract_set_drift",
            f"contract set {candidates[0]} wraps generation {generation_ids[DELIVERY_CONFIGURATION]} "
            "with other members",
        )

    record = contract_set(
        id=new_id("smcs"),
        workspace_id=context.workspace_id,
        meta_mode=meta_mode,
        description=f"{_CONTRACT_SET_DESCRIPTION} for connection {connection_id}",
        created_by=(context.initiator_id or context.executor)[:100],
    )
    session.add(record)
    session.flush()
    for kind, (generation_id, contract_hash) in sorted(wanted.items()):
        session.add(member(
            id=new_id("smcm"),
            contract_set_id=record.id,
            contract_type=kind,
            observation_contract_generation=generation_id,
            contract_hash=contract_hash,
        ))
    session.flush()
    append_event(
        session,
        event_type="scale.metric_contract_set.created",
        resource_type="scale_metric_semantic_contract_set",
        resource_id=record.id,
        details={
            "meta_connection_id": connection_id,
            "members": {kind: generation_id for kind, (generation_id, _hash) in sorted(wanted.items())},
            "account_currency": binding.currency,
            "account_timezone": binding.timezone_name,
        },
    )
    return MetricContractSet(record.id, generation_ids, created=True)


def create_delivery_target_with_contract_set(
    session: Session,
    context: TenantContext,
    request: DeliveryTargetRequest,
    *,
    currency: str | None = None,
    timezone_name: str | None = None,
    target_id: str | None = None,
) -> tuple[ScaleDeliveryTargetRecord, MetricContractSet]:
    """Create a DeliveryTarget on its connection's Scale metric contract set.

    Ensures the set first, then binds the target to the set's
    delivery-configuration generation. ``request.contract_generation_id``
    may be empty (filled in here) or must already name that generation.
    """
    contract_set = ensure_metric_contract_set(
        session, context, connection_id=request.meta_connection_id,
        currency=currency, timezone_name=timezone_name,
    )
    generation_id = contract_set.delivery_configuration_generation_id
    if request.contract_generation_id not in ("", generation_id):
        raise MetricContractSetError(
            "scale_contract_generation_conflict",
            f"target names generation {request.contract_generation_id}, the contract set {generation_id}",
        )
    target = create_delivery_target(
        session, context, replace(request, contract_generation_id=generation_id), target_id=target_id,
    )
    return target, contract_set
