"""Meta read-only synchronization orchestration and projections.

The service owns the durable state machine.  HTTP is a replaceable adapter;
raw pages are the source of truth and SQL rows are rebuildable projections.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from .. import config
from ..adapters.meta.client import MetaClient
from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import (
    AdDailyMetricRecord,
    MetaAccountRecord,
    MetaAdBindingRecord,
    MetaEntityStatusEventRecord,
    MetaIngestionPageRecord,
    MetaIngestionRecord,
    MetaSyncRunRecord,
    MetaSyncScheduleRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from ..platform import jobs
from ..platform.audit import append_event
from ..platform.tenancy import TenantContext, TenantScopeError, current_context
from .contracts import INGESTION_KINDS, contract_hash, metric_contract, request_snapshot


class MetaConfigurationError(RuntimeError):
    pass


class MetaIngestionNotReady(RuntimeError):
    """A safe transient dependency, such as account metadata not projected yet."""


# DEPRECATED: use per-workspace credential resolution instead. See Issue #23 for migration path.
def require_meta_credential_workspace(context: TenantContext) -> None:
    """Bind deployment-global Meta credentials to exactly one Workspace."""
    bound = config.META_CREDENTIAL_WORKSPACE_ID
    if not bound:
        raise MetaConfigurationError(
            "META_CREDENTIAL_WORKSPACE_ID 未配置；全局 Meta 凭证不能作为多租户默认值"
        )
    if bound != context.workspace_id:
        raise MetaConfigurationError(
            "Meta 凭证未绑定到当前 Workspace；拒绝跨租户复用广告账户"
        )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _redact_secrets(value: Any) -> Any:
    """Remove credentials from paging URLs while retaining replayable payload data."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in {"access_token", "appsecret_proof"} else _redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str) and ("access_token=" in value or "appsecret_proof=" in value):
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(value)
        safe_query = [(key, "[REDACTED]" if key in {"access_token", "appsecret_proof"} else item)
                      for key, item in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), parts.fragment))
    return value


def _as_decimal(value: Any, default: Decimal | None = Decimal("0")) -> Decimal | None:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _as_int(value: Any) -> int:
    decimal = _as_decimal(value)
    return int(decimal or 0)


def _action_value(items: Any, action_type: str) -> Decimal:
    """Exact action_type matching prevents purchase-like events from being merged."""
    total = Decimal("0")
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("action_type") == action_type:
            total += _as_decimal(item.get("value")) or Decimal("0")
    return total


def _next_daily_at(timezone_name: str, local_hour: int, now: datetime | None = None) -> datetime:
    now = now or utcnow()
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    local_now = now.astimezone(tz)
    hour = min(23, max(0, local_hour))
    candidate = datetime.combine(local_now.date(), time(hour=hour), tzinfo=tz)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _ensure_workspace_and_account(session) -> MetaAccountRecord:
    context = current_context(session)
    workspace = session.get(WorkspaceRecord, context.workspace_id)
    if workspace is None:
        if session.get_bind().dialect.name != "sqlite":
            raise TenantScopeError("the Workspace projection has not been provisioned")
        workspace = WorkspaceRecord(
            id=context.workspace_id,
            name="Default Workspace",
            default_currency=config.DEFAULT_CURRENCY,
            authorization_epoch=context.authorization_epoch,
        )
        session.add(workspace)
        session.flush()
    elif workspace.authorization_epoch != context.authorization_epoch:
        raise TenantScopeError("the TenantContext authorization epoch is stale")
    account = session.scalar(
        select(MetaAccountRecord).where(
            MetaAccountRecord.workspace_id == context.workspace_id,
            MetaAccountRecord.ad_account_id == config.META_AD_ACCOUNT_ID,
        )
    )
    if account is None:
        account = MetaAccountRecord(
            id=new_id("mact"),
            workspace_id=context.workspace_id,
            ad_account_id=config.META_AD_ACCOUNT_ID,
            currency=config.DEFAULT_CURRENCY,
            timezone_name=config.META_TIMEZONE,
            api_version=config.META_API_VERSION,
        )
        session.add(account)
        session.flush()
    return account


