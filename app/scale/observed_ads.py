"""The DeliveryTarget product entry: pick an already-observed ad (#129).

In the Scale enrollment-candidate flow an Owner or Member picks one ad the
connection has already observed. The server resolves the whole delivery slot
from recorded evidence, never from the request:

* **Observed ads.** ``customer_sandbox`` and ``controlled_mock``: the ``ad``
  objects of the connection's default ScenarioVersion, with the hierarchy the
  sandbox reports (``curated_scenario`` source). ``internal_validation``: the
  ``ads`` edge of the connection's Scale delivery-configuration read, from the
  pages of its current valid attestation (``evidence_observation`` source).
  Without such an attestation the connection has observed nothing yet
  (``delivery_configuration_not_observed``).
* **Creative.** Every record that maps the ``(ad account, ad)`` to an
  AssetVersion -- the sandbox Launch candidate that created the ad, an
  Experiment candidate ad binding, a marketing ad binding and the legacy Meta
  ad binding -- must name exactly one AssetVersion (``creative_unmapped`` /
  ``creative_ambiguous``). ``creative_binding_evidence_hash`` covers those
  records.
* **Offer, Landing Page, market and placement** come from the
  ExperimentVersion behind the ad: the one its Launch candidate, Experiment
  candidate binding or marketing evidence handoff froze. The distinct
  ``(OfferVersion, Landing Page evidence hash, market, placement)`` they name
  must be exactly one (``provenance_unresolved`` / ``provenance_ambiguous``).
  Drift the user makes in Meta afterwards is not detected here (#112).
* **Attribution** is what Scale measures with: the click/view windows of the
  connection's Scale ``daily`` and ``exact_window`` contract generations,
  hashed like the Winner's (``attribution-semantics-v1``).

The connection's Scale metric contract set is created on first use by the
system executor, with the requesting Owner or Member recorded as initiator;
the DeliveryTarget is then created through
:func:`app.scale.delivery_targets.create_delivery_target_with_contract_set`.
Before that, every field is compared with the Winner and a difference is a
typed reason per field (``target_*_mismatch``); after it, the authoritative
lineage gate (:func:`app.scale.enrollment_lineage.
lock_and_validate_enrollment_lineage`) runs on the new row in the same
transaction. Nothing here reads Meta or writes to it: the sandbox reads are
database rows and the internal-validation read is published evidence.
"""
from __future__ import annotations

import gzip
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.meta.fake_adapter import scenario_ad_hierarchy
from app.adapters.persistence.models import (
    ExperimentAdBindingRecord,
    ExperimentEntryRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    MetaAccountRecord,
    MetaAdBindingRecord,
)
from app.experiments.winner_eligibility import (
    EligibleWinnerLineage,
    attribution_semantics_hash_for,
    validate_winner_eligibility,
)
from app.launch.models import (
    LaunchCandidateRecord,
    LaunchProvisioningRecord,
    LaunchSagaStepAttemptRecord,
    MetaLaunchPreviewRecord,
)
from app.marketing.workflow_models import MarketingAdBindingRecord, MarketingHandoffRecord
from app.meta.evidence import current_attestation
from app.meta.models import (
    EvidencePublicationRecord,
    MetaCallIntentRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    MetaEvidencePageRecord,
    ObservationContractGenerationRecord,
    ScenarioVersionRecord,
)
from app.meta.scale_read_contracts import DAILY, DELIVERY_CONFIGURATION, EXACT_WINDOW
from app.platform.audit import append_event
from app.platform.denial_audit import check_and_record_mode_denial
from app.platform.tenancy import TenantContext, apply_to_session

from .canonical_lineage import canonical_lineage_hash
from .command_journal import claim_command, command_request_fingerprint
from .delivery_targets import (
    DeliveryTargetError,
    DeliveryTargetRequest,
    MetricContractSet,
    create_delivery_target_with_contract_set,
    ensure_metric_contract_set,
)
from .enrollment_lineage import lock_and_validate_enrollment_lineage
from .models import ScaleDeliveryTargetRecord

__all__ = [
    "DELIVERY_TARGET_TTL",
    "CONTRACT_SET_EXECUTOR",
    "DeliveryTargetEntryError",
    "ObservedAd",
    "ObservedAdListing",
    "list_observed_ads",
    "create_delivery_target_from_ad",
]

