"""Bounded Meta CSV intake with explicit provenance and correction chains."""
import csv
import hashlib
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .service import MarketingError, digest
from .workflow import scoped, active_batches, audit
from .workflow_models import MarketingAdBindingRecord as Binding, MarketingMetricBatchRecord as Batch
from app.adapters.persistence.models import new_id

ALIASES = {
    "date": ("date", "Day", "Reporting starts"),
    "ad_id": ("ad_id", "Ad ID"),
    "spend": ("spend", "Amount spent"),
    "impressions": ("impressions", "Impressions"),
    "clicks": ("clicks", "Link clicks"),
    "purchases": ("purchases", "Website purchases", "Purchases"),
    "purchase_value": ("purchase_value", "Website purchases conversion value", "Purchases conversion value"),
}

#: Every preflight issue code and the params it carries (#255). An issue is
#: ``{row, field, code}`` plus ``params`` when the code has any; the browser
#: words it from the ``error-<code>`` table (#253), so no sentence is stored
#: here. ``web/__tests__/locales/error-codes.spec.ts`` reads these keys and
#: ``text-codes.spec.ts`` checks the params and ISSUE_FIELDS against the copy.
ISSUE_PARAMS = {
    "reporting_context_mismatch": ("timezone", "currency", "attribution_window"),
    "execution_mode_mismatch": (),
    "account_or_binding_missing": (),
    "invalid_headers": (),
    "mapped_column_missing": ("column",),
    "ambiguous_column_mapping": ("columns",),
    "unknown_mapping_field": (),
    "reused_column_mapping": (),
    "column_currency_mismatch": ("expected", "found"),
    "required_column_missing": (),
    "too_many_rows": ("limit",),
    "malformed_row": (),
    "row_currency_mismatch": ("expected", "found"),
    "row_attribution_mismatch": ("expected", "found"),
    "unbound_ad": (),
    "outside_round_dates": ("start", "end"),
    "daily_rows_required": (),
    "invalid_date": (),
    "duplicate_ad_day": (),
    "missing_value": (),
    "invalid_nonnegative_number": (),
    "clicks_exceed_impressions": (),
    "invalid_csv": (),
    "no_daily_rows": (),
    "replacement_not_current": (),
    "replacement_scope_mismatch": (),
    "already_imported_ad_day": (),
}
#: The ``field`` an issue points at: a metric key or one of the file-level parts.
ISSUE_FIELDS = ("date", "ad_id", "spend", "impressions", "clicks", "purchases", "purchase_value",
                "context", "source_mode", "ad_account_id", "headers", "file", "row", "currency",
                "attribution_window", "replacement")
MAX_DAILY_ROWS = 5000


def _bounded(value):
    # Some values echo the uploaded file; keep what is stored and shown short.
    if isinstance(value, list):
        return [str(item)[:100] for item in value[:10]]
    return value if isinstance(value, int) else str(value)[:100]


def preflight_issue(number, field, code, **params):
    if code not in ISSUE_PARAMS or set(params) != set(ISSUE_PARAMS[code]) or field not in ISSUE_FIELDS:
        raise ValueError(f"undeclared preflight issue: {code} {field} {sorted(params)}")
    return {"row": number, "field": field, "code": code,
            **({"params": {k: _bounded(v) for k, v in params.items()}} if params else {})}


