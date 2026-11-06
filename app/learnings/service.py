"""Results imports, learnings snapshots and their views (Issue #166).

Flow (front-flow contract §3.3, §5.4, §7-3):

1. ``create_import`` parses a CSV (uploaded or pasted), JSON rows or the
   synced Meta ad metrics, validates every row and matches it to one of the
   product's AssetVersions -- by asset code (``MK-XXXXXX``, also found inside
   an ad name) or by a Meta ``ad_id`` bound through an experiment or launch.
   Nothing is written to the evidence yet: the result is a *preview* with a
   status and a reason per row.
2. ``commit_import`` re-validates under the product row lock (a preview that
   went stale is refreshed and refused), appends the accepted rows to
   ``result_observations`` and recomputes the learnings snapshot of every
   evidence partition the rows touched.
3. ``compute_snapshot`` aggregates the *effective* observations (a later
   import of the same asset, ad and date range supersedes an earlier one;
   overlapping ranges are refused at import, never double counted) per
   asset, groups the attributable assets by test plan and runs
   :func:`app.learnings.stats.analyze_round`. Same inputs -> same
   ``input_hash`` -> the latest snapshot is reused.

Idempotency: ``Idempotency-Key`` replays the same import; independently the
normalized-rows fingerprint is unique per product, so re-uploading the same
file returns the existing import (``duplicate: true``) instead of a second
copy. Committing twice returns the committed import.

Mock evidence (CLAUDE.md "Controlled Mock ... cannot mix with real
evidence"): every row carries the partition of its asset (and of the Meta
connection for synced rows). Partitions are stored, aggregated and served
separately; a Mock partition is labelled demo data, and only the production
partition seeds a new plan automatically.

Authorization: Workspace members (Owner or Member) import, commit and read;
another Workspace's product, import or plan id answers 404.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AdDailyMetricRecord,
    AdTestPlanRecord,
    ExperimentAdBindingRecord,
    LearningSnapshotRecord,
    MetaAccountRecord,
    MetaAdBindingRecord,
    ProductRecord,
    ResultImportRecord,
    ResultObservationRecord,
    new_id,
    utcnow,
)
from app.meta.models import MetaConnectionRecord
from app.platform import audit
from app.platform.canonical import canonical_hash
from app.platform.tenancy import TenantContext
from app.platform.upload_intents import UploadIntentNotFoundError, get_upload_intent

from . import codes, parsing, stats
from .attribution import UNATTRIBUTABLE_COPY, AssetInfo, ProductAttribution, combine_modes

RESOURCE_TYPE = "result_import"
UPLOAD_PURPOSE = "results_import"
SNAPSHOT_SCHEMA = "learnings/v1"
IMPORTABLE = ("ready", "replaces", "unattributable")
MAX_ROWS_IN_VIEW = 200
MAX_HIGHLIGHTS = 3

_META_MODE = {"internal_validation": "production", "customer_sandbox": "sandbox", "controlled_mock": "mock"}

ROW_STATUS_COPY = {
    "ready": "可以导入",
    "replaces": "会更新之前导入的同一时间段数据",
    "unattributable": "可以导入，但暂时无法按元素归因",
    "rejected": "不会导入",
}


@dataclass
class LearningsProblem(Exception):
    status: int
    code: str
    message: str
    retryable: bool = False
    params: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def require_member(ctx: TenantContext) -> None:
    if ctx.role not in ("owner", "member"):
        raise LearningsProblem(403, "learnings_forbidden", "只有工作区成员可以导入投放结果和查看结论")


def resolve_product(session, ctx: TenantContext, product_id: str, *, for_update: bool = False) -> ProductRecord:
    statement = select(ProductRecord).where(
        ProductRecord.workspace_id == ctx.workspace_id,
        (ProductRecord.id == product_id) | (ProductRecord.product_profile_entry_id == product_id),
    )
    if for_update:
        statement = statement.with_for_update()
    product = session.scalars(statement).first()
    if product is None:
        raise LearningsProblem(404, "product_not_found", "产品不存在")
    return product


def decode_text(raw: bytes) -> str:
    """UTF-8 (with or without BOM); GB18030 for spreadsheets saved by Chinese Excel."""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise LearningsProblem(422, "file_encoding_invalid", "无法识别文件编码，请另存为 UTF-8 CSV")


# ---------------------------------------------------------------------------
# Upload intent (CSV file)
# ---------------------------------------------------------------------------


def resolve_upload(ctx: TenantContext, upload_intent_id: str) -> dict:
    """The committed blob of a finalized ``results_import`` upload."""
    require_member(ctx)
    with tenant_session(ctx) as session:
        try:
            intent = get_upload_intent(session, upload_intent_id)
        except UploadIntentNotFoundError:
            raise LearningsProblem(404, "upload_intent_not_found", "上传的文件不存在") from None
        if intent.purpose != UPLOAD_PURPOSE:
            raise LearningsProblem(422, "upload_purpose_not_allowed", "这个文件不是用于导入投放结果的上传")
        if intent.state != "finalized" or not intent.committed_blob_ref_key:
            raise LearningsProblem(409, "upload_intent_not_finalized", "文件还在校验中，请稍后再试", retryable=True)
        return {
            "upload_intent_id": intent.id,
            "committed_key": intent.committed_blob_ref_key,
            "storage_version_id": intent.committed_blob_ref_version or "",
            "checksum_sha256_b64": intent.checksum_sha256_b64,
            "content_type": intent.content_type or "text/csv",
            "size": int(intent.content_length_max or 0),
            "file_name": intent.filename_original or "",
        }


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


def _established_currency(session, ctx: TenantContext, product_id: str) -> str | None:
    row = session.execute(
        select(ResultObservationRecord.currency)
        .where(
            ResultObservationRecord.workspace_id == ctx.workspace_id,
            ResultObservationRecord.product_id == product_id,
            ResultObservationRecord.currency.is_not(None),
        )
        .order_by(ResultObservationRecord.created_at, ResultObservationRecord.id)
        .limit(1)
    ).first()
    return row[0] if row else None


def effective_observations(session, ctx: TenantContext, product_id: str) -> list[ResultObservationRecord]:
    """Committed rows minus those superseded by a later import of the same
    (asset, ad, date range). Supersession is a projection; nothing is rewritten."""
    rows = session.execute(
        select(ResultObservationRecord, ResultImportRecord.committed_at)
        .join(
            ResultImportRecord,
            (ResultImportRecord.id == ResultObservationRecord.import_id)
            & (ResultImportRecord.workspace_id == ResultObservationRecord.workspace_id),
        )
        .where(
            ResultObservationRecord.workspace_id == ctx.workspace_id,
            ResultObservationRecord.product_id == product_id,
        )
    ).all()
    latest: dict[tuple, tuple] = {}
    for observation, committed_at in rows:
        key = (observation.asset_version_id, observation.ad_id, observation.date_start, observation.date_end)
        rank = (committed_at.isoformat() if committed_at else "", observation.import_id)
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, observation)
    return sorted((item[1] for item in latest.values()), key=lambda row: (row.asset_version_id, row.ad_id, row.date_start, row.id))


def _overlaps(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    return a_start <= b_end and b_start <= a_end


# ---------------------------------------------------------------------------
# Matching (preview)
# ---------------------------------------------------------------------------


def _row_from_input(line: int, data: dict) -> parsing.NormalizedRow:
    def dec(value):
        return None if value in (None, "") else Decimal(str(value))

    return parsing.NormalizedRow(
        line=line,
        asset_code=data.get("asset_code"),
        ad_name=data.get("ad_name") or "",
        ad_id=data.get("ad_id") or "",
        date_start=date.fromisoformat(data["date_start"]) if data.get("date_start") else None,
        date_end=date.fromisoformat(data["date_end"]) if data.get("date_end") else None,
        impressions=int(data["impressions"]) if data.get("impressions") not in (None, "") else None,
        clicks=int(data["clicks"]) if data.get("clicks") not in (None, "") else None,
        spend=dec(data.get("spend")),
        conversions=dec(data.get("conversions")),
        revenue=dec(data.get("revenue")),
        currency=data.get("currency"),
    )


def _resolve_asset(attribution: ProductAttribution, row: parsing.NormalizedRow) -> tuple[str | None, str | None, str | None]:
    """(asset_version_id, code used, error code)."""
    by_ad, ad_error = (None, None)
    if row.ad_id:
        by_ad, ad_error = attribution.resolve_ad(row.ad_id)
    code = row.asset_code or next(iter(codes.codes_in_text(row.ad_name)), None)
    by_code, code_error = (None, None)
    if code:
        by_code, code_error = attribution.resolve_code(code)
        if code_error == "unknown_code" and attribution.code_belongs_elsewhere(code):
            code_error = "other_product"
    if by_ad and by_code and by_ad != by_code:
        return None, code, "identifier_conflict"
    found = by_ad or by_code
    if found:
        return found, code, None
    if row.ad_id and ad_error != "unknown_ad":
        return None, code, ad_error
    if code:
        return None, code, code_error
    if row.ad_id:
        return None, None, ad_error
    return None, None, "unknown_code" if row.ad_name else "missing_identifier"


def _match(
    session, ctx: TenantContext, attribution: ProductAttribution, rows: list[parsing.NormalizedRow],
    *, meta_modes: dict[int, str | None] | None = None,
) -> list[dict]:
    established = _established_currency(session, ctx, attribution.product_id)
    existing: dict[str, list[tuple[str, date, date]]] = {}
    for observation in effective_observations(session, ctx, attribution.product_id):
        existing.setdefault(observation.asset_version_id, []).append(
            (observation.ad_id, observation.date_start, observation.date_end)
        )
    in_file: dict[str, list[tuple[str, date, date]]] = {}
    out: list[dict] = []
    for row in rows:
        result: dict[str, Any] = {
            "line": row.line,
            "input": row.identity(),
            "status": "rejected",
            "reason_code": None,
            "reason": None,
            "reason_params": None,
            "field": row.error_field,
            "asset_version_id": None,
            "asset_code": None,
            "description": None,
            "description_code": None,
            "plan_id": None,
            "round_number": None,
            "evidence_mode": None,
        }
        if meta_modes is not None:
            result["meta_mode"] = meta_modes.get(row.line)

        def reject(code: str, **params: str) -> None:
            # Only declared codes reach the browser, which words them from the error table.
            if code not in parsing.REASONS or set(params) != set(parsing.REASON_PARAMS.get(code, ())):
                raise ValueError(f"undeclared refusal: {code} {sorted(params)}")
            result["reason_code"] = code
            result["reason"] = parsing.REASONS.get(code, code)
            result["reason_params"] = params or None

        out.append(result)
        if row.error:
            reject(row.error)
            continue
        asset_version_id, used_code, error = _resolve_asset(attribution, row)
        result["asset_code"] = used_code
        if error or asset_version_id is None:
            reject(error or "unknown_code")
            continue
        info: AssetInfo = attribution.assets[asset_version_id]
        result.update(
            asset_version_id=asset_version_id, asset_code=info.code,
            description=attribution.describe(info), description_code=attribution.description_code(info), plan_id=info.plan_id,
            round_number=attribution.plans[info.plan_id].round_number if info.plan_id in attribution.plans else None,
        )
        modes = [info.evidence_mode]
        if meta_modes is not None:
            meta_mode = meta_modes.get(row.line)
            if meta_mode is None:
                reject("meta_source_unknown")
                continue
            modes.append(meta_mode)
        result["evidence_mode"] = combine_modes(*modes)

        has_money = (row.spend or 0) > 0 or (row.revenue or 0) > 0
        if row.currency and has_money:
            if established and row.currency != established:
                reject("currency_mismatch", expected=established, found=row.currency)
                continue
            established = established or row.currency

        start, end, ad_id = row.date_start, row.date_end, row.ad_id
        clash = None
        for other_ad, other_start, other_end in in_file.get(asset_version_id, []):
            if (other_ad, other_start, other_end) == (ad_id, start, end):
                clash = "duplicate_in_file"
                break
            if (not ad_id or not other_ad or other_ad == ad_id) and _overlaps(start, end, other_start, other_end):
                clash = "overlaps_in_file"
                break
        if clash:
            reject(clash)
            continue
        replaces = False
        for other_ad, other_start, other_end in existing.get(asset_version_id, []):
            if (other_ad, other_start, other_end) == (ad_id, start, end):
                replaces = True
                continue
            if (not ad_id or not other_ad or other_ad == ad_id) and _overlaps(start, end, other_start, other_end):
                clash = "overlaps_existing"
                break
        if clash:
            reject(clash)
            continue
        in_file.setdefault(asset_version_id, []).append((ad_id, start, end))
        if info.unattributable:
            result["status"] = "unattributable"
            result["reason_code"] = info.unattributable
            result["reason"] = UNATTRIBUTABLE_COPY[info.unattributable]
        else:
            result["status"] = "replaces" if replaces else "ready"
    return out


def _summary(rows: list[dict], *, ignored_columns: list[str], columns: dict[str, str]) -> dict:
    by_status = {status: 0 for status in ("ready", "replaces", "unattributable", "rejected")}
    reasons: dict[str, int] = {}
    modes: dict[str, int] = {}
    impressions = clicks = 0
    assets: set[str] = set()
    for row in rows:
        by_status[row["status"]] += 1
        if row["status"] == "rejected":
            reasons[row["reason_code"]] = reasons.get(row["reason_code"], 0) + 1
            continue
        modes[row["evidence_mode"]] = modes.get(row["evidence_mode"], 0) + 1
        impressions += int(row["input"].get("impressions") or 0)
        clicks += int(row["input"].get("clicks") or 0)
        if row["status"] != "unattributable":
            assets.add(row["asset_version_id"])
    return {
        "total_rows": len(rows),
        "importable_rows": sum(by_status[s] for s in IMPORTABLE),
        "by_status": by_status,
        "rejected_reasons": [
            {"code": code, "reason": parsing.REASONS.get(code, code), "count": count}
            for code, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
        ],
        "evidence_modes": modes,
        "impressions": impressions,
        "clicks": clicks,
        "attributable_assets": len(assets),
        "columns": columns,
        "ignored_columns": ignored_columns,
    }


# ---------------------------------------------------------------------------
# Meta-synced rows (read-only mapping; never a Meta write)
# ---------------------------------------------------------------------------


def _meta_rows(session, ctx: TenantContext, attribution: ProductAttribution) -> tuple[list[dict], dict[int, str | None]]:
    connections: dict[str, str] = {}
    for connection in session.scalars(
        select(MetaConnectionRecord)
        .where(MetaConnectionRecord.workspace_id == ctx.workspace_id)
        .order_by(MetaConnectionRecord.created_at, MetaConnectionRecord.id)
    ):
        mode = _META_MODE.get(connection.meta_mode)
        if mode and (connection.ad_account_id not in connections or connection.state == "active"):
            connections[connection.ad_account_id] = mode
    raw_rows: list[dict] = []
    modes: dict[int, str | None] = {}
    accounts = list(session.scalars(
        select(MetaAccountRecord).where(MetaAccountRecord.workspace_id == ctx.workspace_id).order_by(MetaAccountRecord.id)
    ))
    for account in accounts:
        ad_ids = set(session.scalars(
            select(ExperimentAdBindingRecord.ad_id).where(
                ExperimentAdBindingRecord.workspace_id == ctx.workspace_id,
                ExperimentAdBindingRecord.ad_account_id == account.ad_account_id,
            )
        )) | set(session.scalars(select(MetaAdBindingRecord.ad_id).where(MetaAdBindingRecord.account_id == account.id)))
        ours = sorted(ad_id for ad_id in ad_ids if attribution.resolve_ad(ad_id)[0] is not None)
        if not ours:
            continue
        latest: dict[tuple[str, date], AdDailyMetricRecord] = {}
        for metric in session.scalars(
            select(AdDailyMetricRecord)
            .where(AdDailyMetricRecord.account_id == account.id, AdDailyMetricRecord.ad_id.in_(ours))
            .order_by(AdDailyMetricRecord.observed_at, AdDailyMetricRecord.id)
        ):
            latest[(metric.ad_id, metric.metric_date)] = metric  # the newest observation wins
        for (ad_id, day), metric in sorted(latest.items()):
            line = len(raw_rows) + 1
            raw_rows.append({
                "line": line, "ad_id": ad_id, "asset_code": "", "ad_name": "",
                "date_start": day.isoformat(), "date_end": day.isoformat(),
                "impressions": str(metric.impressions), "clicks": str(metric.clicks),
                "spend": format(metric.spend, "f"), "conversions": format(metric.purchases, "f"),
                "revenue": format(metric.purchase_value, "f"), "currency": metric.currency or "",
            })
            modes[line] = connections.get(account.ad_account_id)
    return raw_rows, modes


# ---------------------------------------------------------------------------
# Import commands
# ---------------------------------------------------------------------------


def create_import(
    ctx: TenantContext,
    *,
    product_id: str,
    source: str,
    idempotency_key: str,
    csv_text: str | None = None,
    upload: dict | None = None,
    upload_bytes: bytes | None = None,
    rows: list[dict] | None = None,
    currency: str | None = None,
    file_name: str | None = None,
) -> dict:
    """Parse, validate and match rows into a previewed import (no evidence yet)."""
    require_member(ctx)
    if source not in ("csv", "manual", "meta_sync"):
        raise LearningsProblem(422, "source_invalid", "来源必须是 csv、manual 或 meta_sync")
    if source == "csv" and (csv_text is None) == (upload_bytes is None):
        raise LearningsProblem(422, "csv_input_invalid", "CSV 导入需要上传文件或粘贴表格（二选一）")
    if source == "manual" and not rows:
        raise LearningsProblem(422, "rows_required", "手填导入至少需要一行")
    if source != "csv" and (csv_text is not None or upload_bytes is not None):
        raise LearningsProblem(422, "csv_input_invalid", "只有 CSV 导入可以附带文件或表格")
    if source != "manual" and rows:
        raise LearningsProblem(422, "rows_not_allowed", "只有手填导入可以附带 rows")
    currency = (currency or "").strip().upper() or None
    if currency is not None and (len(currency) != 3 or not currency.isalpha()):
        raise LearningsProblem(422, "currency_invalid", "币种应为三个字母，例如 USD")

    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    request_fingerprint = _sha({
        "product_id": product_id, "source": source, "currency": currency, "rows": rows,
        "csv_sha": hashlib.sha256(csv_text.encode("utf-8")).hexdigest() if csv_text is not None else None,
        "upload_intent_id": (upload or {}).get("upload_intent_id"),
    })
    with tenant_session(ctx) as session:
        replay = session.scalars(
            select(ResultImportRecord).where(
                ResultImportRecord.workspace_id == ctx.workspace_id,
                ResultImportRecord.idempotency_key_hash == key_hash,
            )
        ).one_or_none()
        if replay is not None:
            if replay.request_fingerprint != request_fingerprint:
                raise LearningsProblem(409, "idempotency_key_reused", "Idempotency-Key 已用于另一次导入")
            return {**import_view(replay), "replayed": True}

        product = resolve_product(session, ctx, product_id)
        attribution = ProductAttribution(session, ctx, product.id)
        meta_modes = None
        try:
            if source == "meta_sync":
                raw_rows, meta_modes = _meta_rows(session, ctx, attribution)
                if not raw_rows:
                    raise LearningsProblem(
                        422, "no_meta_rows", "还没有已同步、且绑定到这个产品素材的 Meta 广告数据",
                    )
                table = parsing.ParsedTable(rows=raw_rows, columns={name: name for name in parsing.FIELDS})
            elif source == "manual":
                table = parsing.rows_from_manual(list(rows or []))
            else:
                text = csv_text if csv_text is not None else decode_text(upload_bytes or b"")
                table = parsing.parse_table(text)
        except parsing.ParseError as error:
            raise LearningsProblem(422, error.code, error.message, params=error.params) from error

        default_currency = currency or table.header_currency or _established_currency(session, ctx, product.id)
        today = utcnow().date()
        normalized = [parsing.normalize_row(raw, default_currency=default_currency, today=today) for raw in table.rows]
        fingerprint = _sha({
            "product_id": product.id,
            "source": source,
            "rows": sorted(json.dumps(row.identity(), sort_keys=True, ensure_ascii=False) for row in normalized),
        })
        duplicate = session.scalars(
            select(ResultImportRecord).where(
                ResultImportRecord.workspace_id == ctx.workspace_id,
                ResultImportRecord.product_id == product.id,
                ResultImportRecord.fingerprint == fingerprint,
            )
        ).one_or_none()
        if duplicate is not None:
            return {**import_view(duplicate), "replayed": True, "duplicate": True}

        preview_rows = _match(session, ctx, attribution, normalized, meta_modes=meta_modes)
        now = utcnow()
        record = ResultImportRecord(
            id=new_id("rimp"),
            workspace_id=ctx.workspace_id,
            product_id=product.id,
            source=source,
            status="previewed",
            fingerprint=fingerprint,
            file_name=(file_name or (upload or {}).get("file_name") or "")[:300],
            upload_intent_id=(upload or {}).get("upload_intent_id"),
            currency=default_currency,
            rows_json=preview_rows,
            summary_json=_summary(preview_rows, ignored_columns=table.ignored_columns, columns=table.columns),
            idempotency_key_hash=key_hash,
            request_fingerprint=request_fingerprint,
            snapshot_ids_json=[],
            created_by=ctx.initiator_id,
            created_at=now,
        )
        session.add(record)
        session.flush()
        audit.append_event(
            session,
            event_type="result_import.previewed",
            resource_type=RESOURCE_TYPE,
            resource_id=record.id,
            details={
                "product_id": product.id,
                "source": source,
                "fingerprint": fingerprint,
                "upload_intent_id": record.upload_intent_id,
                "total_rows": record.summary_json["total_rows"],
                "by_status": record.summary_json["by_status"],
                "evidence_modes": record.summary_json["evidence_modes"],
            },
        )
        session.flush()
        return {**import_view(record), "replayed": False}


def load_import(session, ctx: TenantContext, import_id: str, *, for_update: bool = False) -> ResultImportRecord:
    statement = select(ResultImportRecord).where(
        ResultImportRecord.workspace_id == ctx.workspace_id, ResultImportRecord.id == import_id,
    )
    if for_update:
        statement = statement.with_for_update()
    record = session.scalars(statement).one_or_none()
    if record is None:
        raise LearningsProblem(404, "result_import_not_found", "导入记录不存在")
    return record


def get_import(ctx: TenantContext, import_id: str) -> dict:
    require_member(ctx)
    with tenant_session(ctx) as session:
        return import_view(load_import(session, ctx, import_id))


def commit_import(ctx: TenantContext, import_id: str) -> dict:
    """Append the accepted rows as evidence and recompute the learnings."""
    require_member(ctx)
    stale = False
    with tenant_session(ctx) as session:
        record = load_import(session, ctx, import_id, for_update=True)
        if record.status == "committed":
            return {**import_view(record), "replayed": True}
        # Serialize commits per product: the overlap check and the insert
        # must see the same committed evidence.
        product = resolve_product(session, ctx, record.product_id, for_update=True)
        attribution = ProductAttribution(session, ctx, product.id)
        previous = list(record.rows_json or [])
        normalized = [_row_from_input(row["line"], row["input"]) for row in previous]
        for row, stored in zip(normalized, previous):
            if stored["status"] == "rejected" and stored["reason_code"] in _ROW_ERRORS:
                row.error, row.error_field = stored["reason_code"], stored.get("field")
        meta_modes = (
            {row["line"]: row.get("meta_mode") for row in previous} if record.source == "meta_sync" else None
        )
        fresh = _match(session, ctx, attribution, normalized, meta_modes=meta_modes)
        if [(row["status"], row["asset_version_id"]) for row in fresh] != [
            (row["status"], row["asset_version_id"]) for row in previous
        ]:
            record.rows_json = fresh
            record.summary_json = {
                **_summary(fresh, ignored_columns=list((record.summary_json or {}).get("ignored_columns") or []),
                           columns=dict((record.summary_json or {}).get("columns") or {})),
            }
            audit.append_event(
                session, event_type="result_import.preview_refreshed", resource_type=RESOURCE_TYPE,
                resource_id=record.id, details={"by_status": record.summary_json["by_status"]},
            )
            stale = True
        else:
            importable = [row for row in fresh if row["status"] in IMPORTABLE]
            if not importable:
                raise LearningsProblem(422, "nothing_to_import", "没有可以导入的行")
            now = utcnow()
            for row in importable:
                data = row["input"]
                session.add(ResultObservationRecord(
                    id=new_id("robs"),
                    workspace_id=ctx.workspace_id,
                    product_id=record.product_id,
                    import_id=record.id,
                    source=record.source,
                    evidence_mode=row["evidence_mode"],
                    asset_version_id=row["asset_version_id"],
                    ad_id=data.get("ad_id") or "",
                    date_start=date.fromisoformat(data["date_start"]),
                    date_end=date.fromisoformat(data["date_end"]),
                    impressions=int(data["impressions"]),
                    clicks=int(data["clicks"]),
                    spend=Decimal(data["spend"]) if data.get("spend") is not None else None,
                    conversions=Decimal(data["conversions"]) if data.get("conversions") is not None else None,
                    revenue=Decimal(data["revenue"]) if data.get("revenue") is not None else None,
                    currency=data.get("currency"),
                    row_fingerprint=_sha(data),
                    created_at=now,
                ))
            record.status = "committed"
            record.committed_at = now
            record.committed_by = ctx.initiator_id
            session.flush()
            snapshot_ids = []
            for mode in sorted({row["evidence_mode"] for row in importable}):
                snapshot = compute_snapshot(session, ctx, record.product_id, mode, trigger_import_id=record.id)
                snapshot_ids.append(snapshot.id)
            record.snapshot_ids_json = snapshot_ids
            audit.append_event(
                session,
                event_type="result_import.committed",
                resource_type=RESOURCE_TYPE,
                resource_id=record.id,
                details={
                    "product_id": record.product_id,
                    "source": record.source,
                    "observations": len(importable),
                    "by_status": record.summary_json.get("by_status"),
                    "snapshot_ids": snapshot_ids,
                },
            )
            session.flush()
            view = {**import_view(record), "replayed": False}
    if stale:
        raise LearningsProblem(
            409, "preview_stale", "导入预览之后数据有变化，已重新匹配；请确认新的预览后再导入", retryable=True,
        )
    return view


#: Row-level validation errors (re-applied on commit instead of re-parsing).
_ROW_ERRORS = frozenset({
    "missing_identifier", "missing_date", "invalid_date", "date_order", "future_date",
    "missing_impressions", "missing_clicks", "invalid_number", "negative_number", "not_integer",
    "clicks_exceed_impressions", "currency_invalid", "currency_missing",
})


def import_view(record: ResultImportRecord) -> dict:
    rows = list(record.rows_json or [])
    shown = [row for row in rows if row["status"] == "rejected"]
    shown += [row for row in rows if row["status"] != "rejected"][: max(0, MAX_ROWS_IN_VIEW - len(shown))]
    shown.sort(key=lambda row: row["line"])
    return {
        "import_id": record.id,
        "product_id": record.product_id,
        "source": record.source,
        "status": record.status,
        "file_name": record.file_name,
        "currency": record.currency,
        "summary": dict(record.summary_json or {}),
        "rows": [
            {
                "line": row["line"],
                "status": row["status"],
                "status_label": ROW_STATUS_COPY[row["status"]],
                "reason_code": row.get("reason_code"),
                "reason": row.get("reason"),
                "reason_params": row.get("reason_params"),
                "asset_code": row.get("asset_code"),
                "asset_version_id": row.get("asset_version_id"),
                "description": row.get("description"),
                "description_code": row.get("description_code"),
                "round_number": row.get("round_number"),
                "evidence_mode": row.get("evidence_mode"),
                "input": row.get("input") or {},
            }
            for row in shown
        ],
        "rows_truncated": len(shown) < len(rows),
        "snapshot_ids": list(record.snapshot_ids_json or []),
        "created_at": _iso(record.created_at),
        "committed_at": _iso(record.committed_at),
        "duplicate": False,
    }


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def _units(observations: list[ResultObservationRecord], attribution: ProductAttribution):
    sums: dict[str, dict] = {}
    for observation in observations:
        entry = sums.setdefault(observation.asset_version_id, {
            "impressions": 0, "clicks": 0, "spend": Decimal("0"), "conversions": Decimal("0"),
            "spend_known": True, "conversions_known": True, "ads": set(), "rows": 0,
        })
        entry["impressions"] += int(observation.impressions)
        entry["clicks"] += int(observation.clicks)
        entry["rows"] += 1
        if observation.spend is None:
            entry["spend_known"] = False
        else:
            entry["spend"] += Decimal(observation.spend)
        if observation.conversions is None:
            entry["conversions_known"] = False
        else:
            entry["conversions"] += Decimal(observation.conversions)
        if observation.ad_id:
            entry["ads"].add(observation.ad_id)
    rounds: dict[str, list[stats.Unit]] = {}
    unattributable: list[dict] = []
    for asset_version_id in sorted(sums):
        entry = sums[asset_version_id]
        info = attribution.assets.get(asset_version_id)
        if info is None:
            continue
        if info.unattributable or not info.plan_id or info.plan_id not in attribution.plans:
            unattributable.append({
                "asset_code": info.code,
                "reason_code": info.unattributable or "not_from_test_plan",
                "reason": UNATTRIBUTABLE_COPY[info.unattributable or "not_from_test_plan"],
                "impressions": entry["impressions"],
            })
            continue
        rounds.setdefault(info.plan_id, []).append(stats.Unit(
            unit_id=asset_version_id,
            direction=info.direction_key or "",
            cells=dict(info.cells),
            impressions=entry["impressions"],
            clicks=entry["clicks"],
            spend=entry["spend"] if entry["spend_known"] else None,
            conversions=entry["conversions"] if entry["conversions_known"] else None,
            ads=len(entry["ads"]),
        ))
    return rounds, unattributable


def _unit_payload(unit: stats.Unit) -> dict:
    return {
        "id": unit.unit_id, "direction": unit.direction, "cells": dict(sorted(unit.cells.items())),
        "impressions": unit.impressions, "clicks": unit.clicks,
        "spend": None if unit.spend is None else format(unit.spend, "f"),
        "conversions": None if unit.conversions is None else format(unit.conversions, "f"),
        "ads": unit.ads,
    }


def compute_snapshot(
    session, ctx: TenantContext, product_id: str, evidence_mode: str, *,
    trigger_import_id: str | None = None, thresholds: stats.Thresholds = stats.DEFAULT_THRESHOLDS,
) -> LearningSnapshotRecord:
    attribution = ProductAttribution(session, ctx, product_id)
    observations = [
        row for row in effective_observations(session, ctx, product_id) if row.evidence_mode == evidence_mode
    ]
    by_plan, unattributable = _units(observations, attribution)
    currencies = sorted({row.currency for row in observations if row.currency})
    round_inputs: list[tuple[Any, stats.RoundInput]] = []
    payload_rounds = []
    for plan_id, units in sorted(by_plan.items(), key=lambda item: attribution.plans[item[0]].round_number):
        catalog = attribution.plans[plan_id]
        spec = stats.RoundInput(
            round_id=plan_id,
            factors=[
                stats.FactorSpec(
                    key=factor["key"], label=factor.get("label") or factor["key"],
                    values=[(value["id"], value["label"]) for value in factor.get("values") or []],
                )
                for factor in catalog.factors
            ],
            directions=[(direction.direction_key, direction.title) for direction in catalog.directions],
            units=units,
        )
        round_inputs.append((catalog, spec))
        payload_rounds.append({
            "plan_id": plan_id,
            "round_number": catalog.round_number,
            "factors": [
                {"key": f.key, "label": f.label, "values": [list(v) for v in f.values]} for f in spec.factors
            ],
            "directions": [list(d) for d in spec.directions],
            "units": [_unit_payload(unit) for unit in sorted(units, key=lambda u: u.unit_id)],
        })
    input_payload = {
        "schema": SNAPSHOT_SCHEMA,
        "algorithm_version": stats.ALGORITHM_VERSION,
        "thresholds": thresholds.as_dict(),
        "product_id": product_id,
        "evidence_mode": evidence_mode,
        "currencies": currencies,
        "rounds": payload_rounds,
        "unattributable": unattributable,
    }
    input_hash = canonical_hash(input_payload)
    latest = latest_snapshot(session, ctx, product_id, evidence_mode)
    if latest is not None and latest.input_hash == input_hash:
        return latest

    rounds_out = []
    for catalog, spec in round_inputs:
        analysis = stats.analyze_round(spec, input_hash=input_hash, th=thresholds)
        rounds_out.append({
            "plan_id": catalog.plan_id,
            "round_number": catalog.round_number,
            "goal": catalog.goal,
            "plan_created_at": catalog.created_at,
            **analysis,
        })
    total_impressions = sum(row.impressions for row in observations)
    result = {
        "schema": SNAPSHOT_SCHEMA,
        "algorithm_version": stats.ALGORITHM_VERSION,
        "thresholds": thresholds.as_dict(),
        "evidence_mode": evidence_mode,
        "currency": currencies[0] if len(currencies) == 1 else None,
        "observations": len(observations),
        "impressions": total_impressions,
        "rounds": rounds_out,
        "unattributable": unattributable,
    }
    record = LearningSnapshotRecord(
        id=new_id("lrn"),
        workspace_id=ctx.workspace_id,
        product_id=product_id,
        evidence_mode=evidence_mode,
        algorithm_version=stats.ALGORITHM_VERSION,
        input_hash=input_hash,
        trigger_import_id=trigger_import_id,
        result_json=result,
        created_by=ctx.initiator_id,
        created_at=utcnow(),
    )
    session.add(record)
    session.flush()
    audit.append_event(
        session,
        event_type="learning_snapshot.computed",
        resource_type="learning_snapshot",
        resource_id=record.id,
        details={
            "product_id": product_id,
            "evidence_mode": evidence_mode,
            "input_hash": input_hash,
            "algorithm_version": stats.ALGORITHM_VERSION,
            "trigger_import_id": trigger_import_id,
            "rounds": len(rounds_out),
            "observations": len(observations),
        },
    )
    return record


def latest_snapshot(session, ctx: TenantContext, product_id: str, evidence_mode: str) -> LearningSnapshotRecord | None:
    return session.scalars(
        select(LearningSnapshotRecord)
        .where(
            LearningSnapshotRecord.workspace_id == ctx.workspace_id,
            LearningSnapshotRecord.product_id == product_id,
            LearningSnapshotRecord.evidence_mode == evidence_mode,
        )
        .order_by(LearningSnapshotRecord.created_at.desc(), LearningSnapshotRecord.id.desc())
        .limit(1)
    ).first()


def _choose_snapshot(session, ctx: TenantContext, product_id: str, evidence_mode: str | None):
    available = {
        mode: snapshot
        for mode in ("production", "sandbox", "mock")
        if (snapshot := latest_snapshot(session, ctx, product_id, mode)) is not None
    }
    if evidence_mode:
        return available.get(evidence_mode), sorted(available)
    for mode in ("production", "sandbox", "mock"):
        if mode in available:
            return available[mode], sorted(available)
    return None, []


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

EVIDENCE_NOTICE = {
    "production": None,
    "sandbox": "Meta Sandbox 模拟数据：只用于体验流程，不代表真实投放效果",
    "mock": "演示数据：这些素材由受控 Mock 生成，结论只用于体验流程，不代表真实投放效果",
}


def _round_view(round_: dict) -> dict:
    return {
        **round_,
        "label": f"第 {round_['round_number']} 轮",
    }


def _highlights(rounds: list[dict]) -> list[str]:
    for round_ in sorted(rounds, key=lambda item: -item["round_number"]):
        decided = [
            value for factor in round_["factors"] for value in factor["values"]
            if value["verdict"] in ("winner", "loser")
        ]
        winners = [value for value in decided if value["verdict"] == "winner"]
        picked = (winners or decided)[:MAX_HIGHLIGHTS]
        if picked:
            return [value["sentence"] for value in picked]
    return []


def _review_signals(session, ctx: TenantContext, attribution: ProductAttribution) -> dict | None:
    """Human review as a weak signal while there is no ad data (contract §3.3)."""
    from app.generation.review import project_effective_review

    latest_plan = None
    for plan_id, catalog in sorted(attribution.plans.items(), key=lambda item: -item[1].round_number):
        if any(info.plan_id == plan_id for info in attribution.assets.values()):
            latest_plan = catalog
            break
    if latest_plan is None:
        return None
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for info in attribution.assets.values():
        if info.plan_id != latest_plan.plan_id:
            continue
        review = project_effective_review(session, ctx, info.asset_version_id)
        for key, value_id in info.cells.items():
            bucket = counts.setdefault((key, value_id), {"approved": 0, "needs_changes": 0, "rejected": 0, "assets": 0})
            bucket["assets"] += 1
            if review in ("approved", "needs_changes", "rejected"):
                bucket[review] += 1
    factors = []
    for factor in latest_plan.factors:
        values = []
        for value in factor.get("values") or []:
            bucket = counts.get((factor["key"], value["id"]))
            if bucket:
                values.append({"value_id": value["id"], "label": value["label"], **bucket})
        if values:
            factors.append({"key": factor["key"], "label": factor.get("label") or factor["key"], "values": values})
    if not factors:
        return None
    return {
        "round_number": latest_plan.round_number,
        "note": "这是人工审核的弱信号，不是投放数据",
        "factors": factors,
    }


def product_learnings(ctx: TenantContext, product_id: str, *, evidence_mode: str | None = None) -> dict:
    require_member(ctx)
    if evidence_mode is not None and evidence_mode not in ("production", "sandbox", "mock"):
        raise LearningsProblem(422, "evidence_mode_invalid", "evidence_mode 必须是 production、sandbox 或 mock")
    with tenant_session(ctx) as session:
        product = resolve_product(session, ctx, product_id)
        snapshot, available = _choose_snapshot(session, ctx, product.id, evidence_mode)
        imports = list(session.scalars(
            select(ResultImportRecord)
            .where(
                ResultImportRecord.workspace_id == ctx.workspace_id,
                ResultImportRecord.product_id == product.id,
                ResultImportRecord.status == "committed",
            )
            .order_by(ResultImportRecord.committed_at.desc(), ResultImportRecord.id.desc())
            .limit(5)
        ))
        result = dict(snapshot.result_json or {}) if snapshot is not None else {}
        rounds = sorted(result.get("rounds") or [], key=lambda item: -item["round_number"])
        has_data = any(round_["totals"]["impressions"] > 0 for round_ in rounds)
        review = None
        if not has_data:
            review = _review_signals(session, ctx, ProductAttribution(session, ctx, product.id))
        decided = [
            value for round_ in rounds for factor in round_["factors"] for value in factor["values"]
            if value["verdict"] in stats.DECIDED
        ]
        mode = snapshot.evidence_mode if snapshot is not None else None
        return {
            "product_id": product.id,
            "product_name": product.name or "",
            "status": "ready" if has_data else "no_data",
            "evidence_mode": mode,
            "evidence_notice": EVIDENCE_NOTICE.get(mode) if mode else None,
            "available_modes": available,
            "snapshot_id": snapshot.id if snapshot is not None else None,
            "computed_at": _iso(snapshot.created_at) if snapshot is not None else None,
            "algorithm_version": result.get("algorithm_version"),
            "thresholds": result.get("thresholds") or stats.DEFAULT_THRESHOLDS.as_dict(),
            "currency": result.get("currency"),
            "highlights": _highlights(rounds),
            "rounds": [_round_view(round_) for round_ in rounds],
            "unattributable": list(result.get("unattributable") or []),
            "can_seed_next_round": bool(decided),
            "review_signals": review,
            "recent_imports": [
                {
                    "import_id": item.id,
                    "source": item.source,
                    "file_name": item.file_name,
                    "committed_at": _iso(item.committed_at),
                    "rows": int((item.summary_json or {}).get("importable_rows") or 0),
                }
                for item in imports
            ],
        }


def plan_results(ctx: TenantContext, plan_id: str, *, evidence_mode: str | None = None) -> dict:
    require_member(ctx)
    with tenant_session(ctx) as session:
        plan = session.scalars(
            select(AdTestPlanRecord).where(
                AdTestPlanRecord.workspace_id == ctx.workspace_id, AdTestPlanRecord.id == plan_id,
            )
        ).one_or_none()
        if plan is None:
            raise LearningsProblem(404, "test_plan_not_found", "测试方案不存在")
        snapshot, available = _choose_snapshot(session, ctx, plan.product_id, evidence_mode)
        round_ = None
        if snapshot is not None:
            round_ = next(
                (item for item in (snapshot.result_json or {}).get("rounds") or [] if item["plan_id"] == plan.id), None,
            )
        decided = bool(round_) and any(
            value["verdict"] in stats.DECIDED for factor in round_["factors"] for value in factor["values"]
        )
        mode = snapshot.evidence_mode if snapshot is not None else None
        return {
            "plan_id": plan.id,
            "product_id": plan.product_id,
            "status": "ready" if round_ else "no_data",
            "evidence_mode": mode,
            "evidence_notice": EVIDENCE_NOTICE.get(mode) if mode else None,
            "available_modes": available,
            "snapshot_id": snapshot.id if snapshot is not None else None,
            "computed_at": _iso(snapshot.created_at) if snapshot is not None else None,
            "thresholds": (snapshot.result_json or {}).get("thresholds") if snapshot is not None else stats.DEFAULT_THRESHOLDS.as_dict(),
            "round": _round_view(round_) if round_ else None,
            "can_seed_next_round": decided,
        }


def results_template(ctx: TenantContext, product_id: str) -> dict:
    """The rows of the downloadable CSV template: one per image of the product."""
    require_member(ctx)
    with tenant_session(ctx) as session:
        product = resolve_product(session, ctx, product_id)
        attribution = ProductAttribution(session, ctx, product.id)
        currency = _established_currency(session, ctx, product.id)
        infos = sorted(
            attribution.assets.values(),
            key=lambda info: (
                -(attribution.plans[info.plan_id].round_number if info.plan_id in attribution.plans else 0),
                info.direction_key or "", info.created_at, info.asset_version_id,
            ),
        )
        # A downloaded file: MarketAI's words follow the requester's language (#255).
        language = ctx.ui_language
        rows = []
        for info in infos:
            catalog = attribution.plans.get(info.plan_id) if info.plan_id else None
            rows.append({
                "asset_code": info.code,
                "round_number": catalog.round_number if catalog else None,
                "direction": catalog.direction_title(info.direction_key) if catalog else attribution.describe(info, language),
                "combination": attribution.describe(info, language),
                "attributable": info.unattributable is None,
                "evidence_mode": info.evidence_mode,
            })
        fallback = parsing.TEMPLATE_PRODUCT_FALLBACK[language]
        name = (product.name or fallback).strip() or fallback
        return {
            "product_id": product.id,
            "file_name": parsing.TEMPLATE_FILE_NAME[language].format(name=name),
            "columns": [{"key": key, "label": label} for key, label in parsing.TEMPLATE_HEADERS[language]],
            "currency": currency,
            "rows": rows,
        }


# ---------------------------------------------------------------------------
# For the plan drafter (#164): learned value verdicts
# ---------------------------------------------------------------------------


def value_verdicts(session, ctx: TenantContext, product_id: str, snapshot_id: str | None) -> tuple[list[dict], dict | None]:
    """(decided value verdicts, basis) from a snapshot of this product.

    ``snapshot_id`` given: that snapshot (404 when it is not this product's).
    Otherwise the latest *production* snapshot, if any -- Mock or Sandbox
    learnings never seed a plan unless the user explicitly asked for it.
    """
    if snapshot_id:
        snapshot = session.scalars(
            select(LearningSnapshotRecord).where(
                LearningSnapshotRecord.workspace_id == ctx.workspace_id,
                LearningSnapshotRecord.id == snapshot_id,
            )
        ).one_or_none()
        if snapshot is None or snapshot.product_id != product_id:
            raise LearningsProblem(404, "learnings_snapshot_not_found", "这份结论不存在或不属于这个产品")
    else:
        snapshot = latest_snapshot(session, ctx, product_id, "production")
        if snapshot is None:
            return [], None
    verdicts = []
    for round_ in sorted((snapshot.result_json or {}).get("rounds") or [], key=lambda item: -item["round_number"]):
        for factor in round_["factors"]:
            for value in factor["values"]:
                if value["verdict"] not in stats.DECIDED:
                    continue
                main = value[value["verdict_metric"]]
                verdicts.append({
                    "factor_key": factor["key"],
                    "factor_label": factor["label"],
                    "value_id": value["value_id"],
                    "value_label": value["label"],
                    "verdict": value["verdict"],
                    "metric": value["verdict_metric"],
                    "lift": main.get("lift"),
                    "lift_low": main.get("lift_low"),
                    "lift_high": main.get("lift_high"),
                    "sentence": value["sentence"],
                    "plan_id": round_["plan_id"],
                    "round_number": round_["round_number"],
                    "evidence_mode": snapshot.evidence_mode,
                })
    basis = {
        "snapshot_id": snapshot.id,
        "evidence_mode": snapshot.evidence_mode,
        "explicit": bool(snapshot_id),
        "winners": [f"{v['factor_label']}：{v['value_label']}" for v in verdicts if v["verdict"] == "winner"],
        "losers": [f"{v['factor_label']}：{v['value_label']}" for v in verdicts if v["verdict"] == "loser"],
        "no_difference": [f"{v['factor_label']}：{v['value_label']}" for v in verdicts if v["verdict"] == "no_difference"],
    }
    return verdicts, basis
