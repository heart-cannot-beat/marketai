"""Immutable experiment lifecycle and judgement snapshot persistence."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import config
from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import (
    AdDailyMetricRecord,
    AssetRecord,
    AssetVersionRecord,
    ContextualVerdictProjectionRecord,
    DecisionConfirmationRecord,
    DecisionProposalRecord,
    DecisionProposalStateEventRecord,
    DimensionConceptKeyRecord,
    ExperimentAdBindingRecord,
    ExperimentDecisionRecord,
    ExperimentEligibilityEventRecord,
    ExperimentEligibilityProjectionRecord,
    ExperimentEntryRecord,
    ExperimentNextRoundRecord,
    ExperimentPatternCandidateRecord,
    ExperimentRecord,
    ExperimentStateEventRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionConceptRecord,
    ExperimentVersionRecord,
    ExperimentVersionResolutionRecord,
    GenerationRunRecord,
    JudgementRecord,
    LibraryVersionRecord,
    MarketAssetBindingRecord,
    MetaAccountRecord,
    ProductRecord,
    QualityReviewRecord,
    TruthPackageRecord,
    new_id,
    utcnow,
)
from ..generation.models import (
    AssetQCAttemptRecord,
    AssetQCCheckResultRecord,
    RunItemRecord,
)
from ..platform.audit import append_event
from ..platform.canonical import _canonical_json, _sha256, canonical_hash
from .eligibility import (
    append_eligibility_event,
    ensure_projection,
    is_effectively_eligible,
)
from .judge import ALGORITHM_VERSION, judge
from .timeline import append_event as append_timeline_event
from .outbox import append_notification
from ..platform.tenancy import TenantContext


logger = logging.getLogger(__name__)


#: A message that starts with a code (``"concept_unresolved:k1"``) carries it.
_MESSAGE_CODE = re.compile(r"([a-z][a-z0-9_]*)(?::|$)")


class ExperimentError(RuntimeError):
    """``code`` is the stable code the interface renders (#253): an explicit
    ``code=``, else the code a message starts with, else the class default."""

    default_code = "experiment_invalid"

    def __init__(self, message: str = "", *, code: str | None = None, params: dict | None = None) -> None:
        super().__init__(message)
        prefix = _MESSAGE_CODE.match(message)
        self.code = code or (prefix.group(1) if prefix else self.default_code)
        self.params = params or {}


class ExperimentConflict(ExperimentError):
    default_code = "experiment_conflict"


class ExperimentNotFound(ExperimentError):
    default_code = "experiment_not_found"


def _get_experiment(session, context: TenantContext, experiment_id: str) -> ExperimentRecord:
    experiment = session.get(ExperimentRecord, experiment_id)
    if experiment is None or experiment.workspace_id != context.workspace_id:
        raise ExperimentNotFound("实验不存在")
    return experiment


def _live_entry(session, experiment_id: str, entry_id: str) -> ExperimentEntryRecord | None:
    """The experiment's entry unless it was removed: a removed draft entry
    keeps its row (#284), and every read leaves it out."""
    entry = session.get(ExperimentEntryRecord, entry_id)
    if entry is None or entry.experiment_id != experiment_id or entry.removed_at is not None:
        return None
    return entry


def create_experiment(
    context: TenantContext, *, product_id: str, name: str, config_snapshot: dict, metric_contract_hash: str,
    entries: list[dict],
) -> str:
    clean_name = name.strip()
    if not clean_name:
        raise ExperimentError("实验名称不能为空", code="experiment_name_required")
    if len(entries) < 2:
        raise ExperimentError("实验至少需要 2 个 Entry；单条 PAUSED canary 属于 Launch 验证，不是实验", code="experiment_entries_too_few")
    if len(metric_contract_hash) != 64:
        raise ExperimentError("metric_contract_hash 必须是 64 位 SHA-256", code="experiment_metric_contract_invalid")
    metric = str(config_snapshot.get("primary_metric") or "").lower()
    if metric not in {"ctr", "cpa", "roas"}:
        raise ExperimentError("primary_metric 只能是 ctr/cpa/roas", code="experiment_metric_invalid")
    try:
        if Decimal(str(config_snapshot.get(f"{metric}_threshold"))) <= 0:
            raise ExperimentError(f"{metric}_threshold 必须大于 0", code="experiment_threshold_invalid", params={"metric": metric})
        if not 0 < Decimal(str(config_snapshot.get("alpha", "0.05"))) < 1:
            raise ExperimentError("alpha 必须在 0 和 1 之间", code="experiment_alpha_invalid")
        if int(config_snapshot.get("bootstrap_samples", 2000)) < 200:
            raise ExperimentError("bootstrap_samples 至少为 200", code="experiment_bootstrap_samples_invalid")
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ExperimentError("实验统计配置无效", code="experiment_statistics_invalid") from exc
    with tenant_session(context) as session:
        product = session.get(ProductRecord, product_id)
        if product is None or product.workspace_id != context.workspace_id:
            raise ExperimentError("产品不存在或不属于当前 workspace", code="product_not_found")
        seen_versions: set[str] = set()
        resolved: list[tuple[AssetVersionRecord, AssetRecord, dict]] = []
        for item in entries:
            version_id = str(item["asset_version_id"])
            if version_id in seen_versions:
                raise ExperimentError("同一个 AssetVersion 不能在实验中重复", code="experiment_asset_duplicate")
            seen_versions.add(version_id)
            version = session.get(AssetVersionRecord, version_id)
            asset = session.get(AssetRecord, version.asset_id) if version else None
            if (
                version is None
                or version.workspace_id != context.workspace_id
                or asset is None
                or asset.workspace_id != context.workspace_id
                or asset.product_id != product_id
            ):
                raise ExperimentError(f"AssetVersion 不存在或不属于该产品: {version_id}", code="asset_version_not_found")
            resolved.append((version, asset, item))

        experiment = ExperimentRecord(
            id=new_id("exp"), workspace_id=context.workspace_id, product_id=product_id,
            name=clean_name, state="draft", revision=1, config_json=config_snapshot,
            metric_contract_hash=metric_contract_hash,
        )
        session.add(experiment)
        session.flush()
        for position, (version, _asset, item) in enumerate(resolved):
            session.add(ExperimentEntryRecord(
                id=new_id("entry"), workspace_id=context.workspace_id,
                experiment_id=experiment.id, asset_version_id=version.id,
                marketing_vars_json=dict(item.get("marketing_vars") or {}), position=position,
            ))
        session.add(ExperimentStateEventRecord(
            id=new_id("expev"), workspace_id=context.workspace_id,
            experiment_id=experiment.id, from_state=None, to_state="draft",
            revision=1,
            actor_id=context.initiator_id or context.executor,
            reason="created",
        ))
        append_event(
            session,
            event_type="experiment.created",
            resource_type="experiment",
            resource_id=experiment.id,
            details={"product_id": product_id, "entry_count": len(entries)},
        )
        return experiment.id


def freeze_experiment(
    context: TenantContext, experiment_id: str, expected_revision: int
) -> dict:
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        if experiment.state != "draft":
            if experiment.state == "frozen":
                return _experiment_dict(session, experiment)
            raise ExperimentConflict(f"只有 draft 可冻结，当前为 {experiment.state}", code="experiment_not_draft")
        if experiment.revision != expected_revision:
            raise ExperimentConflict(f"revision 冲突：期望 {expected_revision}，当前 {experiment.revision}", code="revision_conflict")
        entries = session.scalars(
            select(ExperimentEntryRecord).where(ExperimentEntryRecord.experiment_id == experiment_id,
                                                ExperimentEntryRecord.removed_at.is_(None))
            .order_by(ExperimentEntryRecord.position)
        ).all()
        frozen_entries = []
        for entry in entries:
            version = session.get(AssetVersionRecord, entry.asset_version_id)
            asset = session.get(AssetRecord, version.asset_id)
            frozen_entries.append({
                "id": entry.id,
                "position": entry.position,
                "asset_version": {
                    "id": version.id, "asset_id": version.asset_id, "content_sha256": version.content_sha256,
                    "object_key": version.object_key, "kind": version.kind, "aspect_ratio": version.aspect_ratio,
                },
                "source": {"combo_id": asset.combo_id, "combo_code": asset.combo_code, "tags": asset.tags_json},
                "marketing_vars": entry.marketing_vars_json,
            })
        snapshot = {
            "schema_version": 1,
            "experiment": {
                "id": experiment.id, "product_id": experiment.product_id, "name": experiment.name,
                "config": experiment.config_json, "metric_contract_hash": experiment.metric_contract_hash,
            },
            "entries": frozen_entries,
        }
        digest = canonical_hash(snapshot)
        now = utcnow()
        result = session.execute(
            update(ExperimentRecord)
            .where(ExperimentRecord.id == experiment_id, ExperimentRecord.state == "draft",
                   ExperimentRecord.revision == expected_revision)
            .values(state="frozen", revision=expected_revision + 1, frozen_snapshot_json=snapshot,
                    frozen_snapshot_hash=digest, frozen_at=now)
        )
        if result.rowcount != 1:
            raise ExperimentConflict("实验被并发修改，请刷新后重试", code="revision_conflict")
        session.add(ExperimentStateEventRecord(
            id=new_id("expev"), workspace_id=context.workspace_id,
            experiment_id=experiment.id, from_state="draft", to_state="frozen",
            revision=expected_revision + 1,
            actor_id=context.initiator_id or context.executor,
            reason="snapshot_frozen",
        ))
        append_event(
            session,
            event_type="experiment.frozen",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"revision": expected_revision + 1, "snapshot_hash": digest},
        )
        session.flush()
        session.refresh(experiment)
        return _experiment_dict(session, experiment)


def bind_entry(context: TenantContext, experiment_id: str, entry_id: str, ad_account_id: str, ad_id: str,
               source: str = "manual_read_only") -> dict:
    if not ad_account_id.startswith("act_"):
        ad_account_id = f"act_{ad_account_id}"
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        entry = _live_entry(session, experiment_id, entry_id)
        if entry is None:
            raise ExperimentNotFound("实验或 Entry 不存在", code="experiment_not_found")
        if experiment.state not in {"frozen", "running"}:
            raise ExperimentConflict("只有 frozen/running 实验可以绑定只读 ad_id", code="experiment_not_bindable")
        existing = session.scalar(select(ExperimentAdBindingRecord).where(
            ExperimentAdBindingRecord.experiment_entry_id == entry_id
        ))
        if existing:
            if existing.ad_account_id != ad_account_id or existing.ad_id != ad_id:
                raise ExperimentConflict("Entry 已绑定其他 ad_id；绑定不可覆盖", code="experiment_entry_already_bound")
            return {"id": existing.id, "entry_id": entry_id, "ad_account_id": ad_account_id, "ad_id": ad_id}
        binding = ExperimentAdBindingRecord(
            id=new_id("expbind"), workspace_id=context.workspace_id,
            experiment_entry_id=entry_id, ad_account_id=ad_account_id,
            ad_id=ad_id, source=source,
        )
        session.add(binding)
        append_event(
            session,
            event_type="experiment.entry_bound",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"entry_id": entry_id, "ad_account_id": ad_account_id, "ad_id": ad_id},
        )
        return {"id": binding.id, "entry_id": entry_id, "ad_account_id": ad_account_id, "ad_id": ad_id}


def run_judgement(context: TenantContext, experiment_id: str, as_of: date | None = None) -> dict:
    now = utcnow()
    cutoff = as_of or (now.date() - timedelta(days=1))
    if cutoff >= now.date():
        raise ExperimentError("判定截止日必须早于今天；当天数据视为 partial，不能终判", code="experiment_cutoff_invalid")
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        if experiment.state not in {"frozen", "running"}:
            raise ExperimentConflict("实验必须先冻结", code="experiment_not_frozen")
        entries = session.scalars(select(ExperimentEntryRecord).where(
            ExperimentEntryRecord.experiment_id == experiment_id, ExperimentEntryRecord.removed_at.is_(None)
        ).order_by(ExperimentEntryRecord.position)).all()
        snapshot_entries = []
        for entry in entries:
            binding = session.scalar(select(ExperimentAdBindingRecord).where(
                ExperimentAdBindingRecord.experiment_entry_id == entry.id
            ))
            reasons: list[str] = []
            rows = []
            if binding is None:
                reasons.append("missing_ad_binding")
            else:
                account = session.scalar(select(MetaAccountRecord).where(
                    MetaAccountRecord.workspace_id == experiment.workspace_id,
                    MetaAccountRecord.ad_account_id == binding.ad_account_id,
                ))
                if account is None:
                    reasons.append("unknown_meta_account")
                else:
                    metrics = session.scalars(select(AdDailyMetricRecord).where(
                        AdDailyMetricRecord.account_id == account.id,
                        AdDailyMetricRecord.ad_id == binding.ad_id,
                        AdDailyMetricRecord.contract_hash == experiment.metric_contract_hash,
                        AdDailyMetricRecord.metric_date <= cutoff,
                    ).order_by(AdDailyMetricRecord.metric_date)).all()
                    rows = [{
                        "date": row.metric_date.isoformat(), "spend": str(row.spend),
                        "impressions": row.impressions, "clicks": row.clicks,
                        "purchases": str(row.purchases), "purchase_value": str(row.purchase_value),
                    } for row in metrics]
                    if experiment.config_json.get("require_fresh_data", True):
                        if account.last_successful_sync_at is None:
                            reasons.append("meta_sync_never_completed")
                        else:
                            last = account.last_successful_sync_at
                            if last.tzinfo is None:
                                last = last.replace(tzinfo=timezone.utc)
                            if (now - last).total_seconds() > config.META_FRESHNESS_SLO_HOURS * 3600:
                                reasons.append("meta_data_stale")
            snapshot_entries.append({
                "id": entry.id, "position": entry.position,
                "ad_id": binding.ad_id if binding else None,
                "data_quality_reasons": reasons, "daily": rows,
            })
        input_snapshot = {
            "schema_version": 1,
            "algorithm_version": ALGORITHM_VERSION,
            "experiment": {
                "id": experiment.id, "frozen_snapshot_hash": experiment.frozen_snapshot_hash,
                "metric_contract_hash": experiment.metric_contract_hash, "config": experiment.config_json,
            },
            "as_of": cutoff.isoformat(),
            "entries": snapshot_entries,
        }
        input_snapshot["snapshot_hash"] = canonical_hash(input_snapshot)
        result = judge(input_snapshot)
        existing = session.scalar(select(JudgementRecord).where(
            JudgementRecord.experiment_id == experiment_id,
            JudgementRecord.snapshot_hash == input_snapshot["snapshot_hash"],
            JudgementRecord.algorithm_version == ALGORITHM_VERSION,
        ))
        if existing:
            return _judgement_dict(existing)
        judgement = JudgementRecord(
            id=new_id("judge"), workspace_id=context.workspace_id,
            experiment_id=experiment_id, algorithm_version=ALGORITHM_VERSION,
            as_of=cutoff, snapshot_hash=input_snapshot["snapshot_hash"], input_snapshot_json=input_snapshot,
            result_json=result,
        )
        session.add(judgement)
        session.flush()
        for item in result["entries"]:
            session.add(ExperimentDecisionRecord(
                id=new_id("decision"), workspace_id=context.workspace_id,
                experiment_id=experiment_id, judgement_id=judgement.id,
                experiment_entry_id=item["entry_id"], verdict=item["verdict"], metric=item["metric"],
                estimate=Decimal(item["estimate"]) if item["estimate"] is not None else None,
                interval_low=Decimal(item["interval_low"]) if item["interval_low"] is not None else None,
                interval_high=Decimal(item["interval_high"]) if item["interval_high"] is not None else None,
                reason_codes_json=item["reason_codes"],
            ))
        append_event(
            session,
            event_type="experiment.judged",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"judgement_id": judgement.id, "snapshot_hash": input_snapshot["snapshot_hash"]},
        )
        return _judgement_dict(judgement)


def close_experiment(
    context: TenantContext, experiment_id: str, expected_revision: int
) -> dict:
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        if experiment.state == "closed":
            return _experiment_dict(session, experiment)
        if experiment.state not in {"frozen", "running"} or experiment.revision != expected_revision:
            raise ExperimentConflict("状态或 revision 冲突", code="revision_conflict")
        latest = session.scalar(select(JudgementRecord).where(
            JudgementRecord.experiment_id == experiment_id
        ).order_by(JudgementRecord.created_at.desc()).limit(1))
        if latest is None:
            raise ExperimentConflict("至少追加一条 Judgement 后才能关闭实验", code="no_judgements")
        now = utcnow()
        from_state = experiment.state
        result = session.execute(update(ExperimentRecord).where(
            ExperimentRecord.id == experiment_id,
            ExperimentRecord.state.in_(("frozen", "running")),
            ExperimentRecord.revision == expected_revision,
        ).values(state="closed", revision=expected_revision + 1, closed_at=now))
        if result.rowcount != 1:
            raise ExperimentConflict("实验被并发修改", code="revision_conflict")
        session.add(ExperimentStateEventRecord(
            id=new_id("expev"), workspace_id=context.workspace_id,
            experiment_id=experiment_id, from_state=from_state, to_state="closed",
            revision=expected_revision + 1,
            actor_id=context.initiator_id or context.executor,
            reason=f"closed_with:{latest.id}",
        ))
        append_event(
            session,
            event_type="experiment.closed",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"revision": expected_revision + 1, "judgement_id": latest.id},
        )
        session.flush()
        session.refresh(experiment)
        return _experiment_dict(session, experiment)