#: How long a DeliveryTarget made from a picked ad stays usable. The lineage
#: gate refuses an expired target when a candidate, a draft, an activation
#: preview, an activation or a resume names it. Open on #129 for the
#: maintainer to confirm; the retired golden-path seed used the same value.
DELIVERY_TARGET_TTL = timedelta(days=90)

#: The system executor that sets up a connection's Scale metric contract set
#: for the Owner or Member who picked the ad (maintainer decision on #129).
CONTRACT_SET_EXECUTOR = "system:scale-delivery-target"

_CURATED_MODES = ("customer_sandbox", "controlled_mock")
_NO_SCENARIO = "__no_scenario__"
_MANIFEST_SCHEMA_VERSION = 1
_BINDING_EVIDENCE_VERSION = 1

_LAUNCH_CANDIDATE = "launch_candidate"
_EXPERIMENT_AD_BINDING = "experiment_ad_binding"
_MARKETING_AD_BINDING = "marketing_ad_binding"
_META_AD_BINDING = "meta_ad_binding"



class DeliveryTargetEntryError(DeliveryTargetError):
    """A typed reason the picked ad cannot become this Winner's DeliveryTarget."""


@dataclass(frozen=True)
class ObservedAd:
    """One ad a connection has observed, with the source that observed it."""

    meta_connection_id: str
    ad_account_id: str
    campaign_remote_id: str
    adset_remote_id: str
    ad_remote_id: str
    ad_creative_id: str
    source_type: str
    scenario_version_id: str | None = None
    source_sync_run_id: str | None = None
    source_publication_id: str | None = None
    source_attestation_id: str | None = None
    evidence_watermark: str | None = None
    #: The AssetVersions the ad's launch and ad-binding records map it to,
    #: shown so a person can tell the ads apart; picking re-resolves them.
    asset_version_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservedAdListing:
    ads: tuple[ObservedAd, ...]
    #: ``(meta_connection_id, reason)`` for connections that observed nothing.
    unavailable: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _CreativeBinding:
    """One record mapping the picked ad to an AssetVersion."""

    source: str
    record_id: str
    asset_version_id: str
    experiment_version_ids: tuple[str, ...] = ()

    def evidence(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "record_id": self.record_id,
            "asset_version_id": self.asset_version_id,
            "experiment_version_ids": list(self.experiment_version_ids),
        }


@dataclass(frozen=True)
class _Provenance:
    offer_version_id: str
    landing_page_evidence_hash: str
    market_code: str
    placement: str


@dataclass
class _ResolvedAd:
    ad: ObservedAd
    bindings: list[_CreativeBinding] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Observed ads
# ---------------------------------------------------------------------------


def _require_meta_mode(context: TenantContext) -> str:
    if context.meta_mode is None:
        raise DeliveryTargetEntryError("meta_mode_required")
    return context.meta_mode


def _connections(
    session: Session, context: TenantContext, connection_id: str | None,
) -> list[MetaConnectionRecord]:
    query = select(MetaConnectionRecord).where(
        MetaConnectionRecord.workspace_id == context.workspace_id,
        MetaConnectionRecord.meta_mode == context.meta_mode,
        MetaConnectionRecord.state == "active",
    )
    if connection_id is not None:
        query = query.where(MetaConnectionRecord.id == connection_id)
    connections = list(session.scalars(query.order_by(MetaConnectionRecord.id)))
    if connection_id is not None and not connections:
        check_and_record_mode_denial(
            session, context, MetaConnectionRecord, connection_id, resource_kind="meta.connection",
        )
        raise DeliveryTargetEntryError("connection_not_found", connection_id)
    return connections


def _default_scenario(
    session: Session, context: TenantContext, connection: MetaConnectionRecord,
) -> ScenarioVersionRecord | None:
    return session.scalar(
        select(ScenarioVersionRecord).where(
            ScenarioVersionRecord.workspace_id == context.workspace_id,
            ScenarioVersionRecord.meta_mode == context.meta_mode,
            ScenarioVersionRecord.meta_connection_id == connection.id,
            ScenarioVersionRecord.is_default.is_(True),
        )
    )


