"""The one atomic publication path for a Localization output (spec §2G/§2H).

Publishing a localized output is four writes that only mean anything
together: the child ``AssetVersion`` carrying the bytes, the
``LocalizationOutputPublication`` recording who published them and under
which recheck, the ``LocalizedAssetLineage`` naming every frozen input the
result is derived from, and the active ``MarketAssetBinding`` that makes it
the version this market serves. A run that creates three of the four leaves
a child AssetVersion nothing points at, or a binding whose provenance cannot
be reconstructed.

There are two callers, and the spec is explicit that they must not diverge:
the normal finalize path (``app.localization.worker.handle_finalize_item``)
and the isolated-output adoption command
(``app.localization.batch.adopt_isolated_output``) -- "adoption uses the
same atomic publication path as normal finalization". Adoption previously
had no publication at all: it flipped the Item to ``succeeded`` and stopped,
so a recovered output was reported as published while no AssetVersion,
lineage or binding existed. Keeping the sequence here, rather than copied
into both, is what makes that class of divergence impossible.

Everything is written through the caller's transaction: a failed binding or
Item transition rolls back the child AssetVersion with it.

Import direction: this module imports :mod:`app.localization.binding`,
which imports :mod:`app.localization.batch`. ``batch`` therefore imports
*this* module lazily, inside the adoption command, so the three do not form
an import cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    AssetVersionRecord,
    LocalizationAttemptOutputRecord,
    LocalizationAttemptRecord,
    LocalizationBatchRecord,
    LocalizationBatchSourceRecord,
    LocalizationItemRecord,
    LocalizationOutputPublicationRecord,
    LocalizedAssetLineageRecord,
    new_id,
)
from ..platform.tenancy import TenantContext
from . import binding as binding_mod


__all__ = [
    "PublicationKind",
    "PublicationResult",
    "PublicationError",
    "publish_localized_output",
    "existing_publication",
]


PublicationKind = Literal["normal", "adopted"]


class PublicationError(RuntimeError):
    """The publication cannot be assembled from the evidence it was given."""


@dataclass(frozen=True)
class PublicationResult:
    publication_id: str
    asset_version_id: str
    lineage_id: str
    binding_id: str
    #: True when this output was already published and nothing was written.
    replayed: bool


def existing_publication(
    session: Session, ctx: TenantContext, output_id: str
) -> LocalizationOutputPublicationRecord | None:
    """The publication for one output, if it already has one.

    ``uq_lop_workspace_id_output`` makes one output publishable exactly
    once. Reading it first turns a redelivered finalize -- or a second
    adoption of the same recovered output -- into a replay instead of an
    integrity error.
    """
    return session.scalar(
        select(LocalizationOutputPublicationRecord).where(
            LocalizationOutputPublicationRecord.workspace_id == ctx.workspace_id,
            LocalizationOutputPublicationRecord.output_id == output_id,
        )
    )


def publish_localized_output(
    session: Session,
    ctx: TenantContext,
    *,
    batch: LocalizationBatchRecord,
    item: LocalizationItemRecord,
    attempt: LocalizationAttemptRecord,
    output: LocalizationAttemptOutputRecord,
    publication_kind: PublicationKind,
    actor_id: str,
    role: str,
    recheck_hash: str | None = None,
    now: datetime | None = None,
) -> PublicationResult:
    """Create the child AssetVersion, publication, lineage and binding.

    The caller owns the transaction and owns the authoritative rechecks --
    this function publishes what it is given and does not decide whether
    publishing is allowed. ``publication_kind`` selects the evidence the
    schema demands: ``'adopted'`` requires a ``recheck_hash`` (the proof an
    explicit recheck happened), ``'normal'`` forbids one
    (``ck_lop_recheck_hash``).

    The localized child deliberately starts with no human QualityReview, no
    Winner verdict and ``ready_for_experiment=False`` on its binding -- none
    of that is inherited from whatever the binding supersedes (spec §16.11).
    """
    now = now or datetime.now(timezone.utc)

    if publication_kind == "adopted" and not recheck_hash:
        raise PublicationError("an adopted publication requires a recheck hash")
    if publication_kind == "normal" and recheck_hash:
        raise PublicationError("a normal publication must not carry a recheck hash")

    already = existing_publication(session, ctx, output.id)
    if already is not None:
        lineage = session.scalar(
            select(LocalizedAssetLineageRecord).where(
                LocalizedAssetLineageRecord.workspace_id == ctx.workspace_id,
                LocalizedAssetLineageRecord.publication_id == already.id,
            )
        )
        binding_result = binding_mod.publish_binding(
            session,
            ctx,
            item_id=already.item_id,
            batch_id=already.batch_id,
            publication_id=already.id,
            actor_id=already.actor_id,
            now=now,
        )
        return PublicationResult(
            publication_id=already.id,
            asset_version_id=already.asset_version_id,
            lineage_id=lineage.id if lineage is not None else "",
            binding_id=binding_result.binding_id,
            replayed=True,
        )

    source_av = session.get(AssetVersionRecord, item.source_asset_version_id)
    if source_av is None:
        raise PublicationError(
            f"source AssetVersion {item.source_asset_version_id} is missing"
        )
    batch_source = session.scalar(
        select(LocalizationBatchSourceRecord).where(
            LocalizationBatchSourceRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchSourceRecord.id == item.batch_source_id,
        )
    )
    if batch_source is None:
        raise PublicationError(
            f"batch source {item.batch_source_id} is missing for item {item.id}"
        )

    child = AssetVersionRecord(
        id=new_id("av"),
        workspace_id=ctx.workspace_id,
        asset_id=source_av.asset_id,
        parent_version_id=item.source_asset_version_id,
        kind="localized",
        aspect_ratio=source_av.aspect_ratio,
        language=item.target_market,
        object_key=output.object_key,
        content_sha256=output.content_sha256,
        byte_size=output.byte_size,
        mime_type=output.mime_type,
        truth_package_id=item.truth_package_id,
        created_at=now,
        producing_run_item_id=None,
        provider_attempt_id=None,
        origin_kind="localized",
        execution_mode=batch.provider_execution_mode,
    )
    session.add(child)
    session.flush()

    publication = LocalizationOutputPublicationRecord(
        id=new_id("lop"),
        workspace_id=ctx.workspace_id,
        output_id=output.id,
        attempt_id=attempt.id,
        item_id=item.id,
        batch_id=batch.id,
        publication_kind=publication_kind,
        asset_version_id=child.id,
        actor_id=actor_id,
        role=role,
        recheck_hash=recheck_hash,
        created_at=now,
    )
    session.add(publication)
    session.flush()

    lineage = LocalizedAssetLineageRecord(
        id=new_id("lal"),
        workspace_id=ctx.workspace_id,
        asset_version_id=child.id,
        source_asset_version_id=item.source_asset_version_id,
        batch_id=batch.id,
        batch_source_id=item.batch_source_id,
        item_id=item.id,
        attempt_id=attempt.id,
        output_id=output.id,
        publication_id=publication.id,
        decision_id=item.decision_id,
        claim_decision_set_id=item.claim_decision_set_id,
        product_version_id=batch.product_version_id,
        offer_version_id=batch.offer_version_id,
        source_market=(batch.source_market_snapshot_json or {}).get("market", ""),
        target_market=item.target_market,
        truth_package_id=item.truth_package_id,
        truth_package_market_id=item.truth_package_market_id,
        inventory_id=item.inventory_id,
        copy_version_id=item.copy_version_id,
        market_policy_version_id=item.market_policy_version_id,
        source_generation_mode=batch_source.source_generation_mode,
        experiment_evidence_mode=batch_source.experiment_evidence_mode,
        truth_data_mode=batch.truth_data_mode,
        provider_execution_mode=batch.provider_execution_mode,
        evidence_tier=batch.evidence_tier,
        created_at=now,
    )
    session.add(lineage)
    session.flush()

    binding_result = binding_mod.publish_binding(
        session,
        ctx,
        item_id=item.id,
        batch_id=batch.id,
        publication_id=publication.id,
        actor_id=actor_id,
        now=now,
    )

    return PublicationResult(
        publication_id=publication.id,
        asset_version_id=child.id,
        lineage_id=lineage.id,
        binding_id=binding_result.binding_id,
        replayed=False,
    )