def list_experiments(
    context: TenantContext,
    *,
    cursor: str | None = None,
    limit: int = 20,
    state: str | None = None,
    schema_version: int | None = None,
) -> dict:
    with tenant_session(context) as session:
        q = select(ExperimentRecord).where(
            ExperimentRecord.workspace_id == context.workspace_id,
        ).order_by(ExperimentRecord.created_at.desc(), ExperimentRecord.id.desc())

        if state:
            q = q.where(ExperimentRecord.state == state)
        if schema_version is not None:
            q = q.where(ExperimentRecord.record_schema_version == schema_version)
        if cursor:
            q = q.where(ExperimentRecord.id < cursor)

        q = q.limit(limit + 1)
        rows = session.scalars(q).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        result: dict[str, Any] = {
            "items": [{
                "id": e.id,
                "name": e.name,
                "state": e.state,
                "revision": e.revision,
                "record_schema_version": e.record_schema_version,
                "created_at": e.created_at,
                "frozen_at": e.frozen_at,
                "closed_at": e.closed_at,
            } for e in items],
        }
        if has_more:
            result["next_cursor"] = items[-1].id
        return result


def get_timeline(
    context: TenantContext,
    experiment_id: str,
    *,
    cursor: int | None = None,
    limit: int = 50,
) -> dict:
    from .timeline import ensure_head
    from ..adapters.persistence.models import ExperimentTimelineEventRecord

    with tenant_session(context) as session:
        _get_experiment(session, context, experiment_id)
        head = ensure_head(
            session, workspace_id=context.workspace_id, experiment_id=experiment_id,
        )

        q = (
            select(ExperimentTimelineEventRecord)
            .where(ExperimentTimelineEventRecord.experiment_id == experiment_id)
            .order_by(ExperimentTimelineEventRecord.timeline_sequence.desc())
        )
        if cursor is not None:
            q = q.where(ExperimentTimelineEventRecord.timeline_sequence < cursor)
        q = q.limit(limit + 1)

        rows = session.scalars(q).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        result: dict[str, Any] = {
            "head_sequence": head.head_sequence,
            "items": [{
                "id": e.id,
                "timeline_sequence": e.timeline_sequence,
                "event_type": e.event_type,
                "source_resource_type": e.source_resource_type,
                "source_resource_id": e.source_resource_id,
                "actor_id": e.actor_id,
                "summary": e.summary_json,
                "observed_at": e.observed_at,
            } for e in items],
        }
        if has_more:
            result["next_cursor"] = items[-1].timeline_sequence
        return result


def get_experiment(context: TenantContext, experiment_id: str) -> dict:
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        if experiment.record_schema_version == 2:
            return _experiment_dict_v2(session, experiment)
        return _experiment_dict(session, experiment)


def _experiment_dict(session, experiment: ExperimentRecord) -> dict:
    entries = session.scalars(select(ExperimentEntryRecord).where(
        ExperimentEntryRecord.experiment_id == experiment.id, ExperimentEntryRecord.removed_at.is_(None)
    ).order_by(ExperimentEntryRecord.position)).all()
    bindings = {row.experiment_entry_id: row for row in session.scalars(
        select(ExperimentAdBindingRecord).join(ExperimentEntryRecord).where(
            ExperimentEntryRecord.experiment_id == experiment.id, ExperimentEntryRecord.removed_at.is_(None)
        )
    ).all()}
    judgements = session.scalars(select(JudgementRecord).where(
        JudgementRecord.experiment_id == experiment.id
    ).order_by(JudgementRecord.created_at)).all()
    return {
        "id": experiment.id, "product_id": experiment.product_id, "name": experiment.name,
        "state": experiment.state, "revision": experiment.revision, "config": experiment.config_json,
        "metric_contract_hash": experiment.metric_contract_hash,
        "frozen_snapshot_hash": experiment.frozen_snapshot_hash,
        "created_at": experiment.created_at, "frozen_at": experiment.frozen_at, "closed_at": experiment.closed_at,
        "entries": [{
            "id": row.id, "position": row.position, "asset_version_id": row.asset_version_id,
            "marketing_vars": row.marketing_vars_json,
            "binding": None if row.id not in bindings else {
                "ad_account_id": bindings[row.id].ad_account_id, "ad_id": bindings[row.id].ad_id,
            },
        } for row in entries],
        "judgements": [_judgement_dict(row) for row in judgements],
    }


def _judgement_dict(row: JudgementRecord) -> dict:
    return {
        "id": row.id, "experiment_id": row.experiment_id, "algorithm_version": row.algorithm_version,
        "as_of": row.as_of, "snapshot_hash": row.snapshot_hash, "result": row.result_json,
        "created_at": row.created_at,
    }


# ---------------------------------------------------------------------------
# Schema-2 service functions (Issue #22)
# ---------------------------------------------------------------------------

VALID_MARKETS = {"US", "GB", "DE", "FR", "JP", "CA", "AU", "BR", "IN", "MX", "KR", "IT", "ES", "NL", "SE", "NO", "DK", "FI", "PL", "TR", "SG", "MY", "PH", "TH", "VN", "ID", "TW", "HK"}
VALID_PLACEMENTS = {"feed", "story", "reel", "search", "marketplace"}
VALID_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "BRL", "INR", "MXN", "KRW", "SGD", "MYR", "PHP", "THB", "VND", "IDR", "TWD", "HKD"}


class SchemaVersionMismatch(ExperimentError):
    pass


def _require_schema2(experiment: ExperimentRecord) -> None:
    if experiment.record_schema_version != 2:
        raise SchemaVersionMismatch("schema_version_mismatch")


def _require_state(experiment: ExperimentRecord, *states: str) -> None:
    if experiment.state not in states:
        raise ExperimentConflict(
            f"Expected state {'/'.join(states)}, got {experiment.state}",
            code="experiment_state_conflict",
        )


def _check_revision(experiment: ExperimentRecord, expected: int) -> None:
    if experiment.revision != expected:
        raise ExperimentConflict("revision_conflict")


def _take_draft_revision(session, experiment_id: str, expected: int, **values: Any) -> int:
    """Move a draft from ``expected`` to the next revision, or refuse (#340).

    ``_check_revision`` read a revision that a concurrent command may since
    have committed past. Runs before the command reads or writes anything
    else: on PostgreSQL the UPDATE holds the Experiment row, so a concurrent
    draft command waits here and then finds its revision gone."""
    result = session.execute(
        update(ExperimentRecord)
        .where(ExperimentRecord.id == experiment_id, ExperimentRecord.state == "draft",
               ExperimentRecord.revision == expected)
        .values(revision=expected + 1, **values)
    )
    if result.rowcount != 1:
        raise ExperimentConflict("实验被并发修改，请刷新后重试", code="revision_conflict")
    return expected + 1


def _refuse_duplicate_asset(session, experiment_id: str, asset_version_id: str,
                            *, entry_id: str | None = None) -> None:
    """One live entry per AssetVersion; a removed entry's version is free again."""
    query = select(ExperimentEntryRecord.id).where(
        ExperimentEntryRecord.experiment_id == experiment_id,
        ExperimentEntryRecord.asset_version_id == asset_version_id,
        ExperimentEntryRecord.removed_at.is_(None),
    )
    if entry_id is not None:  # an entry keeping its own version is no duplicate
        query = query.where(ExperimentEntryRecord.id != entry_id)
    if session.scalar(query.limit(1)) is not None:
        raise ExperimentConflict("同一个 AssetVersion 不能在实验中重复", code="experiment_asset_duplicate")


def _changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """The names, never the values, an audit event records."""
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


def _validate_draft_context(ctx: dict) -> dict:
    """Validate and canonicalize draft context fields."""
    errors = []
    market = str(ctx.get("market", "")).strip().upper()
    if len(market) != 2:
        errors.append("market must be ISO 3166-1 alpha-2")

    currency = str(ctx.get("currency", "")).strip().upper()
    if len(currency) != 3:
        errors.append("currency must be ISO 4217 3-letter code")

    placement = str(ctx.get("placement", "")).strip().lower()
    if placement not in VALID_PLACEMENTS:
        errors.append(f"placement must be one of {sorted(VALID_PLACEMENTS)}")

    budget = ctx.get("budget", {})
    try:
        daily = Decimal(str(budget.get("daily_amount", "0")))
        total = Decimal(str(budget.get("total_amount", "0")))
        if daily <= 0:
            errors.append("budget.daily_amount must be > 0")
        if total < daily:
            errors.append("budget.total_amount must be >= daily_amount")
    except (InvalidOperation, ValueError, TypeError):
        errors.append("budget amounts must be valid decimal strings")

    schedule = ctx.get("schedule", {})
    try:
        from datetime import datetime as dt
        start_at = dt.fromisoformat(str(schedule.get("start_at", "")))
        end_at = dt.fromisoformat(str(schedule.get("end_at", "")))
        if end_at <= start_at:
            errors.append("schedule.end_at must be after start_at")
    except (ValueError, TypeError):
        errors.append("schedule dates must be valid ISO 8601")

    attribution = ctx.get("attribution", {})
    click_days = int(attribution.get("click_window_days", 7))
    view_days = int(attribution.get("view_window_days", 1))
    # Issue #27's Meta measurement contract is deliberately fixed.  Accepting
    # arbitrary windows here would let a schema-2 Experiment freeze evidence
    # that the downstream ingestion/judgement path does not implement.
    if click_days != 7 or view_days != 1:
        errors.append("attribution must use 7-day click and 1-day view")

    stat_plan = ctx.get("statistical_plan", {})
    metric = str(stat_plan.get("primary_metric", "")).lower()
    if metric not in {"ctr", "cpa", "roas"}:
        errors.append("statistical_plan.primary_metric must be ctr/cpa/roas")
    try:
        alpha = Decimal(str(stat_plan.get("alpha", "0.05")))
        if not (0 < alpha < 1):
            errors.append("statistical_plan.alpha must be in (0, 1)")
    except (InvalidOperation, ValueError, TypeError):
        errors.append("statistical_plan.alpha must be a valid decimal")
    bootstrap = int(stat_plan.get("bootstrap_samples", 2000))
    if bootstrap < 200:
        errors.append("statistical_plan.bootstrap_samples must be >= 200")

    if errors:
        raise ExperimentError("; ".join(errors), code="experiment_context_invalid")

    validated = dict(ctx)
    validated["schema_version"] = 2
    validated["market"] = market
    validated["currency"] = currency
    validated["placement"] = placement
    return validated


def create_experiment_v2(
    context: TenantContext,
    *,
    product_id: str,
    name: str,
    draft_context: dict,
) -> dict:
    clean_name = name.strip()
    if not clean_name:
        raise ExperimentError("实验名称不能为空", code="experiment_name_required")

    validated_ctx = _validate_draft_context(draft_context)

    product_version_id = validated_ctx.get("product_version_id")
    offer_version_id = validated_ctx.get("offer_version_id")
    if not product_version_id or not offer_version_id:
        raise ExperimentError("product_version_id and offer_version_id are required", code="experiment_versions_required")

    with tenant_session(context) as session:
        product = session.get(ProductRecord, product_id)
        if product is None or product.workspace_id != context.workspace_id:
            raise ExperimentError("产品不存在或不属于当前 workspace", code="product_not_found")

        experiment = ExperimentRecord(
            id=new_id("exp"),
            workspace_id=context.workspace_id,
            product_id=product_id,
            name=clean_name,
            state="draft",
            revision=1,
            config_json=validated_ctx,
            metric_contract_hash="",
            record_schema_version=2,
        )
        session.add(experiment)
        session.flush()
        session.add(ExperimentStateEventRecord(
            id=new_id("expev"),
            workspace_id=context.workspace_id,
            experiment_id=experiment.id,
            from_state=None,
            to_state="draft",
            revision=1,
            actor_id=context.initiator_id or context.executor,
            reason="created",
        ))
        append_event(
            session,
            event_type="experiment.created",
            resource_type="experiment",
            resource_id=experiment.id,
            details={"record_schema_version": 2},
        )
        return {
            "id": experiment.id,
            "state": "draft",
            "revision": 1,
            "record_schema_version": 2,
            "name": clean_name,
            "draft_context": validated_ctx,
        }


def add_entry_v2(
    context: TenantContext,
    experiment_id: str,
    *,
    asset_version_id: str,
    marketing_vars: dict | None = None,
    expected_revision: int,
) -> dict:
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)
        _require_state(experiment, "draft")
        _check_revision(experiment, expected_revision)
        new_rev = _take_draft_revision(session, experiment_id, expected_revision)

        version = session.get(AssetVersionRecord, asset_version_id)
        if version is None or version.workspace_id != context.workspace_id:
            raise ExperimentError(f"AssetVersion not found: {asset_version_id}", code="asset_version_not_found")
        _refuse_duplicate_asset(session, experiment_id, asset_version_id)

        entry_count = session.scalar(
            select(func.count()).select_from(ExperimentEntryRecord).where(
                ExperimentEntryRecord.experiment_id == experiment_id,
                ExperimentEntryRecord.removed_at.is_(None),
            )
        )
        if entry_count >= 100:
            raise ExperimentConflict("candidate_limit_exceeded")

        # Positions are unique among live entries only, so a removed entry's
        # position is free again.
        max_pos = session.scalar(
            select(func.max(ExperimentEntryRecord.position)).where(
                ExperimentEntryRecord.experiment_id == experiment_id,
                ExperimentEntryRecord.removed_at.is_(None),
            )
        )
        next_pos = 0 if max_pos is None else max_pos + 1

        entry = ExperimentEntryRecord(
            id=new_id("entry"),
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            asset_version_id=asset_version_id,
            marketing_vars_json=marketing_vars or {},
            position=next_pos,
        )
        session.add(entry)
        append_event(
            session,
            event_type="experiment.entry_added",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"entry_id": entry.id, "asset_version_id": asset_version_id, "revision": new_rev},
        )
        session.flush()
        return {
            "id": entry.id,
            "experiment_id": experiment_id,
            "asset_version_id": asset_version_id,
            "position": next_pos,
            "revision": new_rev,
        }


def update_entry_v2(
    context: TenantContext,
    experiment_id: str,
    entry_id: str,
    *,
    asset_version_id: str | None = None,
    marketing_vars: dict | None = None,
    expected_revision: int,
) -> dict:
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)
        _require_state(experiment, "draft")
        _check_revision(experiment, expected_revision)
        new_rev = _take_draft_revision(session, experiment_id, expected_revision)

        entry = _live_entry(session, experiment_id, entry_id)
        if entry is None:
            raise ExperimentNotFound("Entry not found", code="experiment_entry_not_found")
        before = {"asset_version_id": entry.asset_version_id, "marketing_vars": entry.marketing_vars_json}

        if asset_version_id is not None:
            version = session.get(AssetVersionRecord, asset_version_id)
            if version is None or version.workspace_id != context.workspace_id:
                raise ExperimentError(f"AssetVersion not found: {asset_version_id}", code="asset_version_not_found")
            _refuse_duplicate_asset(session, experiment_id, asset_version_id, entry_id=entry_id)
            entry.asset_version_id = asset_version_id

        if marketing_vars is not None:
            entry.marketing_vars_json = marketing_vars

        append_event(
            session,
            event_type="experiment.entry_updated",
            resource_type="experiment",
            resource_id=experiment_id,
            details={
                "entry_id": entry_id, "asset_version_id": entry.asset_version_id, "revision": new_rev,
                "changed_fields": _changed_fields(before, {"asset_version_id": entry.asset_version_id,
                                                           "marketing_vars": entry.marketing_vars_json}),
            },
        )
        session.flush()
        return {
            "id": entry.id,
            "experiment_id": experiment_id,
            "asset_version_id": entry.asset_version_id,
            "marketing_vars": entry.marketing_vars_json,
            "revision": new_rev,
        }


def remove_entry_v2(
    context: TenantContext,
    experiment_id: str,
    entry_id: str,
    *,
    expected_revision: int,
) -> dict:
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)
        _require_state(experiment, "draft")
        _check_revision(experiment, expected_revision)
        new_rev = _take_draft_revision(session, experiment_id, expected_revision)

        entry = _live_entry(session, experiment_id, entry_id)
        if entry is None:
            raise ExperimentNotFound("Entry not found", code="experiment_entry_not_found")

        # The domain database grants no DELETE: the row stays, marked removed.
        entry.removed_at = utcnow()
        append_event(
            session,
            event_type="experiment.entry_removed",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"entry_id": entry_id, "asset_version_id": entry.asset_version_id, "revision": new_rev},
        )
        session.flush()
        return {"removed": entry_id, "revision": new_rev}


