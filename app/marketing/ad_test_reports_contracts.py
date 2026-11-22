"""Request and response shapes of a test round's full reports (step 2 design §5.7, #251).

A full report is stored as an ordinary metrics batch, so the round read lists it with the plan-first fields;
what only a full report has -- the rows it counts, replaces or leaves out -- goes out on these endpoints.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

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


# Which ad each Meta ad of a test round shows (step 2 design §5.7, #252).

class SuggestMarketingTestBindings(Document):
    """An uploaded report of the round, whose ad names suggest the matches."""
    batch_id: str = Field(min_length=1, max_length=64)


class MarketingTestBindingEntry(Document):
    """An ad of the round's package that a Meta ad can show."""
    entry_id: str
    #: The Meta ad name the package gave it, opening with its MA key.
    ad_name: str
    display_name: str | None
    asset_version_id: str
    #: The image's MK code, as its downloaded file name carries it.
    asset_code: str


class MarketingTestBindingSuggestion(Document):
    """One Meta ad of the report and the ad its name suggests."""
    ad_id: str
    #: The names the report gives it (more than one when it was renamed between days).
    ad_names: list[str]
    #: ``bound``: matched in this round already, ``entry_id`` is its match. ``suggested``: exactly one ad of the
    #: round's package carries the key its name has. ``no_match``: its name has no key, or a key no ad of this
    #: round has. ``ambiguous``: its name has two keys, or a key several ads have. ``bound_elsewhere``: it is
    #: matched in another round. All but the first two are left for the user to choose or skip.
    status: Literal["bound", "suggested", "no_match", "ambiguous", "bound_elsewhere"]
    #: What the user's choice starts as: the current match, or the suggestion; otherwise none.
    entry_id: str | None
    #: The kind of key that decided: the package's MA key goes before the image's MK code.
    key_kind: Literal["ma_key", "mk_code"] | None
    #: The ads of this round its keys name, in package order.
    candidate_entry_ids: list[str]
    #: Other rounds the Meta ad is matched in now.
    other_round_ids: list[str]


class MarketingTestBindingSuggestions(Document):
    round_id: str
    batch_id: str
    #: The report's ad account, without ``act_``.
    ad_account_id: str
    #: The report's column of ad names; ``None`` when it has none, so no name suggests anything.
    ad_name_column: str | None
    #: Once a report of the round is confirmed, a match can only be added, not changed or taken away.
    locked: bool
    entries: list[MarketingTestBindingEntry]
    #: Each Meta ad of the report, in the order the report first names it.
    ads: list[MarketingTestBindingSuggestion]


class MarketingTestBindingChoice(Document):
    ad_id: str = Field(min_length=1, max_length=64, pattern=r"^[0-9]+$")
    #: The package ad the Meta ad shows; ``null`` takes its match away (only before the first confirmed report).
    entry_id: str | None = Field(min_length=1, max_length=64)


class ConfirmMarketingTestBindings(Document):
    """Every match the user confirms at once: all of them are written, or none."""
    ad_account_id: str = Field(min_length=1, max_length=64, pattern=r"^(act_)?[0-9]+$")
    bindings: list[MarketingTestBindingChoice] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def one_choice_per_ad(self):
        if len({choice.ad_id for choice in self.bindings}) != len(self.bindings):
            raise PydanticCustomError("marketing_test_binding_repeated", "每条 Meta 广告只能选一次")
        return self


class MarketingTestBinding(Document):
    """A current match: the Meta ad shows the package ad ``entry_id``."""
    id: str
    ad_id: str
    entry_id: str
    actor_id: str | None
    created_at: str


class MarketingTestBindingRecord(Document):
    """One record of a match, as confirmed: a later record replaces it; the history keeps it."""
    id: str
    ad_id: str
    #: ``null`` when the record took the Meta ad's match away.
    entry_id: str | None
    replaces_id: str | None
    #: No later record replaces it and it is a match.
    current: bool
    actor_id: str | None
    created_at: str


class MarketingTestBindings(Document):
    round_id: str
    #: The round's ad account, once a Meta ad is matched.
    ad_account_id: str | None
    #: Once a report of the round is confirmed, a match can only be added, not changed or taken away.
    locked: bool
    bindings: list[MarketingTestBinding]
    #: Every record, oldest first.
    history: list[MarketingTestBindingRecord]
