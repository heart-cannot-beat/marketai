"""This round's schedule: a moved start or an early end of a test project's round (#253, design §5.7, §5.9, E9).

A round's frozen snapshot -- its dates, plan and protocol -- is never rewritten. A user who put the ads live on
another day than planned moves the start (before the round's first report is confirmed; the end moves with
it, the number of days stays), and a user who stopped them early records the last day they ran. Each change
is a new ``MarketingRoundScheduleChangeRecord`` naming who made it and when; the latest of each kind wins.

The dates an evaluation uses -- the "effective" start and end -- are the frozen ones plus those changes. Only
rounds of test projects (``flow = "ads_test"``) have them: a round of an earlier test is evaluated on its frozen
dates with exactly the inputs it always had.

The minimal result (§5.9) reads the round's latest evaluation: for each compared group of ads, whether it met
the goal, missed it or cannot tell yet, and how many orders it is short of the protocol's minimum.
"""
from __future__ import annotations

import math
from datetime import date, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.adapters.persistence.models import new_id, utcnow
from app.platform.audit import append_event
from .ad_tests import FLOW
from .models import MarketingProjectRecord
from .service import MarketingError
from .workflow_models import (MarketingHandoffRecord as Handoff, MarketingMetricBatchRecord as Batch,
    MarketingRoundScheduleChangeRecord as Change)

START_MOVED, ENDED_EARLY = "start_moved", "ended_early"
#: The evaluation's business status of a compared group, as the user reads it.
STATUS = {"passes": "meets_goal", "does_not_pass": "misses_goal", "uncertain": "not_clear", "insufficient": "not_clear"}


def is_test_round(session, ctx, row) -> bool:
    """Whether a round belongs to a test project; only those rounds have a schedule beyond their frozen dates."""
    return session.scalar(select(MarketingProjectRecord.flow).where(
        MarketingProjectRecord.workspace_id == ctx.workspace_id, MarketingProjectRecord.id == row.project_id)) == FLOW


def changes(session, ctx, row) -> list[Change]:
    """The round's schedule changes, oldest first."""
    return list(session.scalars(select(Change).where(Change.workspace_id == ctx.workspace_id, Change.round_id == row.id)
        .order_by(Change.created_at, Change.id)).all())


def _dates(row, found) -> tuple[date, date, date, bool]:
    """(effective start, the end the start implies, effective end, whether the round ended early)."""
    moved = [c.date for c in found if c.kind == START_MOVED]
    start = moved[-1] if moved else row.start_date
    scheduled_end = start + (row.end_date - row.start_date)
    ended = [c.date for c in found if c.kind == ENDED_EARLY]
    return start, scheduled_end, ended[-1] if ended else scheduled_end, bool(ended)


def effective_dates(session, ctx, row) -> tuple[date, date]:
    """The first and last day a round's evaluation covers: the frozen dates, plus a test round's changes."""
    if not is_test_round(session, ctx, row):
        return row.start_date, row.end_date
    start, _, end, _ = _dates(row, changes(session, ctx, row))
    return start, end


def evaluation_window(session, ctx, row) -> dict | None:
    """What a test round is evaluated on: its effective dates and the changes behind them (plan E9).

    ``None`` for a round of an earlier test, whose evaluation inputs stay exactly what they were."""
    if not is_test_round(session, ctx, row):
        return None
    found = changes(session, ctx, row)
    start, _, end, ended = _dates(row, found)
    return {"start_date": start, "end_date": end,
            "evidence": {"change_ids": [c.id for c in found], "ended_early": ended}}


def today(row) -> date:
    """Today in the round's ad account time zone."""
    return utcnow().astimezone(ZoneInfo(row.timezone)).date()


def result_date(row, end: date) -> date:
    """The first day the result can be read: the day after the last day plus the attribution lag (E9)."""
    return end + timedelta(days=row.snapshot_json["protocol"]["attribution_lag_days"] + 1)


def report_confirmed(session, ctx, row) -> bool:
    return session.scalar(select(Batch.id).where(Batch.workspace_id == ctx.workspace_id, Batch.round_id == row.id,
                                                 Batch.state == "confirmed").limit(1)) is not None


def _append(session, ctx, row, kind, day, reason) -> Change:
    record = Change(id=new_id("mrsc"), workspace_id=ctx.workspace_id, round_id=row.id, kind=kind, date=day,
                    reason=reason, actor_id=ctx.initiator_id)
    session.add(record)
    session.flush()
    append_event(session, event_type="marketing.round_" + kind, resource_type="marketing_round", resource_id=row.id,
                 details={"change_id": record.id, "date": str(day), "frozen_start_date": str(row.start_date),
                          "frozen_end_date": str(row.end_date)})
    return record