def update_context_v2(
    context: TenantContext,
    experiment_id: str,
    *,
    draft_context: dict,
    expected_revision: int,
) -> dict:
    validated_ctx = _validate_draft_context(draft_context)

    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)
        _require_state(experiment, "draft")
        _check_revision(experiment, expected_revision)
        before = dict(experiment.config_json or {})
        new_rev = _take_draft_revision(session, experiment_id, expected_revision, config_json=validated_ctx)
        append_event(
            session,
            event_type="experiment.context_updated",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"revision": new_rev, "changed_fields": _changed_fields(before, validated_ctx)},
        )
        session.flush()
        return {"revision": new_rev, "draft_context": validated_ctx}


_EXECUTION_MODES = frozenset({"byok", "sandbox", "mock"})
#: The partition an external creative's candidate freezes into (real evidence).
_EXTERNAL_EXECUTION_MODE = "byok"
_RESOLUTION_ACTIONS = frozenset({
    "allow_controlled_duplicate",
    "exclude_source_candidate",
    "remap_before_freeze",
})


@dataclass(frozen=True)
class FrozenConceptInput:
    dimension_key: str
    stable_dimension_signature: str
    concept_key_id: str
    concept_key_scope: str
    concept_key_name: str
    value_json: Mapping[str, Any]
    value_hash: str
    evidence_json: Mapping[str, Any]
    duplicate_group_hash: str


@dataclass(frozen=True)
class FrozenCandidateInput:
    entry: ExperimentEntryRecord
    asset_version: AssetVersionRecord
    source_kind: str
    source_run_item_id: str | None
    source_evidence: Mapping[str, Any]
    marketing_vars_hash: str
    execution_mode: str
    concepts: tuple[FrozenConceptInput, ...]
    source_fingerprint: str = ""


@dataclass(frozen=True)
class FrozenSourceMarketSnapshot:
    """Explicit commercial-market identity; never inferred from an asset or budget."""

    locale: str
    market: str
    placement: str
    country: str
    currency: str


@dataclass(frozen=True)
class FrozenExperimentContext:
    product_version: LibraryVersionRecord
    offer_version: LibraryVersionRecord
    market: str
    placement: str
    landing_page_url: str
    currency: str
    daily_budget_amount: Decimal
    total_budget_cap: Decimal
    schedule_start_at: datetime
    schedule_end_at: datetime
    schedule_timezone: str
    attribution_click_window_days: int
    attribution_view_window_days: int
    audience: Mapping[str, Any]
    statistical_plan: Mapping[str, Any]
    source_market_snapshot: FrozenSourceMarketSnapshot | None


def _hash_json(value: Any) -> str:
    return _sha256(_canonical_json(value).encode("utf-8"))


def _duplicate_group_hash(dimension_key: str, concept_key_id: str) -> str:
    """Canonical identity of one same-concept candidate group.

    Keep this tiny helper as the single implementation used by detection,
    remapping and persisted concept rows.  The tuple is represented as a
    two-item canonical JSON array so separators cannot make two pairs
    ambiguous.
    """
    return _hash_json([dimension_key, concept_key_id])


def _candidate_execution_mode(
    session: Session,
    *,
    asset_version: AssetVersionRecord,
    asset: AssetRecord,
) -> tuple[str, dict[str, str]]:
    """Resolve the exact controlled execution mode without inventing one.

    New AssetVersions carry the mode directly.  Pre-column AssetVersions are
    still common in imported workspaces, so their immutable producing
    GenerationRun is the only accepted fallback.  There is deliberately no
    provider-name heuristic and no ``mock`` default here.
    """
    if asset_version.execution_mode in _EXECUTION_MODES:
        return asset_version.execution_mode, {
            "source": "asset_version",
            "source_id": asset_version.id,
        }

    if (
        asset_version.origin_kind == "upload"
        and asset_version.execution_mode is None
        and getattr(asset, "source", None) == "external"
    ):
        # An external creative (Issue #168) is the customer's own material,
        # never Controlled-Mock output: it sits in the real-evidence
        # partition, so it can share an experiment with BYOK images and
        # never with Mock or Sandbox ones (``execution_mode_mixed``).
        return _EXTERNAL_EXECUTION_MODE, {
            "source": "external_creative",
            "source_id": asset_version.id,
        }

    run = session.get(GenerationRunRecord, asset.run_id) if asset.run_id else None
    if (
        run is not None
        and run.workspace_id == asset_version.workspace_id
        and run.execution_mode in _EXECUTION_MODES
    ):
        return run.execution_mode, {
            "source": "generation_run",
            "source_id": run.id,
        }

    raise ExperimentConflict(f"execution_mode_missing:{asset_version.id}")


def _candidate_concepts(
    session: Session,
    *,
    context: TenantContext,
    asset_version: AssetVersionRecord,
) -> tuple[tuple[FrozenConceptInput, ...], str | None, Mapping[str, Any]]:
    """Load explicitly governed concept identities from the producing RunItem.

    A dimension label/value is presentation data, not concept identity.  Freeze
    therefore accepts only an explicit ``concepts`` projection whose keys
    already exist in the governed ``dimension_concept_keys`` registry.  Manual,
    upload and legacy assets without that projection fail closed instead of
    silently becoming concept-less candidates.

    An external creative (origin ``upload``, Issue #168) has no RunItem; its
    concepts come from the latest confirmation of its tags, which writes the
    same governed ``dck_`` keys a generated combination with those values
    carries. Unconfirmed (or confirmed with no tag) still fails closed.
    """
    if not asset_version.producing_run_item_id:
        if asset_version.origin_kind == "upload":
            return _external_candidate_concepts(session, context=context, asset_version=asset_version)
        raise ExperimentConflict(f"concept_identity_missing:{asset_version.id}")

    run_item = session.get(RunItemRecord, asset_version.producing_run_item_id)
    if run_item is None or run_item.workspace_id != context.workspace_id:
        raise ExperimentConflict(f"concept_source_missing:{asset_version.id}")

    identity = dict(run_item.combination_identity_json or {})
    explicit = identity.get("concepts")
    projected: list[dict[str, Any]] = []
    seen_dimensions: set[str] = set()

    if not isinstance(explicit, list) or not explicit:
        raise ExperimentConflict(f"concept_identity_missing:{asset_version.id}")
    for raw in explicit:
        if not isinstance(raw, dict):
            raise ExperimentError("RunItem concept entries must be objects")
        dimension_key = str(raw.get("dimension_key") or "").strip()
        concept_key_id = str(raw.get("concept_key_id") or "").strip()
        if not dimension_key or not concept_key_id:
            raise ExperimentConflict(f"concept_identity_missing:{asset_version.id}")
        key = session.get(DimensionConceptKeyRecord, concept_key_id)
        if key is None or (
            key.scope == "workspace" and key.workspace_id != context.workspace_id
        ) or (key.scope == "platform" and key.workspace_id is not None):
            raise ExperimentConflict(f"concept_key_not_governed:{concept_key_id}")
        if dimension_key in seen_dimensions:
            raise ExperimentConflict(f"duplicate_candidate_dimension:{dimension_key}")
        seen_dimensions.add(dimension_key)
        value = raw.get("value", {"name": key.name})
        projected.append({
            "dimension_key": dimension_key,
            "stable_dimension_signature": str(
                raw.get("stable_dimension_signature")
                or run_item.stable_dimension_signature
                or ""
            ).strip(),
            "concept_key_id": key.id,
            "concept_key_scope": key.scope,
            "concept_key_name": key.name,
            "value_json": value if isinstance(value, dict) else {"value": value},
        })

    if any(not row["stable_dimension_signature"] for row in projected):
        raise ExperimentConflict(f"concept_identity_missing:{asset_version.id}")

    concepts: list[FrozenConceptInput] = []
    for concept in sorted(projected, key=lambda row: row["dimension_key"]):
        value_hash = _hash_json(concept["value_json"])
        evidence = {
            "source_kind": "generation_run_item",
            "run_item_id": run_item.id,
            "combination_fingerprint": run_item.combination_fingerprint,
            "combination_identity_hash": _hash_json(identity),
        }
        concepts.append(FrozenConceptInput(
            dimension_key=concept["dimension_key"],
            stable_dimension_signature=concept["stable_dimension_signature"],
            concept_key_id=concept["concept_key_id"],
            concept_key_scope=concept["concept_key_scope"],
            concept_key_name=concept["concept_key_name"],
            value_json=concept["value_json"],
            value_hash=value_hash,
            evidence_json=evidence,
            duplicate_group_hash=_duplicate_group_hash(
                concept["dimension_key"], concept["concept_key_id"]
            ),
        ))
    source_evidence = {
        "run_item_id": run_item.id,
        "combination_fingerprint": run_item.combination_fingerprint,
        "stable_dimension_signature": run_item.stable_dimension_signature,
        "combination_identity_hash": _hash_json(identity),
    }
    return tuple(concepts), run_item.id, source_evidence


def _external_candidate_concepts(
    session: Session,
    *,
    context: TenantContext,
    asset_version: AssetVersionRecord,
) -> tuple[tuple[FrozenConceptInput, ...], str | None, Mapping[str, Any]]:
    """Concept identity of an external creative from its confirmed tags."""
    from ..external_creatives.identity import SOURCE_KIND, latest_confirmation

    confirmation = latest_confirmation(session, context, asset_version.id)
    if confirmation is None or not confirmation.concepts_json:
        raise ExperimentConflict(f"concept_identity_missing:{asset_version.id}")
    seen_dimensions: set[str] = set()
    concepts: list[FrozenConceptInput] = []
    for raw in sorted(confirmation.concepts_json, key=lambda row: str(row.get("dimension_key") or "")):
        dimension_key = str(raw.get("dimension_key") or "").strip()
        concept_key_id = str(raw.get("concept_key_id") or "").strip()
        signature = str(
            raw.get("stable_dimension_signature") or confirmation.stable_dimension_signature or ""
        ).strip()
        if not dimension_key or not concept_key_id or not signature:
            raise ExperimentConflict(f"concept_identity_missing:{asset_version.id}")
        key = session.get(DimensionConceptKeyRecord, concept_key_id)
        if key is None or (
            key.scope == "workspace" and key.workspace_id != context.workspace_id
        ) or (key.scope == "platform" and key.workspace_id is not None):
            raise ExperimentConflict(f"concept_key_not_governed:{concept_key_id}")
        if dimension_key in seen_dimensions:
            raise ExperimentConflict(f"duplicate_candidate_dimension:{dimension_key}")
        seen_dimensions.add(dimension_key)
        value = raw.get("value", {"name": key.name})
        value_json = value if isinstance(value, dict) else {"value": value}
        concepts.append(FrozenConceptInput(
            dimension_key=dimension_key,
            stable_dimension_signature=signature,
            concept_key_id=key.id,
            concept_key_scope=key.scope,
            concept_key_name=key.name,
            value_json=value_json,
            value_hash=_hash_json(value_json),
            evidence_json={
                "source_kind": SOURCE_KIND,
                "external_creative_id": confirmation.external_creative_id,
                "tag_confirmation_id": confirmation.id,
                "tag_confirmation_revision": confirmation.revision,
            },
            duplicate_group_hash=_duplicate_group_hash(dimension_key, key.id),
        ))
    source_evidence = {
        "source_kind": SOURCE_KIND,
        "external_creative_id": confirmation.external_creative_id,
        "tag_confirmation_id": confirmation.id,
        "tag_confirmation_revision": confirmation.revision,
        "stable_dimension_signature": confirmation.stable_dimension_signature,
        "concepts_hash": _hash_json(confirmation.concepts_json),
    }
    return tuple(concepts), None, source_evidence


def _source_fingerprint(source: FrozenCandidateInput) -> str:
    return _hash_json({
        "source_entry_id": source.entry.id,
        "asset_version_id": source.asset_version.id,
        "asset_version_content_sha256": source.asset_version.content_sha256,
        "execution_mode": source.execution_mode,
        "marketing_vars_hash": source.marketing_vars_hash,
        "concepts": [
            {
                "dimension_key": concept.dimension_key,
                "concept_key_id": concept.concept_key_id,
                "value_hash": concept.value_hash,
            }
            for concept in source.concepts
        ],
    })


def _load_candidate_sources(
    session: Session,
    context: TenantContext,
    experiment: ExperimentRecord,
) -> list[FrozenCandidateInput]:
    """Load and resolve draft candidate sources for an experiment.

    Shared seam used by both analysis and freeze. Pure read — no mutations.
    """
    entries = session.scalars(
        select(ExperimentEntryRecord)
        .where(ExperimentEntryRecord.experiment_id == experiment.id, ExperimentEntryRecord.removed_at.is_(None))
        .order_by(ExperimentEntryRecord.position)
    ).all()

    asset_versions = session.scalars(
        select(AssetVersionRecord).where(
            AssetVersionRecord.workspace_id == context.workspace_id,
            AssetVersionRecord.id.in_(sorted(entry.asset_version_id for entry in entries)),
        ).order_by(AssetVersionRecord.id)
    ).all()
    asset_versions_by_id = {row.id: row for row in asset_versions}

    sources: list[FrozenCandidateInput] = []
    for entry in entries:
        av = asset_versions_by_id.get(entry.asset_version_id)
        asset = session.get(AssetRecord, av.asset_id) if av is not None else None
        if (
            av is None
            or av.workspace_id != context.workspace_id
            or asset is None
            or asset.workspace_id != context.workspace_id
            or asset.product_id != experiment.product_id
        ):
            raise ExperimentError(f"AssetVersion missing: {entry.asset_version_id}", code="asset_version_not_found")

        mvars_json = _canonical_json(entry.marketing_vars_json)
        mvars_hash = _sha256(mvars_json.encode("utf-8"))

        execution_mode, mode_evidence = _candidate_execution_mode(
            session, asset_version=av, asset=asset,
        )
        concepts, source_run_item_id, concept_source_evidence = _candidate_concepts(
            session, context=context, asset_version=av,
        )
        source_kind = av.origin_kind
        if source_kind == "legacy":
            source_kind = "generation" if source_run_item_id or asset.run_id else "manual"
        elif source_kind == "localized":
            source_kind = "reuse"
        if source_kind not in {"generation", "upload", "reuse", "manual"}:
            raise ExperimentConflict(f"candidate_source_kind_invalid:{av.id}")
        sources.append(FrozenCandidateInput(
            entry=entry,
            asset_version=av,
            source_kind=source_kind,
            source_run_item_id=source_run_item_id,
            source_evidence={
                "asset_origin_kind": av.origin_kind,
                "execution_mode": mode_evidence,
                "concept_source": concept_source_evidence,
            },
            marketing_vars_hash=mvars_hash,
            execution_mode=execution_mode,
            concepts=concepts,
        ))
    return sources


def _detect_duplicate_groups(
    sources: list[FrozenCandidateInput],
) -> tuple[str, bool, list[dict[str, Any]]]:
    """Detect duplicate-concept groups from resolved candidates.

    Returns (candidate_set_hash, has_duplicates, groups).
    Groups are deterministically ordered by (dimension_key, concept_key_id).
    """
    fingerprinted = [
        replace(source, source_fingerprint=_source_fingerprint(source))
        for source in sources
    ]

    candidate_set_hash = _hash_json(
        sorted(source.source_fingerprint for source in fingerprinted)
    )

    all_groups: dict[str, dict[str, Any]] = {}
    for source in fingerprinted:
        for concept in source.concepts:
            group = all_groups.setdefault(concept.duplicate_group_hash, {
                "dimension_key": concept.dimension_key,
                "concept_key_id": concept.concept_key_id,
                "concept_key_name": concept.concept_key_name,
                "members": [],
            })
            group["members"].append(source)

    duplicate_groups = {
        group_hash: group
        for group_hash, group in all_groups.items()
        if len(group["members"]) > 1
    }

    groups_list: list[dict[str, Any]] = []
    for group_hash in sorted(
        duplicate_groups,
        key=lambda gh: (
            duplicate_groups[gh]["dimension_key"],
            duplicate_groups[gh]["concept_key_id"],
        ),
    ):
        group = duplicate_groups[group_hash]
        members_sorted = sorted(
            group["members"],
            key=lambda s: s.source_fingerprint,
        )
        member_count = len(members_sorted)
        allowed_actions = ["allow_controlled_duplicate"]
        if member_count == 2:
            allowed_actions.append("exclude_source_candidate")

        groups_list.append({
            "duplicate_group_hash": group_hash,
            "dimension_key": group["dimension_key"],
            "concept_key_id": group["concept_key_id"],
            "concept_key_name": group["concept_key_name"],
            "members": [
                {
                    "source_entry_id": m.entry.id,
                    "source_candidate_fingerprint": m.source_fingerprint,
                    "asset_version_id": m.asset_version.id,
                    "display_label": m.entry.id,
                }
                for m in members_sorted
            ],
            "allowed_actions": allowed_actions,
            "_sources": members_sorted,
        })

    return candidate_set_hash, bool(duplicate_groups), groups_list