def _scenario_ads(
    session: Session, context: TenantContext, connection: MetaConnectionRecord,
) -> tuple[ObservedAd, ...]:
    scenario = _default_scenario(session, context, connection)
    if scenario is None:
        raise DeliveryTargetEntryError("delivery_configuration_not_observed", connection.id)
    return tuple(
        ObservedAd(
            meta_connection_id=connection.id,
            ad_account_id=connection.ad_account_id,
            campaign_remote_id=row.campaign_id,
            adset_remote_id=row.adset_id,
            ad_remote_id=row.ad_id,
            ad_creative_id=row.creative_id,
            source_type="curated_scenario",
            scenario_version_id=scenario.id,
        )
        for row in scenario_ad_hierarchy(
            session, workspace_id=context.workspace_id, meta_mode=context.meta_mode,
            scenario_version_id=scenario.id,
        )
    )


def _scale_generation(
    session: Session, context: TenantContext, connection: MetaConnectionRecord, contract_type: str,
) -> ObservationContractGenerationRecord | None:
    """The connection's active generation of a Scale read contract."""
    for generation in session.scalars(
        select(ObservationContractGenerationRecord).where(
            ObservationContractGenerationRecord.workspace_id == context.workspace_id,
            ObservationContractGenerationRecord.meta_mode == context.meta_mode,
            ObservationContractGenerationRecord.meta_connection_id == connection.id,
            ObservationContractGenerationRecord.contract_type == contract_type,
            ObservationContractGenerationRecord.state == "active",
        ).order_by(ObservationContractGenerationRecord.id)
    ):
        if (generation.time_window or {}).get("scale_read_contract") == contract_type:
            return generation
    return None


def _evidence_ads(
    session: Session, context: TenantContext, connection: MetaConnectionRecord,
) -> tuple[ObservedAd, ...]:
    generation = _scale_generation(session, context, connection, DELIVERY_CONFIGURATION)
    attestation = (
        current_attestation(session, contract_generation_id=generation.id)
        if generation is not None else None
    )
    publication = session.scalar(
        select(EvidencePublicationRecord).where(
            EvidencePublicationRecord.workspace_id == context.workspace_id,
            EvidencePublicationRecord.meta_mode == context.meta_mode,
            EvidencePublicationRecord.id == attestation.publication_id,
            EvidencePublicationRecord.state == "published",
        )
    ) if attestation is not None else None
    if publication is None:
        raise DeliveryTargetEntryError("delivery_configuration_not_observed", connection.id)
    pages = list(session.scalars(
        select(MetaEvidencePageRecord).where(
            MetaEvidencePageRecord.workspace_id == context.workspace_id,
            MetaEvidencePageRecord.meta_mode == context.meta_mode,
            MetaEvidencePageRecord.sync_run_id == publication.sync_run_id,
        ).order_by(MetaEvidencePageRecord.page_order)
    ))
    intents = {
        intent.id: intent
        for intent in session.scalars(
            select(MetaCallIntentRecord).where(
                MetaCallIntentRecord.workspace_id == context.workspace_id,
                MetaCallIntentRecord.meta_mode == context.meta_mode,
                MetaCallIntentRecord.id.in_({page.intent_id for page in pages}),
            )
        )
    } if pages else {}
    ads: dict[str, ObservedAd] = {}
    for page in pages:
        intent = intents.get(page.intent_id)
        edge = (intent.request_parameters or {}).get("edge") if intent is not None else None
        if edge != "ads":
            continue
        body = json.loads(gzip.decompress(page.payload_compressed))
        for row in body.get("data") or ():
            ad = _evidence_ad(row, connection, generation, publication, attestation)
            if ad is not None:
                ads.setdefault(ad.ad_remote_id, ad)
    return tuple(ads[key] for key in sorted(ads))