def _get_sync(session, context: TenantContext, sync_id: str) -> MetaSyncRunRecord | None:
    return session.scalar(
        select(MetaSyncRunRecord).where(
            MetaSyncRunRecord.id == sync_id,
            MetaSyncRunRecord.workspace_id == context.workspace_id,
        )
    )


def _get_ingestion(
    session, context: TenantContext, ingestion_id: str
) -> MetaIngestionRecord | None:
    return session.scalar(
        select(MetaIngestionRecord)
        .join(MetaSyncRunRecord)
        .where(
            MetaIngestionRecord.id == ingestion_id,
            MetaSyncRunRecord.workspace_id == context.workspace_id,
        )
    )


def ensure_schedule(context: TenantContext) -> None:
    if not config.meta_credentials_ready():
        return
    require_meta_credential_workspace(context)
    with tenant_session(context) as session:
        account = _ensure_workspace_and_account(session)
        schedule = session.scalar(select(MetaSyncScheduleRecord).where(MetaSyncScheduleRecord.account_id == account.id))
        if schedule is None:
            schedule = MetaSyncScheduleRecord(
                id=new_id("msched"),
                workspace_id=context.workspace_id,
                account_id=account.id,
                enabled=config.META_SYNC_ENABLED,
                timezone_name=account.timezone_name or config.META_TIMEZONE,
                local_hour=config.META_SYNC_HOUR,
                lookback_days=max(1, config.META_LOOKBACK_DAYS),
                next_run_at=_next_daily_at(account.timezone_name or config.META_TIMEZONE, config.META_SYNC_HOUR),
            )
            session.add(schedule)
        else:
            schedule.enabled = config.META_SYNC_ENABLED
            schedule.local_hour = config.META_SYNC_HOUR
            schedule.lookback_days = max(1, config.META_LOOKBACK_DAYS)
            schedule.updated_at = utcnow()


def request_sync(
    context: TenantContext,
    date_start: date | None = None,
    date_stop: date | None = None,
    *,
    force: bool = False,
) -> str:
    if not config.meta_credentials_ready():
        raise MetaConfigurationError("Meta 只读同步未配置完整：需要 API version、access token、app secret、ad account id")
    require_meta_credential_workspace(context)
    ensure_schedule(context)
    stop = date_stop or (utcnow().date() - timedelta(days=1))
    start = date_start or (stop - timedelta(days=max(1, config.META_LOOKBACK_DAYS) - 1))
    if start > stop:
        raise ValueError("date_start 不能晚于 date_stop")
    if (stop - start).days > 90:
        raise ValueError("单次同步最多 91 天，请拆分时间范围")

    contract = metric_contract()
    digest = contract_hash(contract)
    key_material = f"{config.META_AD_ACCOUNT_ID}:{start}:{stop}:{digest}"
    if force:
        key_material += f":{uuid.uuid4().hex}"
    idempotency_key = f"meta:{hashlib.sha256(key_material.encode('utf-8')).hexdigest()}"
    with tenant_session(context) as session:
        account = _ensure_workspace_and_account(session)
        existing = session.scalar(
            select(MetaSyncRunRecord).where(
                MetaSyncRunRecord.workspace_id == context.workspace_id,
                MetaSyncRunRecord.idempotency_key == idempotency_key,
            )
        )
        if existing:
            return existing.id
        sync = MetaSyncRunRecord(
            id=new_id("msync"),
            workspace_id=context.workspace_id,
            account_id=account.id,
            state="queued",
            date_start=start,
            date_stop=stop,
            contract_hash=digest,
            contract_json=contract,
            api_version=config.META_API_VERSION,
            account_currency=account.currency,
            account_timezone=account.timezone_name,
            idempotency_key=idempotency_key,
        )
        session.add(sync)
        session.flush()
        for kind in INGESTION_KINDS:
            ingestion = MetaIngestionRecord(
                id=new_id("ming"),
                sync_run_id=sync.id,
                kind=kind,
                state="queued",
                request_json=request_snapshot(kind, start, stop),
            )
            session.add(ingestion)
            session.flush()
            jobs.enqueue_in_session(
                session,
                context=context,
                job_type="meta_read_ingestion",
                payload={"ingestion_id": ingestion.id},
                idempotency_key=f"meta-ingestion:{ingestion.id}",
                resource_type="meta_sync",
                resource_id=sync.id,
                safe_to_retry_after_crash=True,
                max_attempts=8,
            )
        append_event(
            session,
            event_type="meta.sync_requested",
            resource_type="meta_sync",
            resource_id=sync.id,
            details={
                "date_start": start.isoformat(),
                "date_stop": stop.isoformat(),
                "contract_hash": digest,
                "forced": force,
            },
        )
        return sync.id