def _compute_remap_targets(
    session: Session,
    context: TenantContext,
    groups: list[dict[str, Any]],
) -> None:
    """Mutate groups in place to add remap_targets and update allowed_actions."""
    for group in groups:
        if len(group["members"]) != 2:
            group["remap_targets"] = []
            continue
        current_key_id = group["concept_key_id"]
        candidates = session.scalars(
            select(DimensionConceptKeyRecord).where(
                DimensionConceptKeyRecord.id != current_key_id,
                (
                    (DimensionConceptKeyRecord.workspace_id == context.workspace_id)
                    | (DimensionConceptKeyRecord.workspace_id.is_(None))
                ),
            ).order_by(DimensionConceptKeyRecord.id)
        ).all()
        targets = [
            {"concept_key_id": k.id, "concept_key_name": k.name}
            for k in candidates
        ]
        group["remap_targets"] = targets
        if targets:
            group["allowed_actions"].append("remap_before_freeze")


def analyze_duplicates(
    context: TenantContext,
    experiment_id: str,
) -> dict:
    """Read-only duplicate-concept analysis for the pre-freeze UI."""
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)

        sources = _load_candidate_sources(session, context, experiment)
        candidate_set_hash, has_duplicates, groups = _detect_duplicate_groups(sources)
        _compute_remap_targets(session, context, groups)

        # Strip internal _sources key before returning
        for g in groups:
            g.pop("_sources", None)

        return {
            "experiment_id": experiment.id,
            "analyzed_revision": experiment.revision,
            "candidate_set_hash": candidate_set_hash,
            "has_duplicates": has_duplicates,
            "duplicate_groups": groups,
        }


def _apply_concept_resolution_plan(
    session: Session,
    *,
    context: TenantContext,
    sources: list[FrozenCandidateInput],
    concept_resolution_plan: list[dict] | None,
    candidate_set_hash: str | None = None,
) -> tuple[list[FrozenCandidateInput], list[dict[str, Any]], str]:
    """Validate a command-scoped plan and return survivors + evidence rows."""
    detected_hash, has_duplicates, detected_groups = _detect_duplicate_groups(sources)

    if candidate_set_hash is not None and candidate_set_hash != detected_hash:
        raise ExperimentConflict("concept_resolution_stale")

    sources = [replace(source, source_fingerprint=_source_fingerprint(source)) for source in sources]

    groups: dict[str, dict[str, Any]] = {}
    for source in sources:
        for concept in source.concepts:
            group = groups.setdefault(concept.duplicate_group_hash, {
                "dimension_key": concept.dimension_key,
                "concept_key_id": concept.concept_key_id,
                "members": [],
            })
            group["members"].append(source)
    duplicate_groups = {
        group_hash: group
        for group_hash, group in groups.items()
        if len(group["members"]) > 1
    }

    plan = concept_resolution_plan or []
    by_group: dict[str, dict[str, Any]] = {}
    allowed_plan_keys = {
        "duplicate_group_hash",
        "action",
        "reason",
        "source_candidate_fingerprints",
        "source_entry_ids",
        "source_candidate_fingerprint",
        "remap_concept_key_id",
    }
    for item in plan:
        if not isinstance(item, dict):
            raise ExperimentError("concept_resolution_plan entries must be objects")
        unknown_keys = sorted(set(item) - allowed_plan_keys)
        if unknown_keys:
            raise ExperimentError(
                f"unknown concept resolution fields:{','.join(unknown_keys)}"
            )
        group_hash = str(item.get("duplicate_group_hash") or "")
        if group_hash in by_group:
            raise ExperimentError(f"duplicate resolution entry:{group_hash}")
        if group_hash not in duplicate_groups:
            raise ExperimentError(f"unknown duplicate group:{group_hash}")
        action = str(item.get("action") or "")
        if action not in _RESOLUTION_ACTIONS:
            raise ExperimentError(f"invalid concept resolution action:{action}")
        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise ExperimentError("concept resolution reason is required")
        expected_members = sorted(
            member.source_fingerprint for member in duplicate_groups[group_hash]["members"]
        )
        submitted_members = item.get("source_candidate_fingerprints")
        submitted_entry_ids = item.get("source_entry_ids")
        if submitted_members is not None:
            if (
                not isinstance(submitted_members, list)
                or sorted(map(str, submitted_members)) != expected_members
            ):
                raise ExperimentConflict(f"concept_resolution_stale:{group_hash}")
        elif submitted_entry_ids is not None:
            expected_entry_ids = sorted(member.entry.id for member in duplicate_groups[group_hash]["members"])
            if (
                not isinstance(submitted_entry_ids, list)
                or sorted(map(str, submitted_entry_ids)) != expected_entry_ids
            ):
                raise ExperimentConflict(f"concept_resolution_stale:{group_hash}")
        else:
            raise ExperimentError(
                "concept resolution requires source_candidate_fingerprints or source_entry_ids"
            )
        by_group[group_hash] = item

    unresolved = sorted(set(duplicate_groups) - set(by_group))
    if unresolved:
        raise ExperimentConflict(f"concept_unresolved:{','.join(unresolved)}")

    excluded: set[str] = set()
    remaps: dict[tuple[str, str], DimensionConceptKeyRecord] = {}
    resolution_rows: list[dict[str, Any]] = []
    for group_hash in sorted(by_group):
        item = by_group[group_hash]
        group = duplicate_groups[group_hash]
        action = str(item["action"])
        members = group["members"]
        member_fingerprints = sorted(member.source_fingerprint for member in members)
        outcome: dict[str, Any] = {"remaining_source_fingerprints": member_fingerprints}

        if action == "exclude_source_candidate":
            target = str(item.get("source_candidate_fingerprint") or "")
            if target not in member_fingerprints:
                raise ExperimentError(f"exclude target is not a group member:{group_hash}")
            excluded.add(target)
            outcome = {
                "remaining_source_fingerprints": [fp for fp in member_fingerprints if fp != target],
                "excluded_source_fingerprints": [target],
            }
        elif action == "remap_before_freeze":
            target = str(item.get("source_candidate_fingerprint") or "")
            target_key_id = str(item.get("remap_concept_key_id") or "")
            if target not in member_fingerprints:
                raise ExperimentError(f"remap target is not a group member:{group_hash}")
            target_key = session.get(DimensionConceptKeyRecord, target_key_id)
            if target_key is None or (
                target_key.scope == "workspace" and target_key.workspace_id != context.workspace_id
            ):
                raise ExperimentError(f"remap concept key is unavailable:{target_key_id}")
            if target_key.id == group["concept_key_id"]:
                raise ExperimentError("remap concept key must differ from source concept key")
            remaps[(target, group["dimension_key"])] = target_key
            outcome = {
                "remaining_source_fingerprints": member_fingerprints,
                "remapped_source_fingerprint": target,
                "from_concept_key_id": group["concept_key_id"],
                "to_concept_key_id": target_key.id,
                "to_concept_key_name": target_key.name,
            }
        elif item.get("source_candidate_fingerprint") or item.get("remap_concept_key_id"):
            raise ExperimentError(
                "allow_controlled_duplicate does not accept target/remap fields"
            )

        if action == "exclude_source_candidate" and item.get("remap_concept_key_id"):
            raise ExperimentError("exclude_source_candidate does not accept remap_concept_key_id")

        resolution_rows.append({
            "duplicate_group_hash": group_hash,
            "dimension_key": group["dimension_key"],
            "concept_key_id": group["concept_key_id"],
            "source_candidate_fingerprints_json": {"fingerprints": member_fingerprints},
            "action": action,
            "outcome_evidence_json": outcome,
            "reason": str(item["reason"]).strip(),
        })

    survivors = [source for source in sources if source.source_fingerprint not in excluded]
    remapped_survivors: list[FrozenCandidateInput] = []
    for source in survivors:
        remapped_concepts: list[FrozenConceptInput] = []
        for concept in source.concepts:
            target_key = remaps.get((source.source_fingerprint, concept.dimension_key))
            if target_key is None:
                remapped_concepts.append(concept)
                continue
            remapped_concepts.append(replace(
                concept,
                concept_key_id=target_key.id,
                concept_key_scope=target_key.scope,
                concept_key_name=target_key.name,
                duplicate_group_hash=_duplicate_group_hash(concept.dimension_key, target_key.id),
            ))
        remapped_survivors.append(replace(source, concepts=tuple(remapped_concepts)))
    survivors = remapped_survivors

    # A remap may accidentally collide with another candidate.  That target
    # group was not among the groups the submitted plan was validated against,
    # so accepting it would create a fresh unresolved duplicate.
    surviving_groups: dict[str, int] = {}
    for source in survivors:
        for concept in source.concepts:
            group_hash = concept.duplicate_group_hash
            surviving_groups[group_hash] = surviving_groups.get(group_hash, 0) + 1
    allowed_groups = {
        group_hash for group_hash, item in by_group.items()
        if item["action"] == "allow_controlled_duplicate"
    }
    unexpected_duplicates = sorted(
        group_hash for group_hash, count in surviving_groups.items()
        if count > 1 and group_hash not in allowed_groups
    )
    if unexpected_duplicates:
        raise ExperimentConflict(
            f"concept_unresolved:{','.join(unexpected_duplicates)}"
        )

    plan_hash = _hash_json([
        {
            "duplicate_group_hash": row["duplicate_group_hash"],
            "dimension_key": row["dimension_key"],
            "concept_key_id": row["concept_key_id"],
            "source_candidate_fingerprints": row["source_candidate_fingerprints_json"]["fingerprints"],
            "action": row["action"],
            "outcome": row["outcome_evidence_json"],
            "reason": row["reason"],
        }
        for row in resolution_rows
    ])
    return survivors, resolution_rows, plan_hash


