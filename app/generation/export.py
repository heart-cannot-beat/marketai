"""Export package creation and provenance manifest (#21 Task 6; expanded
Issue #49 Task 9 for the real async 202 + Operation + Worker + ZIP flow).

An ``ExportPackageRecord`` freezes an exact selection of ``AssetVersion``
IDs, one generation execution mode, and the Operation that authorized the
selection. It is immutable at the selector level: later changes to the
underlying AssetVersions never silently alter package membership, and a
selection spanning more than one execution mode (``byok``/``sandbox``/
``mock``) is rejected outright rather than partially packaged.

This module owns package creation (including server-side resolution of the
``approved_only``/``winner_only``/``all_successful`` selector presets) and
the deterministic provenance Manifest built from the frozen selection.
Building the ZIP/blob itself and transitioning the package to ``ready`` is
``app.generation.export_worker.handle_export_package`` -- this module
produces the inputs that Worker needs (the frozen selector, the Manifest
shape) and the read-side projections (``get_export_package``,
``resolve_export_blob_ref``) the v1 router calls for polling recovery and
download-intent issuance.

A draft of ads-first ideas does not leave through a package (Issue #219, ADR
0018 §2): an explicit selection that names one is refused whole, a preset
leaves them out and says how many, and a ready package is handed out only
while every ad in it may still be exported. Whether an image is such a
draft is ``app.marketing.ideas_export``'s judgement, not this module's; any
other image is selected and packaged exactly as before.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetVersionRecord,
    new_id,
    utcnow,
)
from app.generation.experiment_conclusions import project_contextual_conclusion_for_context
from app.generation.models import ExportPackageRecord
from app.generation.review import project_effective_review
from app.platform.blob_store import CommittedBlobRef
from app.platform.denial_audit import check_and_record_denial
from app.platform.idempotency import create_or_replay_operation, get_idempotency_record, hash_idempotency_key
from app.platform.jobs import enqueue_in_session
from app.platform.tenancy import TenantContext

__all__ = [
    "ExportError",
    "create_export_package",
    "get_export_package",
    "build_manifest",
    "resolve_export_blob_ref",
    "left_out_drafts",
]


class ExportError(ValueError):
    def __init__(self, code: str, detail: str, *, params: dict | None = None):
        self.code = code
        self.detail = detail
        self.params = params or {}
        super().__init__(f"{code}: {detail}")


def _load_versions_in_selector_order(
    session: Session, ctx: TenantContext, version_ids: list[str]
) -> list[AssetVersionRecord]:
    """Load the requested AssetVersions, workspace-scoped, in ``version_ids`` order.

    Any id that does not resolve inside the caller's Workspace fails the
    whole selection closed rather than silently shrinking the package.
    """
    rows = session.execute(
        select(AssetVersionRecord).where(
            AssetVersionRecord.id.in_(version_ids),
            AssetVersionRecord.workspace_id == ctx.workspace_id,
        )
    ).scalars().all()
    by_id = {v.id: v for v in rows}

    missing = [vid for vid in version_ids if vid not in by_id]
    if missing:
        for vid in missing:
            check_and_record_denial(
                session,
                ctx,
                AssetVersionRecord,
                vid,
                resource_kind="asset_version",
                detail="create_export_package",
            )
        raise ExportError(
            "asset_version_not_found",
            f"AssetVersion(s) not found in workspace: {', '.join(missing)}",
        )

    return [by_id[vid] for vid in version_ids]


def _all_workspace_version_ids(session: Session, ctx: TenantContext) -> list[str]:
    """Every AssetVersion id in the caller's Workspace, deterministically ordered.

    ``AssetVersionRecord`` carries no failure/success status of its own --
    a row only ever exists for a produced output (a failed RunItem never
    creates one) -- so this is also the basis for the ``all_successful``
    selector preset.
    """
    return list(
        session.execute(
            select(AssetVersionRecord.id)
            .where(AssetVersionRecord.workspace_id == ctx.workspace_id)
            .order_by(AssetVersionRecord.created_at, AssetVersionRecord.id)
        ).scalars()
    )


def _query_approved_only(session: Session, ctx: TenantContext) -> list[str]:
    """``approved_only`` preset: every AssetVersion whose Task 5
    ``project_effective_review`` head is ``"approved"``."""
    candidates = _all_workspace_version_ids(session, ctx)
    return [
        vid for vid in candidates
        if project_effective_review(session, ctx, vid) == "approved"
    ]


def _query_winner_only(session: Session, ctx: TenantContext, experiment_context: str) -> list[str]:
    """``winner_only`` preset: every AssetVersion whose Task 6 contextual
    conclusion, anchored to ``experiment_context``, is ``"winner"``."""
    candidates = _all_workspace_version_ids(session, ctx)
    return [
        vid for vid in candidates
        if project_contextual_conclusion_for_context(session, ctx, vid, experiment_context) == "winner"
    ]


def _resolve_selector(session: Session, ctx: TenantContext, selector: dict) -> list[str]:
    """Turn a discriminated ``ExportSelector`` into a concrete, ordered
    ``version_ids`` list.

    * ``explicit`` -- the caller-supplied ``version_ids`` verbatim.
    * ``approved_only`` -- server-resolved via the effective-review projection.
    * ``winner_only`` -- server-resolved via the contextual-conclusion
      projection; requires ``experiment_context_id`` (top-level or nested
      under ``scope``), otherwise ``experiment_context_required``.
    * ``all_successful`` -- every AssetVersion in the Workspace.
    """
    kind = selector.get("kind") or "explicit"

    if kind == "explicit":
        return list(selector.get("version_ids") or [])

    if kind == "approved_only":
        return _query_approved_only(session, ctx)

    if kind == "winner_only":
        scope = selector.get("scope") or {}
        experiment_context = selector.get("experiment_context_id") or scope.get("experiment_context_id")
        if not experiment_context:
            raise ExportError(
                "experiment_context_required",
                "winner_only selector requires experiment_context_id",
            )
        return _query_winner_only(session, ctx, experiment_context)

    if kind == "all_successful":
        return _all_workspace_version_ids(session, ctx)

    raise ExportError("invalid_selector", f"Unknown export selector kind: {kind!r}")


def _export_fingerprint(selector: dict) -> str:
    """Deterministic fingerprint of the request identity, matching the
    ``_submission_fingerprint`` pattern in ``app.generation.submission``: a
    replayed ``idempotency_key`` bound to a *different* selector is a reuse
    conflict, not a replay.
    """
    raw = json.dumps(selector, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _unexportable_ads(session: Session, ctx: TenantContext, version_ids: list[str]) -> list[dict]:
    """The ads of ads-first ideas among ``version_ids`` that may not be
    exported now, each with its status and reasons (Issue #219). Empty for
    any selection without such an ad."""
    from app.marketing import ideas_export

    return ideas_export.library_refusals(session, ctx, version_ids)


def _refuse(refused: list[dict], detail: str) -> ExportError:
    from app.marketing import ideas_export

    return ExportError("ads_not_exportable", detail, params=ideas_export.refusal_params(refused))


def _accepted_replay(
    session: Session, ctx: TenantContext, selector: dict, idempotency_key: str | None
) -> dict | None:
    """The package an earlier, identical request under ``idempotency_key``
    already created, as that request was answered -- or ``None``.

    Asked only when the gate would refuse (Issue #219): an ad that was
    exportable when the package was accepted may have stopped being so since,
    and a client retrying after a lost response still gets its package id
    back. The package's link is refused anyway (``resolve_export_blob_ref``).
    A key bound to any other request is left to ``create_or_replay_operation``
    to refuse, exactly as before."""
    if not idempotency_key:
        return None
    record = get_idempotency_record(session, hash_idempotency_key(idempotency_key), ctx.workspace_id)
    if (
        record is None
        or record.state != "committed"
        or record.command_type != "generation.export"
        or record.request_fingerprint_hash != _export_fingerprint(selector)
    ):
        return None
    operation = create_or_replay_operation(
        session,
        idempotency_key=idempotency_key,
        command_type="generation.export",
        fingerprint_hash=_export_fingerprint(selector),
        actor=ctx.initiator_id or ctx.executor,
    )
    package = session.execute(
        select(ExportPackageRecord).where(
            ExportPackageRecord.workspace_id == ctx.workspace_id,
            ExportPackageRecord.operation_id == operation.id,
        )
    ).scalar_one_or_none()
    if package is None:
        return None
    return {"package_id": package.id, "operation_id": operation.id, "state": operation.state}


def left_out_drafts(package: ExportPackageRecord) -> list[str]:
    """The ads a preset selector matched but left out of ``package``
    because they could not be exported when it was created."""
    return list((package.selected_versions_json or {}).get("left_out_draft_ids") or [])


def create_export_package(
    session: Session,
    ctx: TenantContext,
    *,
    selector: dict,
    idempotency_key: str | None = None,
) -> dict:
    """Freeze an exact AssetVersion selection into a new ExportPackageRecord
    and enqueue the Worker Job that builds its ZIP (spec §0.4, Issue #49
    Task 9's 202 + Operation flow).

    Idempotency is layered on the platform's Operation protocol
    (``app.platform.idempotency.create_or_replay_operation``), matching
    ``app.generation.submission.submit_generation``: a replayed
    ``idempotency_key`` returns the same Operation instead of minting a
    second one, and this function then looks up (rather than recreates) the
    ExportPackage already bound to that Operation. Without a key, behavior
    is unchanged -- a fresh Operation/package/Job every call.

    Returns ``{"package_id", "operation_id", "state"}`` -- the Operation's
    state (``"queued"`` for a fresh call), never the ORM record directly:
    the caller (the v1 router) is a 202 async-command response, and the
    package's own ``state`` column (``pending``/``building``/...) is a
    separate, Worker-owned projection surfaced by ``get_export_package``.
    """
    version_ids = _resolve_selector(session, ctx, selector)
    if not version_ids:
        raise ExportError("empty_selection", "No versions selected")

    versions = _load_versions_in_selector_order(session, ctx, version_ids)

    # A draft of ads-first ideas does not leave (Issue #219). Named by the
    # caller, it refuses the whole selection and says which ads and why;
    # matched by a preset, it is left out -- an ad that needs redoing never
    # becomes exportable again, so refusing would close the preset for good.
    left_out: list[str] = []
    refused = _unexportable_ads(session, ctx, [v.id for v in versions])
    if refused:
        replay = _accepted_replay(session, ctx, selector, idempotency_key)
        if replay is not None:
            return replay
        left_out = [item["asset_version_id"] for item in refused]
        versions = [v for v in versions if v.id not in set(left_out)]
        if (selector.get("kind") or "explicit") == "explicit" or not versions:
            raise _refuse(refused, "选中的素材里有还不能导出的广告：先核对它们用到的事实、外观和检查结果，需要重出的请先重出。")

    modes = {v.execution_mode or "mock" for v in versions}
    if len(modes) > 1:
        raise ExportError(
            "mixed_execution_modes",
            "Selected versions span multiple execution modes",
        )

    execution_mode = modes.pop()

    fingerprint_hash = _export_fingerprint(selector)
    operation = create_or_replay_operation(
        session,
        idempotency_key=idempotency_key,
        command_type="generation.export",
        fingerprint_hash=fingerprint_hash,
        actor=ctx.initiator_id or ctx.executor,
    )

    existing = session.execute(
        select(ExportPackageRecord).where(
            ExportPackageRecord.workspace_id == ctx.workspace_id,
            ExportPackageRecord.operation_id == operation.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # A replay: the first call already created the package (and enqueued
        # its Job) bound to this Operation.
        return {"package_id": existing.id, "operation_id": operation.id, "state": operation.state}

    now = utcnow()
    package = ExportPackageRecord(
        id=new_id("xp"),
        workspace_id=ctx.workspace_id,
        operation_id=operation.id,
        actor_id=ctx.initiator_id or "unknown",
        selector_json=selector,
        selected_versions_json={
            "version_ids": [v.id for v in versions],
            **({"left_out_draft_ids": left_out} if left_out else {}),
        },
        execution_mode=execution_mode,
        state="pending",
        revision=1,
        created_at=now,
        updated_at=now,
    )
    session.add(package)
    session.flush()

    enqueue_in_session(
        session,
        context=ctx,
        job_type="generation.export_package",
        resource_type="export_package",
        resource_id=package.id,
        payload={"package_id": package.id},
        idempotency_key=f"export_pkg:{package.id}",
    )

    return {"package_id": package.id, "operation_id": operation.id, "state": operation.state}


def get_export_package(session: Session, ctx: TenantContext, package_id: str) -> ExportPackageRecord:
    package = session.execute(
        select(ExportPackageRecord).where(
            ExportPackageRecord.id == package_id,
            ExportPackageRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if package is None:
        raise ExportError(
            "export_package_not_found", f"ExportPackage {package_id} not found"
        )
    return package


#: Internal alias -- ``app.generation.export_worker`` (Task 9) imports this
#: private name directly, matching the private-import convention already
#: established for ``_load_versions_in_selector_order`` between this module
#: and its Worker.
_get_package = get_export_package


def build_manifest(
    session: Session,
    ctx: TenantContext,
    package_id: str,
) -> dict:
    """Build the deterministic provenance Manifest for a frozen ExportPackage.

    Entry order follows the package's frozen ``selected_versions_json`` order
    (the original selector order), not database insertion order, so the
    Manifest is reproducible across builds. This is also the exact content
    the Worker (``app.generation.export_worker``) writes to ``manifest.json``
    inside the committed ZIP.
    """
    package = get_export_package(session, ctx, package_id)

    version_ids = package.selected_versions_json.get("version_ids", [])
    versions = _load_versions_in_selector_order(session, ctx, version_ids)

    from app.learnings.codes import asset_code

    entries = []
    for v in versions:
        entries.append({
            "asset_version_id": v.id,
            # The printable code a seller copies into the results sheet or
            # the ad name (Issue #166).
            "asset_code": asset_code(ctx.workspace_id, v.id),
            "asset_id": v.asset_id,
            "content_sha256": v.content_sha256,
            "byte_size": v.byte_size,
            "mime_type": v.mime_type,
            "origin_kind": v.origin_kind,
            "execution_mode": v.execution_mode,
            "producing_run_item_id": v.producing_run_item_id,
            "provider_attempt_id": v.provider_attempt_id,
            "requested_seed": v.requested_seed,
            "effective_seed": v.effective_seed,
            "seed_unavailable_reason": v.seed_unavailable_reason,
        })

    return {
        "package_id": package.id,
        "execution_mode": package.execution_mode,
        "entries": entries,
        "total_versions": len(entries),
    }


def resolve_export_blob_ref(
    session: Session, ctx: TenantContext, package_id: str
) -> CommittedBlobRef:
    """Resolve the exact ``CommittedBlobRef`` a ready ExportPackage's ZIP was
    committed to (Issue #49 Task 9), mirroring
    ``app.generation.asset_queries.resolve_asset_blob_ref``'s shape for
    AssetVersions.

    Resolved server-side, never supplied by the browser -- and resolved
    fresh on every call, replays included -- so a replay still fails closed
    if the package's blob somehow became unavailable between requests. A
    package still ``pending``/``building``/``failed``/``expired`` (or one
    the Worker hasn't yet committed a ZIP for) raises
    ``export_package_not_ready`` rather than issuing a capability against a
    blob that doesn't exist (spec: "Export completion never creates a
    capability that may expire before the user polls").

    The package is frozen, but whether an ad of ads-first ideas may leave is
    not (Issue #219): a fact it cites may have been changed since the
    package was built. Every call asks again, and a package holding an ad
    that may no longer be exported raises ``ads_not_exportable``; the
    package stays as history and a new one has to be made.
    """
    package = get_export_package(session, ctx, package_id)
    if (
        package.state != "ready"
        or not package.committed_object_key
        or not package.committed_storage_version_id
    ):
        raise ExportError(
            "export_package_not_ready",
            f"ExportPackage {package_id} is not ready for download",
        )
    refused = _unexportable_ads(
        session, ctx, list((package.selected_versions_json or {}).get("version_ids") or [])
    )
    if refused:
        raise _refuse(refused, "这个导出包里有广告在建包之后已经不能导出了，请重新建一个导出包。")

    return CommittedBlobRef(
        committed_key=package.committed_object_key,
        storage_version_id=package.committed_storage_version_id,
        size_bytes=package.byte_size,
        checksum_sha256_b64=package.zip_hash,
        content_type_verified="application/zip",
    )
