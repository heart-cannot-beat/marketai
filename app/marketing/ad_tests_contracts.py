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


class MarketingTestReportUpload(Document):
    """One confirmed report of a round: a first upload, or a correction that replaces an earlier one (#255)."""
    batch_id: str
    #: A correction replaces the days of the upload it names; an upload adds its own.
    kind: Literal["upload", "correction"]
    replaces_batch_id: str | None
    #: When the report was uploaded: its batch's ``created_at``.
    uploaded_at: str
    #: Who uploaded it: the person on the first audit event naming the batch; ``None`` when that event names nobody.
    uploaded_by: str | None
    row_count: int


class MarketingTestHistoryRound(MarketingTestRoundSummary):
    #: The round's confirmed reports, oldest first; a previewed upload that was never confirmed is not one.
    reports: list[MarketingTestReportUpload]


class MarketingTestHistoryItem(Document):
    """A test of the product, as ``MarketingTestDetail`` shows it, with each round's reports."""
    id: str
    source_project_id: str
    comparison: Literal["claims", "expressions", "single"]
    plan_summary: MarketingTestPlanSummary
    rounds: list[MarketingTestHistoryRound]
    current_round_id: str | None
    created_at: str


class MarketingEarlierTest(Document):
    """A test made the earlier way (plan first) that stands for the product, with each round's reports."""
    id: str
    state: Literal["input", "understanding_draft", "understanding_confirmed", "plan_draft", "plan_confirmed"]
    #: The product name of its understanding, or the start of its source text, as the earlier test list shows it.
    product_name: str
    rounds: list[MarketingTestHistoryRound]
    created_at: str


class MarketingTestHistory(Document):
    """``list_marketing_tests``: a product's tests and its earlier tests, each newest first (design §5.10)."""
    product_id: str
    tests: list[MarketingTestHistoryItem]
    #: Plan-first tests started from this product's card (``app.marketing.card_lineage``), or from pasted text
    #: whose confirmed understanding is this product.
    earlier_tests: list[MarketingEarlierTest]