def _aware_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _parse_schedule_datetime(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ExperimentError(f"invalid_{field}", code="invalid_schedule") from exc
    return _aware_datetime(parsed)


def _load_frozen_context(
    session: Session,
    *,
    context: TenantContext,
    experiment: ExperimentRecord,
    draft_context: Mapping[str, Any],
) -> FrozenExperimentContext:
    """Resolve and validate every version-level input under one transaction."""
    product = session.execute(
        select(ProductRecord).where(
            ProductRecord.id == experiment.product_id,
            ProductRecord.workspace_id == context.workspace_id,
        ).with_for_update()
    ).scalar_one_or_none()
    if product is None or not product.product_profile_entry_id:
        raise ExperimentConflict("product_profile_not_linked")

    product_version_id = str(draft_context.get("product_version_id") or "")
    offer_version_id = str(draft_context.get("offer_version_id") or "")
    versions = session.scalars(
        select(LibraryVersionRecord).where(
            LibraryVersionRecord.workspace_id == context.workspace_id,
            LibraryVersionRecord.id.in_(sorted({product_version_id, offer_version_id})),
        ).order_by(LibraryVersionRecord.id).with_for_update()
    ).all()
    by_id = {row.id: row for row in versions}
    product_version = by_id.get(product_version_id)
    offer_version = by_id.get(offer_version_id)
    if (
        product_version is None
        or product_version.entry_type != "product"
        or product_version.entry_id != product.product_profile_entry_id
        or product_version.state != "effective"
        or product_version.eligibility != "eligible"
    ):
        raise ExperimentConflict("product_version_ineligible")
    if (
        offer_version is None
        or offer_version.entry_type != "offer"
        or offer_version.state != "effective"
        or offer_version.eligibility != "eligible"
    ):
        raise ExperimentConflict("offer_version_ineligible")

    offer_content = dict(offer_version.content_json or {})
    if offer_content.get("product_version_id") != product_version.id:
        raise ExperimentConflict("offer_product_mismatch")
    market = str(draft_context.get("market") or "").strip().upper()
    market_scope = offer_content.get("market_scope")
    if not market or not isinstance(market_scope, list) or market not in {
        str(item).strip().upper() for item in market_scope
    }:
        raise ExperimentConflict("market_scope_mismatch")

    schedule = draft_context.get("schedule")
    if not isinstance(schedule, dict):
        raise ExperimentError("invalid_schedule")
    schedule_start = _parse_schedule_datetime(schedule.get("start_at"), "schedule_start_at")
    schedule_end = _parse_schedule_datetime(schedule.get("end_at"), "schedule_end_at")
    if schedule_end <= schedule_start:
        raise ExperimentError("invalid_schedule_range")
    try:
        valid_from = date.fromisoformat(str(offer_content.get("valid_from")))
        valid_to_raw = offer_content.get("valid_to")
        valid_to = date.max if valid_to_raw in (None, "") else date.fromisoformat(str(valid_to_raw))
    except ValueError as exc:
        raise ExperimentConflict("validity_expired") from exc
    if schedule_start.date() < valid_from or schedule_end.date() > valid_to:
        raise ExperimentConflict("validity_expired")

    budget = draft_context.get("budget")
    attribution = draft_context.get("attribution")
    audience = draft_context.get("audience", {"schema_version": "1", "content": {}})
    statistical_plan = draft_context.get("statistical_plan")
    if not isinstance(budget, dict) or not isinstance(attribution, dict):
        raise ExperimentError("invalid_frozen_context")
    if not isinstance(audience, dict) or not isinstance(statistical_plan, dict):
        raise ExperimentError("invalid_frozen_context")
    landing_page_url = str(offer_content.get("landing_page_url") or "").strip()
    if not landing_page_url:
        raise ExperimentConflict("landing_page_missing")
    try:
        daily_budget = Decimal(str(budget["daily_amount"]))
        total_budget = Decimal(str(budget["total_amount"]))
        click_window = int(attribution["click_window_days"])
        view_window = int(attribution["view_window_days"])
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ExperimentError("invalid_frozen_context") from exc
    if daily_budget <= 0 or total_budget <= 0 or click_window < 0 or view_window < 0:
        raise ExperimentError("invalid_frozen_context")

    # Source-market identity belongs to the exact immutable OfferVersion.  A
    # caller-supplied draft value is mutable presentation input and therefore
    # cannot be authoritative (nor may budget.currency be used as a fallback).
    source_market_raw = offer_content.get("source_market_snapshot")
    if not isinstance(source_market_raw, dict):
        raise ExperimentConflict("source_market_context_missing")
    source_locale = str(source_market_raw.get("locale") or "").strip()
    source_market = str(source_market_raw.get("market") or "").strip().upper()
    source_placement = str(source_market_raw.get("placement") or "").strip()
    source_country = str(source_market_raw.get("country") or "").strip().upper()
    source_currency = str(source_market_raw.get("currency") or "").strip().upper()
    if (
        not source_locale
        or len(source_market) != 2
        or not source_placement
        or len(source_country) != 2
        or len(source_currency) != 3
    ):
        raise ExperimentConflict("source_market_context_incomplete")
    source_market_snapshot = FrozenSourceMarketSnapshot(
        locale=source_locale,
        market=source_market,
        placement=source_placement,
        country=source_country,
        currency=source_currency,
    )

    return FrozenExperimentContext(
        product_version=product_version,
        offer_version=offer_version,
        market=market,
        placement=str(draft_context.get("placement") or "").strip(),
        landing_page_url=landing_page_url,
        currency=str(draft_context.get("currency") or "").strip().upper(),
        daily_budget_amount=daily_budget,
        total_budget_cap=total_budget,
        schedule_start_at=schedule_start,
        schedule_end_at=schedule_end,
        schedule_timezone=str(schedule.get("timezone") or "").strip(),
        attribution_click_window_days=click_window,
        attribution_view_window_days=view_window,
        audience=audience,
        statistical_plan=statistical_plan,
        source_market_snapshot=source_market_snapshot,
    )


def _review_evidence(
    session: Session, context: TenantContext, asset_version_id: str,
) -> tuple[bool, dict[str, Any]]:
    from ..generation.review import find_review_head

    reviews = session.scalars(
        select(QualityReviewRecord).where(
            QualityReviewRecord.workspace_id == context.workspace_id,
            QualityReviewRecord.asset_version_id == asset_version_id,
            QualityReviewRecord.legacy_unscoped.is_(False),
        ).order_by(
            QualityReviewRecord.created_at.desc(), QualityReviewRecord.id.desc()
        ).with_for_update()
    ).all()
    review_set = [
        {
            "id": row.id,
            "review_value": row.review_value,
            "created_at": _aware_datetime(row.created_at).isoformat(),
        }
        for row in reviews
    ]
    # Review history is a correction graph, not a last-write-wins stream.  The
    # effective head is the row that has not been superseded, even when an
    # older correction receives a later timestamp during import/replay.
    head = find_review_head(session, context, asset_version_id)
    passed = head is not None and head.review_value == "approved"
    code = None if passed else (
        "review_unknown" if head is not None and head.review_value is None
        else "review_not_approved"
    )
    return passed, {
        "outcome": "pass" if passed else "fail",
        "code": code,
        "effective_review_id": head.id if head else None,
        "review_value": head.review_value if head else None,
        "review_created_at": _aware_datetime(head.created_at).isoformat() if head else None,
        "superseded_count": max(0, len(reviews) - (1 if head else 0)),
        "review_set_hash": _hash_json(review_set),
        "review_ordering": "unsuperseded correction head; created_at DESC, id DESC tie-break",
        "linearization_lock": "asset_version_for_update",
    }


def _qc_evidence(
    session: Session,
    context: TenantContext,
    asset_version: AssetVersionRecord,
    execution_mode: str,
) -> tuple[bool, dict[str, Any]]:
    attempts = session.scalars(
        select(AssetQCAttemptRecord).where(
            AssetQCAttemptRecord.workspace_id == context.workspace_id,
            AssetQCAttemptRecord.asset_version_id == asset_version.id,
        ).order_by(AssetQCAttemptRecord.attempt_sequence, AssetQCAttemptRecord.id)
    ).all()
    active = next((row for row in attempts if row.state in {"pending", "running"}), None)
    unresolved: dict[str, AssetQCAttemptRecord] = {}
    remediation_chain: list[dict[str, Any]] = []
    for attempt in attempts:
        if attempt.state == "failed_blocking":
            unresolved[attempt.id] = attempt
        elif attempt.state == "passed" and attempt.remediates_attempt_id:
            blocked = unresolved.get(attempt.remediates_attempt_id)
            if blocked is None:
                continue
            input_match = attempt.input_hash == blocked.input_hash
            policy_match = (
                attempt.qc_policy_version == blocked.qc_policy_version
                and attempt.rule_set_version == blocked.rule_set_version
            )
            remediation_chain.append({
                "blocked_id": blocked.id,
                "remediated_by_id": attempt.id,
                "input_hash_match": input_match,
                "coverage_decision": {
                    "covers": policy_match,
                    "registry_version": "exact-policy-v1",
                    "proof_hash": _hash_json([
                        attempt.qc_policy_version,
                        attempt.rule_set_version,
                        blocked.qc_policy_version,
                        blocked.rule_set_version,
                        policy_match,
                    ]),
                },
            })
            if input_match and policy_match:
                unresolved.pop(blocked.id, None)

    terminal = [row for row in attempts if row.state not in {"pending", "running"}]
    latest = terminal[-1] if terminal else None
    if active is not None:
        passed, outcome, code = False, "fail", "qc_in_progress"
    elif unresolved:
        passed, outcome, code = False, "fail", (
            "qc_remediation_invalid" if latest is not None and latest.state == "passed"
            else "qc_blocked"
        )
    elif latest is None:
        qc_expected = (
            asset_version.origin_kind == "generation"
            and execution_mode in _EXECUTION_MODES
        )
        passed, outcome, code = (
            (False, "fail", "qc_missing")
            if qc_expected
            else (True, "pass", None)
        )
    elif latest.state in {"failed_nonblocking", "uncertain"}:
        passed, outcome, code = True, "warn", None
    else:
        passed, outcome, code = True, "pass", None

    check_results = []
    if latest is not None:
        check_results = session.scalars(
            select(AssetQCCheckResultRecord).where(
                AssetQCCheckResultRecord.workspace_id == context.workspace_id,
                AssetQCCheckResultRecord.qc_attempt_id == latest.id,
            ).order_by(AssetQCCheckResultRecord.id)
        ).all()
    return passed, {
        "outcome": outcome,
        "code": code,
        "latest_effective_attempt_id": latest.id if latest else None,
        "latest_effective_state": latest.state if latest else None,
        "latest_effective_sequence": latest.attempt_sequence if latest else None,
        "active_attempt_id": active.id if active else None,
        "unresolved_block_count": len(unresolved),
        "unresolved_block_set_hash": _hash_json(sorted(unresolved)),
        "remediation_chain": remediation_chain,
        "qc_expected": asset_version.origin_kind == "generation" and execution_mode in _EXECUTION_MODES,
        "qc_policy_version": latest.qc_policy_version if latest else None,
        "check_result_ids": [row.id for row in check_results],
        "check_result_hashes": [row.evidence_hash for row in check_results],
        "total_attempts": len(attempts),
        "linearization_lock": "asset_version_for_update",
    }


def _truth_evidence(
    session: Session,
    context: TenantContext,
    asset_version: AssetVersionRecord,
    frozen_context: FrozenExperimentContext,
    now: datetime,
) -> tuple[bool, dict[str, Any]]:
    base = {
        "truth_package_id": asset_version.truth_package_id,
        "truth_dependency_expected": asset_version.truth_dependency_expected,
        "truth_dependency_policy_version": asset_version.truth_dependency_policy_version,
        "truth_dependency_evidence_hash": asset_version.truth_dependency_evidence_hash,
    }
    if (
        asset_version.truth_dependency_expected is None
        or not asset_version.truth_dependency_policy_version
        or not isinstance(asset_version.truth_dependency_evidence_json, dict)
        or not asset_version.truth_dependency_evidence_hash
    ):
        return False, {**base, "outcome": "fail", "code": "truth_dependency_missing"}
    computed_evidence_hash = _hash_json(asset_version.truth_dependency_evidence_json)
    if computed_evidence_hash != asset_version.truth_dependency_evidence_hash:
        return False, {
            **base,
            "outcome": "fail",
            "code": "truth_dependency_evidence_mismatch",
            "computed_truth_dependency_evidence_hash": computed_evidence_hash,
        }
    if asset_version.truth_dependency_expected is False:
        if asset_version.truth_package_id is not None:
            return False, {**base, "outcome": "fail", "code": "stale_truth_unevaluable"}
        return True, {
            **base, "outcome": "pass", "code": "not_applicable",
            "dependency_version_token": asset_version.truth_dependency_evidence_hash,
        }
    if not asset_version.truth_package_id:
        return False, {**base, "outcome": "fail", "code": "truth_dependency_missing"}

    package = session.execute(
        select(TruthPackageRecord).where(
            TruthPackageRecord.workspace_id == context.workspace_id,
            TruthPackageRecord.id == asset_version.truth_package_id,
        ).with_for_update()
    ).scalar_one_or_none()
    if package is None:
        return False, {**base, "outcome": "fail", "code": "stale_truth_unevaluable"}
    dependency_chain = {
        "truth_package_id": package.id,
        "canonical_hash": package.canonical_hash,
        "status": package.status,
        "valid_until": _aware_datetime(package.valid_until).isoformat(),
        "product_version_id": package.product_version_id,
        "offer_version_id": package.offer_version_id,
    }
    stale = (
        package.status == "failed"
        or _aware_datetime(package.valid_until) <= now
        or package.product_version_id != frozen_context.product_version.id
        or package.offer_version_id != frozen_context.offer_version.id
    )

    binding = session.scalar(
        select(MarketAssetBindingRecord).where(
            MarketAssetBindingRecord.workspace_id == context.workspace_id,
            MarketAssetBindingRecord.bound_asset_version_id == asset_version.id,
            MarketAssetBindingRecord.target_market == frozen_context.market,
            MarketAssetBindingRecord.offer_version_id == frozen_context.offer_version.id,
            MarketAssetBindingRecord.active.is_(True),
        ).with_for_update()
    )
    if asset_version.origin_kind == "localized" and binding is None:
        return False, {
            **base, "outcome": "fail", "code": "stale_truth_unevaluable",
            "dependency_chain": dependency_chain,
        }
    if binding is not None:
        dependency_chain["market_asset_binding"] = {
            "id": binding.id,
            "revision": binding.revision,
            "truth_freshness": binding.truth_freshness,
            "ready_for_experiment": binding.ready_for_experiment,
            "review_state": binding.review_state,
        }
        stale = stale or binding.truth_freshness != "current" or not binding.ready_for_experiment
    token = _hash_json(dependency_chain)
    return not stale, {
        **base,
        "outcome": "fail" if stale else "pass",
        "code": "stale_truth" if stale else "clear",
        "dependency_chain": dependency_chain,
        "dependency_version_token": token,
    }


def _candidate_eligibility_evidence(
    session: Session,
    *,
    context: TenantContext,
    source: FrozenCandidateInput,
    frozen_context: FrozenExperimentContext,
    resolution_plan_hash: str,
    now: datetime,
) -> Mapping[str, Any]:
    review_ok, review = _review_evidence(session, context, source.asset_version.id)
    qc_ok, qc = _qc_evidence(
        session, context, source.asset_version, source.execution_mode,
    )
    truth_ok, truth = _truth_evidence(
        session, context, source.asset_version, frozen_context, now,
    )
    gate_results = [
        {"gate": "review", "outcome": review["outcome"], "code": review["code"]},
        {"gate": "qc", "outcome": qc["outcome"], "code": qc["code"]},
        {"gate": "stale_truth", "outcome": truth["outcome"], "code": truth["code"]},
        {"gate": "product_version", "outcome": "pass", "code": None},
        {"gate": "offer_version", "outcome": "pass", "code": None},
        {"gate": "concept_resolution", "outcome": "pass", "code": None},
    ]
    if not review_ok or not qc_ok or not truth_ok:
        first = next(item for item in gate_results if item["outcome"] == "fail")
        raise ExperimentConflict(
            f"candidate_ineligible:{source.asset_version.id}:{first['code']}"
        )
    return {
        "eligibility_policy_version": "freeze-v2-authoritative",
        "review": review,
        "qc": qc,
        "asset_version": {
            "id": source.asset_version.id,
            "content_sha256": source.asset_version.content_sha256,
            "origin_kind": source.asset_version.origin_kind,
            "execution_mode": source.execution_mode,
        },
        "product_version": {
            "id": frozen_context.product_version.id,
            "entry_id": frozen_context.product_version.entry_id,
            "state": frozen_context.product_version.state,
            "eligibility": frozen_context.product_version.eligibility,
            "content_hash": frozen_context.product_version.content_hash,
        },
        "offer_version": {
            "id": frozen_context.offer_version.id,
            "entry_id": frozen_context.offer_version.entry_id,
            "state": frozen_context.offer_version.state,
            "eligibility": frozen_context.offer_version.eligibility,
            "content_hash": frozen_context.offer_version.content_hash,
        },
        "stale_truth": truth,
        "execution_mode": {"outcome": "pass", "mode": source.execution_mode},
        "concept_resolution": {
            "outcome": "pass",
            "source_fingerprint": source.source_fingerprint,
            "resolution_plan_hash": resolution_plan_hash,
        },
        "gate_results": gate_results,
        "evaluated_at": now.isoformat(),
    }


def validate_live_launch_eligibility(
    session: Session,
    context: TenantContext,
    *,
    experiment_id: str,
    experiment_version_id: str,
) -> list[ExperimentVersionCandidateRecord]:
    """Re-evaluate mutable Launch gates against frozen candidate identities.

    Freeze evidence explains eligibility at one instant; it is not a durable
    authorization for a later remote write. Launch invokes this both when it
    enqueues and immediately before every saga call.
    """
    experiment = session.execute(
        select(ExperimentRecord).where(
            ExperimentRecord.id == experiment_id,
            ExperimentRecord.workspace_id == context.workspace_id,
        ).with_for_update()
    ).scalar_one_or_none()
    if experiment is None or experiment.state != "frozen":
        raise ExperimentConflict("launch_experiment_not_frozen")

    version = session.execute(
        select(ExperimentVersionRecord).where(
            ExperimentVersionRecord.id == experiment_version_id,
            ExperimentVersionRecord.workspace_id == context.workspace_id,
            ExperimentVersionRecord.experiment_id == experiment_id,
        ).with_for_update()
    ).scalar_one_or_none()
    if version is None:
        raise ExperimentConflict("launch_experiment_version_missing")
    if _hash_json(version.frozen_snapshot_json) != version.frozen_snapshot_hash:
        raise ExperimentConflict("launch_frozen_snapshot_hash_mismatch")

    library_versions = session.scalars(
        select(LibraryVersionRecord).where(
            LibraryVersionRecord.workspace_id == context.workspace_id,
            LibraryVersionRecord.id.in_([
                version.product_version_id,
                version.offer_version_id,
            ]),
        ).order_by(LibraryVersionRecord.id).with_for_update()
    ).all()
    by_id = {row.id: row for row in library_versions}
    product_version = by_id.get(version.product_version_id)
    offer_version = by_id.get(version.offer_version_id)
    if (
        product_version is None
        or product_version.state != "effective"
        or product_version.eligibility != "eligible"
    ):
        raise ExperimentConflict("launch_product_version_ineligible")
    if (
        offer_version is None
        or offer_version.state != "effective"
        or offer_version.eligibility != "eligible"
    ):
        raise ExperimentConflict("launch_offer_version_ineligible")

    snapshot = dict(version.frozen_snapshot_json or {})
    source_raw = snapshot.get("source_market_snapshot")
    if not isinstance(source_raw, dict):
        raise ExperimentConflict("launch_source_market_context_missing")
    source_snapshot = FrozenSourceMarketSnapshot(
        locale=str(source_raw.get("locale") or ""),
        market=str(source_raw.get("market") or ""),
        placement=str(source_raw.get("placement") or ""),
        country=str(source_raw.get("country") or ""),
        currency=str(source_raw.get("currency") or ""),
    )
    if not all((
        source_snapshot.locale,
        source_snapshot.market,
        source_snapshot.placement,
        source_snapshot.country,
        source_snapshot.currency,
    )):
        raise ExperimentConflict("launch_source_market_context_incomplete")
    delivery_context = snapshot.get("delivery_context") or {}
    landing_page = delivery_context.get("landing_page") or {}
    frozen_context = FrozenExperimentContext(
        product_version=product_version,
        offer_version=offer_version,
        market=version.market,
        placement=version.placement,
        landing_page_url=str(landing_page.get("url") or ""),
        currency=version.budget_currency,
        daily_budget_amount=version.daily_budget_amount,
        total_budget_cap=version.total_budget_cap,
        schedule_start_at=version.schedule_start_at,
        schedule_end_at=version.schedule_end_at,
        schedule_timezone=version.schedule_timezone,
        attribution_click_window_days=version.attribution_click_window_days,
        attribution_view_window_days=version.attribution_view_window_days,
        audience=version.audience_snapshot_json,
        statistical_plan=version.statistical_plan_json,
        source_market_snapshot=source_snapshot,
    )

    candidates = list(session.scalars(
        select(ExperimentVersionCandidateRecord).where(
            ExperimentVersionCandidateRecord.workspace_id == context.workspace_id,
            ExperimentVersionCandidateRecord.experiment_version_id == version.id,
        ).order_by(ExperimentVersionCandidateRecord.position).with_for_update()
    ).all())
    if len(candidates) < 2:
        raise ExperimentConflict("launch_candidate_manifest_incomplete")
    asset_ids = [candidate.asset_version_id for candidate in candidates]
    assets = session.scalars(
        select(AssetVersionRecord).where(
            AssetVersionRecord.workspace_id == context.workspace_id,
            AssetVersionRecord.id.in_(asset_ids),
        ).order_by(AssetVersionRecord.id).with_for_update()
    ).all()
    assets_by_id = {row.id: row for row in assets}
    now = utcnow()
    for candidate in candidates:
        asset_version = assets_by_id.get(candidate.asset_version_id)
        if (
            asset_version is None
            or asset_version.content_sha256 != candidate.asset_version_content_sha256
            or asset_version.execution_mode != candidate.execution_mode
        ):
            raise ExperimentConflict(
                f"launch_candidate_identity_changed:{candidate.id}"
            )
        review_ok, review = _review_evidence(session, context, asset_version.id)
        qc_ok, qc = _qc_evidence(
            session, context, asset_version, candidate.execution_mode,
        )
        truth_ok, truth = _truth_evidence(
            session, context, asset_version, frozen_context, now,
        )
        if not (review_ok and qc_ok and truth_ok):
            failed = next(
                evidence for evidence in (review, qc, truth)
                if evidence["outcome"] == "fail"
            )
            raise ExperimentConflict(
                f"launch_candidate_ineligible:{candidate.id}:{failed['code']}"
            )

    eligible, _evidence = is_effectively_eligible(
        session,
        workspace_id=context.workspace_id,
        experiment_id=experiment_id,
        experiment_version_id=experiment_version_id,
    )
    if not eligible:
        raise ExperimentConflict("launch_eligibility_projection_ineligible")
    return candidates


def freeze_experiment_v2(
    context: TenantContext,
    experiment_id: str,
    expected_revision: int,
    *,
    concept_resolution_plan: list[dict] | None = None,
    analyzed_candidate_set_hash: str | None = None,
) -> dict:
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)

        if experiment.state == "frozen":
            return _experiment_dict_v2(session, experiment)

        _require_state(experiment, "draft")
        _check_revision(experiment, expected_revision)

        draft_ctx = dict(experiment.config_json or {})
        now = utcnow()

        version_id = new_id("expver")

        sources = _load_candidate_sources(session, context, experiment)
        # Re-acquire FOR UPDATE on asset versions for freeze atomicity. The
        # shared loader above is a plain read shared with read-only analysis.
        session.scalars(
            select(AssetVersionRecord).where(
                AssetVersionRecord.workspace_id == context.workspace_id,
                AssetVersionRecord.id.in_(sorted(s.asset_version.id for s in sources)),
            ).order_by(AssetVersionRecord.id).with_for_update()
        ).all()

        frozen_context = _load_frozen_context(
            session,
            context=context,
            experiment=experiment,
            draft_context=draft_ctx,
        )

        survivors, resolution_evidence, resolutions_hash = _apply_concept_resolution_plan(
            session,
            context=context,
            sources=sources,
            concept_resolution_plan=concept_resolution_plan,
            candidate_set_hash=analyzed_candidate_set_hash,
        )
        if len(survivors) < 2:
            raise ExperimentConflict("insufficient_candidates_after_resolution")
        if len(survivors) > 100:
            raise ExperimentConflict("candidate_limit_exceeded")
        execution_modes = {source.execution_mode for source in survivors}
        if len(execution_modes) != 1:
            raise ExperimentConflict("execution_mode_mixed")
        frozen_execution_mode = next(iter(execution_modes))

        candidates: list[ExperimentVersionCandidateRecord] = []
        candidate_concepts: list[
            tuple[ExperimentVersionCandidateRecord, FrozenConceptInput]
        ] = []
        candidate_snapshot_rows: list[dict[str, Any]] = []
        for pos, source in enumerate(survivors):
            entry = source.entry
            av = source.asset_version
            concepts_content = [
                {
                    "dimension_key": concept.dimension_key,
                    "stable_dimension_signature": concept.stable_dimension_signature,
                    "concept_key_id": concept.concept_key_id,
                    "concept_key_scope": concept.concept_key_scope,
                    "concept_key_name": concept.concept_key_name,
                    "value_hash": concept.value_hash,
                    "duplicate_group_hash": concept.duplicate_group_hash,
                    "evidence_hash": _hash_json(concept.evidence_json),
                }
                for concept in source.concepts
            ]
            concepts_hash = _hash_json(concepts_content)
            source_evidence = dict(source.source_evidence)
            source_evidence_hash = _hash_json(source_evidence)

            elig_evidence = _candidate_eligibility_evidence(
                session,
                context=context,
                source=source,
                frozen_context=frozen_context,
                resolution_plan_hash=resolutions_hash,
                now=now,
            )
            elig_json = _canonical_json(elig_evidence)
            elig_hash = _sha256(elig_json.encode("utf-8"))

            content = _canonical_json({
                "position": pos,
                "asset_version_id": av.id,
                "content_sha256": av.content_sha256,
                "marketing_vars_hash": source.marketing_vars_hash,
                "concepts_hash": concepts_hash,
                "execution_mode": source.execution_mode,
                "eligibility_evidence_hash": elig_hash,
                "source_kind": source.source_kind,
                "source_evidence_hash": source_evidence_hash,
            })
            candidate_hash = _sha256(content.encode("utf-8"))

            cand = ExperimentVersionCandidateRecord(
                id=new_id("expcand"),
                workspace_id=context.workspace_id,
                experiment_version_id=version_id,
                source_entry_id=entry.id,
                source_kind=source.source_kind,
                source_run_item_id=source.source_run_item_id,
                source_evidence_json=source_evidence,
                source_evidence_hash=source_evidence_hash,
                asset_version_id=av.id,
                asset_version_content_sha256=av.content_sha256,
                position=pos,
                marketing_vars_json=entry.marketing_vars_json,
                marketing_vars_hash=source.marketing_vars_hash,
                execution_mode=source.execution_mode,
                eligibility_evidence_json=elig_evidence,
                eligibility_evidence_hash=elig_hash,
                concepts_hash=concepts_hash,
                candidate_hash=candidate_hash,
                evaluated_at=now,
            )
            candidates.append(cand)
            candidate_snapshot_rows.append({
                "id": cand.id,
                "position": pos,
                "source_entry_id": entry.id,
                "source_kind": source.source_kind,
                "source_run_item_id": source.source_run_item_id,
                "source_fingerprint": source.source_fingerprint,
                "asset_version_id": av.id,
                "asset_version_content_sha256": av.content_sha256,
                "marketing_vars": entry.marketing_vars_json,
                "marketing_vars_hash": source.marketing_vars_hash,
                "execution_mode": source.execution_mode,
                "concepts": concepts_content,
                "concepts_hash": concepts_hash,
                "eligibility_evidence": elig_evidence,
                "eligibility_evidence_hash": elig_hash,
                "source_evidence": source_evidence,
                "source_evidence_hash": source_evidence_hash,
                "candidate_hash": candidate_hash,
            })
            for concept in source.concepts:
                candidate_concepts.append((cand, concept))

        candidates_hash = _hash_json([
            {"id": row["id"], "candidate_hash": row["candidate_hash"]}
            for row in candidate_snapshot_rows
        ])

        stat_plan = dict(frozen_context.statistical_plan)
        stat_json = _canonical_json(stat_plan)
        stat_hash = _sha256(stat_json.encode("utf-8"))

        audience = dict(frozen_context.audience)
        audience_json = _canonical_json(audience)
        audience_hash = _sha256(audience_json.encode("utf-8"))

        landing_page_evidence = {
            "source": "offer_version",
            "offer_version_id": frozen_context.offer_version.id,
            "offer_version_content_hash": frozen_context.offer_version.content_hash,
            "landing_page_url": frozen_context.landing_page_url,
        }
        lp_hash = _hash_json(landing_page_evidence)

        snapshot_resolution_plan = [
            {
                "duplicate_group_hash": row["duplicate_group_hash"],
                "dimension_key": row["dimension_key"],
                "concept_key_id": row["concept_key_id"],
                "source_candidate_fingerprints": row["source_candidate_fingerprints_json"]["fingerprints"],
                "action": row["action"],
                "outcome": row["outcome_evidence_json"],
                "reason": row["reason"],
            }
            for row in resolution_evidence
        ]

        snapshot = {
            "schema_version": 2,
            "experiment_id": experiment.id,
            "version_id": version_id,
            "execution_mode": frozen_execution_mode,
            "candidates_hash": candidates_hash,
            "resolutions_hash": resolutions_hash,
            "statistical_plan_hash": stat_hash,
            "candidates": candidate_snapshot_rows,
            "candidate_manifest": {
                "count": len(candidate_snapshot_rows),
                "candidates_hash": candidates_hash,
            },
            "concept_resolution_plan": snapshot_resolution_plan,
            "concept_resolutions_hash": resolutions_hash,
            "product": {
                "product_id": experiment.product_id,
                "product_version_id": frozen_context.product_version.id,
                "entry_id": frozen_context.product_version.entry_id,
                "content_hash": frozen_context.product_version.content_hash,
                "state": frozen_context.product_version.state,
                "eligibility": frozen_context.product_version.eligibility,
            },
            "offer": {
                "offer_version_id": frozen_context.offer_version.id,
                "entry_id": frozen_context.offer_version.entry_id,
                "content_hash": frozen_context.offer_version.content_hash,
                "state": frozen_context.offer_version.state,
                "eligibility": frozen_context.offer_version.eligibility,
            },
            "delivery_context": {
                "market": frozen_context.market,
                "placement": frozen_context.placement,
                "landing_page": landing_page_evidence,
            },
            "source_market_snapshot": (
                None
                if frozen_context.source_market_snapshot is None
                else {
                    "locale": frozen_context.source_market_snapshot.locale,
                    "market": frozen_context.source_market_snapshot.market,
                    "placement": frozen_context.source_market_snapshot.placement,
                    "country": frozen_context.source_market_snapshot.country,
                    "currency": frozen_context.source_market_snapshot.currency,
                }
            ),
            "budget": {
                "daily_amount": str(frozen_context.daily_budget_amount),
                "total_amount": str(frozen_context.total_budget_cap),
                "currency": frozen_context.currency,
                "duration": {
                    "start_at": frozen_context.schedule_start_at.isoformat(),
                    "end_at": frozen_context.schedule_end_at.isoformat(),
                    "timezone": frozen_context.schedule_timezone,
                },
            },
            "attribution": {
                "click_window_days": frozen_context.attribution_click_window_days,
                "view_window_days": frozen_context.attribution_view_window_days,
            },
            "audience": audience,
            "audience_hash": audience_hash,
            "decision_policy": {
                "metric_contract_hash": experiment.metric_contract_hash or "",
                "statistical_plan": stat_plan,
                "statistical_plan_hash": stat_hash,
            },
            "execution": {"mode": frozen_execution_mode},
        }
        snapshot_json = _canonical_json(snapshot)
        snapshot_hash = _sha256(snapshot_json.encode("utf-8"))

        version = ExperimentVersionRecord(
            id=version_id,
            workspace_id=context.workspace_id,
            experiment_id=experiment.id,
            canonicalization_version="p1-migration-v1",
            frozen_snapshot_json=snapshot,
            frozen_snapshot_hash=snapshot_hash,
            metric_contract_hash=experiment.metric_contract_hash or "",
            market=frozen_context.market,
            placement=frozen_context.placement,
            landing_page_evidence_hash=lp_hash,
            audience_snapshot_json=audience,
            audience_snapshot_hash=audience_hash,
            daily_budget_amount=frozen_context.daily_budget_amount,
            total_budget_cap=frozen_context.total_budget_cap,
            budget_currency=frozen_context.currency,
            schedule_start_at=frozen_context.schedule_start_at,
            schedule_end_at=frozen_context.schedule_end_at,
            schedule_timezone=frozen_context.schedule_timezone,
            attribution_click_window_days=frozen_context.attribution_click_window_days,
            attribution_view_window_days=frozen_context.attribution_view_window_days,
            statistical_plan_json=stat_plan,
            statistical_plan_hash=stat_hash,
            marketing_vars_schema_version=str(draft_ctx.get("marketing_vars_schema_version", "1")),
            product_version_id=frozen_context.product_version.id,
            offer_version_id=frozen_context.offer_version.id,
            candidates_hash=candidates_hash,
            concept_resolutions_hash=resolutions_hash,
        )
        session.add(version)
        session.flush()
        for cand in candidates:
            session.add(cand)
        session.flush()
        for cand, concept in candidate_concepts:
            session.add(ExperimentVersionConceptRecord(
                id=new_id("expconcept"),
                workspace_id=context.workspace_id,
                experiment_version_id=version_id,
                experiment_version_candidate_id=cand.id,
                dimension_key=concept.dimension_key,
                stable_dimension_signature=concept.stable_dimension_signature,
                concept_key_id=concept.concept_key_id,
                concept_key_scope=concept.concept_key_scope,
                concept_key_name=concept.concept_key_name,
                value_json=dict(concept.value_json),
                value_hash=concept.value_hash,
                evidence_json=dict(concept.evidence_json),
                duplicate_group_hash=concept.duplicate_group_hash,
            ))
        for resolution in resolution_evidence:
            session.add(ExperimentVersionResolutionRecord(
                id=new_id("expresolution"),
                workspace_id=context.workspace_id,
                experiment_version_id=version_id,
                duplicate_group_hash=resolution["duplicate_group_hash"],
                dimension_key=resolution["dimension_key"],
                concept_key_id=resolution["concept_key_id"],
                source_candidate_fingerprints_json=resolution[
                    "source_candidate_fingerprints_json"
                ],
                action=resolution["action"],
                outcome_evidence_json=resolution["outcome_evidence_json"],
                reason=resolution["reason"],
                actor_id=context.initiator_id or context.executor,
                authorization_evidence_json={
                    "role": context.role,
                    "authorization_epoch": context.authorization_epoch,
                },
                resolved_at=now,
            ))
        session.flush()

        new_rev = expected_revision + 1
        result = session.execute(
            update(ExperimentRecord)
            .where(
                ExperimentRecord.id == experiment_id,
                ExperimentRecord.state == "draft",
                ExperimentRecord.revision == expected_revision,
            )
            .values(
                state="frozen",
                revision=new_rev,
                frozen_snapshot_json=snapshot,
                frozen_snapshot_hash=snapshot_hash,
                frozen_at=now,
            )
        )
        if result.rowcount != 1:
            raise ExperimentConflict("实验被并发修改，请刷新后重试", code="revision_conflict")

        session.add(ExperimentStateEventRecord(
            id=new_id("expev"),
            workspace_id=context.workspace_id,
            experiment_id=experiment.id,
            from_state="draft",
            to_state="frozen",
            revision=new_rev,
            actor_id=context.initiator_id or context.executor,
            reason="snapshot_frozen",
        ))

        ensure_projection(
            session,
            workspace_id=context.workspace_id,
            experiment_id=experiment.id,
            experiment_version_id=version_id,
            level="version",
        )
        for cand in candidates:
            ensure_projection(
                session,
                workspace_id=context.workspace_id,
                experiment_id=experiment.id,
                experiment_version_id=version_id,
                level="candidate",
                candidate_id=cand.id,
            )

        append_event(
            session,
            event_type="experiment.frozen",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"revision": new_rev, "snapshot_hash": snapshot_hash},
        )
        session.flush()
        session.refresh(experiment)
        return _experiment_dict_v2(session, experiment)