def _raw_path(account_id: str, ingestion_id: str, page_number: int, digest: str) -> Path:
    return config.META_RAW_DIR / account_id / ingestion_id / f"{page_number:06d}-{digest}.json.gz"


def _write_raw_page(account_id: str, ingestion_id: str, page_number: int, body: dict) -> tuple[Path, str]:
    safe_body = _redact_secrets(body)
    raw = _canonical_json(safe_body)
    digest = hashlib.sha256(raw).hexdigest()
    path = _raw_path(account_id, ingestion_id, page_number, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        with temp.open("wb") as handle:
            with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as archive:
                archive.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    return path, digest


def _read_raw_page(path: str) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("Meta raw page 顶层不是 JSON object")
    return value


async def process_ingestion(
    context: TenantContext, ingestion_id: str, client: MetaClient | None = None
) -> None:
    require_meta_credential_workspace(context)
    client = client or MetaClient()
    while True:
        with tenant_session(context) as session:
            ingestion = _get_ingestion(session, context, ingestion_id)
            if ingestion is None:
                raise KeyError(f"Meta ingestion 不存在: {ingestion_id}")
            sync = _get_sync(session, context, ingestion.sync_run_id)
            account = session.get(MetaAccountRecord, sync.account_id) if sync else None
            if sync is None or account is None:
                raise KeyError("Meta ingestion 的父记录不存在")
            if ingestion.state == "completed":
                return
            if ingestion.kind != "account":
                account_state = session.scalar(
                    select(MetaIngestionRecord.state).where(
                        MetaIngestionRecord.sync_run_id == sync.id,
                        MetaIngestionRecord.kind == "account",
                    )
                )
                if account_state != "completed":
                    raise MetaIngestionNotReady("等待 account 元数据完成后再投影指标与状态")
            ingestion.state = "fetching"
            ingestion.error_code = ""
            ingestion.error_detail = ""
            sync.state = "running"
            sync.started_at = sync.started_at or utcnow()
            fetch_complete = ingestion.fetch_complete
            request = dict(ingestion.request_json)
            after = ingestion.next_after_cursor
            page_number = ingestion.pages_fetched + 1
            account_db_id = account.id
            ad_account_id = account.ad_account_id

        if fetch_complete:
            break
        page = await client.fetch_page(request, after)
        path, digest = _write_raw_page(ad_account_id, ingestion_id, page_number, page.body)
        with tenant_session(context) as session:
            ingestion = _get_ingestion(session, context, ingestion_id)
            if ingestion is None:
                raise KeyError(f"Meta ingestion 不存在: {ingestion_id}")
            existing = session.scalar(
                select(MetaIngestionPageRecord).where(
                    MetaIngestionPageRecord.ingestion_id == ingestion_id,
                    MetaIngestionPageRecord.page_number == page_number,
                )
            )
            if existing is None:
                session.add(MetaIngestionPageRecord(
                    id=new_id("mpage"),
                    ingestion_id=ingestion_id,
                    page_number=page_number,
                    request_after_cursor=after,
                    response_after_cursor=page.after_cursor,
                    object_key=str(path),
                    response_sha256=digest,
                    row_count=len(page.data),
                    response_meta_json=page.usage_headers,
                ))
                ingestion.pages_fetched += 1
                ingestion.rows_fetched += len(page.data)
            elif existing.response_sha256 != digest:
                raise RuntimeError("同一 Meta 页码返回内容发生变化，拒绝覆盖原始页")
            ingestion.next_after_cursor = page.after_cursor
            ingestion.fetch_complete = page.after_cursor is None
        if page.after_cursor is None:
            break

    _project_ingestion(context, ingestion_id, account_db_id)
    _finalize_sync_for_ingestion(context, ingestion_id)


def _project_ingestion(context: TenantContext, ingestion_id: str, account_id: str) -> None:
    with tenant_session(context) as session:
        ingestion = _get_ingestion(session, context, ingestion_id)
        if ingestion is None:
            raise KeyError(f"Meta ingestion 不存在: {ingestion_id}")
        sync = _get_sync(session, context, ingestion.sync_run_id)
        if sync is None:
            raise KeyError("Meta ingestion 的父记录不存在")
        account = session.get(MetaAccountRecord, account_id)
        pages = session.scalars(
            select(MetaIngestionPageRecord)
            .where(MetaIngestionPageRecord.ingestion_id == ingestion_id)
            .order_by(MetaIngestionPageRecord.page_number)
        ).all()
        rows: list[dict] = []
        for page in pages:
            body = _read_raw_page(page.object_key)
            data = body.get("data")
            if isinstance(data, list):
                rows.extend(item for item in data if isinstance(item, dict))
            elif ingestion.kind == "account":
                rows.append(body)

        if ingestion.kind == "account" and rows:
            item = rows[-1]
            account.name = str(item.get("name") or account.name)
            account.currency = str(item.get("currency") or account.currency).upper()[:3]
            account.timezone_name = str(item.get("timezone_name") or account.timezone_name)
            account.api_version = sync.api_version
            account.updated_at = utcnow()
            sync.account_currency = account.currency
            sync.account_timezone = account.timezone_name
        elif ingestion.kind == "insights":
            for item in rows:
                _upsert_metric(session, account, sync, item)
        elif ingestion.kind in {"ads", "adsets"}:
            entity_type = "ad" if ingestion.kind == "ads" else "adset"
            for item in rows:
                _append_status_snapshot(session, account.id, sync.id, entity_type, item)

        ingestion.state = "completed"
        ingestion.completed_at = utcnow()
        ingestion.error_code = ""
        ingestion.error_detail = ""


def _upsert_metric(session, account: MetaAccountRecord, sync: MetaSyncRunRecord, item: dict) -> None:
    ad_id = str(item.get("ad_id") or "")
    day_text = item.get("date_start")
    if not ad_id or not day_text:
        return
    day = date.fromisoformat(str(day_text))
    row = session.scalar(
        select(AdDailyMetricRecord).where(
            AdDailyMetricRecord.account_id == account.id,
            AdDailyMetricRecord.ad_id == ad_id,
            AdDailyMetricRecord.metric_date == day,
            AdDailyMetricRecord.contract_hash == sync.contract_hash,
        )
    )
    if row is None:
        row = AdDailyMetricRecord(
            id=new_id("adm"), account_id=account.id, sync_run_id=sync.id, ad_id=ad_id,
            metric_date=day, currency=account.currency, contract_hash=sync.contract_hash, raw_json=item,
        )
        session.add(row)
    row.sync_run_id = sync.id
    row.ad_name = str(item.get("ad_name") or "")
    row.adset_id = str(item.get("adset_id") or "")
    row.adset_name = str(item.get("adset_name") or "")
    row.spend = _as_decimal(item.get("spend")) or Decimal("0")
    row.impressions = _as_int(item.get("impressions"))
    row.reach = _as_int(item.get("reach"))
    row.clicks = _as_int(item.get("inline_link_clicks"))
    row.ctr = _as_decimal(item.get("inline_link_click_ctr"), None)
    row.cpc = _as_decimal(item.get("cpc"), None)
    row.cpm = _as_decimal(item.get("cpm"), None)
    action_type = str(sync.contract_json["purchase_action_type"])
    row.purchases = _action_value(item.get("actions"), action_type)
    row.purchase_value = _action_value(item.get("action_values"), action_type)
    roas = _action_value(item.get("purchase_roas"), action_type)
    row.purchase_roas = roas if roas != 0 else None
    row.frequency = _as_decimal(item.get("frequency"), None)
    row.attribution_setting = str(item.get("attribution_setting") or "")
    row.currency = account.currency
    row.raw_json = item
    row.observed_at = utcnow()


def _append_status_snapshot(session, account_id: str, sync_run_id: str, entity_type: str, item: dict) -> None:
    entity_id = str(item.get("id") or "")
    if not entity_id:
        return
    existing = session.scalar(
        select(MetaEntityStatusEventRecord).where(
            MetaEntityStatusEventRecord.sync_run_id == sync_run_id,
            MetaEntityStatusEventRecord.entity_type == entity_type,
            MetaEntityStatusEventRecord.entity_id == entity_id,
        )
    )
    snapshot = {
        "name": str(item.get("name") or ""),
        "configured_status": str(item.get("status") or ""),
        "effective_status": str(item.get("effective_status") or ""),
    }
    digest = hashlib.sha256(_canonical_json(snapshot)).hexdigest()
    if existing is None:
        session.add(MetaEntityStatusEventRecord(
            id=new_id("mstatus"), account_id=account_id, sync_run_id=sync_run_id,
            entity_type=entity_type, entity_id=entity_id, name=snapshot["name"],
            configured_status=snapshot["configured_status"], effective_status=snapshot["effective_status"],
            snapshot_sha256=digest,
        ))


def _finalize_sync_for_ingestion(context: TenantContext, ingestion_id: str) -> None:
    with tenant_session(context) as session:
        ingestion = _get_ingestion(session, context, ingestion_id)
        if ingestion is None:
            return
        sync = _get_sync(session, context, ingestion.sync_run_id)
        if sync is None:
            return
        states = session.scalars(
            select(MetaIngestionRecord.state).where(MetaIngestionRecord.sync_run_id == sync.id)
        ).all()
        if states and all(state == "completed" for state in states):
            now = utcnow()
            sync.state = "completed"
            sync.completed_at = now
            account = session.get(MetaAccountRecord, sync.account_id)
            account.last_successful_sync_at = now
            if account.observation_contract_hash != sync.contract_hash:
                account.observation_started_at = now
                account.observation_contract_hash = sync.contract_hash
            elif account.observation_started_at is None:
                account.observation_started_at = now
            account.updated_at = now
        elif any(state == "failed" for state in states):
            sync.state = "failed"


def record_ingestion_failure(
    context: TenantContext, ingestion_id: str, code: str, detail: str, *, terminal: bool
) -> None:
    with tenant_session(context) as session:
        ingestion = _get_ingestion(session, context, ingestion_id)
        if ingestion is None:
            return
        ingestion.state = "failed" if terminal else "retry_wait"
        ingestion.error_code = code[:100]
        ingestion.error_detail = detail[:2000]
    if terminal:
        _finalize_sync_for_ingestion(context, ingestion_id)


def enqueue_due_schedules(context: TenantContext, now: datetime | None = None) -> int:
    """Claim due schedules first, then enqueue syncs outside the transaction."""
    if not config.META_SYNC_ENABLED or not config.meta_credentials_ready():
        return 0
    now = now or utcnow()
    due_ranges: list[tuple[date, date]] = []
    with tenant_session(context) as session:
        due = session.scalars(
            select(MetaSyncScheduleRecord).where(
                MetaSyncScheduleRecord.enabled.is_(True),
                MetaSyncScheduleRecord.next_run_at <= now,
            )
        ).all()
        for schedule in due:
            try:
                schedule_tz = ZoneInfo(schedule.timezone_name)
            except ZoneInfoNotFoundError:
                schedule_tz = timezone.utc
            stop = now.astimezone(schedule_tz).date() - timedelta(days=1)
            start = stop - timedelta(days=max(1, schedule.lookback_days) - 1)
            due_ranges.append((start, stop))
            schedule.last_enqueued_at = now
            schedule.next_run_at = _next_daily_at(schedule.timezone_name, schedule.local_hour, now + timedelta(seconds=1))
            schedule.updated_at = now
    for start, stop in due_ranges:
        request_sync(context, start, stop)
    return len(due_ranges)


def run_due_schedule(
    context: TenantContext, schedule_id: str, now: datetime | None = None
) -> str | None:
    """Advance one dispatcher-issued schedule tick and enqueue its sync."""
    now = now or utcnow()
    with tenant_session(context) as session:
        schedule = session.get(MetaSyncScheduleRecord, schedule_id)
        if (
            schedule is None
            or schedule.workspace_id != context.workspace_id
            or not schedule.enabled
            or schedule.next_run_at > now
        ):
            return None
        if not config.META_SYNC_ENABLED:
            # The database schedule may have been enabled by an older deploy.
            # Persist the new deployment intent so the SECURITY DEFINER
            # dispatcher does not keep issuing work the Worker will discard.
            schedule.enabled = False
            schedule.updated_at = now
            return None
        try:
            schedule_tz = ZoneInfo(schedule.timezone_name)
        except ZoneInfoNotFoundError:
            schedule_tz = timezone.utc
        if not config.meta_credentials_ready():
            # A temporary secret outage must not strand this exact due time:
            # the dispatch job's idempotency key includes next_run_at. Advance
            # to the next slot so restoring credentials makes it eligible again.
            schedule.next_run_at = _next_daily_at(
                schedule.timezone_name, schedule.local_hour, now + timedelta(seconds=1)
            )
            schedule.updated_at = now
            return None
        stop = now.astimezone(schedule_tz).date() - timedelta(days=1)
        start = stop - timedelta(days=max(1, schedule.lookback_days) - 1)
        schedule.last_enqueued_at = now
        schedule.next_run_at = _next_daily_at(
            schedule.timezone_name, schedule.local_hour, now + timedelta(seconds=1)
        )
        schedule.updated_at = now
    return request_sync(context, start, stop)


def _sync_payload(sync: MetaSyncRunRecord, ingestions: list[MetaIngestionRecord]) -> dict:
    return {
        "id": sync.id,
        "state": sync.state,
        "date_start": sync.date_start,
        "date_stop": sync.date_stop,
        "contract_hash": sync.contract_hash,
        "created_at": sync.created_at,
        "completed_at": sync.completed_at,
        "ingestions": [
            {
                "kind": row.kind,
                "state": row.state,
                "pages": row.pages_fetched,
                "rows": row.rows_fetched,
                "fetch_complete": row.fetch_complete,
                "error_code": row.error_code,
            }
            for row in ingestions
        ],
    }


def sync_detail(context: TenantContext, sync_id: str) -> dict | None:
    """Return one tenant-owned historical sync without consulting live credentials."""
    with tenant_session(context) as session:
        sync = _get_sync(session, context, sync_id)
        if sync is None:
            return None
        ingestions = session.scalars(
            select(MetaIngestionRecord)
            .where(MetaIngestionRecord.sync_run_id == sync.id)
            .order_by(MetaIngestionRecord.kind)
        ).all()
        return _sync_payload(sync, list(ingestions))


def sync_status(context: TenantContext, sync_id: str | None = None) -> dict:
    require_meta_credential_workspace(context)
    with tenant_session(context) as session:
        account = session.scalar(
            select(MetaAccountRecord).where(
                MetaAccountRecord.workspace_id == context.workspace_id,
                MetaAccountRecord.ad_account_id == config.META_AD_ACCOUNT_ID,
            )
        ) if config.META_AD_ACCOUNT_ID else None
        sync = _get_sync(session, context, sync_id) if sync_id else session.scalar(
            select(MetaSyncRunRecord)
            .where(MetaSyncRunRecord.workspace_id == context.workspace_id)
            .order_by(MetaSyncRunRecord.created_at.desc())
            .limit(1)
        )
        ingestions = []
        if sync:
            ingestions = session.scalars(
                select(MetaIngestionRecord).where(MetaIngestionRecord.sync_run_id == sync.id)
                .order_by(MetaIngestionRecord.kind)
            ).all()
        now = utcnow()
        observation_days = 0
        freshness_hours = None
        current_contract_hash = contract_hash()
        observation_contract_current = bool(
            account and account.observation_contract_hash == current_contract_hash
        )
        if account and account.observation_started_at and observation_contract_current:
            started = account.observation_started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            observation_days = max(0, (now - started).days)
        if account and account.last_successful_sync_at:
            last_success = account.last_successful_sync_at
            if last_success.tzinfo is None:
                last_success = last_success.replace(tzinfo=timezone.utc)
            freshness_hours = max(0, (now - last_success).total_seconds() / 3600)
        return {
            "configured": config.meta_credentials_ready(),
            "daily_enabled": config.META_SYNC_ENABLED,
            "api_version": config.META_API_VERSION,
            "ad_account_id": config.META_AD_ACCOUNT_ID or None,
            "account": None if account is None else {
                "name": account.name,
                "currency": account.currency,
                "timezone": account.timezone_name,
                "observation_started_at": account.observation_started_at,
                "observation_contract_hash": account.observation_contract_hash,
                "observation_contract_current": observation_contract_current,
                "observation_days": observation_days,
                "observation_gate_ready": observation_contract_current and observation_days >= 14,
                "last_successful_sync_at": account.last_successful_sync_at,
                "freshness_hours": freshness_hours,
                "freshness_slo_hours": config.META_FRESHNESS_SLO_HOURS,
                "stale": freshness_hours is None or freshness_hours > config.META_FRESHNESS_SLO_HOURS,
            },
            "sync": None if sync is None else _sync_payload(sync, list(ingestions)),
        }


def reconcile(context: TenantContext) -> dict:
    """Read-only reconciliation across raw pages, projections, and future launch bindings."""
    require_meta_credential_workspace(context)
    with tenant_session(context) as session:
        account = session.scalar(
            select(MetaAccountRecord).where(
                MetaAccountRecord.workspace_id == context.workspace_id,
                MetaAccountRecord.ad_account_id == config.META_AD_ACCOUNT_ID,
            )
        )
        if account is None:
            raise MetaConfigurationError("还没有 Meta account 同步记录")
        latest_sync = session.scalar(
            select(MetaSyncRunRecord).where(MetaSyncRunRecord.account_id == account.id)
            .order_by(MetaSyncRunRecord.created_at.desc()).limit(1)
        )
        latest_status_subquery = (
            select(
                MetaEntityStatusEventRecord.entity_id,
                func.max(MetaEntityStatusEventRecord.observed_at).label("observed_at"),
            )
            .where(MetaEntityStatusEventRecord.account_id == account.id,
                   MetaEntityStatusEventRecord.entity_type == "ad")
            .group_by(MetaEntityStatusEventRecord.entity_id)
            .subquery()
        )
        latest_ads = session.scalars(
            select(MetaEntityStatusEventRecord).join(
                latest_status_subquery,
                (MetaEntityStatusEventRecord.entity_id == latest_status_subquery.c.entity_id)
                & (MetaEntityStatusEventRecord.observed_at == latest_status_subquery.c.observed_at),
            )
        ).all()
        ad_ids = {row.entity_id for row in latest_ads}
        metric_ad_ids = set(session.scalars(
            select(AdDailyMetricRecord.ad_id).where(AdDailyMetricRecord.account_id == account.id).distinct()
        ).all())
        bound_ad_ids = set(session.scalars(
            select(MetaAdBindingRecord.ad_id).where(MetaAdBindingRecord.account_id == account.id)
        ).all())
        managed_ads = {row.entity_id for row in latest_ads if row.name.startswith("MAI|")}
        page_rows = 0
        projected_rows = 0
        if latest_sync:
            page_rows = session.scalar(
                select(func.coalesce(func.sum(MetaIngestionPageRecord.row_count), 0))
                .join(MetaIngestionRecord)
                .where(MetaIngestionRecord.sync_run_id == latest_sync.id)
            ) or 0
            projected_rows = session.scalar(
                select(func.count()).select_from(AdDailyMetricRecord)
                .where(AdDailyMetricRecord.sync_run_id == latest_sync.id)
            ) or 0
        issues = {
            "metric_ads_missing_from_status": sorted(metric_ad_ids - ad_ids),
            "managed_ads_without_binding": sorted(managed_ads - bound_ad_ids),
            "bindings_missing_from_meta": sorted(bound_ad_ids - ad_ids),
        }
        return {
            "ok": not any(issues.values()),
            "account_id": account.ad_account_id,
            "latest_sync_id": latest_sync.id if latest_sync else None,
            "raw_page_rows_latest_sync": int(page_rows),
            "daily_metric_rows_latest_sync": int(projected_rows),
            "issues": issues,
        }
