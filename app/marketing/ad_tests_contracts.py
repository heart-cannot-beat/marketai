"""Response shapes of the test-project endpoints (ADR 0019, #247).

New data goes out on new endpoints: the plan-first and ideas responses keep their exact fields, because the
web's generated validators reject unknown keys.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from .contracts import Document, Text
from .ideas_contracts import AdReason
from .reporting import AttributionWindow, MediaCurrency


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


class MarketingTestAnswers(Document):
    """What taking kept ads to a test asks (design §5.2): three answers, the landing page when the product has
    no link, and any default the user changed. Read from the quote's query; the start command (#249) sends the
    same answers with the quote hash it confirms."""
    #: Cost per order at or under which an ad meets the goal: the plan's CPA threshold.
    target_cost: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
    #: This round's budget, split evenly across the compared objects.
    round_budget: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
    #: At least the protocol's ``min_days`` (7).
    duration_days: int = Field(ge=7, le=90)
    #: Where the ads send people; the product link when omitted. Required when the product has none.
    landing_page: str | None = Field(default=None, max_length=2000, pattern=r"^https?://\S+$")
    # The defaults (design §5.2), each changeable; omitted keeps the default.
    audience: Text | None = None
    offer: Text | None = None
    placement: Text | None = None
    currency: MediaCurrency | None = None
    #: The ad account's time zone (the browser's); UTC when omitted.
    account_timezone: str | None = Field(default=None, max_length=64)
    #: Tomorrow in the ad account's time zone when omitted.
    start_date: date | None = None
    attribution_window: AttributionWindow | None = None
    #: The whole test's limits: at most this many rounds (3) and this budget (three times this round's).
    max_small_rounds: int | None = Field(default=None, ge=1, le=20)
    media_budget: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)

    @field_validator("account_timezone")
    @classmethod
    def known_timezone(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except (KeyError, ValueError) as exc:
                raise PydanticCustomError("marketing_timezone_invalid", "无效的广告账户时区") from exc
        return value


class StartMarketingTest(Document):
    """The start command (#249): what the user saw on the quote -- the answers it was read with, its hash and the
    kept ads -- confirmed with one click. Anything that changed since refuses the start (``test_quote_changed``)."""
    answers: MarketingTestAnswers
    quote_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The kept ads the page showed, in any order.
    asset_version_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(max_length=200)


class MarketingTestSubject(Document):
    """One compared object: a claim, one way of telling a claim, or the one ad."""
    kind: Literal["claim", "expression", "ad"]
    angle_id: str
    expression_id: str | None
    #: The claim, or the way of telling it, in the ideas' strategy language.
    title: str
    combination_ids: list[str]
    asset_version_ids: list[str]
    #: Its equal share of this round's budget.
    budget: str
    #: Orders that share buys at exactly the target cost, rounded down.
    expected_orders: int


class MarketingTestChecks(Document):
    """The "look together" check the start command admits (plan E6): one call per kept ad, paid in BYOK."""
    available: bool
    unavailable_reason: str | None
    quantity: int
    #: The most it can cost: the start button's "at most".
    maximum_cost: str
    currency: Literal["USD"] = "USD"


class MarketingTestRefusedAd(Document):
    asset_version_id: str
    status: Literal["draft", "needs_redo"]
    reasons: list[AdReason]


class MarketingTestQuote(Document):
    """Whether the money answers the round's question, when the result is due and what starting costs at most.

    Read-only; the page shows these numbers and does not compute its own (design §5.6). ``available`` false
    names why (``unavailable_reason``); a plan the confirming checks refuse names their code (``plan_error``)."""
    available: bool
    unavailable_reason: Literal["ideas_not_ready", "no_kept_ads", "ads_not_exportable", "ads_execution_mixed",
                                "start_date_past", "landing_page_required", "plan_invalid", "checks_unavailable"] | None
    plan_error: str | None
    #: The kept ads that may not be exported yet, with why: check them before download first.
    not_exportable: list[MarketingTestRefusedAd]
    ideas_revision: int
    execution_mode: Literal["byok", "mock"] | None
    comparison: Literal["claims", "expressions", "single"] | None
    #: The round's question, in the ideas' strategy language.
    question: str | None
    subjects: list[MarketingTestSubject]
    target_cost: str
    round_budget: str
    currency: MediaCurrency
    duration_days: int
    #: Orders each compared object needs for a verdict (the protocol's ``min_purchases``).
    min_purchases: int
    #: Enough for a verdict, a direction only, or a rough read only.
    tier: Literal["enough", "direction", "rough"] | None
    #: What a verdict on every compared object needs: objects x ``min_purchases`` x target cost.
    needed_budget: str | None
    #: Short of enough: compare this many and each would get enough.
    suggested_subjects: int | None
    start_date: date
    end_date: date
    #: The day the result is due: the last day plus the attribution wait, plus one.
    conclusion_date: date
    account_timezone: str
    attribution_window: AttributionWindow
    attribution_lag_days: int
    landing_page: str | None
    landing_page_source: Literal["product_link", "answer"] | None
    checks: MarketingTestChecks
    quote_hash: str


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