def bind_candidate_v2(
    context: TenantContext,
    experiment_id: str,
    candidate_id: str,
    *,
    ad_account_id: str,
    ad_id: str,
    source: str = "manual_read_only",
    expected_revision: int,
) -> dict:
    if not ad_account_id.startswith("act_"):
        ad_account_id = f"act_{ad_account_id}"

    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)
        _require_state(experiment, "frozen")
        _check_revision(experiment, expected_revision)

        candidate = session.get(ExperimentVersionCandidateRecord, candidate_id)
        if candidate is None or candidate.workspace_id != context.workspace_id:
            raise ExperimentNotFound("Candidate not found", code="candidate_not_found")

        version = session.scalar(
            select(ExperimentVersionRecord).where(
                ExperimentVersionRecord.experiment_id == experiment_id,
                ExperimentVersionRecord.workspace_id == context.workspace_id,
            )
        )
        if version is None or candidate.experiment_version_id != version.id:
            raise ExperimentConflict("Candidate does not belong to this experiment version", code="experiment_candidate_mismatch")

        existing = session.scalar(
            select(ExperimentAdBindingRecord).where(
                ExperimentAdBindingRecord.experiment_version_candidate_id == candidate_id,
            )
        )
        if existing:
            if existing.ad_account_id != ad_account_id or existing.ad_id != ad_id:
                raise ExperimentConflict("binding_conflict")
            return {
                "id": existing.id,
                "candidate_id": candidate_id,
                "ad_account_id": ad_account_id,
                "ad_id": ad_id,
            }

        binding = ExperimentAdBindingRecord(
            id=new_id("expbind"),
            workspace_id=context.workspace_id,
            experiment_entry_id=candidate.source_entry_id or "",
            experiment_version_candidate_id=candidate_id,
            ad_account_id=ad_account_id,
            ad_id=ad_id,
            source=source,
            record_schema_version=2,
        )
        session.add(binding)
        session.flush()
        return {
            "id": binding.id,
            "candidate_id": candidate_id,
            "ad_account_id": ad_account_id,
            "ad_id": ad_id,
        }


def start_experiment_v2(
    context: TenantContext,
    experiment_id: str,
    expected_revision: int,
) -> dict:
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)
        _require_state(experiment, "frozen")
        _check_revision(experiment, expected_revision)

        version = session.scalar(
            select(ExperimentVersionRecord).where(
                ExperimentVersionRecord.experiment_id == experiment_id,
                ExperimentVersionRecord.workspace_id == context.workspace_id,
            )
        )
        if version is None:
            raise ExperimentConflict("No frozen version found", code="experiment_not_frozen")

        candidates = session.scalars(
            select(ExperimentVersionCandidateRecord).where(
                ExperimentVersionCandidateRecord.experiment_version_id == version.id,
            ).order_by(ExperimentVersionCandidateRecord.position)
        ).all()

        bindings_by_cand = {}
        for cand in candidates:
            binding = session.scalar(
                select(ExperimentAdBindingRecord).where(
                    ExperimentAdBindingRecord.experiment_version_candidate_id == cand.id,
                )
            )
            if binding is None:
                raise ExperimentConflict(f"Missing binding for candidate {cand.id}")
            bindings_by_cand[cand.id] = binding

        eligible, evidence = is_effectively_eligible(
            session,
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            experiment_version_id=version.id,
        )
        if not eligible:
            raise ExperimentConflict("Experiment is not eligible to start", code="experiment_not_startable")

        binding_tuples = sorted(
            [(cid, b.ad_account_id, b.ad_id) for cid, b in bindings_by_cand.items()]
        )
        binding_set_json = _canonical_json(binding_tuples)
        binding_set_hash = _sha256(binding_set_json.encode("utf-8"))

        now = utcnow()
        new_rev = expected_revision + 1

        result = session.execute(
            update(ExperimentRecord)
            .where(
                ExperimentRecord.id == experiment_id,
                ExperimentRecord.state == "frozen",
                ExperimentRecord.revision == expected_revision,
            )
            .values(state="running", revision=new_rev, started_at=now)
        )
        if result.rowcount != 1:
            raise ExperimentConflict("实验被并发修改", code="revision_conflict")

        session.add(ExperimentStateEventRecord(
            id=new_id("expev"),
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            from_state="frozen",
            to_state="running",
            revision=new_rev,
            actor_id=context.initiator_id or context.executor,
            reason="started",
            evidence_json={"binding_set_hash": binding_set_hash},
        ))
        append_event(
            session,
            event_type="experiment.started",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"revision": new_rev, "binding_set_hash": binding_set_hash},
        )
        session.flush()
        session.refresh(experiment)
        return _experiment_dict_v2(session, experiment)


