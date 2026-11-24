"""Shapes of a test round's running numbers and recorded deviations (#254, step 2 design §5.7, §5.8)."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field

from .contracts import Document, Text
from .reporting import MediaCurrency


class RecordMarketingTestDeviation(Document):
    """Something that ran differently from the plan for these ads of the round's package (design §5.7).

    The compared groups holding them draw no conclusion this round; the page says so before it is sent."""
    asset_version_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(min_length=1, max_length=50)
    #: What was different: the budget, the audience, the dates, an ad changed in Meta...
    note: Text


class MarketingTestDeviation(Document):
    id: str
    asset_version_ids: list[str]
    note: str
    #: The compared groups holding these ads: this round draws no conclusion on them.
    comparison_ids: list[str]
    #: Who recorded it.
    actor_id: str
    created_at: str


class MarketingTestProgressComparison(Document):
    """One compared group of ads (a claim, a way of telling it, or the one ad) as its reports stand today."""
    id: str
    label: str
    asset_version_ids: list[str]
    #: In the plan's currency, over the counted days so far.
    spend: str
    purchases: str
    #: Spend per order so far; ``None`` until the first order.
    cost_per_purchase: str | None
    #: Orders still missing to reach the protocol's minimum; zero once reached.
    remaining_purchases: int
    #: A deviation was recorded for its ads, or noted when their Meta ads were matched: no conclusion this round.
    conditions_changed: bool


class MarketingTestChangedAd(Document):
    """An ad of the round whose fact, photo or copy the ads-first ideas changed after the test started (§5.8).

    The running test keeps what it started on: its evaluation and ad package do not change. The Meta ad still
    shows the old content, so the page suggests stopping it or making it again."""
    asset_version_id: str
    #: ``fact``: a fact it cites was changed or removed; ``photo``: a product photo it used was rejected or can
    #: no longer be used; ``copy``: what the ideas say under its combination (its words, claim or way of
    #: telling) is no longer what it shows.
    changes: list[Literal["fact", "photo", "copy"]]
    #: The cited facts that were changed or removed.
    fact_ids: list[str]


class MarketingTestProgress(Document):
    """``get_marketing_test_progress``: the round's numbers so far (design §5.7). Reading it evaluates nothing."""
    test_id: str
    round_id: str
    #: The round's revision, for ``If-Match`` on the next deviation.
    revision: int
    #: Always true: these are running numbers, not a conclusion; the result is ``get_marketing_test_result``.
    provisional: Literal[True] = True
    currency: MediaCurrency
    #: Cost per order at or under which a group meets the goal.
    target_cost: str
    min_purchases: int
    effective_start_date: date
    effective_end_date: date
    #: The first day the result can be read.
    result_date: date
    #: The last day the counted reports cover; ``None`` before the first confirmed report.
    data_through: date | None
    comparisons: list[MarketingTestProgressComparison]
    #: Every recorded deviation, oldest first.
    deviations: list[MarketingTestDeviation]
    #: The round's ads the ideas changed since the test started, in the order they joined the round.
    changed_ads: list[MarketingTestChangedAd]
