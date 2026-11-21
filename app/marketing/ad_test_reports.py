"""Full reports for a test project's rounds (ADR 0019 decision 6, step 2 design §5.7, #251).

The user uploads Meta's daily report "from the start of the test to today", again and again. Each upload is an
ordinary metrics batch, read by ``metrics.read_rows`` and kept as it came: no upload is deleted or rewritten.
What the round counts is worked out when it is read:

* for each day and Meta ad, the row of the latest confirmed report (the one uploaded last): a later report
  replaces the days it repeats and leaves the others;
* only this round's matched Meta ads on its days: the report's other rows are kept, not counted, and listed.

Counting on read means a Meta ad matched later (#252) or a moved start or early end (#253) changes what
counts without touching an upload. A plan-first round keeps its own rules (``metrics``): each day and ad is
imported once and a correction replaces a whole batch; its paths refuse a test's round, and these paths never
reach its rounds.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, timezone
from decimal import Decimal

from app.adapters.persistence.models import new_id
from . import ad_tests, metrics
from .reporting import MEDIA_CURRENCIES
from .service import MarketingError, digest
from .workflow import audit, get_round, scoped
from .workflow_models import (MarketingAdBindingRecord as Binding, MarketingHandoffRecord as Handoff,
                              MarketingMetricBatchRecord as Batch)

#: Plan-first preflight issues a full report does not raise: rows of other ads or days are kept, not refused;
#: each setting it does not share with the frozen round is named with what the report has
#: (``report_*_mismatch``); and a test takes only the customer's own file, whatever the round's mode.
NOT_REFUSED = frozenset({"unbound_ad", "outside_round_dates", "account_or_binding_missing",
                         "reporting_context_mismatch", "execution_mode_mismatch"})
#: An amount column names its currency at its end, as Ads Manager exports it: "Amount spent (EUR)".
COLUMN_CURRENCY = re.compile(r"\(([A-Z]{3})\)\s*$")


def test_round(session, ctx, test_id, round_id):
    """A round of this Workspace's test ``test_id``; a round of another test or a plan-first test is not found."""
    test = ad_tests.get_test(session, ctx, test_id)
    row = get_round(session, ctx, round_id)
    if row.project_id != test.id:
        raise MarketingError("round_not_found", "找不到这轮测试。", 404)
    return test, row


def counting_dates(session, ctx, row) -> tuple[date, date]:
    """The days a round counts: its effective dates, after a moved start or an early end (#253)."""
    from .ad_test_schedule import effective_dates
    return effective_dates(session, ctx, row)


def _utc(value):
    # SQLite reads stored times back naive; they were written in UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _order(batch):
    # Uploads are serialised per round (``workflow.mutate``), so the time each was kept orders them.
    return _utc(batch.created_at), batch.id


def confirmed(session, ctx, row):
    """The round's confirmed full reports, oldest upload first."""
    return sorted(scoped(session, ctx, Batch, round_id=row.id, state="confirmed"), key=_order)


def _matched(session, ctx, row):
    # Each matched Meta ad of the round and the package entry it shows.
    return {b.ad_id: b.entry_id for b in scoped(session, ctx, Binding, round_id=row.id)}


def _left_out(item, matched, start, end):
    """Why a report row is not counted, or ``None`` when it is this round's ad on one of its days."""
    if item["ad_id"] not in matched:
        return "other_ad"
    day = date.fromisoformat(item["date"])
    return "before_start" if day < start else "after_end" if day > end else None


def counted(session, ctx, row):
    """The rows the round counts, by day then Meta ad: each day and ad's latest confirmed row, its own only."""
    matched = _matched(session, ctx, row)
    start, end = counting_dates(session, ctx, row)
    latest = {}
    for batch in confirmed(session, ctx, row):
        for item in batch.rows_json:
            latest[(item["date"], item["ad_id"])] = item
    return sorted(({**item, "entry_id": matched[item["ad_id"]]} for item in latest.values()
                   if _left_out(item, matched, start, end) is None), key=lambda d: (d["date"], d["ad_id"]))


def _column_currencies(text):
    try:
        header = next(csv.reader(io.StringIO(text.lstrip("﻿"), newline="")), [])
    except csv.Error:
        return set()
    return {m.group(1) for column in header if (m := COLUMN_CURRENCY.search(column)) and m.group(1) in MEDIA_CURRENCIES}