def run_judgement_v2(
    context: TenantContext,
    experiment_id: str,
    expected_revision: int,
    *,
    as_of: date | None = None,
) -> dict:
    now = utcnow()
    cutoff = as_of or (now.date() - timedelta(days=1))
    if cutoff >= now.date():
        raise ExperimentError("判定截止日必须早于今天", code="experiment_cutoff_invalid")

    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)
        _require_state(experiment, "running")
        _check_revision(experiment, expected_revision)

        version = session.scalar(
            select(ExperimentVersionRecord).where(
                ExperimentVersionRecord.experiment_id == experiment_id,
                ExperimentVersionRecord.workspace_id == context.workspace_id,
            )
        )
        if version is None:
            raise ExperimentConflict("No frozen version found", code="experiment_not_frozen")

        candidates = session.scalars(
            select(ExperimentVersionCandidateRecord).where(
                ExperimentVersionCandidateRecord.experiment_version_id == version.id,
            ).order_by(ExperimentVersionCandidateRecord.position)
        ).all()

        snapshot_entries = []
        for cand in candidates:
            binding = session.scalar(
                select(ExperimentAdBindingRecord).where(
                    ExperimentAdBindingRecord.experiment_version_candidate_id == cand.id,
                )
            )
            reasons: list[str] = []
            rows: list[dict] = []

            if binding is None:
                reasons.append("missing_ad_binding")
            else:
                metrics = session.scalars(
                    select(AdDailyMetricRecord).where(
                        AdDailyMetricRecord.ad_id == binding.ad_id,
                        AdDailyMetricRecord.metric_date <= cutoff,
                    ).order_by(AdDailyMetricRecord.metric_date)
                ).all()
                rows = [{
                    "date": row.metric_date.isoformat(),
                    "spend": str(row.spend),
                    "impressions": row.impressions,
                    "clicks": row.clicks,
                    "purchases": str(row.purchases),
                    "purchase_value": str(row.purchase_value),
                } for row in metrics]

            snapshot_entries.append({
                "id": cand.id,
                "position": cand.position,
                "ad_id": binding.ad_id if binding else None,
                "data_quality_reasons": reasons,
                "daily": rows,
            })

        input_snapshot = {
            "schema_version": 2,
            "algorithm_version": ALGORITHM_VERSION,
            "experiment": {
                "id": experiment.id,
                "frozen_snapshot_hash": version.frozen_snapshot_hash,
                "config": version.statistical_plan_json,
            },
            "as_of": cutoff.isoformat(),
            "entries": snapshot_entries,
        }
        input_snapshot["snapshot_hash"] = canonical_hash(input_snapshot)
        result = judge(input_snapshot)

        existing_j = session.scalar(
            select(JudgementRecord).where(
                JudgementRecord.experiment_id == experiment_id,
                JudgementRecord.snapshot_hash == input_snapshot["snapshot_hash"],
                JudgementRecord.algorithm_version == ALGORITHM_VERSION,
            )
        )
        if existing_j:
            existing_p = session.scalar(
                select(DecisionProposalRecord).where(
                    DecisionProposalRecord.judgement_id == existing_j.id,
                )
            )
            return {
                "judgement": _judgement_dict(existing_j),
                "proposal_id": existing_p.id if existing_p else None,
            }

        judgement = JudgementRecord(
            id=new_id("judge"),
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            experiment_version_id=version.id,
            algorithm_version=ALGORITHM_VERSION,
            as_of=cutoff,
            snapshot_hash=input_snapshot["snapshot_hash"],
            input_snapshot_json=input_snapshot,
            result_json=result,
            record_schema_version=2,
        )
        session.add(judgement)
        session.flush()

        eligible, elig_evidence = is_effectively_eligible(
            session,
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            experiment_version_id=version.id,
        )
        elig_context = _canonical_json({
            "frozen_hash": version.frozen_snapshot_hash,
            "eligibility": elig_evidence,
        })
        elig_context_hash = _sha256(elig_context.encode("utf-8"))

        proposed_entries = []
        for item in result.get("entries", []):
            proposed_entries.append({
                "entry_id": item["entry_id"],
                "verdict": item["verdict"],
                "metric": item["metric"],
                "estimate": item.get("estimate"),
                "interval_low": item.get("interval_low"),
                "interval_high": item.get("interval_high"),
                "reason_codes": item.get("reason_codes", []),
            })

        proposal_content = _canonical_json({
            "judgement_id": judgement.id,
            "eligibility_context_hash": elig_context_hash,
            "proposed_entries": proposed_entries,
        })
        proposal_hash = _sha256(proposal_content.encode("utf-8"))

        proposal = DecisionProposalRecord(
            id=new_id("prop"),
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            judgement_id=judgement.id,
            experiment_version_id=version.id,
            eligibility_context_hash=elig_context_hash,
            proposal_hash=proposal_hash,
            proposed_entries_json=proposed_entries,
            evidence_json={"eligibility": elig_evidence},
        )
        session.add(proposal)
        session.flush()

        session.add(DecisionProposalStateEventRecord(
            id=new_id("dpse"),
            workspace_id=context.workspace_id,
            proposal_id=proposal.id,
            event_type="created",
            sequence=1,
        ))

        if not eligible:
            session.add(DecisionProposalStateEventRecord(
                id=new_id("dpse"),
                workspace_id=context.workspace_id,
                proposal_id=proposal.id,
                event_type="invalidated",
                sequence=2,
                reason="born_invalid",
            ))

        append_event(
            session,
            event_type="experiment.judged",
            resource_type="experiment",
            resource_id=experiment_id,
            details={
                "judgement_id": judgement.id,
                "proposal_id": proposal.id,
                "snapshot_hash": input_snapshot["snapshot_hash"],
            },
        )
        session.flush()
        return {
            "judgement": _judgement_dict(judgement),
            "proposal_id": proposal.id,
            "proposal_state": "created" if eligible else "invalidated",
        }


def confirm_proposal_v2(
    context: TenantContext,
    experiment_id: str,
    proposal_id: str,
    *,
    expected_proposal_sequence: int,
    idempotency_key_hash: str = "",
) -> dict:
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)
        _require_state(experiment, "running")

        proposal = session.get(DecisionProposalRecord, proposal_id)
        if proposal is None or proposal.workspace_id != context.workspace_id:
            raise ExperimentNotFound("Proposal not found", code="experiment_proposal_not_found")
        if proposal.experiment_id != experiment_id:
            raise ExperimentConflict("Proposal does not belong to this experiment", code="experiment_proposal_mismatch")

        existing_conf = session.scalar(
            select(DecisionConfirmationRecord).where(
                DecisionConfirmationRecord.proposal_id == proposal_id,
                DecisionConfirmationRecord.workspace_id == context.workspace_id,
            )
        )
        if existing_conf is not None:
            decisions = session.scalars(
                select(ExperimentDecisionRecord).where(
                    ExperimentDecisionRecord.confirmation_id == existing_conf.id,
                )
            ).all()
            return {
                "confirmation_id": existing_conf.id,
                "decisions": [_decision_dict(d) for d in decisions],
                "idempotent_replay": True,
            }

        latest_event = session.scalar(
            select(DecisionProposalStateEventRecord)
            .where(DecisionProposalStateEventRecord.proposal_id == proposal_id)
            .order_by(DecisionProposalStateEventRecord.sequence.desc())
            .limit(1)
        )
        if latest_event is None:
            raise ExperimentConflict("Proposal has no state events")

        if latest_event.event_type == "consumed":
            raise ExperimentConflict("proposal_already_consumed")
        if latest_event.event_type == "invalidated":
            raise ExperimentConflict("proposal_invalidated")
        if latest_event.event_type == "superseded":
            raise ExperimentConflict("proposal_superseded")

        if latest_event.sequence != expected_proposal_sequence:
            raise ExperimentConflict("sequence_conflict")

        version = session.scalar(
            select(ExperimentVersionRecord).where(
                ExperimentVersionRecord.id == proposal.experiment_version_id,
            )
        )
        if version is None:
            raise ExperimentConflict("Version not found")

        eligible, elig_evidence = is_effectively_eligible(
            session,
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            experiment_version_id=version.id,
        )
        if not eligible:
            raise ExperimentConflict("Experiment is not eligible for confirmation", code="experiment_not_confirmable")

        has_material = any(
            e["verdict"] != "inconclusive"
            for e in proposal.proposed_entries_json
        )
        if not has_material:
            raise ExperimentConflict("no_material_verdicts")

        now = utcnow()
        actor_id = context.initiator_id or context.executor

        confirmation = DecisionConfirmationRecord(
            id=new_id("conf"),
            workspace_id=context.workspace_id,
            proposal_id=proposal_id,
            proposal_hash=proposal.proposal_hash,
            expected_proposal_sequence=expected_proposal_sequence,
            idempotency_key_hash=idempotency_key_hash or _sha256(new_id("idem").encode("utf-8")),
            request_fingerprint_hash=_sha256(
                f"{proposal_id}:{expected_proposal_sequence}".encode("utf-8")
            ),
            actor_id=actor_id,
            role=context.role,
            authorization_epoch=0,
            authorization_evidence_json={"role": context.role},
            confirmed_at=now,
        )
        session.add(confirmation)
        session.flush()

        session.add(DecisionProposalStateEventRecord(
            id=new_id("dpse"),
            workspace_id=context.workspace_id,
            proposal_id=proposal_id,
            event_type="consumed",
            sequence=latest_event.sequence + 1,
            actor_id=actor_id,
        ))

        context_hash = _sha256(
            _canonical_json({
                "experiment_id": experiment_id,
                "offer_version_id": version.offer_version_id,
                "market": version.market,
                "placement": version.placement,
                "landing_page_evidence_hash": version.landing_page_evidence_hash,
            }).encode("utf-8")
        )

        decisions = []
        for entry in proposal.proposed_entries_json:
            if entry["verdict"] == "inconclusive":
                continue

            candidate = session.get(ExperimentVersionCandidateRecord, entry["entry_id"])
            if candidate is None:
                raise ExperimentConflict("Candidate not found for decision")

            decision = ExperimentDecisionRecord(
                id=new_id("decision"),
                workspace_id=context.workspace_id,
                experiment_id=experiment_id,
                judgement_id=proposal.judgement_id,
                experiment_entry_id=candidate.source_entry_id,
                experiment_version_id=version.id,
                experiment_version_candidate_id=candidate.id,
                proposal_id=proposal_id,
                confirmation_id=confirmation.id,
                verdict=entry["verdict"],
                metric=entry["metric"],
                estimate=Decimal(str(entry["estimate"])) if entry.get("estimate") is not None else None,
                interval_low=Decimal(str(entry["interval_low"])) if entry.get("interval_low") is not None else None,
                interval_high=Decimal(str(entry["interval_high"])) if entry.get("interval_high") is not None else None,
                reason_codes_json=entry.get("reason_codes", []),
                actor_id=actor_id,
                confirmed_at=now,
                context_hash=context_hash,
                decision_schema_version=2,
            )
            session.add(decision)
            decisions.append(decision)

        session.flush()

        for decision in decisions:
            cand = session.get(ExperimentVersionCandidateRecord, decision.experiment_version_candidate_id)

            existing_proj = session.scalar(
                select(ContextualVerdictProjectionRecord).where(
                    ContextualVerdictProjectionRecord.workspace_id == context.workspace_id,
                    ContextualVerdictProjectionRecord.experiment_id == experiment_id,
                    ContextualVerdictProjectionRecord.asset_version_id == (cand.asset_version_id if cand else ""),
                    ContextualVerdictProjectionRecord.offer_version_id == version.offer_version_id,
                    ContextualVerdictProjectionRecord.market == version.market,
                    ContextualVerdictProjectionRecord.placement == version.placement,
                    ContextualVerdictProjectionRecord.landing_page_evidence_hash == version.landing_page_evidence_hash,
                )
            )
            if existing_proj:
                if now >= existing_proj.confirmed_at:
                    session.execute(
                        update(ContextualVerdictProjectionRecord)
                        .where(ContextualVerdictProjectionRecord.id == existing_proj.id)
                        .values(
                            candidate_id=decision.experiment_version_candidate_id or "",
                            decision_id=decision.id,
                            confirmation_id=confirmation.id,
                            verdict=decision.verdict,
                            metric=decision.metric,
                            estimate=decision.estimate,
                            confirmed_at=now,
                            updated_at=now,
                        )
                    )
            else:
                session.add(ContextualVerdictProjectionRecord(
                    id=new_id("cvp"),
                    workspace_id=context.workspace_id,
                    experiment_id=experiment_id,
                    experiment_version_id=version.id,
                    candidate_id=decision.experiment_version_candidate_id or "",
                    decision_id=decision.id,
                    confirmation_id=confirmation.id,
                    verdict=decision.verdict,
                    metric=decision.metric,
                    estimate=decision.estimate,
                    asset_version_id=cand.asset_version_id if cand else "",
                    product_version_id=version.product_version_id,
                    offer_version_id=version.offer_version_id,
                    market=version.market,
                    placement=version.placement,
                    landing_page_evidence_hash=version.landing_page_evidence_hash,
                    confirmed_at=now,
                ))

        tl_event = append_timeline_event(
            session,
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            event_type="proposal_confirmed",
            source_resource_type="decision_confirmation",
            source_resource_id=confirmation.id,
            actor_id=actor_id,
            summary={
                "proposal_id": proposal_id,
                "confirmation_id": confirmation.id,
                "decision_count": len(decisions),
            },
        )

        append_notification(
            session,
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            timeline_event_id=tl_event.id,
            notification_type="proposal_confirmed",
            payload={
                "experiment_id": experiment_id,
                "proposal_id": proposal_id,
                "confirmation_id": confirmation.id,
                "decision_count": len(decisions),
            },
        )

        append_event(
            session,
            event_type="experiment.confirmed",
            resource_type="experiment",
            resource_id=experiment_id,
            details={
                "confirmation_id": confirmation.id,
                "proposal_id": proposal_id,
                "decision_count": len(decisions),
            },
        )
        session.flush()
        return {
            "confirmation_id": confirmation.id,
            "decisions": [_decision_dict(d) for d in decisions],
        }


def close_experiment_v2(
    context: TenantContext,
    experiment_id: str,
    expected_revision: int,
    *,
    closure_kind: str,
    reason: str = "",
    evidence: dict | None = None,
) -> dict:
    valid_kinds = {"completed", "abandoned_before_start", "aborted", "invalidated"}
    if closure_kind not in valid_kinds:
        raise ExperimentError(f"closure_kind must be one of {sorted(valid_kinds)}")

    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)
        _check_revision(experiment, expected_revision)

        if experiment.state == "closed":
            return _experiment_dict_v2(session, experiment)

        if closure_kind == "completed":
            _require_state(experiment, "running")
            j_count = session.scalar(
                select(func.count()).select_from(JudgementRecord).where(
                    JudgementRecord.experiment_id == experiment_id,
                )
            )
            if j_count == 0:
                raise ExperimentConflict("no_judgements")

        elif closure_kind == "abandoned_before_start":
            _require_state(experiment, "frozen")

        elif closure_kind == "aborted":
            _require_state(experiment, "frozen", "running")

        elif closure_kind == "invalidated":
            _require_state(experiment, "frozen", "running")
            version = session.scalar(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.experiment_id == experiment_id,
                    ExperimentVersionRecord.workspace_id == context.workspace_id,
                )
            )
            if version is not None:
                proj = session.scalar(
                    select(ExperimentEligibilityProjectionRecord).where(
                        ExperimentEligibilityProjectionRecord.experiment_version_id == version.id,
                        ExperimentEligibilityProjectionRecord.level == "version",
                        ExperimentEligibilityProjectionRecord.irreversible_active.is_(True),
                    )
                )
                if proj is None:
                    raise ExperimentConflict("no_active_invalidation")
            else:
                raise ExperimentConflict("no_active_invalidation")

        now = utcnow()
        from_state = experiment.state
        new_rev = expected_revision + 1

        result = session.execute(
            update(ExperimentRecord)
            .where(
                ExperimentRecord.id == experiment_id,
                ExperimentRecord.revision == expected_revision,
            )
            .values(state="closed", revision=new_rev, closed_at=now)
        )
        if result.rowcount != 1:
            raise ExperimentConflict("实验被并发修改", code="revision_conflict")

        session.add(ExperimentStateEventRecord(
            id=new_id("expev"),
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            from_state=from_state,
            to_state="closed",
            revision=new_rev,
            actor_id=context.initiator_id or context.executor,
            reason=f"closed:{closure_kind}",
            evidence_json={
                "closure_kind": closure_kind,
                "reason": reason,
                **(evidence or {}),
            },
        ))
        append_event(
            session,
            event_type="experiment.closed",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"revision": new_rev, "closure_kind": closure_kind},
        )
        session.flush()
        session.refresh(experiment)
        return _experiment_dict_v2(session, experiment)