def preview(session, ctx, row, body):
    if len(body.csv_text.encode("utf-8")) > 262144:
        raise MarketingError("csv_too_large", "回传 CSV 不能超过 256KB，请按本轮日期导出每日广告数据。")
    metadata = body.model_dump(mode="json", exclude={"csv_text", "replaces_batch_id"})
    metadata["ad_account_id"] = body.ad_account_id.removeprefix("act_")
    fingerprint = digest({"metadata": metadata, "csv": body.csv_text, "replaces": body.replaces_batch_id})
    prior = scoped(session, ctx, Batch, round_id=row.id, request_hash=fingerprint)
    if prior:
        return prior[0]
    issues, rows = [], []
    def issue(number, field, code, **params):
        if len(issues) < 100:
            issues.append(preflight_issue(number, field, code, **params))
    if body.account_timezone != row.timezone or body.currency != row.snapshot_json["currency"] or body.attribution_window != row.snapshot_json["attribution_window"]:
        issue(0, "context", "reporting_context_mismatch", timezone=row.timezone,
              currency=row.snapshot_json["currency"], attribution_window=row.snapshot_json["attribution_window"])
    expected_source = "controlled_mock" if row.execution_mode == "mock" else "customer_file"
    if body.source_mode != expected_source:
        issue(0, "source_mode", "execution_mode_mismatch")
    bindings = {b.ad_id: b for b in scoped(session, ctx, Binding, round_id=row.id)}
    if not bindings or any(b.ad_account_id != metadata["ad_account_id"] for b in bindings.values()):
        issue(0, "ad_account_id", "account_or_binding_missing")
    try:
        reader = csv.DictReader(io.StringIO(body.csv_text.lstrip("\ufeff"), newline=""), strict=True)
        fields = reader.fieldnames or []
        if not fields or len(set(fields)) != len(fields) or len(fields) > 100:
            issue(1, "headers", "invalid_headers")
        mapping = {}
        for key, aliases in ALIASES.items():
            if key == 'spend':
                aliases = ('spend', 'Amount spent', f'Amount spent ({body.currency})')
            candidates = [f for f in fields if f in aliases]
            selected = body.field_mapping.get(key)
            if selected:
                if selected not in fields:
                    issue(1, key, "mapped_column_missing", column=selected)
                mapping[key] = selected
            elif len(candidates) == 1:
                mapping[key] = candidates[0]
            elif len(candidates) > 1:
                issue(1, key, "ambiguous_column_mapping", columns=candidates)
        if set(body.field_mapping) - ALIASES.keys():
            issue(1, "headers", "unknown_mapping_field")
        if len(set(mapping.values())) != len(mapping):
            issue(1, "headers", "reused_column_mapping")
        for key in ('spend', 'purchase_value'):
            currency_suffix = re.search(r'\(([A-Z]{3})\)$', mapping.get(key, ''))
            if currency_suffix and currency_suffix.group(1) != body.currency:
                issue(1, key, 'column_currency_mismatch', expected=body.currency, found=currency_suffix.group(1))
        metric = row.snapshot_json["plan"]["rules"]["business_metric"]
        required = {"date", "ad_id", "spend", "impressions", "clicks"}
        if metric in ("cpa", "roas"):
            required.add("purchases")
        if metric == "roas":
            required.add("purchase_value")
        for key in required - mapping.keys():
            issue(1, key, "required_column_missing")
        seen = set()
        for number, record in enumerate(reader, start=2):
            if number > MAX_DAILY_ROWS + 1:
                issue(number, "file", "too_many_rows", limit=MAX_DAILY_ROWS)
                break
            if None in record or any(v is None for v in record.values()):
                issue(number, "row", "malformed_row")
                continue
            values = {key: str(record.get(column, "")).strip() for key, column in mapping.items()}
            reported_currency = record.get('Currency', record.get('currency', '')).strip()
            if reported_currency and reported_currency.upper() != body.currency:
                issue(number, 'currency', 'row_currency_mismatch', expected=body.currency, found=reported_currency)
            reported_attribution = record.get('attribution_window', '').strip()
            if reported_attribution and reported_attribution != body.attribution_window:
                issue(number, 'attribution_window', 'row_attribution_mismatch', expected=body.attribution_window, found=reported_attribution)
            ad = values.get("ad_id", "")
            if ad not in bindings:
                issue(number, "ad_id", "unbound_ad")
            try:
                day = date.fromisoformat(values.get("date", ""))
                if not row.start_date <= day <= row.end_date:
                    issue(number, "date", "outside_round_dates", start=str(row.start_date), end=str(row.end_date))
                if "Reporting ends" in record and record["Reporting ends"].strip() != str(day):
                    issue(number, "date", "daily_rows_required")
            except ValueError:
                issue(number, "date", "invalid_date")
                continue
            key = (str(day), ad)
            if key in seen:
                issue(number, "ad_id", "duplicate_ad_day")
            seen.add(key)
            item = {"date": str(day), "ad_id": ad, "entry_id": bindings[ad].entry_id if ad in bindings else None}
            for field in ("spend", "impressions", "clicks", "purchases", "purchase_value"):
                value = values.get(field, "")
                if not value:
                    item[field] = None
                    if field in required:
                        issue(number, field, "missing_value")
                    continue
                try:
                    num = Decimal(value)
                    if not num.is_finite() or num < 0 or num > Decimal("1000000000"):
                        raise ValueError()
                    if field in ("impressions", "clicks"):
                        if num != num.to_integral_value():
                            raise ValueError()
                        item[field] = int(num)
                    else:
                        if num.as_tuple().exponent < -6:
                            raise ValueError()
                        item[field] = format(num, "f")
                except (InvalidOperation, ValueError):
                    issue(number, field, "invalid_nonnegative_number")
                    item[field] = None
            if item.get("clicks") is not None and item.get("impressions") is not None and item["clicks"] > item["impressions"]:
                issue(number, "clicks", "clicks_exceed_impressions")
            rows.append(item)
        metadata["resolved_mapping"] = mapping
    except (csv.Error, UnicodeError):
        issue(0, "file", "invalid_csv")
    if not rows:
        issue(0, "file", "no_daily_rows")
    record = Batch(id=new_id("mmb"), workspace_id=ctx.workspace_id, round_id=row.id,
        replaces_id=body.replaces_batch_id, request_hash=fingerprint,
        raw_hash=hashlib.sha256(body.csv_text.encode()).hexdigest(), raw_csv=body.csv_text,
        metadata_json=metadata, rows_json=rows, issues_json=issues, state="preview")
    # Never persist an arbitrary replacement FK supplied by another tenant.
    if body.replaces_batch_id:
        replaced = scoped(session, ctx, Batch, id=body.replaces_batch_id, round_id=row.id)
        if not replaced:
            raise MarketingError("replacement_not_found", "找不到本轮待更正的数据批次。", 404)
    issues.extend(validate_conflicts(session, ctx, row, record))
    record.issues_json = issues[:100]
    session.add(record)
    session.flush()
    audit(session, row, "metrics_previewed", {"batch_id": record.id, "raw_hash": record.raw_hash, "issues": len(issues)})
    return record


