"""Full reports for a test project's rounds (ADR 0019 decision 6, step 2 design §5.7, #251).

The user uploads Meta's daily report "from the start of the test to today", again and again. Each upload is an
ordinary metrics batch, read by ``metrics.read_rows`` and kept as it came: no upload is deleted or rewritten.
What the round counts is worked out when it is read:

* for each day and Meta ad, the row of the latest confirmed report (the one uploaded last): a later report
  replaces the days it repeats and leaves the others;
* only this round's matched Meta ads on its days: the report's other rows are kept, not counted, and listed.

Counting on read means a Meta ad matched later (#252) or a moved start or early end (#253) changes what
counts without touching an upload.

Which ad each Meta ad shows (#252) is suggested from the short keys in the report's ad names -- the package's
``MA-`` key first, then the image's ``MK-`` code -- among this round's ads only, and confirmed in one command.
Before the round's first report is confirmed a match can be changed or taken away; each change is a new
record replacing the current one, and the old record stays. Afterwards matches can only be added. A plan-first round keeps its own rules (``metrics``): each day and ad is
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

from sqlalchemy import select

from app.adapters.persistence.models import new_id, utcnow
from app.learnings.codes import asset_code, codes_in_text
from . import ad_tests, metrics
from .export_text import ad_key
from .reporting import MEDIA_CURRENCIES
from .service import MarketingError, digest
from .workflow import audit, current_bindings, get_round, round_bindings, scoped
from .workflow_models import (MarketingAdBindingRecord as Binding, MarketingHandoffRecord as Handoff,
                              MarketingMetricBatchRecord as Batch)

#: Plan-first preflight issues a full report does not raise: rows of other ads or days are kept, not refused;
#: each setting it does not share with the frozen round is named with what the report has
#: (``report_*_mismatch``); and a test takes only the customer's own file, whatever the round's mode.
NOT_REFUSED = frozenset({"unbound_ad", "outside_round_dates", "account_or_binding_missing",
                         "reporting_context_mismatch", "execution_mode_mismatch"})
#: An amount column names its currency at its end, as Ads Manager exports it: "Amount spent (EUR)".
COLUMN_CURRENCY = re.compile(r"\(([A-Z]{3})\)\s*$")
#: The package's MA key inside a Meta ad name (``export_text.ad_key``): the first 12 characters of the entry id.
MA_KEY = re.compile(r"(?<![0-9A-Za-z])MA-([0-9a-f]{12})(?![0-9A-Za-z])", re.IGNORECASE)
#: A report column holding the Meta ad name, as Ads Manager exports it ("Ad name") or a template names it.
AD_NAME_COLUMNS = ("ad name", "ad_name")
#: The names of one Meta ad a suggestion shows, and how long each may be.
SHOWN_NAMES, NAME_LENGTH = 5, 255
#: A test round's match records no deviation: a test's deviations are recorded on their own (#254).
TEST_CONDITIONS = {"conditions_match": True, "deviations": []}


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
    # Each matched Meta ad of the round and the package entry it shows now.
    return {b.ad_id: b.entry_id for b in round_bindings(session, ctx, row)}


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
    bindings = {b.ad_id: b for b in round_bindings(session, ctx, row)}
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


def _handoff(session, ctx, row):
    handoffs = scoped(session, ctx, Handoff, round_id=row.id)
    if not handoffs or handoffs[0].state != "ready":
        raise MarketingError("handoff_not_ready", "请先生成广告包，投放之后再确认每条 Meta 广告是哪张图。", 409)
    return handoffs[0]


def _locked(session, ctx, row) -> bool:
    """Once a report of the round is confirmed its matches can only be added (design §5.7)."""
    from .ad_test_schedule import report_confirmed
    return report_confirmed(session, ctx, row)


def _report_names(batch):
    """(the report's ad name column or ``None``, each Meta ad ID's distinct names in report order)."""
    names = {item["ad_id"]: [] for item in batch.rows_json if item["ad_id"]}
    id_column = (batch.metadata_json.get("resolved_mapping") or {}).get("ad_id")
    try:
        reader = csv.DictReader(io.StringIO(batch.raw_csv.lstrip("\ufeff"), newline=""))
        column = next((f for f in reader.fieldnames or [] if f and f.strip().lower() in AD_NAME_COLUMNS), None)
        if column is None or id_column is None:
            return column, names
        for record in reader:
            ad, name = str(record.get(id_column) or "").strip(), str(record.get(column) or "").strip()
            if ad in names and name and name not in names[ad]:
                names[ad].append(name)
    except (csv.Error, UnicodeError):
        return None, names
    return column, names


def _from_keys(names, by_key, by_code):
    """(status, entry, kind of key, candidates) a Meta ad's names suggest among this round's ads.

    The package's MA keys decide when the names have any, else the images' MK codes. One key that one ad of
    the round has is a suggestion; two keys, or a key several ads have, are for the user to choose; a key no
    ad of the round has (another round's, say) suggests nothing."""
    ma = list(dict.fromkeys(m.group(1).lower() for name in names for m in MA_KEY.finditer(name)))
    mk = list(dict.fromkeys(code for name in names for code in codes_in_text(name)))
    if not ma and not mk:
        return "no_match", None, None, []
    kind, keys, index = ("ma_key", ma, by_key) if ma else ("mk_code", mk, by_code)
    candidates = list(dict.fromkeys(entry for key in keys for entry in index.get(key, [])))
    if not candidates:
        return "no_match", None, kind, []
    if len(keys) == 1 and len(candidates) == 1:
        return "suggested", candidates[0], kind, candidates
    return "ambiguous", None, kind, candidates


def _elsewhere(session, ctx, row, account, ads):
    """Each Meta ad's current matches in the Workspace's other rounds."""
    if not ads:
        return {}
    records = session.scalars(select(Binding).where(Binding.workspace_id == ctx.workspace_id,
        Binding.ad_account_id == account, Binding.ad_id.in_(sorted(ads)))).all()
    found = {}
    for record in current_bindings(records):
        if record.round_id != row.id:
            found.setdefault(record.ad_id, []).append(record)
    return found


def suggest_bindings(session, ctx, row, body) -> dict:
    """``MarketingTestBindingSuggestions``: the ad each Meta ad of an uploaded report shows, from its name.

    Reads only: the suggestion is worked out from the stored report and stored nowhere."""
    found = scoped(session, ctx, Batch, id=body.batch_id, round_id=row.id)
    if not found:
        raise MarketingError("batch_not_found", "找不到这一轮的这份报表。", 404)
    batch = found[0]
    entries = _handoff(session, ctx, row).manifest_json["entries"]
    by_key, by_code = {}, {}
    for entry in entries:
        by_key.setdefault(ad_key(entry["entry_id"])[3:].lower(), []).append(entry["entry_id"])
        by_code.setdefault(asset_code(ctx.workspace_id, entry["asset_version_id"]), []).append(entry["entry_id"])
    account = batch.metadata_json["ad_account_id"]
    column, names = _report_names(batch)
    current = _matched(session, ctx, row)
    elsewhere = _elsewhere(session, ctx, row, account, names)
    ads = []
    for ad, ad_names in names.items():
        status, entry, kind, candidates = _from_keys(ad_names, by_key, by_code)
        others = sorted({b.round_id for b in elsewhere.get(ad, [])})
        if ad in current:
            status, entry = "bound", current[ad]
        elif others:
            status, entry = "bound_elsewhere", None
        ads.append({"ad_id": ad, "ad_names": [n[:NAME_LENGTH] for n in ad_names[:SHOWN_NAMES]], "status": status,
                    "entry_id": entry, "key_kind": kind, "candidate_entry_ids": candidates, "other_round_ids": others})
    return {"round_id": row.id, "batch_id": batch.id, "ad_account_id": account, "ad_name_column": column,
            "locked": _locked(session, ctx, row), "ads": ads,
            "entries": [{"entry_id": e["entry_id"], "ad_name": e["ad_name"], "display_name": e.get("display_name"),
                         "asset_version_id": e["asset_version_id"],
                         "asset_code": asset_code(ctx.workspace_id, e["asset_version_id"])} for e in entries]}


def _heads(records):
    """Each Meta ad's latest record in the round: its current match, or the record that took it away."""
    replaced = {b.replaces_id for b in records if b.replaces_id}
    return {b.ad_id: b for b in records if b.id not in replaced}


def confirm_bindings(session, ctx, row, body):
    """Write every match the user confirmed, in one go: if one is refused, none is written (design §5.7).

    A choice the round already has changes nothing. Before the round's first report is confirmed a choice may
    change a Meta ad's match or take it away (``entry_id`` null); afterwards only a Meta ad not matched now may
    be. Each change is a new record replacing the Meta ad's latest one, which stays as it was. A Meta ad
    matched in another round on overlapping days is refused, as for any round."""
    handoff = _handoff(session, ctx, row)
    entries = {e["entry_id"] for e in handoff.manifest_json["entries"]}
    account = body.ad_account_id.removeprefix("act_")
    heads = _heads(scoped(session, ctx, Binding, round_id=row.id))
    current = {ad: b for ad, b in heads.items() if not b.removed}
    locked = _locked(session, ctx, row)
    changes = []
    for choice in body.bindings:
        if choice.entry_id is not None and choice.entry_id not in entries:
            raise MarketingError("entry_not_found", "请选择本轮广告包里的广告。")
        bound = current.get(choice.ad_id)
        if (bound.entry_id if bound else None) == choice.entry_id:
            continue
        if bound and locked:
            raise MarketingError("test_bindings_locked", "这一轮已经确认过报表，已有的对应不能再改或删，只能补上新的 Meta 广告。", 409)
        changes.append((choice, heads.get(choice.ad_id)))
    taken_away = {choice.ad_id for choice, _ in changes if choice.entry_id is None}
    kept = {b.ad_account_id for ad, b in current.items() if ad not in taken_away}
    if any(choice.entry_id is not None for choice in body.bindings) and kept - {account}:
        raise MarketingError("account_mismatch", "同一小轮必须使用同一个广告账户。")
    start, end = counting_dates(session, ctx, row)
    new = {choice.ad_id for choice, _ in changes if choice.entry_id is not None and choice.ad_id not in current}
    dates = {}
    for other in (b for found in _elsewhere(session, ctx, row, account, new).values() for b in found):
        if other.round_id not in dates:
            dates[other.round_id] = counting_dates(session, ctx, get_round(session, ctx, other.round_id))
        other_start, other_end = dates[other.round_id]
        if other_start <= end and other_end >= start:
            raise MarketingError("overlapping_ad_binding", "该广告在这个日期范围内已对应其他记录，请核对广告 ID。", 409)
    if not changes:
        return row
    # One moment for the whole confirmation: its records are read back together, in Meta ad order.
    confirmed_at = utcnow()
    records = [Binding(id=new_id("mab"), workspace_id=ctx.workspace_id, round_id=row.id, handoff_id=handoff.id,
                       entry_id=choice.entry_id or head.entry_id, ad_account_id=head.ad_account_id if choice.entry_id is None else account,
                       ad_id=choice.ad_id, start_date=start, end_date=end,
                       conditions_json=head.conditions_json if head else dict(TEST_CONDITIONS),
                       replaces_id=head.id if head else None, removed=choice.entry_id is None, actor_id=ctx.initiator_id,
                       created_at=confirmed_at) for choice, head in changes]
    session.add_all(records)
    session.flush()
    if row.state != "closed":
        row.state = "awaiting_data"
    audit(session, row, "test_bindings_confirmed", {"binding_ids": [r.id for r in records],
        "added": sum(not r.replaces_id for r in records), "removed": sum(r.removed for r in records),
        "changed": sum(bool(r.replaces_id) and not r.removed for r in records)})
    return row


def bindings_view(session, ctx, row) -> dict:
    """``MarketingTestBindings``: the round's current matches and every record behind them, oldest first."""
    records = sorted(scoped(session, ctx, Binding, round_id=row.id), key=lambda b: (_utc(b.created_at), b.ad_id, b.id))
    current = current_bindings(records)
    ids = {b.id for b in current}
    return {"round_id": row.id, "ad_account_id": current[0].ad_account_id if current else None,
            "locked": _locked(session, ctx, row),
            "bindings": [{"id": b.id, "ad_id": b.ad_id, "entry_id": b.entry_id, "actor_id": b.actor_id,
                          "created_at": _utc(b.created_at).isoformat()} for b in sorted(current, key=lambda b: b.ad_id)],
            "history": [{"id": b.id, "ad_id": b.ad_id, "entry_id": None if b.removed else b.entry_id,
                         "replaces_id": b.replaces_id, "current": b.id in ids, "actor_id": b.actor_id,
                         "created_at": _utc(b.created_at).isoformat()} for b in records]}
