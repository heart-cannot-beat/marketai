"""GenerationEvidenceRecorder for ExternalCallCoordinator integration.

Bridges generation-specific persistence (``AssetRecord``,
``AssetVersionRecord``, ``RunItemAssetVersionBindingRecord``, RunItem state
transitions) into the coordinator's three evidence-recorder seam methods,
matching the ``ExternalCallEvidenceRecorder[GenerationOutput]`` protocol
from ``app.providers.dispatch``.

Three responsibilities (matching spec section 2D exactly):

* ``link_prepared_attempt`` -- in the prepare transaction, transition the
  RunItem from a dispatchable state to ``running``.  The
  ``ProviderCallAttemptRecord`` already carries ``run_item_id``; no
  additional FK column on ``RunItemRecord`` is needed.
* ``materialize`` -- outside any transaction, place the output into durable
  storage and return its reference. A real provider's image bytes are
  committed to the ``BlobStore`` (the committed key is the reference), and
  a gallery thumbnail is derived from the same bytes and committed beside
  it, best effort (``app.generation.thumbnails``); the controlled mock's
  synthetic output is only hashed. An orphaned object or reference from a
  later rollback is harmless.
* ``persist_result`` -- in the complete transaction, create the
  ``AssetRecord`` + ``AssetVersionRecord`` (thumbnail columns included when
  one was committed) + binding, count the version in Usage
  (``asset_versions_generated``), transition the RunItem to ``qc_pending``,
  enqueue the QC job, and sync the GenerationRun projection.

Issue #98 Task 1; the blob-committing path arrived with BYOK dispatch, the
thumbnail derivative with Issue #159.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    new_id,
    utcnow,
)
from app.entitlements.cost_models import ProviderCallAttemptRecord
from app.entitlements.usage import record_asset_version_generated
from app.generation.controlled_adapter import GenerationOutput
from app.generation.models import (
    GenerationRecipeSnapshotRecord,
    RunItemAssetVersionBindingRecord,
    RunItemRecord,
)
from app.generation.thumbnails import (
    CommittedThumbnail,
    apply_thumbnail,
    commit_thumbnail_best_effort,
)
from app.generation.worker import (
    _DISPATCHABLE_STATES,
    _sync_run_state_best_effort,
    _transition_item,
)
from app.platform.blob_store import BlobStore, CommittedBlobRef, GeneratedArtifactSpec
from app.platform.jobs import enqueue_in_session
from app.platform.tenancy import TenantContext

logger = logging.getLogger(__name__)


def _canonical_hash(value: object) -> str:
    from ..platform.canonical import canonical_hash
    return canonical_hash(value)


class GenerationEvidenceRecorder:
    """``ExternalCallEvidenceRecorder[GenerationOutput]`` for generation.

    Constructed with the ``run_item_id`` so every method can locate the
    RunItem in its own session without relying on cross-session object
    identity.
    """

    def __init__(
        self,
        run_item_id: str,
        *,
        blob_store: BlobStore | None = None,
        credential_version_id: str | None = None,
        prompt: str = "",
        reference_sources: dict | None = None,
    ) -> None:
        self._run_item_id = run_item_id
        self._blob_store = blob_store
        self._credential_version_id = credential_version_id
        self._prompt = prompt
        # Which reference versions the call sent (``creative_recipe.recipe_lineage``),
        # recorded on the AssetVersion so it traces back to its sources (v9 §3.1).
        self._reference_sources = reference_sources
        self._committed: CommittedBlobRef | None = None
        self._thumbnail: CommittedThumbnail | None = None

    # -- Phase 1: prepare transaction --------------------------------------

    def link_prepared_attempt(
        self,
        session: Session,
        ctx: TenantContext,
        *,
        attempt: ProviderCallAttemptRecord,
    ) -> None:
        """Transition RunItem to ``running`` in the prepare transaction.

        The ``ProviderCallAttemptRecord.run_item_id`` FK already links the
        attempt to the RunItem (set by the coordinator's ``prepare_attempt``
        call); this method performs the domain-side state transition so the
        RunItem reflects that dispatch is in progress.
        """
        item = session.execute(
            select(RunItemRecord).where(
                RunItemRecord.id == self._run_item_id,
                RunItemRecord.workspace_id == ctx.workspace_id,
            )
        ).scalar_one()

        if item.state in _DISPATCHABLE_STATES:
            _transition_item(session, ctx, item, state="running")
        if self._credential_version_id and attempt.credential_version_id is None:
            attempt.credential_version_id = self._credential_version_id

    # -- Phase 2: materialize (outside any transaction) ---------------------

    def materialize(
        self,
        ctx: TenantContext,
        *,
        attempt_id: str,
        output: GenerationOutput,
    ):
        """Place the output in durable storage and return its reference.

        Real image bytes (``output.content_bytes``) are committed to the blob
        store under a content-addressed key -- the same
        ``committed/<workspace>/generation_output/<sha256>.<ext>`` layout the
        mock path uses, so a retry re-commits the identical object instead of
        leaking a second one -- and the committed key is the reference. That
        is I/O, so this returns an awaitable the coordinator awaits. The
        controlled mock's output is synthetic: a SHA-256 of its salient
        fields is returned directly.
        """
        if self._blob_store is None or output.content_bytes is None:
            return hashlib.sha256(
                f"{attempt_id}:{output.object_key}:{output.content_sha256}".encode()
            ).hexdigest()
        return self._commit_bytes(ctx, output)

    async def _commit_bytes(self, ctx: TenantContext, output: GenerationOutput) -> str:
        assert self._blob_store is not None and output.content_bytes is not None
        digest = hashlib.sha256(output.content_bytes)
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(
            output.mime_type, "png"
        )
        committed_key = (
            f"committed/{ctx.workspace_id}/generation_output/{digest.hexdigest()}.{extension}"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / f"generation-output.{extension}"
            source.write_bytes(output.content_bytes)
            self._committed = await self._blob_store.commit_generated_artifact(
                source,
                GeneratedArtifactSpec(
                    committed_key=committed_key,
                    content_type=output.mime_type,
                    expected_sha256_b64=base64.b64encode(digest.digest()).decode("ascii"),
                ),
            )
        # Only once the original is safely committed: the thumbnail is a
        # derivative of it, and a derivative that cannot be produced must
        # never cost the paid call its evidence.
        self._thumbnail = await commit_thumbnail_best_effort(
            self._blob_store,
            ctx.workspace_id,
            output.content_bytes,
            log_context={"run_item_id": self._run_item_id, "object_key": committed_key},
        )
        return self._committed.committed_key

    # -- Phase 3: completion transaction ------------------------------------

    def persist_result(
        self,
        session: Session,
        ctx: TenantContext,
        *,
        attempt: ProviderCallAttemptRecord,
        reference: str,
        output: GenerationOutput,
    ) -> None:
        """Create the AssetVersion evidence and advance the RunItem.

        All writes happen inside the coordinator's completion session, so
        they commit (or roll back) atomically with the attempt's terminal
        ``succeeded`` state -- no orphaned assets, no inconsistent RunItem.
        """
        item = session.execute(
            select(RunItemRecord).where(
                RunItemRecord.id == self._run_item_id,
                RunItemRecord.workspace_id == ctx.workspace_id,
            )
        ).scalar_one()

        run = session.execute(
            select(GenerationRunRecord).where(
                GenerationRunRecord.id == item.generation_run_id,
                GenerationRunRecord.workspace_id == ctx.workspace_id,
            )
        ).scalar_one()

        # -- Asset + AssetVersion (mirrors worker._persist_output) ---------
        asset = AssetRecord(
            id=new_id("asset"),
            workspace_id=ctx.workspace_id,
            product_id=run.product_id,
            run_id=item.generation_run_id,
            combo_id=item.combination_fingerprint,
            combo_code=item.combination_fingerprint,
            tags_json=item.tags_json,
            filename=output.object_key,
            provider=attempt.provider,
            model=attempt.model or "",
            prompt=self._prompt,
            fingerprint=item.semantic_fingerprint,
            status="ok",
        )
        session.add(asset)
        session.flush()

        recipe_snapshot = session.get(
            GenerationRecipeSnapshotRecord, run.recipe_snapshot_id
        )
        truth_dependency = dict(
            (recipe_snapshot.canonical_json or {}).get("truth_dependency") or {}
        )
        truth_evidence = truth_dependency.get("evidence")
        if not isinstance(truth_evidence, dict):
            raise RuntimeError(
                "Generation recipe is missing frozen truth-dependency evidence"
            )

        committed = self._committed
        version = AssetVersionRecord(
            id=new_id("av"),
            workspace_id=ctx.workspace_id,
            asset_id=asset.id,
            kind="generated",
            aspect_ratio=run.aspect_ratio,
            object_key=output.object_key,
            committed_object_key=committed.committed_key if committed is not None else None,
            committed_storage_version_id=(
                committed.storage_version_id if committed is not None else None
            ),
            content_sha256=output.content_sha256,
            byte_size=committed.size_bytes if committed is not None else output.byte_size,
            mime_type=(
                committed.content_type_verified if committed is not None else output.mime_type
            ),
            producing_run_item_id=item.id,
            provider_attempt_id=attempt.id,
            origin_kind="generation",
            execution_mode=item.execution_mode,
            effective_seed=item.effective_seed,
            requested_seed=item.requested_seed,
            seed_unavailable_reason=item.seed_unavailable_reason,
            truth_package_id=truth_dependency.get("truth_package_id"),
            truth_dependency_expected=truth_dependency.get("expected"),
            truth_dependency_policy_version=truth_dependency.get("policy_version"),
            truth_dependency_evidence_json=truth_evidence,
            truth_dependency_evidence_hash=_canonical_hash(truth_evidence),
            reference_sources_json=self._reference_sources,
        )
        apply_thumbnail(version, self._thumbnail)
        session.add(version)
        session.flush()
        record_asset_version_generated(session, ctx, version)

        # -- Binding -------------------------------------------------------
        session.add(
            RunItemAssetVersionBindingRecord(
                id=new_id("riavb"),
                workspace_id=ctx.workspace_id,
                run_item_id=item.id,
                asset_version_id=version.id,
                kind="produced",
                producing_provider_attempt_id=attempt.id,
                created_at=utcnow(),
            )
        )
        session.flush()

        # -- RunItem state advance -----------------------------------------
        item = _transition_item(session, ctx, item, state="qc_pending")

        # -- Enqueue QC ----------------------------------------------------
        enqueue_in_session(
            session,
            context=ctx,
            job_type="generation.qc_asset_version",
            resource_type="asset_version",
            resource_id=version.id,
            payload={
                "asset_version_id": version.id,
                "run_item_id": item.id,
            },
            idempotency_key=f"qc:{version.id}",
        )

        # -- Sync GenerationRun projection ---------------------------------
        _sync_run_state_best_effort(session, ctx, item.generation_run_id)