def _evidence_ad(row, connection, generation, publication, attestation) -> ObservedAd | None:
    if not isinstance(row, dict):
        return None
    creative = row.get("creative")
    values = (
        row.get("id"), row.get("adset_id"), row.get("campaign_id"),
        creative.get("id") if isinstance(creative, dict) else None,
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    ad_id, adset_id, campaign_id, creative_id = values
    return ObservedAd(
        meta_connection_id=connection.id,
        ad_account_id=connection.ad_account_id,
        campaign_remote_id=campaign_id,
        adset_remote_id=adset_id,
        ad_remote_id=ad_id,
        ad_creative_id=creative_id,
        source_type="evidence_observation",
        source_sync_run_id=publication.sync_run_id,
        source_publication_id=publication.id,
        source_attestation_id=attestation.id,
        evidence_watermark=attestation.evidence_watermark,
    )


def _observed_ads(
    session: Session, context: TenantContext, connection: MetaConnectionRecord,
) -> tuple[ObservedAd, ...]:
    if context.meta_mode in _CURATED_MODES:
        return _scenario_ads(session, context, connection)
    return _evidence_ads(session, context, connection)


def list_observed_ads(
    session: Session, context: TenantContext, *, connection_id: str | None = None,
) -> ObservedAdListing:
    """The ads every active connection of the context's mode has observed.

    Reads only. A connection that has observed nothing is listed in
    ``unavailable`` with its reason instead.
    """
    _require_meta_mode(context)
    ads: list[ObservedAd] = []
    unavailable: list[tuple[str, str]] = []
    for connection in _connections(session, context, connection_id):
        try:
            observed = _observed_ads(session, context, connection)
        except DeliveryTargetEntryError as exc:
            unavailable.append((connection.id, exc.code))
            continue
        ads.extend(
            replace(ad, asset_version_ids=tuple(sorted({
                binding.asset_version_id for binding in _creative_bindings(session, context, ad)
            })))
            for ad in observed
        )
    return ObservedAdListing(tuple(ads), tuple(unavailable))


# ---------------------------------------------------------------------------
# Creative mapping and provenance
# ---------------------------------------------------------------------------


def _version_candidate(
    session: Session, context: TenantContext, candidate_id: str | None,
) -> ExperimentVersionCandidateRecord | None:
    if not candidate_id:
        return None
    return session.scalar(
        select(ExperimentVersionCandidateRecord).where(
            ExperimentVersionCandidateRecord.workspace_id == context.workspace_id,
            ExperimentVersionCandidateRecord.id == candidate_id,
        )
    )


def _launch_bindings(
    session: Session, context: TenantContext, ad: ObservedAd,
) -> Iterable[_CreativeBinding]:
    """The Launch candidates whose applied ``create_ad`` step created this ad
    on this connection and ad account."""
    attempts = session.scalars(
        select(LaunchSagaStepAttemptRecord).where(
            LaunchSagaStepAttemptRecord.workspace_id == context.workspace_id,
            LaunchSagaStepAttemptRecord.meta_mode == context.meta_mode,
            LaunchSagaStepAttemptRecord.step_type == "create_ad",
            LaunchSagaStepAttemptRecord.state == "applied",
            LaunchSagaStepAttemptRecord.remote_object_id == ad.ad_remote_id,
        ).order_by(LaunchSagaStepAttemptRecord.id)
    )
    seen: set[str] = set()
    for attempt in attempts:
        if not attempt.candidate_id or attempt.candidate_id in seen:
            continue
        provisioning = session.scalar(
            select(LaunchProvisioningRecord).where(
                LaunchProvisioningRecord.workspace_id == context.workspace_id,
                LaunchProvisioningRecord.meta_mode == context.meta_mode,
                LaunchProvisioningRecord.id == attempt.provisioning_id,
            )
        )
        if provisioning is None or provisioning.meta_connection_id != ad.meta_connection_id:
            continue
        preview = session.scalar(
            select(MetaLaunchPreviewRecord).where(
                MetaLaunchPreviewRecord.workspace_id == context.workspace_id,
                MetaLaunchPreviewRecord.meta_mode == context.meta_mode,
                MetaLaunchPreviewRecord.id == provisioning.preview_id,
            )
        )
        account_binding = session.scalar(
            select(MetaDataSourceBindingRecord).where(
                MetaDataSourceBindingRecord.workspace_id == context.workspace_id,
                MetaDataSourceBindingRecord.meta_mode == context.meta_mode,
                MetaDataSourceBindingRecord.id == preview.meta_binding_id,
            )
        ) if preview is not None else None
        if account_binding is None or account_binding.ad_account_id != ad.ad_account_id:
            continue
        candidate = session.scalar(
            select(LaunchCandidateRecord).where(
                LaunchCandidateRecord.workspace_id == context.workspace_id,
                LaunchCandidateRecord.meta_mode == context.meta_mode,
                LaunchCandidateRecord.id == attempt.candidate_id,
            )
        )
        if candidate is None:
            continue
        seen.add(candidate.id)
        version_candidate = _version_candidate(session, context, candidate.experiment_version_candidate_id)
        yield _CreativeBinding(
            source=_LAUNCH_CANDIDATE,
            record_id=candidate.id,
            asset_version_id=candidate.asset_version_id,
            experiment_version_ids=(
                (version_candidate.experiment_version_id,) if version_candidate is not None else ()
            ),
        )


def _experiment_ad_bindings(
    session: Session, context: TenantContext, ad: ObservedAd,
) -> Iterable[_CreativeBinding]:
    for binding in session.scalars(
        select(ExperimentAdBindingRecord).where(
            ExperimentAdBindingRecord.workspace_id == context.workspace_id,
            ExperimentAdBindingRecord.ad_account_id == ad.ad_account_id,
            ExperimentAdBindingRecord.ad_id == ad.ad_remote_id,
        ).order_by(ExperimentAdBindingRecord.id)
    ):
        version_candidate = _version_candidate(session, context, binding.experiment_version_candidate_id)
        if version_candidate is not None:
            yield _CreativeBinding(
                source=_EXPERIMENT_AD_BINDING,
                record_id=binding.id,
                asset_version_id=version_candidate.asset_version_id,
                experiment_version_ids=(version_candidate.experiment_version_id,),
            )
            continue
        entry = session.scalar(
            select(ExperimentEntryRecord).where(
                ExperimentEntryRecord.workspace_id == context.workspace_id,
                ExperimentEntryRecord.id == binding.experiment_entry_id,
            )
        )
        if entry is not None:
            yield _CreativeBinding(
                source=_EXPERIMENT_AD_BINDING, record_id=binding.id,
                asset_version_id=entry.asset_version_id,
            )


def _marketing_ad_bindings(
    session: Session, context: TenantContext, ad: ObservedAd,
) -> Iterable[_CreativeBinding]:
    from app.marketing.evidence_handoff import POLICY, origin

    for binding in session.scalars(
        select(MarketingAdBindingRecord).where(
            MarketingAdBindingRecord.workspace_id == context.workspace_id,
            MarketingAdBindingRecord.ad_account_id == ad.ad_account_id,
            MarketingAdBindingRecord.ad_id == ad.ad_remote_id,
        ).order_by(MarketingAdBindingRecord.id)
    ):
        handoff = session.scalar(
            select(MarketingHandoffRecord).where(
                MarketingHandoffRecord.workspace_id == context.workspace_id,
                MarketingHandoffRecord.id == binding.handoff_id,
            )
        )
        entries = (handoff.manifest_json or {}).get("entries", []) if handoff is not None else []
        asset_version_id = next(
            (entry.get("asset_version_id") for entry in entries if entry.get("entry_id") == binding.entry_id),
            None,
        )
        if not asset_version_id:
            continue
        version_ids = tuple(sorted(
            version.id
            for version in session.scalars(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.workspace_id == context.workspace_id,
                    ExperimentVersionRecord.canonicalization_version == POLICY,
                )
            )
            if (proof := origin(version))
            and proof.get("round_id") == binding.round_id
            and proof.get("asset_version_id") == asset_version_id
        ))
        yield _CreativeBinding(
            source=_MARKETING_AD_BINDING, record_id=binding.id,
            asset_version_id=asset_version_id, experiment_version_ids=version_ids,
        )


