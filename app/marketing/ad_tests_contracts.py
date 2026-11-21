"""Response shapes of the test-project endpoints (ADR 0019, #247).

New data goes out on new endpoints: the plan-first and ideas responses keep their exact fields, because the
web's generated validators reject unknown keys.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

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