def move_start(session, ctx, row, body) -> Change:
    """The ads went live on ``body.start_date``: the start moves there and the end with it (design §5.7).

    Only before the round's first report is confirmed and before it ended early. Real delivery cannot start
    before the day its rules were frozen (the protocol is decided before the ads run)."""
    found = changes(session, ctx, row)
    start, _, _, ended = _dates(row, found)
    if ended:
        raise MarketingError("round_ended_early", "这一轮已经提前结束，开始日期不能再改。", 409)
    if report_confirmed(session, ctx, row):
        raise MarketingError("round_report_confirmed", "这一轮已经确认过报表，开始日期不能再改。", 409)
    if body.start_date == start:
        raise MarketingError("schedule_unchanged", "日期没有变化。", 409)
    frozen_on = (row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)) \
        .astimezone(ZoneInfo(row.timezone)).date()
    if row.execution_mode == "byok" and body.start_date < frozen_on:
        raise MarketingError("start_date_out_of_range", "开始日期不能早于定下这一轮的那天。", 422,
                             params={"earliest": str(frozen_on)})
    return _append(session, ctx, row, START_MOVED, body.start_date, body.reason)


def end_early(session, ctx, row, body) -> Change:
    """The ads stopped after ``body.end_date``: the evaluation takes it as the last day (design §5.7).

    The day is no earlier than the effective start, no later than today and before the planned end; a later
    call corrects an earlier one. The ads can only have run once the ad package was made."""
    if not session.scalar(select(Handoff.id).where(Handoff.workspace_id == ctx.workspace_id, Handoff.round_id == row.id,
                                                   Handoff.state == "ready")):
        raise MarketingError("handoff_not_ready", "广告包还没有生成，这一轮还没有开始投放。", 409)
    start, scheduled_end, end, ended = _dates(row, changes(session, ctx, row))
    if ended and body.end_date == end:
        raise MarketingError("schedule_unchanged", "日期没有变化。", 409)
    latest = min(today(row), scheduled_end - timedelta(days=1))
    if not start <= body.end_date <= latest:
        raise MarketingError("end_date_out_of_range", "结束的那天要在这一轮开始之后、今天或之前，并且早于原定的最后一天。", 422,
                             params={"earliest": str(start), "latest": str(latest)})
    return _append(session, ctx, row, ENDED_EARLY, body.end_date, body.reason)


def _change_dict(record) -> dict:
    return {"id": record.id, "kind": record.kind, "date": record.date, "reason": record.reason,
            "actor_id": record.actor_id, "created_at": record.created_at.isoformat()}


def schedule(session, ctx, test_row, row) -> dict:
    """``MarketingTestRoundSchedule``: the frozen and effective dates, when the result can be read, the changes."""
    found = changes(session, ctx, row)
    start, _, end, ended = _dates(row, found)
    return {"test_id": test_row.id, "round_id": row.id, "revision": row.revision,
            "start_date": row.start_date, "end_date": row.end_date,
            "effective_start_date": start, "effective_end_date": end, "ended_early": ended,
            "result_date": result_date(row, end),
            "start_movable": row.state != "closed" and not ended and not report_confirmed(session, ctx, row),
            "changes": [_change_dict(c) for c in found]}


def remaining_purchases(min_purchases, purchases) -> int:
    """Orders still missing to reach the protocol's minimum; never below zero."""
    return max(0, math.ceil(Decimal(str(min_purchases)) - Decimal(str(purchases or 0))))


def result(session, ctx, test_row, row) -> dict:
    """``MarketingTestResult``: the round's schedule and, once evaluated, each compared group's minimal result."""
    from .workflow import evaluation_current, latest_evaluation
    dates = schedule(session, ctx, test_row, row)
    min_purchases = row.snapshot_json["protocol"]["min_purchases"]
    evaluation = latest_evaluation(session, ctx, row)
    comparisons = []
    if evaluation:
        versions = {e["entry_id"]: e["asset_version_id"] for e in evaluation.input_json["entries"]}
        for item in evaluation.result_json["results"]:
            if item["kind"] != "group":
                continue
            purchases = format(Decimal(item["totals"]["purchases"] or "0").normalize(), "f")
            comparisons.append({"id": item["id"].removeprefix("group:"), "label": item["label"],
                "status": STATUS[item["business_status"]], "purchases": purchases,
                "remaining_purchases": remaining_purchases(min_purchases, purchases),
                "asset_version_ids": [versions[entry] for entry in item["entry_ids"]]})
    return {"test_id": test_row.id, "round_id": row.id, "schedule": dates,
            "mature": today(row) >= dates["result_date"], "min_purchases": min_purchases, "scope": "these_ads",
            "evaluation_id": evaluation.id if evaluation else None,
            "evaluated_at": evaluation.created_at.isoformat() if evaluation else None,
            "current": bool(evaluation) and evaluation_current(session, ctx, row, evaluation),
            "comparisons": comparisons}