def create_next_round(
    context: TenantContext,
    experiment_id: str,
    decision_id: str,
    *,
    intent_kind: str,
    prompt_overrides: dict | None = None,
    qc_attempt_id: str | None = None,
    expected_revision: int,
) -> dict:
    valid_intents = {"same_recipe", "new_seed", "edit_prompt", "quality_feedback"}
    if intent_kind not in valid_intents:
        raise ExperimentError(f"intent_kind must be one of {sorted(valid_intents)}")

    if intent_kind == "quality_feedback" and not qc_attempt_id:
        raise ExperimentError("qc_attempt_id is required for quality_feedback")
    if intent_kind != "quality_feedback" and qc_attempt_id:
        raise ExperimentError("qc_attempt_id is forbidden for non-quality_feedback intents")

    from ..generation.regeneration import create_regeneration_draft

    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)
        _require_state(experiment, "closed")
        _check_revision(experiment, expected_revision)

        decision = session.get(ExperimentDecisionRecord, decision_id)
        if decision is None or decision.workspace_id != context.workspace_id:
            raise ExperimentNotFound("Decision not found", code="experiment_decision_not_found")
        if decision.experiment_id != experiment_id:
            raise ExperimentNotFound("Decision does not belong to this experiment", code="experiment_decision_not_found")
        if decision.verdict == "inconclusive":
            raise ExperimentError("Decision is not material (inconclusive)", code="experiment_decision_not_material")

        candidate = session.get(ExperimentVersionCandidateRecord, decision.experiment_version_candidate_id)
        if candidate is None:
            raise ExperimentError("Source candidate not found")

        if qc_attempt_id:
            from ..generation.models import AssetQCAttemptRecord
            qc_attempt = session.get(AssetQCAttemptRecord, qc_attempt_id)
            if qc_attempt is None or qc_attempt.workspace_id != context.workspace_id:
                raise ExperimentNotFound("QC attempt not found", code="qc_attempt_not_found")
            if qc_attempt.asset_version_id != candidate.asset_version_id:
                raise ExperimentError("QC attempt does not match candidate's AssetVersion", code="qc_attempt_mismatch")

        draft = create_regeneration_draft(
            session, context,
            source_version_id=candidate.asset_version_id,
            intent_kind=intent_kind,
            prompt_overrides=prompt_overrides,
            qc_attempt_id=qc_attempt_id,
        )

        actor_id = context.initiator_id or context.executor
        record = ExperimentNextRoundRecord(
            id=new_id("enrr"),
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            experiment_version_id=decision.experiment_version_id,
            decision_id=decision_id,
            source_candidate_id=candidate.id,
            source_asset_version_id=candidate.asset_version_id,
            generation_draft_id=draft.id,
            intent_kind=intent_kind,
            qc_attempt_id=qc_attempt_id,
            actor_id=actor_id,
        )
        session.add(record)
        session.flush()

        tl_event = append_timeline_event(
            session,
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            event_type="next_round_created",
            source_resource_type="next_round_record",
            source_resource_id=record.id,
            actor_id=actor_id,
            summary={"decision_id": decision_id, "intent_kind": intent_kind},
        )

        append_notification(
            session,
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            timeline_event_id=tl_event.id,
            notification_type="next_round_created",
            payload={
                "experiment_id": experiment_id,
                "decision_id": decision_id,
                "next_round_id": record.id,
                "intent_kind": intent_kind,
            },
        )

        append_event(
            session,
            event_type="experiment.next_round",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"next_round_id": record.id, "decision_id": decision_id},
        )
        session.flush()
        return {
            "id": record.id,
            "experiment_id": experiment_id,
            "decision_id": decision_id,
            "generation_draft_id": draft.id,
            "intent_kind": intent_kind,
            "source_candidate_id": candidate.id,
            "source_asset_version_id": candidate.asset_version_id,
        }


def create_pattern_candidate(
    context: TenantContext,
    experiment_id: str,
    decision_id: str,
    *,
    expected_revision: int,
) -> dict:
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)
        _require_state(experiment, "closed")
        _check_revision(experiment, expected_revision)

        decision = session.get(ExperimentDecisionRecord, decision_id)
        if decision is None or decision.workspace_id != context.workspace_id:
            raise ExperimentNotFound("Decision not found", code="experiment_decision_not_found")
        if decision.experiment_id != experiment_id:
            raise ExperimentNotFound("Decision does not belong to this experiment", code="experiment_decision_not_found")
        if decision.verdict == "inconclusive":
            raise ExperimentError("Decision is not material (inconclusive)", code="experiment_decision_not_material")

        existing = session.scalar(
            select(ExperimentPatternCandidateRecord).where(
                ExperimentPatternCandidateRecord.workspace_id == context.workspace_id,
                ExperimentPatternCandidateRecord.decision_id == decision_id,
            )
        )
        if existing:
            return _pattern_candidate_dict(existing)

        candidate = session.get(ExperimentVersionCandidateRecord, decision.experiment_version_candidate_id)
        if candidate is None:
            raise ExperimentError("Source candidate not found")

        concepts = session.scalars(
            select(ExperimentVersionConceptRecord).where(
                ExperimentVersionConceptRecord.experiment_version_id == candidate.experiment_version_id,
                ExperimentVersionConceptRecord.experiment_version_candidate_id == candidate.id,
            )
        ).all()
        concept_count = max(len(concepts), 1)

        evidence = {
            "experiment_id": experiment_id,
            "decision_id": decision_id,
            "candidate_id": candidate.id,
            "verdict": decision.verdict,
            "concepts_hash": candidate.concepts_hash,
        }
        evidence_json = _canonical_json(evidence)
        evidence_hash = _sha256(evidence_json.encode("utf-8"))

        actor_id = context.initiator_id or context.executor
        record = ExperimentPatternCandidateRecord(
            id=new_id("epc"),
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            experiment_version_id=decision.experiment_version_id,
            decision_id=decision_id,
            candidate_id=candidate.id,
            source_concepts_hash=candidate.concepts_hash,
            concept_count=concept_count,
            execution_mode=candidate.execution_mode or "mock",
            verdict=decision.verdict,
            pattern_evidence_json=evidence,
            pattern_evidence_hash=evidence_hash,
            actor_id=actor_id,
        )
        session.add(record)
        session.flush()

        append_timeline_event(
            session,
            workspace_id=context.workspace_id,
            experiment_id=experiment_id,
            event_type="pattern_candidate_created",
            source_resource_type="pattern_candidate",
            source_resource_id=record.id,
            actor_id=actor_id,
            summary={"decision_id": decision_id, "candidate_id": candidate.id},
        )

        append_event(
            session,
            event_type="experiment.pattern_candidate",
            resource_type="experiment",
            resource_id=experiment_id,
            details={"pattern_candidate_id": record.id, "decision_id": decision_id},
        )
        session.flush()
        return _pattern_candidate_dict(record)


def _pattern_candidate_dict(row: ExperimentPatternCandidateRecord) -> dict:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "decision_id": row.decision_id,
        "candidate_id": row.candidate_id,
        "source_concepts_hash": row.source_concepts_hash,
        "concept_count": row.concept_count,
        "execution_mode": row.execution_mode,
        "verdict": row.verdict,
    }


def _decision_dict(row: ExperimentDecisionRecord) -> dict:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "verdict": row.verdict,
        "metric": row.metric,
        "estimate": str(row.estimate) if row.estimate is not None else None,
        "interval_low": str(row.interval_low) if row.interval_low is not None else None,
        "interval_high": str(row.interval_high) if row.interval_high is not None else None,
        "candidate_id": row.experiment_version_candidate_id,
        "proposal_id": row.proposal_id,
        "confirmation_id": row.confirmation_id,
        "reason_codes": row.reason_codes_json or [],
        "confirmed_at": row.confirmed_at,
    }


def _verify_resolution_invariant(
    version_record: ExperimentVersionRecord,
    resolution_records: list[ExperimentVersionResolutionRecord],
) -> None:
    """Three-way consistency check (spec §1.6): rows, hash, and snapshot must agree."""
    # 1. Recompute hash from normalized resolution rows.
    canonical_rows = [
        {
            "duplicate_group_hash": r.duplicate_group_hash,
            "dimension_key": r.dimension_key,
            "concept_key_id": r.concept_key_id,
            "source_candidate_fingerprints": (
                r.source_candidate_fingerprints_json or {}
            ).get("fingerprints", []),
            "action": r.action,
            "outcome": r.outcome_evidence_json or {},
            "reason": r.reason,
        }
        for r in sorted(resolution_records, key=lambda r: r.duplicate_group_hash)
    ]
    recomputed_hash = _hash_json(canonical_rows)

    # 2. Compare to stored hash.
    if recomputed_hash != version_record.concept_resolutions_hash:
        logger.error(
            "Resolution invariant violation: recomputed hash %s != stored hash %s "
            "for version %s",
            recomputed_hash, version_record.concept_resolutions_hash, version_record.id,
        )
        raise ExperimentError("resolution_integrity_violation")

    # 3. Compare snapshot plan hash.
    snapshot = version_record.frozen_snapshot_json or {}
    snapshot_plan = snapshot.get("concept_resolution_plan", [])
    snapshot_hash = _hash_json(snapshot_plan) if snapshot_plan else _hash_json([])
    if snapshot_hash != version_record.concept_resolutions_hash:
        logger.error(
            "Resolution invariant violation: snapshot plan hash %s != stored hash %s "
            "for version %s",
            snapshot_hash, version_record.concept_resolutions_hash, version_record.id,
        )
        raise ExperimentError("resolution_integrity_violation")


def _experiment_dict_v2(session: Session, experiment: ExperimentRecord) -> dict:
    base = {
        "id": experiment.id,
        "product_id": experiment.product_id,
        "name": experiment.name,
        "state": experiment.state,
        "revision": experiment.revision,
        "record_schema_version": experiment.record_schema_version,
        "draft_context": experiment.config_json,
        "frozen_snapshot_hash": experiment.frozen_snapshot_hash,
        "created_at": experiment.created_at,
        "frozen_at": experiment.frozen_at,
        "started_at": experiment.started_at,
        "closed_at": experiment.closed_at,
    }

    version = session.scalar(
        select(ExperimentVersionRecord).where(
            ExperimentVersionRecord.experiment_id == experiment.id,
        )
    )
    if version:
        base["version_id"] = version.id
        base["frozen_context"] = {
            "product_version_id": version.product_version_id,
            "offer_version_id": version.offer_version_id,
            "market": version.market,
            "placement": version.placement,
            "landing_page_evidence_hash": version.landing_page_evidence_hash,
            "audience": version.audience_snapshot_json,
            "daily_budget_amount": str(version.daily_budget_amount),
            "total_budget_cap": str(version.total_budget_cap),
            "budget_currency": version.budget_currency,
            "schedule_start_at": version.schedule_start_at,
            "schedule_end_at": version.schedule_end_at,
            "schedule_timezone": version.schedule_timezone,
            "attribution_click_window_days": version.attribution_click_window_days,
            "attribution_view_window_days": version.attribution_view_window_days,
            "statistical_plan": version.statistical_plan_json,
        }
        candidates = session.scalars(
            select(ExperimentVersionCandidateRecord).where(
                ExperimentVersionCandidateRecord.experiment_version_id == version.id,
            ).order_by(ExperimentVersionCandidateRecord.position)
        ).all()
        bindings = {
            binding.experiment_version_candidate_id: binding
            for binding in session.scalars(
                select(ExperimentAdBindingRecord).where(
                    ExperimentAdBindingRecord.workspace_id == experiment.workspace_id,
                    ExperimentAdBindingRecord.experiment_version_candidate_id.in_(
                        [candidate.id for candidate in candidates]
                    ),
                )
            ).all()
        } if candidates else {}
        base["candidates"] = []
        for candidate in candidates:
            binding = bindings.get(candidate.id)
            base["candidates"].append({
                "id": candidate.id,
                "position": candidate.position,
                "asset_version_id": candidate.asset_version_id,
                "candidate_hash": candidate.candidate_hash,
                "execution_mode": candidate.execution_mode,
                "marketing_vars": candidate.marketing_vars_json or {},
                "binding": None if binding is None else {
                    "ad_account_id": binding.ad_account_id,
                    "ad_id": binding.ad_id,
                    "source": binding.source,
                },
            })

        resolution_records = list(session.scalars(
            select(ExperimentVersionResolutionRecord).where(
                ExperimentVersionResolutionRecord.experiment_version_id == version.id,
            ).order_by(ExperimentVersionResolutionRecord.duplicate_group_hash)
        ).all())

        concept_records_by_key: dict[str, ExperimentVersionConceptRecord] = {}
        for cr in session.scalars(
            select(ExperimentVersionConceptRecord).where(
                ExperimentVersionConceptRecord.experiment_version_id == version.id,
            )
        ).all():
            concept_records_by_key[cr.concept_key_id] = cr

        evidence_rows: list[dict[str, Any]] = []
        for res in resolution_records:
            outcome = res.outcome_evidence_json or {}
            concept_rec = concept_records_by_key.get(res.concept_key_id)
            concept_key_name = concept_rec.concept_key_name if concept_rec else res.concept_key_id

            affected_fp = None
            remap_dest_id = None
            remap_dest_name = None
            if res.action == "exclude_source_candidate":
                excluded = outcome.get("excluded_source_fingerprints", [])
                affected_fp = excluded[0] if excluded else None
            elif res.action == "remap_before_freeze":
                affected_fp = outcome.get("remapped_source_fingerprint")
                remap_dest_id = outcome.get("to_concept_key_id")
                remap_dest_name = outcome.get("to_concept_key_name")

            authorization_evidence = res.authorization_evidence_json or {}
            evidence_rows.append({
                "duplicate_group_hash": res.duplicate_group_hash,
                "dimension_key": res.dimension_key,
                "concept_key_id": res.concept_key_id,
                "concept_key_name": concept_key_name,
                "action": res.action,
                "reason": res.reason,
                "affected_candidate_fingerprint": affected_fp,
                "remap_destination_concept_key_id": remap_dest_id,
                "remap_destination_concept_key_name": remap_dest_name,
                "actor_role": authorization_evidence.get("role") or res.actor_id,
                "resolved_at": res.resolved_at,
            })

        # Three-way invariant check (spec §1.6): normalized DB rows, the
        # stored concept_resolutions_hash, and the frozen snapshot's
        # concept_resolution_plan must all agree.
        _verify_resolution_invariant(version, resolution_records)

        base["concept_resolution_evidence"] = {
            "concept_resolutions_hash": version.concept_resolutions_hash,
            "had_duplicates": len(resolution_records) > 0,
            "resolutions": evidence_rows,
        }

    entries = session.scalars(
        select(ExperimentEntryRecord).where(
            ExperimentEntryRecord.experiment_id == experiment.id,
            ExperimentEntryRecord.removed_at.is_(None),
        ).order_by(ExperimentEntryRecord.position)
    ).all()
    base["entries"] = [{
        "id": e.id,
        "position": e.position,
        "asset_version_id": e.asset_version_id,
        "marketing_vars": e.marketing_vars_json,
    } for e in entries]

    judgements = session.scalars(
        select(JudgementRecord)
        .where(
            JudgementRecord.workspace_id == experiment.workspace_id,
            JudgementRecord.experiment_id == experiment.id,
        )
        .order_by(JudgementRecord.created_at.desc())
    ).all()
    base["judgements"] = [_judgement_dict(row) for row in judgements]

    proposals = session.scalars(
        select(DecisionProposalRecord)
        .where(
            DecisionProposalRecord.workspace_id == experiment.workspace_id,
            DecisionProposalRecord.experiment_id == experiment.id,
        )
        .order_by(DecisionProposalRecord.created_at.desc())
    ).all()
    proposal_items = []
    for proposal in proposals:
        latest_state = session.scalar(
            select(DecisionProposalStateEventRecord)
            .where(
                DecisionProposalStateEventRecord.workspace_id == experiment.workspace_id,
                DecisionProposalStateEventRecord.proposal_id == proposal.id,
            )
            .order_by(DecisionProposalStateEventRecord.sequence.desc())
            .limit(1)
        )
        proposal_items.append({
            "id": proposal.id,
            "judgement_id": proposal.judgement_id,
            "proposal_hash": proposal.proposal_hash,
            "state": latest_state.event_type if latest_state else "unknown",
            "sequence": latest_state.sequence if latest_state else 0,
            "proposed_entries": proposal.proposed_entries_json or [],
            "evidence": proposal.evidence_json or {},
            "created_at": proposal.created_at,
        })
    base["proposals"] = proposal_items

    decisions = session.scalars(
        select(ExperimentDecisionRecord)
        .where(
            ExperimentDecisionRecord.workspace_id == experiment.workspace_id,
            ExperimentDecisionRecord.experiment_id == experiment.id,
            ExperimentDecisionRecord.confirmation_id.is_not(None),
        )
        .order_by(ExperimentDecisionRecord.created_at.desc())
    ).all()
    base["decisions"] = [_decision_dict(row) for row in decisions]

    return base