def _meta_ad_bindings(
    session: Session, context: TenantContext, ad: ObservedAd,
) -> Iterable[_CreativeBinding]:
    for binding in session.scalars(
        select(MetaAdBindingRecord)
        .join(MetaAccountRecord, MetaAccountRecord.id == MetaAdBindingRecord.account_id)
        .where(
            MetaAccountRecord.workspace_id == context.workspace_id,
            MetaAccountRecord.ad_account_id == ad.ad_account_id,
            MetaAdBindingRecord.ad_id == ad.ad_remote_id,
        ).order_by(MetaAdBindingRecord.id)
    ):
        yield _CreativeBinding(
            source=_META_AD_BINDING, record_id=binding.id, asset_version_id=binding.asset_version_id,
        )


def _creative_bindings(
    session: Session, context: TenantContext, ad: ObservedAd,
) -> list[_CreativeBinding]:
    return [
        *_launch_bindings(session, context, ad),
        *_experiment_ad_bindings(session, context, ad),
        *_marketing_ad_bindings(session, context, ad),
        *_meta_ad_bindings(session, context, ad),
    ]


def _single_asset_version(bindings: list[_CreativeBinding]) -> str:
    asset_version_ids = {binding.asset_version_id for binding in bindings}
    if not asset_version_ids:
        raise DeliveryTargetEntryError("creative_unmapped", "no record maps the ad to an AssetVersion")
    if len(asset_version_ids) > 1:
        raise DeliveryTargetEntryError(
            "creative_ambiguous", f"the ad maps to {len(asset_version_ids)} AssetVersions",
        )
    return next(iter(asset_version_ids))


