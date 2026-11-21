"""Response shapes of the test-project endpoints (ADR 0019, #247).

New data goes out on new endpoints: the plan-first and ideas responses keep their exact fields, because the
web's generated validators reject unknown keys.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from .contracts import Document
from .reporting import MediaCurrency


class MarketingTestPlanSummary(Document):
    """The parts of the test's plan the user chose or can see at a glance; money in the plan's currency."""
    #: Cost per order at or under which an ad meets the goal (the plan's CPA threshold).
    target_cost: str
    round_budget: str
    duration_days: int
    currency: MediaCurrency
    landing_page: str


class MarketingTestRoundSummary(Document):
    id: str
    ordinal: int
    state: Literal["reviewing", "handoff", "awaiting_data", "evaluated", "closed"]
    #: The frozen dates; the round's snapshot never changes.
    start_date: date
    end_date: date
    #: The dates the evaluation uses: the frozen ones plus any later move of the start or early end (#253).
    effective_start_date: date
    effective_end_date: date


class MarketingTestDetail(Document):
    id: str
    revision: int
    product_id: str
    #: The ads-first ideas whose kept ads this test took.
    source_project_id: str
    #: Claims against each other, one claim's ways of telling it, or one ad against the goal.
    comparison: Literal["claims", "expressions", "single"]
    plan_summary: MarketingTestPlanSummary
    rounds: list[MarketingTestRoundSummary]
    #: The round not yet closed, if any: while it is set, the product has this test in progress.
    current_round_id: str | None
    created_at: str


class MoveMarketingTestStart(Document):
    """The day the ads actually went live; the round's end moves with it (#253)."""
    start_date: date
    reason: str | None = Field(default=None, max_length=500)


class EndMarketingTestRoundEarly(Document):
    """The last day the ads ran; the evaluation takes it as the round's last day (#253)."""
    end_date: date
    reason: str | None = Field(default=None, max_length=500)


class MarketingTestScheduleChange(Document):
    id: str
    kind: Literal["start_moved", "ended_early"]
    #: ``start_moved``: the new first day; ``ended_early``: the last day the ads ran.
    date: date
    reason: str | None
    #: Who made the change.
    actor_id: str
    created_at: str


class MarketingTestRoundSchedule(Document):
    """This round's schedule (#253): the frozen dates never change; the effective ones are what is evaluated."""
    test_id: str
    round_id: str
    #: The round's revision, for ``If-Match`` on the next change.
    revision: int
    start_date: date
    end_date: date
    effective_start_date: date
    effective_end_date: date
    ended_early: bool
    #: The first day the result can be read: the day after the last day plus the attribution lag.
    result_date: date
    #: Whether the start can still move: the round is open, not ended early and has no confirmed report.
    start_movable: bool
    #: Every change, oldest first; the latest of each kind is in effect.
    changes: list[MarketingTestScheduleChange]


class MarketingTestComparisonResult(Document):
    """One compared group of ads (a claim, a way of telling it, or the one ad) in the round's latest evaluation."""
    id: str
    label: str
    #: Met the cost-per-order goal, missed it, or cannot tell yet.
    status: Literal["meets_goal", "misses_goal", "not_clear"]
    purchases: str
    #: Orders still missing to reach the protocol's minimum; zero once reached.
    remaining_purchases: int
    #: The ads the conclusion covers.
    asset_version_ids: list[str]


class MarketingTestResult(Document):
    """The minimal result of a round (design §5.9): its schedule and, once evaluated, each group's status."""
    test_id: str
    round_id: str
    schedule: MarketingTestRoundSchedule
    #: Whether today is on or after ``schedule.result_date``.
    mature: bool
    min_purchases: int
    #: A conclusion covers these ads, not the claim or the way of telling it in general.
    scope: Literal["these_ads"]
    #: The latest evaluation, if any; ``current`` is false once data, dates or reviews changed since.
    evaluation_id: str | None
    evaluated_at: str | None
    current: bool
    comparisons: list[MarketingTestComparisonResult]
