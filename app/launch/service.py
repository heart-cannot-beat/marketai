"""Preview-confirm-Saga workflow for one minimal PAUSED Meta ad."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select, update

from .. import config
from ..adapters.meta.write_client import HttpMetaWriteAdapter, MetaWriteError, MetaWritePort
from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import (
    AssetVersionRecord,
    ExperimentAdBindingRecord,
    ExperimentEntryRecord,
    ExperimentRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    LaunchAuditRecord,
    LaunchConfirmationRecord,
    LaunchOperationRecord,
    LaunchPreviewRecord,
    LaunchStepRecord,
    MetaAccountRecord,
    MetaAdBindingRecord,
    new_id,
    utcnow,
)
from ..experiments.judge import canonical_hash
from ..platform import jobs
from ..platform.audit import append_event, redact
from ..platform.tenancy import TenantContext, current_context
from ..meta.models import MetaConnectionRecord, MetaDataSourceBindingRecord
from .models import (
    LaunchCandidateRecord,
    MetaLaunchConfirmationRecord,
    MetaLaunchPreviewRecord,
)


REMOTE_STEPS = ("campaign_created", "adset_created", "image_uploaded", "creative_created", "ad_created")


#: A message that is a code (``"confirmation_token_expired"``) carries it.
_MESSAGE_CODE = re.compile(r"([a-z][a-z0-9_]*)(?::|$)")


class LaunchError(RuntimeError):
    """``code`` is the stable code the interface renders (#253): an explicit
    ``code=``, else the code a message starts with, else the class default."""

    default_code = "launch_invalid"

    def __init__(self, message: str = "", *, code: str | None = None, params: dict | None = None) -> None:
        super().__init__(message)
        prefix = _MESSAGE_CODE.match(message)
        self.code = code or (prefix.group(1) if prefix else self.default_code)
        self.params = params or {}


class LaunchForbidden(LaunchError):
    default_code = "launch_forbidden"


class LaunchConflict(LaunchError):
    default_code = "launch_conflict"


class LaunchNotFound(LaunchError):
    default_code = "launch_not_found"


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _asset_path(object_key: str) -> Path:
    path = Path(object_key)
    return path if path.is_absolute() else config.DATA_DIR / path


def _audit(session, event: str, details: dict, operation_id: str | None = None) -> None:
    context = current_context(session)
    actor = context.initiator_id or context.executor
    safe_details = redact(details)
    session.add(LaunchAuditRecord(
        id=new_id("laudit"), workspace_id=context.workspace_id, operation_id=operation_id,
        event_type=event, actor_id=actor, details_json=safe_details,
    ))
    append_event(
        session,
        event_type=event,
        resource_type="launch_operation" if operation_id else "launch",
        resource_id=operation_id or "",
        details=safe_details,
    )


def _get_preview(session, context: TenantContext, preview_id: str) -> LaunchPreviewRecord:
    preview = session.get(LaunchPreviewRecord, preview_id)
    if preview is None or preview.workspace_id != context.workspace_id:
        raise LaunchNotFound("Launch Preview 不存在", code="launch_preview_not_found")
    return preview


def _get_operation(session, context: TenantContext, operation_id: str) -> LaunchOperationRecord:
    operation = session.get(LaunchOperationRecord, operation_id)
    if operation is None or operation.workspace_id != context.workspace_id:
        raise LaunchNotFound("Launch Operation 不存在", code="launch_operation_not_found")
    return operation


# ---------------------------------------------------------------------------
# Multi-candidate preview creation (Task 13, spec §3.15-§3.16, §5.1-§5.2)
#
# Unlike the single-candidate `create_preview`/`confirm_preview`/
# `start_operation` trio above -- which use `tenant_session(context)`
# internally and the pre-existing `LaunchPreviewRecord` from
# `app.adapters.persistence.models` -- these functions take a raw
# SQLAlchemy `session` explicitly (matching the Task 9-12 service
# pattern) and write the new `MetaLaunchPreviewRecord` / `LaunchCandidateRecord`
# rows from `app.launch.models`. The old functions are intentionally left
# unmodified.
# ---------------------------------------------------------------------------

def _connection_idempotency_key_material(workspace_id: str, connection_id: str) -> bytes:
    """Deterministic per-(workspace, connection) HMAC keying material.

    Spec §3.16 specifies `HMAC-SHA256(connection_secret, canonical_identity)`.
    A real per-connection secret is only resolvable via KMS dereference of
    the binding's `app_secret_version_id` (`credential_resolution.py`),
    which is a saga-execution concern (Task 14+), not preview planning --
    and the binding's credential fields are optional/nullable at preview
    time (see `MetaDataSourceBindingRecord`). This derives a stable,
    connection-scoped substitute so idempotency keys are still (a)
    deterministic for identical inputs, (b) unique per connection, and (c)
    never mixed across connections or workspaces -- the properties spec
    §3.16 actually depends on for candidate-level idempotency.
    """
    return hashlib.sha256(
        f"launch_idempotency_secret:{workspace_id}:{connection_id}".encode("utf-8")
    ).digest()


def _candidate_idempotency_key(
    workspace_id: str, connection_id: str, preview_id: str, candidate_index: int, object_type: str,
) -> str:
    canonical_identity = canonical_hash({
        "connection_id": connection_id,
        "preview_id": preview_id,
        "candidate_index": candidate_index,
        "object_type": object_type,
    })
    key = _connection_idempotency_key_material(workspace_id, connection_id)
    return hmac.new(key, canonical_identity.encode("utf-8"), hashlib.sha256).hexdigest()


_META_MODES = frozenset({"customer_sandbox", "controlled_mock", "internal_validation"})


@dataclass(frozen=True)
class IssuedMetaLaunchConfirmation:
    preview: MetaLaunchPreviewRecord
    confirmation: MetaLaunchConfirmationRecord
    confirmation_token: str


def _multi_confirmation_context_hash(
    context: TenantContext, preview: MetaLaunchPreviewRecord,
) -> str:
    return canonical_hash({
        "workspace_id": context.workspace_id,
        "meta_mode": context.meta_mode,
        "actor_id": context.initiator_id or context.executor,
        "authorization_epoch": context.authorization_epoch,
        "preview_id": preview.id,
        "preview_hash": preview.preview_hash,
        "experiment_id": preview.experiment_id,
        "experiment_version_id": preview.experiment_version_id,
        "meta_connection_id": preview.meta_connection_id,
        "meta_binding_id": preview.meta_binding_id,
        "frozen_snapshot": preview.frozen_snapshot,
    })


def _load_exact_frozen_candidate_manifest(
    session,
    context: TenantContext,
    *,
    experiment_id: str,
    experiment_version_id: str,
) -> list[ExperimentVersionCandidateRecord]:
    """Load the complete immutable candidate set for one frozen Experiment.

    Launch never trusts client-provided candidate/version relationships.  It
    reconstructs the manifest under Workspace scope and later compares every
    id and AssetVersion pair in position order.
    """
    experiment = session.execute(
        select(ExperimentRecord).where(
            ExperimentRecord.id == experiment_id,
            ExperimentRecord.workspace_id == context.workspace_id,
        )
    ).scalar_one_or_none()
    if experiment is None:
        raise LaunchNotFound("Frozen Experiment not found", code="experiment_not_found")
    if experiment.state != "frozen" or experiment.record_schema_version != 2:
        raise LaunchConflict("Launch Preview requires a frozen schema-2 Experiment", code="launch_experiment_not_frozen")

    version = session.execute(
        select(ExperimentVersionRecord).where(
            ExperimentVersionRecord.id == experiment_version_id,
            ExperimentVersionRecord.workspace_id == context.workspace_id,
            ExperimentVersionRecord.experiment_id == experiment_id,
        )
    ).scalar_one_or_none()
    if version is None:
        raise LaunchNotFound("ExperimentVersion does not belong to this Experiment", code="launch_experiment_version_missing")

    candidates = list(session.execute(
        select(ExperimentVersionCandidateRecord)
        .where(
            ExperimentVersionCandidateRecord.workspace_id == context.workspace_id,
            ExperimentVersionCandidateRecord.experiment_version_id == experiment_version_id,
        )
        .order_by(ExperimentVersionCandidateRecord.position)
    ).scalars().all())
    if len(candidates) < 2:
        raise LaunchConflict("Frozen Experiment must contain at least two candidates", code="launch_candidates_too_few")
    modes = {candidate.execution_mode for candidate in candidates}
    if len(modes) != 1 or not modes.issubset({"byok", "sandbox", "mock"}):
        raise LaunchConflict("execution_mode_mixed")
    return candidates


def validate_multi_candidate_preview_integrity(
    session,
    context: TenantContext,
    preview: MetaLaunchPreviewRecord,
) -> list[ExperimentVersionCandidateRecord]:
    """Revalidate the preview's complete Workspace/mode/candidate chain."""
    if context.meta_mode not in _META_MODES or preview.meta_mode != context.meta_mode:
        raise LaunchConflict("launch_meta_mode_mismatch")

    connection = session.execute(
        select(MetaConnectionRecord).where(
            MetaConnectionRecord.id == preview.meta_connection_id,
            MetaConnectionRecord.workspace_id == context.workspace_id,
            MetaConnectionRecord.meta_mode == context.meta_mode,
            MetaConnectionRecord.state == "active",
        )
    ).scalar_one_or_none()
    binding = session.execute(
        select(MetaDataSourceBindingRecord).where(
            MetaDataSourceBindingRecord.id == preview.meta_binding_id,
            MetaDataSourceBindingRecord.workspace_id == context.workspace_id,
            MetaDataSourceBindingRecord.meta_mode == context.meta_mode,
            MetaDataSourceBindingRecord.meta_connection_id == preview.meta_connection_id,
            MetaDataSourceBindingRecord.state == "active",
        )
    ).scalar_one_or_none()
    if connection is None or binding is None:
        raise LaunchConflict("launch_meta_scope_changed")

    frozen = _load_exact_frozen_candidate_manifest(
        session,
        context,
        experiment_id=preview.experiment_id,
        experiment_version_id=preview.experiment_version_id,
    )
    launched = list(session.execute(
        select(LaunchCandidateRecord)
        .where(
            LaunchCandidateRecord.workspace_id == context.workspace_id,
            LaunchCandidateRecord.meta_mode == context.meta_mode,
            LaunchCandidateRecord.preview_id == preview.id,
        )
        .order_by(LaunchCandidateRecord.candidate_index)
    ).scalars().all())
    expected_pairs = [
        (candidate.id, candidate.asset_version_id) for candidate in frozen
    ]
    launched_pairs = [
        (candidate.experiment_version_candidate_id, candidate.asset_version_id)
        for candidate in launched
    ]
    if launched_pairs != expected_pairs or preview.candidate_count != len(frozen):
        raise LaunchConflict("launch_candidate_manifest_mismatch")
    snapshot = dict(preview.frozen_snapshot or {})
    if snapshot.get("meta_mode") != context.meta_mode:
        raise LaunchConflict("launch_meta_mode_mismatch")
    if snapshot.get("candidate_execution_mode") != frozen[0].execution_mode:
        raise LaunchConflict("launch_execution_mode_mismatch")
    from ..experiments.service import (
        ExperimentConflict,
        validate_live_launch_eligibility,
    )

    try:
        live_candidates = validate_live_launch_eligibility(
            session,
            context,
            experiment_id=preview.experiment_id,
            experiment_version_id=preview.experiment_version_id,
        )
    except ExperimentConflict as exc:
        raise LaunchConflict(str(exc), code=exc.code) from exc
    if [candidate.id for candidate in live_candidates] != [
        candidate.id for candidate in frozen
    ]:
        raise LaunchConflict("launch_candidate_manifest_mismatch")
    return frozen


def create_multi_candidate_preview(
    session, context: TenantContext, *, experiment_id: str, experiment_version_id: str,
    connection_id: str, ad_account_id: str, candidates: list[dict[str, Any]],
    daily_budget_minor: int, currency: str, campaign_config: dict[str, Any],
    expires_at=None,
) -> tuple[MetaLaunchPreviewRecord, list[LaunchCandidateRecord]]:
    """Freeze an N-candidate Launch preview (spec §3.15-§3.16).

    Takes a raw `session` (not `tenant_session(context)`) so callers can
    supply a plain SQLAlchemy session -- production code is expected to
    open one under `tenant_session(context)` itself before calling this.
    Meta write access stays disabled throughout: this only persists local
    planning rows and never calls a Meta write adapter.
    """
    if context.meta_mode not in _META_MODES:
        raise LaunchError("meta_mode must be customer_sandbox, controlled_mock, or internal_validation", code="invalid_meta_mode")
    if daily_budget_minor <= 0 or daily_budget_minor > config.LAUNCH_MAX_DAILY_BUDGET_MINOR:
        raise LaunchError(f"daily_budget_minor 必须在 1..{config.LAUNCH_MAX_DAILY_BUDGET_MINOR}", code="launch_daily_budget_invalid", params={"limit": config.LAUNCH_MAX_DAILY_BUDGET_MINOR})
    if not currency or len(currency) != 3:
        raise LaunchError("currency 必须是 3 位 ISO 货币代码", code="launch_currency_invalid")
    if not candidates:
        raise LaunchError("candidates 不能为空；预览至少需要一个候选", code="launch_candidates_required")
    if any(
        str(campaign_config.get(key, "PAUSED")).upper() != "PAUSED"
        for key in ("initial_status", "campaign_status", "adset_status", "ad_status")
    ):
        raise LaunchConflict("Launch Preview permits PAUSED status only", code="launch_status_not_paused")

    frozen_manifest = _load_exact_frozen_candidate_manifest(
        session,
        context,
        experiment_id=experiment_id,
        experiment_version_id=experiment_version_id,
    )
    submitted_pairs: list[tuple[str, str]] = []
    for index, candidate in enumerate(candidates):
        try:
            submitted_pairs.append((
                str(candidate["experiment_version_candidate_id"]),
                str(candidate["asset_version_id"]),
            ))
        except KeyError as exc:
            raise LaunchError(f"候选 {index} 缺少必填字段: {exc}", code="launch_candidate_incomplete", params={"index": index}) from exc
    frozen_pairs = [
        (candidate.id, candidate.asset_version_id) for candidate in frozen_manifest
    ]
    if submitted_pairs != frozen_pairs:
        raise LaunchConflict("launch_candidate_manifest_mismatch")

    connection = session.scalar(select(MetaConnectionRecord).where(
        MetaConnectionRecord.id == connection_id,
        MetaConnectionRecord.workspace_id == context.workspace_id,
        MetaConnectionRecord.meta_mode == context.meta_mode,
    ))
    if connection is None or connection.state != "active":
        raise LaunchNotFound("Meta Connection 不存在或未激活", code="connection_not_found")

    binding = session.scalar(select(MetaDataSourceBindingRecord).where(
        MetaDataSourceBindingRecord.workspace_id == context.workspace_id,
        MetaDataSourceBindingRecord.meta_mode == context.meta_mode,
        MetaDataSourceBindingRecord.meta_connection_id == connection_id,
        MetaDataSourceBindingRecord.ad_account_id == ad_account_id,
        MetaDataSourceBindingRecord.state == "active",
    ))
    if binding is None:
        raise LaunchNotFound("未找到该 Connection/Account 的激活 Data Source Binding", code="launch_binding_not_found")

    preview_id = new_id("lprev")
    candidate_count = len(candidates)

    frozen_candidates: list[dict[str, Any]] = []
    candidate_rows: list[LaunchCandidateRecord] = []
    for index, candidate in enumerate(candidates):
        try:
            experiment_version_candidate_id = candidate["experiment_version_candidate_id"]
            asset_version_id = candidate["asset_version_id"]
        except KeyError as exc:
            raise LaunchError(f"候选 {index} 缺少必填字段: {exc}", code="launch_candidate_incomplete", params={"index": index}) from exc
        creative_config = candidate.get("creative_config") or {}
        adset_key = _candidate_idempotency_key(
            context.workspace_id, connection_id, preview_id, index, "adset",
        )
        ad_key = _candidate_idempotency_key(
            context.workspace_id, connection_id, preview_id, index, "ad",
        )
        frozen_candidates.append({
            "candidate_index": index,
            "experiment_version_candidate_id": experiment_version_candidate_id,
            "asset_version_id": asset_version_id,
            "creative_config": creative_config,
            "adset_idempotency_key": adset_key,
            "ad_idempotency_key": ad_key,
        })
        candidate_rows.append(LaunchCandidateRecord(
            id=new_id("lcand"), workspace_id=context.workspace_id,
            meta_mode=context.meta_mode, preview_id=preview_id,
            candidate_index=index,
            experiment_version_candidate_id=experiment_version_candidate_id,
            asset_version_id=asset_version_id,
            creative_config=creative_config,
            adset_idempotency_key=adset_key,
            ad_idempotency_key=ad_key,
        ))

    frozen_snapshot = {
        "schema_version": 1,
        "mode": "multi_candidate",
        "meta_mode": context.meta_mode,
        "candidate_execution_mode": frozen_manifest[0].execution_mode,
        "experiment_id": experiment_id,
        "experiment_version_id": experiment_version_id,
        "connection_id": connection_id,
        "meta_binding_id": binding.id,
        "ad_account_id": ad_account_id,
        "candidate_count": candidate_count,
        "campaign_config": campaign_config,
        "daily_budget_minor": daily_budget_minor,
        "currency": currency,
        "candidates": frozen_candidates,
    }
    preview_hash = canonical_hash(frozen_snapshot)
    now = utcnow()

    preview = MetaLaunchPreviewRecord(
        id=preview_id, workspace_id=context.workspace_id, meta_mode=context.meta_mode,
        meta_connection_id=connection_id, meta_binding_id=binding.id,
        experiment_id=experiment_id, experiment_version_id=experiment_version_id,
        candidate_count=candidate_count, campaign_config=campaign_config,
        daily_budget_minor=daily_budget_minor, currency=currency,
        preview_hash=preview_hash, frozen_snapshot=frozen_snapshot,
        expires_at=expires_at or now + timedelta(seconds=config.LAUNCH_PREVIEW_TTL_SECONDS),
    )
    # Flushed in two passes, not one `add_all` + single `flush()`: neither
    # mapper declares an ORM `relationship()` to the other (spec §3.15/§3.16
    # keep them as plain composite-FK'd tables), so SQLAlchemy's automatic
    # unit-of-work insert ordering has nothing to sort these two mappers'
    # tables by and can emit the `launch_candidate_records` INSERTs before
    # `launch_preview_records`'s -- which an FK-enforced database (this
    # repo's SQLite always runs with `PRAGMA foreign_keys=ON`, and so does
    # Postgres) then rejects, since `fk_lcand_workspace_mode_preview` has
    # nothing to reference yet. Flushing the parent row first removes the
    # ambiguity instead of relying on unit-of-work's best-effort ordering.
    session.add(preview)
    session.flush()
    session.add_all(candidate_rows)
    session.flush()
    return preview, candidate_rows


def confirm_multi_candidate_preview(
    session, context: TenantContext, preview_id: str, preview_hash: str,
) -> IssuedMetaLaunchConfirmation:
    """Confirm a multi-candidate preview: draft -> confirmed (spec §5.2).

    Takes a raw `session`, mirroring `create_multi_candidate_preview`. The
    caller must re-supply `preview_hash` so any drift between what the user
    saw and what is currently frozen is caught before confirmation --
    same integrity contract as the single-candidate `confirm_preview()`
    above, adapted to `MetaLaunchPreviewRecord`.
    """
    preview = session.execute(
        select(MetaLaunchPreviewRecord).where(
            MetaLaunchPreviewRecord.id == preview_id,
            MetaLaunchPreviewRecord.workspace_id == context.workspace_id,
            MetaLaunchPreviewRecord.meta_mode == context.meta_mode,
        )
    ).scalar_one_or_none()
    if preview is None:
        raise LaunchNotFound(f"Preview {preview_id} not found", code="launch_preview_not_found")
    if preview.state != "draft":
        raise LaunchConflict(f"Preview {preview_id} is {preview.state}, not draft", code="launch_preview_not_draft")
    now = utcnow()
    if _aware(preview.expires_at) <= now:
        raise LaunchConflict(f"Preview {preview_id} expired", code="launch_preview_expired")
    if not hmac.compare_digest(preview.preview_hash, preview_hash):
        raise LaunchConflict("Preview hash mismatch; content changed, regenerate preview", code="launch_preview_hash_mismatch")
    validate_multi_candidate_preview_integrity(session, context, preview)
    if not config.LAUNCH_CONFIRMATION_SECRET:
        raise LaunchError("LAUNCH_CONFIRMATION_SECRET 未配置", code="launch_confirmation_unconfigured")
    context_hash = _multi_confirmation_context_hash(context, preview)
    nonce = secrets.token_urlsafe(32)
    signature = hmac.new(
        config.LAUNCH_CONFIRMATION_SECRET.encode("utf-8"),
        f"{preview.id}:{context_hash}:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    token = f"confirm-v2.{preview.id}.{nonce}.{signature}"
    confirmation_expires_at = min(
        _aware(preview.expires_at),
        now + timedelta(seconds=config.LAUNCH_CONFIRM_TTL_SECONDS),
    )
    confirmation = MetaLaunchConfirmationRecord(
        id=new_id("lconf"),
        workspace_id=context.workspace_id,
        meta_mode=str(context.meta_mode),
        preview_id=preview.id,
        preview_hash=preview.preview_hash,
        context_hash=context_hash,
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        actor_id=context.initiator_id or context.executor,
        expires_at=confirmation_expires_at,
    )
    session.add(confirmation)
    preview.state = "confirmed"
    preview.confirmed_at = now
    session.flush()
    return IssuedMetaLaunchConfirmation(
        preview=preview,
        confirmation=confirmation,
        confirmation_token=token,
    )


def consume_multi_candidate_confirmation(
    session,
    context: TenantContext,
    preview: MetaLaunchPreviewRecord,
    confirmation_token: str,
) -> MetaLaunchConfirmationRecord:
    """Validate and atomically consume one fresh context-bound token."""
    token_hash = hashlib.sha256(confirmation_token.encode("utf-8")).hexdigest()
    confirmation = session.execute(
        select(MetaLaunchConfirmationRecord).where(
            MetaLaunchConfirmationRecord.workspace_id == context.workspace_id,
            MetaLaunchConfirmationRecord.meta_mode == context.meta_mode,
            MetaLaunchConfirmationRecord.preview_id == preview.id,
            MetaLaunchConfirmationRecord.token_hash == token_hash,
        ).with_for_update()
    ).scalar_one_or_none()
    now = utcnow()
    if confirmation is None:
        raise LaunchConflict("confirmation_token_invalid")
    if confirmation.actor_id != (context.initiator_id or context.executor):
        raise LaunchConflict("confirmation_context_mismatch")
    if (
        not hmac.compare_digest(confirmation.preview_hash, preview.preview_hash)
        or not hmac.compare_digest(
            confirmation.context_hash,
            _multi_confirmation_context_hash(context, preview),
        )
    ):
        raise LaunchConflict("confirmation_context_mismatch")
    if confirmation.consumed_at is not None:
        raise LaunchConflict("confirmation_token_consumed")
    if _aware(confirmation.expires_at) <= now or _aware(preview.expires_at) <= now:
        raise LaunchConflict("confirmation_token_expired")
    result = session.execute(
        update(MetaLaunchConfirmationRecord).where(
            MetaLaunchConfirmationRecord.id == confirmation.id,
            MetaLaunchConfirmationRecord.workspace_id == context.workspace_id,
            MetaLaunchConfirmationRecord.meta_mode == context.meta_mode,
            MetaLaunchConfirmationRecord.consumed_at.is_(None),
        ).values(consumed_at=now)
    )
    if result.rowcount != 1:
        raise LaunchConflict("confirmation_token_consumed")
    confirmation.consumed_at = now
    session.flush()
    return confirmation


def create_preview(context: TenantContext, *, experiment_id: str, entry_id: str, ad_account_id: str, page_id: str,
                   landing_page_url: str, message: str, headline: str, country: str,
                   daily_budget_minor: int, call_to_action: str = "LEARN_MORE",
                   pixel_id: str = "", special_ad_categories: list[str] | None = None) -> dict:
    if not ad_account_id.startswith("act_"):
        ad_account_id = f"act_{ad_account_id}"
    if daily_budget_minor <= 0 or daily_budget_minor > config.LAUNCH_MAX_DAILY_BUDGET_MINOR:
        raise LaunchError(f"daily_budget_minor 必须在 1..{config.LAUNCH_MAX_DAILY_BUDGET_MINOR}", code="launch_daily_budget_invalid", params={"limit": config.LAUNCH_MAX_DAILY_BUDGET_MINOR})
    parsed = urlparse(landing_page_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise LaunchError("landing_page_url 必须是有效 HTTPS URL")
    country = country.strip().upper()
    if len(country) != 2:
        raise LaunchError("country 必须是 2 位国家代码")
    cta = call_to_action.strip().upper()
    if cta not in {"LEARN_MORE", "SHOP_NOW", "GET_OFFER"}:
        raise LaunchError("call_to_action 不在 canary allowlist")
    categories = [value.strip().upper() for value in (special_ad_categories or [])]
    allowed_categories = {"CREDIT", "EMPLOYMENT", "HOUSING", "ISSUES_ELECTIONS_POLITICS"}
    if any(value not in allowed_categories for value in categories):
        raise LaunchError("special_ad_categories 包含未知类别")
    if not page_id.strip() or not message.strip() or not headline.strip():
        raise LaunchError("Page、正文和标题不能为空")
    if pixel_id.strip():
        raise LaunchError("当前 canary 仅支持 OUTCOME_TRAFFIC；pixel_id 不会被静默忽略，请留空")

    with tenant_session(context) as session:
        experiment = session.get(ExperimentRecord, experiment_id)
        entry = session.get(ExperimentEntryRecord, entry_id)
        if (
            experiment is None
            or experiment.workspace_id != context.workspace_id
            or entry is None
            or entry.experiment_id != experiment_id
            or entry.removed_at is not None
        ):
            raise LaunchNotFound("实验或 Entry 不存在")
        if experiment.state != "frozen":
            raise LaunchConflict("Launch Preview 只接受 frozen 实验")
        version = session.get(AssetVersionRecord, entry.asset_version_id)
        account = session.scalar(select(MetaAccountRecord).where(
            MetaAccountRecord.workspace_id == context.workspace_id,
            MetaAccountRecord.ad_account_id == ad_account_id,
        ))
        if version is None or version.workspace_id != context.workspace_id:
            raise LaunchNotFound("AssetVersion 不存在")
        if account is None or not account.currency or not account.timezone_name:
            raise LaunchError("必须先完成目标 Meta account 的只读同步，确认币种和时区")
        if not _asset_path(version.object_key).is_file():
            raise LaunchError("AssetVersion 对象文件不存在")
        existing_binding = session.scalar(select(ExperimentAdBindingRecord).where(
            ExperimentAdBindingRecord.experiment_entry_id == entry_id
        ))
        if existing_binding:
            raise LaunchConflict("该 Entry 已绑定 ad_id，不能重复 Launch")

        preview = {
            "schema_version": 1,
            "mode": "single_paused_canary",
            "experiment": {
                "id": experiment.id, "entry_id": entry.id,
                "frozen_snapshot_hash": experiment.frozen_snapshot_hash,
                "marketing_vars": entry.marketing_vars_json,
            },
            "account": {
                "ad_account_id": ad_account_id, "currency": account.currency,
                "timezone": account.timezone_name,
            },
            "asset_version": {
                "id": version.id, "content_sha256": version.content_sha256,
                "object_key": version.object_key,
            },
            "delivery": {
                "campaign_status": "PAUSED", "adset_status": "PAUSED", "ad_status": "PAUSED",
                "objective": "OUTCOME_TRAFFIC", "optimization_goal": "LINK_CLICKS",
                "billing_event": "IMPRESSIONS", "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "daily_budget_minor": daily_budget_minor,
                "max_daily_budget_minor": config.LAUNCH_MAX_DAILY_BUDGET_MINOR,
                "targeting": {"geo_locations": {"countries": [country]}},
                "special_ad_categories": categories,
            },
            "creative": {
                "page_id": page_id.strip(), "pixel_id": pixel_id.strip(),
                "landing_page_url": landing_page_url, "message": message.strip(),
                "headline": headline.strip(), "call_to_action": cta,
            },
        }
        digest = canonical_hash(preview)
        now = utcnow()
        row = LaunchPreviewRecord(
            id=new_id("preview"), workspace_id=context.workspace_id,
            experiment_id=experiment_id, experiment_entry_id=entry_id,
            asset_version_id=version.id, ad_account_id=ad_account_id,
            preview_json=preview, preview_hash=digest,
            expires_at=now + timedelta(seconds=config.LAUNCH_PREVIEW_TTL_SECONDS),
            created_by=context.initiator_id or context.executor,
        )
        session.add(row)
        _audit(session, "launch_preview_created", {
            "preview_id": row.id, "preview_hash": digest, "expires_at": row.expires_at.isoformat(),
        })
        session.flush()
        return _preview_dict(row)


def confirm_preview(
    context: TenantContext, preview_id: str, preview_hash: str
) -> dict:
    if not config.LAUNCH_CONFIRMATION_SECRET:
        raise LaunchError("LAUNCH_CONFIRMATION_SECRET 未配置", code="launch_confirmation_unconfigured")
    with tenant_session(context) as session:
        preview = _get_preview(session, context, preview_id)
        now = utcnow()
        if _aware(preview.expires_at) <= now:
            raise LaunchConflict("Launch Preview 已过期，请重新生成")
        if not hmac.compare_digest(preview.preview_hash, preview_hash):
            raise LaunchConflict("Preview hash 不一致；任何内容变化都必须重新确认")
        nonce = secrets.token_urlsafe(32)
        signature = hmac.new(
            config.LAUNCH_CONFIRMATION_SECRET.encode(), f"{preview.id}:{preview.preview_hash}:{nonce}".encode(),
            hashlib.sha256,
        ).hexdigest()
        token = f"confirm.{preview.id}.{nonce}.{signature}"
        row = LaunchConfirmationRecord(
            id=new_id("confirm"), preview_id=preview.id, preview_hash=preview.preview_hash,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            actor_id=context.initiator_id or context.executor,
            expires_at=now + timedelta(seconds=config.LAUNCH_CONFIRM_TTL_SECONDS),
        )
        session.add(row)
        _audit(session, "launch_preview_confirmed", {
            "preview_id": preview.id, "preview_hash": preview.preview_hash,
            "confirmation_id": row.id, "expires_at": row.expires_at.isoformat(),
        })
        session.flush()
        return {"confirmation_token": token, "expires_at": row.expires_at, "preview_hash": preview.preview_hash}


def start_operation(context: TenantContext, preview_id: str, confirmation_token: str, *,
                    test_adapter: MetaWritePort | None = None) -> str:
    if test_adapter is None:
        if context.role != "owner":
            raise LaunchForbidden("只有 Workspace Owner 可以提交真实 Meta 写入")
        from ..meta.service import require_meta_credential_workspace

        require_meta_credential_workspace(context)
        if not config.meta_write_ready():
            raise LaunchError("Meta 写入默认禁用；需独立写 Token、confirmation secret 和显式 kill switch")
    if test_adapter is not None and not test_adapter.is_test_adapter:
        raise LaunchError("只有测试 adapter 可以注入；生产 adapter 由 Worker 内部创建")
    token_hash = hashlib.sha256(confirmation_token.encode()).hexdigest()
    with tenant_session(context) as session:
        preview = _get_preview(session, context, preview_id)
        if test_adapter is None and preview.ad_account_id != config.META_AD_ACCOUNT_ID:
            raise LaunchConflict("Preview 账户与部署冻结的 META_AD_ACCOUNT_ID 不一致")
        confirmation = session.scalar(select(LaunchConfirmationRecord).where(
            LaunchConfirmationRecord.preview_id == preview_id,
            LaunchConfirmationRecord.token_hash == token_hash,
        ))
        now = utcnow()
        if confirmation is None or not hmac.compare_digest(confirmation.preview_hash, preview.preview_hash):
            raise LaunchConflict("二次确认 token 无效或不属于当前 Preview")
        if confirmation.consumed_at is not None:
            existing = session.scalar(select(LaunchOperationRecord).where(
                LaunchOperationRecord.confirmation_id == confirmation.id
            ))
            if existing:
                return existing.id
            raise LaunchConflict("二次确认 token 已使用")
        if _aware(confirmation.expires_at) <= now or _aware(preview.expires_at) <= now:
            raise LaunchConflict("Preview 或二次确认已过期")
        operation_id = new_id("launch")
        operation = LaunchOperationRecord(
            id=operation_id, workspace_id=context.workspace_id, preview_id=preview.id,
            confirmation_id=confirmation.id, experiment_id=preview.experiment_id,
            state="queued", current_step="planned", marker=f"MAI|{operation_id[-12:]}",
        )
        confirmation.consumed_at = now
        session.add(operation)
        session.flush()
        jobs.enqueue_in_session(
            session, context=context, job_type="launch_canary",
            payload={"operation_id": operation.id}, idempotency_key=f"launch-canary:{operation.id}",
            resource_type="launch_operation", resource_id=operation.id,
            safe_to_retry_after_crash=True, max_attempts=10,
        )
        _audit(session, "launch_operation_queued", {
            "preview_id": preview.id, "confirmation_id": confirmation.id,
        }, operation.id)
        return operation.id


def _step_request(step: str, preview: dict, ids: dict[str, str]) -> dict:
    delivery = preview["delivery"]
    creative = preview["creative"]
    if step == "campaign_created":
        return {
            "objective": delivery["objective"], "buying_type": "AUCTION",
            "special_ad_categories": delivery["special_ad_categories"], "status": "PAUSED",
        }
    if step == "adset_created":
        return {
            "campaign_id": ids["campaign_id"], "daily_budget": str(delivery["daily_budget_minor"]),
            "billing_event": delivery["billing_event"], "optimization_goal": delivery["optimization_goal"],
            "bid_strategy": delivery["bid_strategy"], "targeting": delivery["targeting"], "status": "PAUSED",
        }
    if step == "image_uploaded":
        return {"asset_path": str(_asset_path(preview["asset_version"]["object_key"])),
                "content_sha256": preview["asset_version"]["content_sha256"]}
    if step == "creative_created":
        return {"object_story_spec": {
            "page_id": creative["page_id"],
            "link_data": {
                "image_hash": ids["image_hash"], "link": creative["landing_page_url"],
                "message": creative["message"], "name": creative["headline"],
                "call_to_action": {"type": creative["call_to_action"]},
            },
        }}
    if step == "ad_created":
        return {"adset_id": ids["adset_id"], "creative": {"creative_id": ids["creative_id"]},
                "status": "PAUSED"}
    raise ValueError(step)


async def execute_operation(
    context: TenantContext, operation_id: str, adapter: MetaWritePort | None = None
) -> None:
    real_adapter = adapter is None or not adapter.is_test_adapter
    if real_adapter:
        if context.audit_initiator_role != "owner":
            raise LaunchForbidden("真实 Meta 写入必须由 Workspace Owner 发起")
        from ..meta.service import require_meta_credential_workspace

        require_meta_credential_workspace(context)
        if not config.META_WRITE_ENABLED:
            raise MetaWriteError("kill_switch_closed", "Meta write kill switch 已关闭", retryable=False)
    adapter = adapter or HttpMetaWriteAdapter()
    with tenant_session(context) as session:
        operation = _get_operation(session, context, operation_id)
        if operation.state == "completed":
            return
        preview_row = session.get(LaunchPreviewRecord, operation.preview_id)
        preview = dict(preview_row.preview_json)
        operation.state = "running"
        operation.started_at = operation.started_at or utcnow()

    preflight = await adapter.preflight(preview)
    with tenant_session(context) as session:
        _audit(session, "launch_preflight_passed", preflight, operation_id)

    ids: dict[str, str] = {}
    with tenant_session(context) as session:
        completed = session.scalars(select(LaunchStepRecord).where(
            LaunchStepRecord.operation_id == operation_id,
            LaunchStepRecord.state == "completed",
        )).all()
        for row in completed:
            key = {
                "campaign_created": "campaign_id", "adset_created": "adset_id",
                "image_uploaded": "image_hash", "creative_created": "creative_id", "ad_created": "ad_id",
            }.get(row.step_key)
            if key:
                ids[key] = row.remote_object_id

    for step in REMOTE_STEPS:
        if {
            "campaign_created": "campaign_id", "adset_created": "adset_id", "image_uploaded": "image_hash",
            "creative_created": "creative_id", "ad_created": "ad_id",
        }[step] in ids:
            continue
        request = _step_request(step, preview, ids)
        request_hash = canonical_hash(request)
        with tenant_session(context) as session:
            operation = _get_operation(session, context, operation_id)
            row = session.scalar(select(LaunchStepRecord).where(
                LaunchStepRecord.operation_id == operation_id, LaunchStepRecord.step_key == step,
            ))
            if row is None:
                row = LaunchStepRecord(
                    id=new_id("lstep"), operation_id=operation_id, step_key=step, state="running",
                    attempt=1, request_hash=request_hash, request_json=request, started_at=utcnow(),
                )
                session.add(row)
            else:
                if row.request_hash != request_hash:
                    raise LaunchConflict(f"Saga step 请求漂移: {step}")
                row.state = "running"
                row.attempt += 1
                row.started_at = row.started_at or utcnow()
                row.error_code = ""
                row.error_detail = ""
            operation.current_step = step
            step_id = row.id

        marker = f"{operation.marker}|{step}"
        remote = await adapter.ensure_step(step, marker, request)
        with tenant_session(context) as session:
            row = session.get(LaunchStepRecord, step_id)
            operation = _get_operation(session, context, operation_id)
            row.state = "completed"
            row.remote_object_type = remote.kind
            row.remote_object_id = remote.id
            row.response_json = {**remote.snapshot, "recovered": remote.recovered}
            row.completed_at = utcnow()
            operation.current_step = step
            ids[{
                "campaign_created": "campaign_id", "adset_created": "adset_id", "image_uploaded": "image_hash",
                "creative_created": "creative_id", "ad_created": "ad_id",
            }[step]] = remote.id
            _audit(session, "launch_step_completed", {
                "step": step, "remote_object_type": remote.kind, "remote_object_id": remote.id,
                "recovered": remote.recovered,
            }, operation_id)

    verification = await adapter.verify_hierarchy(ids)
    with tenant_session(context) as session:
        operation = _get_operation(session, context, operation_id)
        preview_row = session.get(LaunchPreviewRecord, operation.preview_id)
        account = session.scalar(select(MetaAccountRecord).where(
            MetaAccountRecord.workspace_id == context.workspace_id,
            MetaAccountRecord.ad_account_id == preview_row.ad_account_id,
        ))
        existing = session.scalar(select(MetaAdBindingRecord).where(
            MetaAdBindingRecord.account_id == account.id, MetaAdBindingRecord.ad_id == ids["ad_id"],
        ))
        if existing is None:
            session.add(MetaAdBindingRecord(
                id=new_id("mbind"), account_id=account.id, ad_id=ids["ad_id"],
                asset_version_id=preview_row.asset_version_id,
                experiment_entry_id=preview_row.experiment_entry_id,
                launch_operation_id=operation_id,
                campaign_id=ids["campaign_id"], adset_id=ids["adset_id"],
                creative_id=ids["creative_id"], source="launch_saga",
            ))
        exp_binding = session.scalar(select(ExperimentAdBindingRecord).where(
            ExperimentAdBindingRecord.experiment_entry_id == preview_row.experiment_entry_id,
        ))
        if exp_binding is None:
            session.add(ExperimentAdBindingRecord(
                id=new_id("expbind"), workspace_id=context.workspace_id,
                experiment_entry_id=preview_row.experiment_entry_id,
                ad_account_id=preview_row.ad_account_id, ad_id=ids["ad_id"], source="launch_saga",
            ))
        operation.state = "completed"
        operation.current_step = "completed"
        operation.completed_at = utcnow()
        operation.error_code = ""
        operation.error_detail = ""
        _audit(session, "launch_completed", {"remote_ids": ids, "verification": verification}, operation_id)


def record_failure(
    context: TenantContext, operation_id: str, code: str, detail: str, terminal: bool
) -> None:
    with tenant_session(context) as session:
        try:
            operation = _get_operation(session, context, operation_id)
        except LaunchNotFound:
            return
        operation.state = "failed" if terminal else "retry_wait"
        operation.error_code = code[:100]
        operation.error_detail = detail[:2000]
        step = session.scalar(select(LaunchStepRecord).where(
            LaunchStepRecord.operation_id == operation_id,
            LaunchStepRecord.step_key == operation.current_step,
        ))
        if step and step.state != "completed":
            step.state = operation.state
            step.error_code = code[:100]
            step.error_detail = detail[:2000]
        _audit(session, "launch_failed" if terminal else "launch_retry_scheduled", {
            "code": code, "step": operation.current_step,
        }, operation_id)


def get_operation(context: TenantContext, operation_id: str) -> dict:
    with tenant_session(context) as session:
        operation = _get_operation(session, context, operation_id)
        steps = session.scalars(select(LaunchStepRecord).where(
            LaunchStepRecord.operation_id == operation_id
        ).order_by(LaunchStepRecord.started_at)).all()
        return {
            "id": operation.id, "state": operation.state, "current_step": operation.current_step,
            "marker": operation.marker, "error_code": operation.error_code,
            "error_detail": operation.error_detail, "created_at": operation.created_at,
            "started_at": operation.started_at, "completed_at": operation.completed_at,
            "steps": [{
                "step": row.step_key, "state": row.state, "attempt": row.attempt,
                "request_hash": row.request_hash, "remote_object_type": row.remote_object_type,
                "remote_object_id": row.remote_object_id, "error_code": row.error_code,
            } for row in steps],
        }


def _preview_dict(row: LaunchPreviewRecord) -> dict:
    return {
        "id": row.id, "preview_hash": row.preview_hash, "preview": row.preview_json,
        "expires_at": row.expires_at, "created_at": row.created_at,
    }