def _single_provenance(
    session: Session, context: TenantContext, bindings: list[_CreativeBinding],
) -> _Provenance:
    version_ids = sorted({
        version_id
        for binding in bindings if binding.source != _META_AD_BINDING
        for version_id in binding.experiment_version_ids
    })
    provenances = {
        _Provenance(
            offer_version_id=version.offer_version_id,
            landing_page_evidence_hash=version.landing_page_evidence_hash,
            market_code=version.market,
            placement=version.placement,
        )
        for version in session.scalars(
            select(ExperimentVersionRecord).where(
                ExperimentVersionRecord.workspace_id == context.workspace_id,
                ExperimentVersionRecord.id.in_(version_ids),
            )
        )
    } if version_ids else set()
    if not provenances:
        raise DeliveryTargetEntryError(
            "provenance_unresolved", "no ExperimentVersion froze the ad's Offer and Landing Page",
        )
    if len(provenances) > 1:
        raise DeliveryTargetEntryError(
            "provenance_ambiguous", f"the ad's sources froze {len(provenances)} delivery contexts",
        )
    return next(iter(provenances))


# ---------------------------------------------------------------------------
# Contract set and attribution
# ---------------------------------------------------------------------------


def _account_binding_from_generation(
    session: Session, context: TenantContext, connection: MetaConnectionRecord,
) -> tuple[str | None, str | None]:
    """A real account's currency and time zone, as its Scale contracts froze
    them. A sandbox connection needs none: its scenario simulates them."""
    if context.meta_mode in _CURATED_MODES:
        return None, None
    generation = _scale_generation(session, context, connection, DELIVERY_CONFIGURATION)
    window = (generation.time_window or {}) if generation is not None else {}
    currency, timezone_name = window.get("account_currency"), window.get("account_timezone")
    if not currency or not timezone_name:
        raise DeliveryTargetEntryError("delivery_configuration_not_observed", connection.id)
    return currency, timezone_name


def _windows(generation: ObservationContractGenerationRecord) -> tuple[int, int] | None:
    windows = (generation.attribution_window or {}).get("action_attribution_windows")
    click = view = None
    for window in windows or ():
        days, _, kind = str(window).partition("d_")
        if not days.isdigit():
            return None
        if kind == "click" and click is None:
            click = int(days)
        elif kind == "view" and view is None:
            view = int(days)
        else:
            return None
    return (click, view) if click is not None and view is not None else None