def preview(session, ctx, row, body):
    """Keep one upload of the round's full report with what stops it being confirmed; the same upload is kept once."""
    if len(body.csv_text.encode("utf-8")) > 262144:
        raise MarketingError("csv_too_large", "回传 CSV 不能超过 256KB，请按本轮日期导出每日广告数据。")
    handoffs = scoped(session, ctx, Handoff, round_id=row.id)
    if not handoffs or handoffs[0].state != "ready":
        raise MarketingError("handoff_not_ready", "请先生成广告包，投放之后再上传报表。", 409)
    metadata = body.model_dump(mode="json", exclude={"csv_text"})
    metadata["ad_account_id"] = body.ad_account_id.removeprefix("act_")
    fingerprint = digest({"full_report": metadata, "csv": body.csv_text})
    prior = scoped(session, ctx, Batch, round_id=row.id, request_hash=fingerprint)
    if prior:
        return prior[0]
    issues = []

    def issue(number, field, code, **params):
        if code not in NOT_REFUSED and len(issues) < 100:
            issues.append(metrics.preflight_issue(number, field, code, **params))
    frozen = row.snapshot_json
    if body.account_timezone != row.timezone:
        issue(0, "context", "report_timezone_mismatch", expected=row.timezone, found=body.account_timezone)
    # The account's currency as the user states it, and as the amount columns name it.
    for found in sorted({body.currency, *_column_currencies(body.csv_text)} - {frozen["currency"]}):
        issue(0, "currency", "report_currency_mismatch", expected=frozen["currency"], found=found)
    if body.attribution_window != frozen["attribution_window"]:
        issue(0, "attribution_window", "report_attribution_mismatch", expected=frozen["attribution_window"],
              found=body.attribution_window)
    bindings = {b.ad_id: b for b in scoped(session, ctx, Binding, round_id=row.id)}
    for expected in sorted({b.ad_account_id for b in bindings.values()} - {metadata["ad_account_id"]}):
        issue(0, "ad_account_id", "report_account_mismatch", expected=expected, found=metadata["ad_account_id"])
    rows, mapping = metrics.read_rows(row, body, bindings, issue)
    if mapping is not None:
        metadata["resolved_mapping"] = mapping
    record = Batch(id=new_id("mmb"), workspace_id=ctx.workspace_id, round_id=row.id, replaces_id=None,
                   request_hash=fingerprint, raw_hash=hashlib.sha256(body.csv_text.encode()).hexdigest(),
                   raw_csv=body.csv_text, metadata_json=metadata, rows_json=rows, issues_json=issues, state="preview")
    session.add(record)
    session.flush()
    audit(session, row, "test_report_previewed", {"batch_id": record.id, "raw_hash": record.raw_hash, "issues": len(issues)})
    return record


def confirm(session, ctx, row, body):
    """Count an upload from now on; a report older than one already confirmed would undo newer days, so it is refused."""
    found = scoped(session, ctx, Batch, id=body.batch_id, round_id=row.id)
    if not found:
        raise MarketingError("batch_not_found", "找不到这一轮的这份报表。", 404)
    batch = found[0]
    if batch.state == "confirmed":
        return batch
    if batch.issues_json:
        raise MarketingError("metrics_preflight_failed", "报表里有要先处理的问题，请修正后重新上传。", 409)
    if any(_order(other) > _order(batch) for other in confirmed(session, ctx, row)):
        raise MarketingError("test_report_outdated", "已经确认过一份更新的报表，这份较早的不再计入；请上传最新的完整报表。", 409)
    batch.state = "confirmed"
    if row.state != "closed":
        row.state = "awaiting_data"
    audit(session, row, "test_report_confirmed", {"batch_id": batch.id, "raw_hash": batch.raw_hash})
    return batch


def report(session, ctx, row, batch):
    """``MarketingTestReport``: one upload, what it counts, replaces and leaves out under the round as it is now."""
    matched = _matched(session, ctx, row)
    start, end = counting_dates(session, ctx, row)
    others = [b for b in confirmed(session, ctx, row) if b.id != batch.id]
    earlier = {key for b in others if _order(b) < _order(batch) for key in metrics.keys(b)}
    later = {key for b in others if _order(b) > _order(batch) for key in metrics.keys(b)}
    counts = {"counted_rows": 0, "replaces_rows": 0, "replaced_rows": 0}
    left_out = {}
    for item in batch.rows_json:
        key = (item["date"], item["ad_id"])
        counts["replaces_rows"] += key in earlier
        counts["replaced_rows"] += key in later
        reason = _left_out(item, matched, start, end)
        if reason is None:
            counts["counted_rows"] += key not in later
            continue
        group = left_out.setdefault((item["ad_id"], reason), {"ad_id": item["ad_id"], "reason": reason,
            "first_date": item["date"], "last_date": item["date"], "rows": 0, "spend": Decimal("0")})
        group["first_date"], group["last_date"] = min(group["first_date"], item["date"]), max(group["last_date"], item["date"])
        group["rows"] += 1
        group["spend"] += Decimal(item["spend"]) if item.get("spend") is not None else Decimal("0")
    days = sorted(item["date"] for item in batch.rows_json)
    meta = batch.metadata_json
    return {"id": batch.id, "round_id": row.id, "state": batch.state, "filename": meta["filename"],
            "raw_hash": batch.raw_hash, "created_at": _utc(batch.created_at).isoformat(), "ad_account_id": meta["ad_account_id"],
            "account_timezone": meta["account_timezone"], "currency": meta["currency"],
            "attribution_window": meta["attribution_window"], "first_date": days[0] if days else None,
            "last_date": days[-1] if days else None, "row_count": len(batch.rows_json), "issues": batch.issues_json,
            **counts, "excluded": [{**group, "spend": str(group["spend"])}
                                   for group in sorted(left_out.values(), key=lambda g: (g["ad_id"], g["first_date"], g["reason"]))]}