def keys(batch):
    return {(r["date"], r["ad_id"]) for r in batch.rows_json}


def validate_conflicts(session, ctx, row, record):
    issues = []
    active = active_batches(session, ctx, row)
    replaced = next((b for b in active if b.id == record.replaces_id), None)
    if record.replaces_id:
        if not replaced:
            issues.append(preflight_issue(0, "replacement", "replacement_not_current"))
        elif keys(replaced) != keys(record) or any(record.metadata_json[k] != replaced.metadata_json[k]
                for k in ("ad_account_id", "account_timezone", "currency", "attribution_window", "source_mode")):
            issues.append(preflight_issue(0, "replacement", "replacement_scope_mismatch"))
    occupied = {key for b in active if b.id != record.replaces_id and b.id != record.id for key in keys(b)}
    if occupied & keys(record):
        issues.append(preflight_issue(0, "ad_id", "already_imported_ad_day"))
    return issues


def confirm(session, ctx, row, body):
    candidates = scoped(session, ctx, Batch, id=body.batch_id, round_id=row.id)
    if not candidates:
        raise MarketingError("batch_not_found", "找不到本轮数据预检。", 404)
    batch = candidates[0]
    if batch.state == "confirmed":
        return batch
    if batch.issues_json or validate_conflicts(session, ctx, row, batch):
        raise MarketingError("metrics_preflight_failed", "回传存在匹配、重复或数据问题，请修正后重新预检。", 409)
    batch.state = "confirmed"
    if row.state != "closed":
        row.state = "awaiting_data"
    audit(session, row, "metrics_confirmed", {"batch_id": batch.id, "raw_hash": batch.raw_hash, "replaces_id": batch.replaces_id})
    return batch