def _attribution_setting(
    session: Session, context: TenantContext, contract_set: MetricContractSet,
) -> str:
    """The attribution Scale measures the target with: one click/view window
    pair shared by the set's daily and exact-window generations."""
    pairs = set()
    for contract_type in (DAILY, EXACT_WINDOW):
        generation = session.scalar(
            select(ObservationContractGenerationRecord).where(
                ObservationContractGenerationRecord.workspace_id == context.workspace_id,
                ObservationContractGenerationRecord.meta_mode == context.meta_mode,
                ObservationContractGenerationRecord.id == contract_set.generation_ids.get(contract_type),
            )
        )
        pairs.add(_windows(generation) if generation is not None else None)
    if len(pairs) != 1 or None in pairs:
        raise DeliveryTargetEntryError(
            "attribution_contract_unresolved",
            "the Scale daily and exact-window contracts do not share one click/view window",
        )
    click, view = next(iter(pairs))
    return attribution_semantics_hash_for(click, view)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _scenario_scope_key(
    session: Session, context: TenantContext, connection: MetaConnectionRecord,
) -> str:
    if context.meta_mode != "controlled_mock":
        return _NO_SCENARIO
    scenario = _default_scenario(session, context, connection)
    if scenario is None:
        raise DeliveryTargetEntryError("delivery_configuration_not_observed", connection.id)
    return scenario.id


def _compare_with_winner(winner: EligibleWinnerLineage, request: DeliveryTargetRequest) -> None:
    """Each field of the slot that differs from the Winner's is its own reason."""
    def differs(target_value: str, winner_value: str) -> str:
        return f"Winner {winner_value!r}, ad {target_value!r}"

    if request.creative_asset_version_id != winner.asset_version_id:
        raise DeliveryTargetEntryError(
            "target_creative_mismatch", differs(request.creative_asset_version_id, winner.asset_version_id),
        )
    if request.market_code != winner.market_code:
        raise DeliveryTargetEntryError(
            "target_market_mismatch", differs(request.market_code, winner.market_code),
        )
    if request.placement != winner.placement:
        raise DeliveryTargetEntryError(
            "target_placement_mismatch", differs(request.placement, winner.placement),
        )
    if request.offer_version_id != winner.offer_version_id:
        raise DeliveryTargetEntryError(
            "target_offer_mismatch", differs(request.offer_version_id, winner.offer_version_id),
        )
    if request.landing_page_evidence_hash != winner.landing_page_evidence_hash:
        raise DeliveryTargetEntryError(
            "target_landing_page_mismatch",
            differs(request.landing_page_evidence_hash, winner.landing_page_evidence_hash),
        )
    if request.attribution_setting != winner.attribution_semantics_hash:
        raise DeliveryTargetEntryError(
            "target_attribution_mismatch",
            differs(request.attribution_setting, winner.attribution_semantics_hash),
        )


def _source_manifest(
    ad: ObservedAd, bindings: list[_CreativeBinding], provenance: _Provenance,
    contract_set: MetricContractSet,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "source_type": ad.source_type,
        "entry": "observed_ad_pick",
        "ad": {
            "campaign_remote_id": ad.campaign_remote_id,
            "adset_remote_id": ad.adset_remote_id,
            "ad_remote_id": ad.ad_remote_id,
            "ad_creative_id": ad.ad_creative_id,
        },
        "creative_bindings": sorted(
            (binding.evidence() for binding in bindings),
            key=lambda item: (item["source"], item["record_id"]),
        ),
        "provenance": {
            "offer_version_id": provenance.offer_version_id,
            "landing_page_evidence_hash": provenance.landing_page_evidence_hash,
            "market_code": provenance.market_code,
            "placement": provenance.placement,
        },
        "contract_set_id": contract_set.contract_set_id,
    }
    if ad.source_type == "curated_scenario":
        manifest["scenario_version_id"] = ad.scenario_version_id
    else:
        manifest.update({
            "source_sync_run_id": ad.source_sync_run_id,
            "source_publication_id": ad.source_publication_id,
            "source_attestation_id": ad.source_attestation_id,
            "evidence_watermark": ad.evidence_watermark,
        })
    return manifest


