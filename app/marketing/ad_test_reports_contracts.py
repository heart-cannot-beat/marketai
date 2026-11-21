"""Request and response shapes of a test round's full reports (step 2 design §5.7, #251).

A full report is stored as an ordinary metrics batch, so the round read lists it with the plan-first fields;
what only a full report has -- the rows it counts, replaces or leaves out -- goes out on these endpoints.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from .contracts import Document
from .reporting import AttributionWindow, MediaCurrency
from .workflow_contracts import MetricsIssue


class PreviewTestReport(Document):
    """Meta's daily report from the start of the test to today, as the user exported it."""
    csv_text: str = Field(min_length=1, max_length=262144)
    filename: str = Field(min_length=1, max_length=160)
    ad_account_id: str = Field(min_length=1, max_length=64, pattern=r"^(act_)?[0-9]+$")
    #: The Meta account's settings as the user states them; each must match the frozen round.
    account_timezone: str = Field(max_length=64)
    currency: MediaCurrency
    attribution_window: AttributionWindow
    #: A test counts only the customer's own Meta report: never simulated data.
    source_mode: Literal["customer_file"]
    field_mapping: dict[str, str] = Field(default_factory=dict, max_length=10)


class ExcludedReportRows(Document):
    """Rows of one Meta ad the report has but the test does not count, for one reason."""
    ad_id: str
    #: Not one of this round's ads, or a day before the round's start or after its end.
    reason: Literal["other_ad", "before_start", "after_end"]
    first_date: date
    last_date: date
    rows: int
    #: Their spend in the report's own currency.
    spend: str


class MarketingTestReport(Document):
    id: str
    round_id: str
    #: ``preview`` until confirmed; a confirmed report counts.
    state: Literal["preview", "confirmed"]
    filename: str
    raw_hash: str
    created_at: str
    ad_account_id: str
    account_timezone: str
    currency: MediaCurrency
    attribution_window: AttributionWindow
    first_date: date | None
    last_date: date | None
    row_count: int
    #: What stops it being confirmed; empty when it can be.
    issues: list[MetricsIssue]
    #: Its rows that count now (once confirmed, for a preview): this round's ads on its days, not reported
    #: again by a later confirmed report.
    counted_rows: int
    #: Rows of earlier confirmed reports for the same day and ad, which it replaces.
    replaces_rows: int
    #: Its rows a later confirmed report replaces.
    replaced_rows: int
    #: Rows it keeps but does not count.
    excluded: list[ExcludedReportRows]