def create_delivery_target_from_ad(
    session: Session,
    context: TenantContext,
    *,
    decision_id: str,
    connection_id: str,
    ad_remote_id: str,
    idempotency_key: str,
) -> ScaleDeliveryTargetRecord:
    """Create the DeliveryTarget for ``decision_id`` from one observed ad.

    Idempotent on ``idempotency_key``: a replay returns the target the first
    call created, and the same key with another request is an
    ``IdempotencyConflictError``. Raises ``WinnerIneligibleError`` for a
    Winner that cannot be enrolled (another Workspace's included) and
    :class:`DeliveryTargetEntryError` with a typed reason otherwise.
    """
    _require_meta_mode(context)
    connection = _connections(session, context, connection_id)[0]
    target_id = f"sdt_{uuid.uuid4().hex[:24]}"
    claim = claim_command(
        session, context,
        command_type="create_delivery_target",
        deduplication_key=idempotency_key,
        result_id=target_id,
        request_fingerprint=command_request_fingerprint(
            decision_id=decision_id, meta_connection_id=connection_id, ad_remote_id=ad_remote_id,
        ),
        scenario_scope_key=_scenario_scope_key(session, context, connection),
        result_resource_type="delivery_target",
    )
    if claim.replay:
        replayed = session.scalar(
            select(ScaleDeliveryTargetRecord).where(
                ScaleDeliveryTargetRecord.workspace_id == context.workspace_id,
                ScaleDeliveryTargetRecord.meta_mode == context.meta_mode,
                ScaleDeliveryTargetRecord.id == claim.record.result_resource_id,
            )
        )
        if replayed is None:
            raise DeliveryTargetEntryError("idempotency_result_missing", claim.record.result_resource_id)
        return replayed

    winner = validate_winner_eligibility(session, context, decision_id=decision_id)
    ad = next(
        (item for item in _observed_ads(session, context, connection) if item.ad_remote_id == ad_remote_id),
        None,
    )
    if ad is None:
        raise DeliveryTargetEntryError("ad_not_observed", ad_remote_id)
    bindings = _creative_bindings(session, context, ad)
    asset_version_id = _single_asset_version(bindings)
    provenance = _single_provenance(session, context, bindings)
    currency, timezone_name = _account_binding_from_generation(session, context, connection)

    system_context = context.for_system_execution(CONTRACT_SET_EXECUTOR)
    apply_to_session(session, system_context)
    try:
        contract_set = ensure_metric_contract_set(
            session, system_context, connection_id=connection.id,
            currency=currency, timezone_name=timezone_name,
        )
        manifest = _source_manifest(ad, bindings, provenance, contract_set)
        now = datetime.now(timezone.utc)
        request = DeliveryTargetRequest(
            meta_connection_id=connection.id,
            ad_account_id=connection.ad_account_id,
            scenario_scope_key=claim.record.scenario_scope_key,
            campaign_remote_id=ad.campaign_remote_id,
            adset_remote_id=ad.adset_remote_id,
            ad_remote_id=ad.ad_remote_id,
            ad_creative_id=ad.ad_creative_id,
            creative_asset_version_id=asset_version_id,
            creative_binding_evidence_hash=canonical_lineage_hash(
                _BINDING_EVIDENCE_VERSION, {"bindings": manifest["creative_bindings"]},
            ),
            market_code=provenance.market_code,
            placement=provenance.placement,
            attribution_setting=_attribution_setting(session, context, contract_set),
            contract_generation_id=contract_set.delivery_configuration_generation_id,
            offer_version_id=provenance.offer_version_id,
            landing_page_evidence_hash=provenance.landing_page_evidence_hash,
            source_type=ad.source_type,
            source_manifest=manifest,
            observed_at=now,
            expires_at=now + DELIVERY_TARGET_TTL,
            scenario_version_id=ad.scenario_version_id,
            source_sync_run_id=ad.source_sync_run_id,
            source_publication_id=ad.source_publication_id,
            source_attestation_id=ad.source_attestation_id,
        )
        _compare_with_winner(winner, request)
        target, _ = create_delivery_target_with_contract_set(
            session, system_context, request,
            currency=currency, timezone_name=timezone_name, target_id=target_id,
        )
    finally:
        apply_to_session(session, context)

    lock_and_validate_enrollment_lineage(
        session, context, decision_id=decision_id, delivery_target_id=target.id,
    )
    append_event(
        session,
        event_type="scale.delivery_target.created",
        resource_type="scale_delivery_target",
        resource_id=target.id,
        details={
            "winner_decision_id": decision_id,
            "meta_connection_id": connection.id,
            "ad_remote_id": ad.ad_remote_id,
            "source_type": ad.source_type,
            "metric_contract_set_id": contract_set.contract_set_id,
            "creative_binding_sources": sorted({binding.source for binding in bindings}),
        },
    )
    return target
