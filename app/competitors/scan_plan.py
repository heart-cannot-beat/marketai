"""What a competitor scan may call, under which names, and what that can cost (spec §5.1, §5.2).

A scan is one Job (``competitor_scan.run``) per admission. Every paid call
it may make is reserved when it is admitted, under one Operation, by the
call's name within that Job (the call key is ``(job_id, call_name)``):

* market data (``market_data_call_key``), one reservation per call:
  ``page:<competitor_id>:<n>`` (a competitor's page ``n``),
  ``keyword:<k>:<n>`` (keyword ``k``, 1-based, page ``n``) and
  ``ad_refresh:<i>`` (the media-refresh quota);
* LLM (``llm_call_key``), one ``reserve_llm_chat`` per numbered name:
  ``filter:1..ceil(max observations / 20)`` (max observations = pages x 30),
  ``tag:1..max_tagged_ads``, ``cluster:1`` and, with keywords,
  ``discover:1..max_discover_advertisers``.

These are the names C-22 (filter), C-23 (tag, cluster) and C-33 (discover)
execute and replay with; each step freezes its "ordinal -> object" table
into ``evidence_json[step].assignments`` before its first call and releases
the ordinals it does not need. The token ceilings each name is reserved
for are :data:`LLM_BUDGETS`: a request whose input ceiling exceeds its
reservation is refused by ``llm_chat`` unbilled, so the steps build their
requests within them.

A resume is a new Job: it reserves only the pages a target still has left
(from its stored cursor), the unused refresh quota and, of the steps not
finished, the LLM ordinals without a recorded answer (:class:`LlmBacklog`).
A recluster is a Job of its own reserving ``cluster:1`` (:func:`recluster_plan`);
once one completes, the scan's own ``cluster:1`` is no longer owed, answered
or not (#353).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from app.market_data.calls import PlannedCall
from app.market_data.pricing import MarketDataPrice
from app.providers.llm_chat import quote_llm_chat
from app.providers.llm_registry import LLM_PRICE_RULE_VERSION

#: Ads one listing page can hold at most (the first page has 30, later ones 10).
MAX_ADS_PER_PAGE = 30
#: The smallest filter batch (spec §5.2: 20-30 observations a call).
FILTER_BATCH_FLOOR = 20
#: Images one ``tag:i`` call may look at (a DCO ad's cards, a video poster).
TAG_MAX_IMAGES = 3

MARKET_DATA_OPERATIONS = ("page", "keyword_page", "ad_refresh")
LLM_STEPS = ("filter", "tag", "cluster", "discover")
#: Steps after fetch and media, in order (C-22 filter; C-23 tag, cluster, freeze).
JUDGMENT_STEPS = ("filter", "tag", "cluster", "freeze")


@dataclass(frozen=True)
class LlmCallBudget:
    """The ceilings one call name of a step is reserved for.

    ``max_input_tokens`` is in ``llm_registry.estimate_input_tokens`` units
    (UTF-8 bytes of the text, plus each image's ceiling), so it bounds the
    request the step may build, prompt, schema and images included.
    """

    step: str
    max_input_tokens: int
    max_output_tokens: int


LLM_BUDGETS: Mapping[str, LlmCallBudget] = {
    # Up to 30 observations: title, body, link description, landing path,
    # advertiser categories, plus the product's category need and situations.
    "filter": LlmCallBudget("filter", 40_000, 4_000),
    # One relevant observation's cards (<= TAG_MAX_IMAGES images) and the
    # product's frozen dimension vocabulary.
    "tag": LlmCallBudget("tag", 18_000, 2_000),
    # Every relevant observation's headline claims, once per scan.
    "cluster": LlmCallBudget("cluster", 100_000, 8_000),
    # One advertiser's observations found by keyword.
    "discover": LlmCallBudget("discover", 8_000, 1_000),
}


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanParams:
    """The frozen parameters of a scan (``competitor_scans.params_json``)."""

    competitor_ids: tuple[str, ...]
    keywords: tuple[str, ...]
    country: str
    max_pages_per_competitor: int
    keyword_pages: int
    max_media_refreshes: int
    max_tagged_ads: int
    max_discover_advertisers: int
    language: str

    @property
    def total_pages(self) -> int:
        return len(self.competitor_ids) * self.max_pages_per_competitor + len(self.keywords) * self.keyword_pages

    def to_json(self) -> dict[str, Any]:
        return {
            "competitor_ids": list(self.competitor_ids),
            "keywords": list(self.keywords),
            "country": self.country,
            "platform": "meta",
            "ad_status": "active",
            "max_pages_per_competitor": self.max_pages_per_competitor,
            "keyword_pages": self.keyword_pages,
            "max_media_refreshes": self.max_media_refreshes,
            "max_tagged_ads": self.max_tagged_ads,
            "max_discover_advertisers": self.max_discover_advertisers,
            "language": self.language,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ScanParams:
        return cls(
            competitor_ids=tuple(data.get("competitor_ids") or ()),
            keywords=tuple(data.get("keywords") or ()),
            country=str(data["country"]),
            max_pages_per_competitor=int(data["max_pages_per_competitor"]),
            keyword_pages=int(data["keyword_pages"]),
            max_media_refreshes=int(data["max_media_refreshes"]),
            max_tagged_ads=int(data["max_tagged_ads"]),
            max_discover_advertisers=int(data["max_discover_advertisers"]),
            language=str(data["language"]),
        )


def normalize_keywords(keywords: Iterable[str]) -> tuple[str, ...]:
    """Trimmed, inner whitespace collapsed, duplicates (case-insensitive) dropped, order kept."""
    seen: set[str] = set()
    kept: list[str] = []
    for keyword in keywords:
        text = " ".join(str(keyword).split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            kept.append(text)
    return tuple(kept)


def unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value and value.strip()))


# ---------------------------------------------------------------------------
# Call names
# ---------------------------------------------------------------------------


def page_call(competitor_id: str, page: int) -> str:
    return f"page:{competitor_id}:{page}"


def keyword_call(keyword_index: int, page: int) -> str:
    return f"keyword:{keyword_index}:{page}"


def refresh_call(index: int) -> str:
    return f"ad_refresh:{index}"


def filter_calls(params: ScanParams) -> int:
    """``ceil(max observations / 20)``, max observations = pages x 30."""
    return math.ceil(params.total_pages * MAX_ADS_PER_PAGE / FILTER_BATCH_FLOOR)


def llm_call_names(params: ScanParams) -> list[tuple[str, str]]:
    """``(step, call_name)`` of every LLM call a scan may make, in order (spec §5.1)."""
    names = [("filter", f"filter:{i}") for i in range(1, filter_calls(params) + 1)]
    names += [("tag", f"tag:{i}") for i in range(1, params.max_tagged_ads + 1)]
    names.append(("cluster", "cluster:1"))
    if params.keywords:
        names += [("discover", f"discover:{i}") for i in range(1, params.max_discover_advertisers + 1)]
    return names


@dataclass(frozen=True)
class TargetPlan:
    """The pages one fetch path may still fetch: ``pages`` are absolute page numbers."""

    origin: str
    pages: tuple[int, ...]
    operation: str
    competitor_id: str | None = None
    keyword_index: int | None = None

    def call_name(self, page: int) -> str:
        if self.competitor_id is not None:
            return page_call(self.competitor_id, page)
        return keyword_call(self.keyword_index or 0, page)


def initial_targets(params: ScanParams) -> list[TargetPlan]:
    """All pages first, then the keywords (spec §5.2)."""
    pages = tuple(range(1, params.max_pages_per_competitor + 1))
    targets = [
        TargetPlan(f"page:{competitor_id}", pages, "page", competitor_id=competitor_id)
        for competitor_id in params.competitor_ids
    ]
    keyword_pages = tuple(range(1, params.keyword_pages + 1))
    targets += [
        TargetPlan(f"keyword:{keyword}", keyword_pages, "keyword_page", keyword_index=index)
        for index, keyword in enumerate(params.keywords, start=1)
    ]
    return targets


def remaining_targets(targets: Sequence[Mapping[str, Any]]) -> list[TargetPlan]:
    """What each stored target has left: pages after the last fetched one, unless exhausted."""
    plans = []
    for target in targets:
        if target.get("exhausted"):
            continue
        fetched = int(target.get("pages_fetched") or 0)
        planned = int(target.get("pages_planned") or 0)
        pages = tuple(range(fetched + 1, planned + 1))
        if not pages:
            continue
        plans.append(TargetPlan(
            target["origin"], pages, "page" if target["kind"] == "page" else "keyword_page",
            competitor_id=target.get("competitor_id"), keyword_index=target.get("keyword_index"),
        ))
    return plans


@dataclass(frozen=True)
class CallPlan:
    """Every call one admission reserves."""

    targets: tuple[TargetPlan, ...]
    refreshes: tuple[int, ...]
    llm: tuple[tuple[str, str], ...]

    @property
    def market_data_calls(self) -> list[PlannedCall]:
        calls = [
            PlannedCall(target.call_name(page), target.operation)
            for target in self.targets for page in target.pages
        ]
        calls += [PlannedCall(refresh_call(index), "ad_refresh") for index in self.refreshes]
        return calls

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(MARKET_DATA_OPERATIONS, 0)
        for call in self.market_data_calls:
            counts[call.operation] += 1
        return counts

    def llm_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(LLM_STEPS, 0)
        for step, _name in self.llm:
            counts[step] += 1
        return counts

    def to_json(self) -> dict[str, Any]:
        return {
            "market_data_calls": [
                {"call_name": call.call_name, "operation": call.operation} for call in self.market_data_calls
            ],
            "llm_calls": [{"step": step, "call_name": name} for step, name in self.llm],
        }


def initial_plan(params: ScanParams) -> CallPlan:
    return CallPlan(
        targets=tuple(initial_targets(params)),
        refreshes=tuple(range(1, params.max_media_refreshes + 1)),
        llm=tuple(llm_call_names(params)),
    )


@dataclass(frozen=True)
class LlmBacklog:
    """What the judgment steps still owe on a fully fetched scan, counted
    from its rows (Issue #272): observation groups no filter batch holds and
    nothing judged, relevant observations neither tagged nor in the tag
    table, how many observations are relevant, and the advertisers an
    unfrozen discover step would judge (Issue #276)."""

    unjudged_groups: int = 0
    untagged_candidates: int = 0
    relevant: int = 0
    #: Advertisers discovery would judge on a scan whose discover step never
    #: froze a table (Issue #276): it stopped before its first call, or the
    #: scan was admitted before the step existed.
    undiscovered_advertisers: int = 0


def _unanswered(evidence: Mapping[str, Any], step: str, names: Sequence[str]) -> tuple[list[str], list[str]]:
    """``(assigned but unanswered, free)``: names a frozen table holds without
    an answer (they are called again under their own names), then the
    names no table holds yet, both by ordinal."""
    state = evidence.get(step) or {}
    answered = set(state.get("answered") or [])
    assignments = state.get("assignments") or {}
    assigned = [name for name in names if name in assignments and name not in answered]
    free = [name for name in names if name not in assignments and name not in answered]
    return assigned, free


def llm_backlog_names(params: ScanParams, evidence: Mapping[str, Any], backlog: LlmBacklog, *,
                      pages_left: bool) -> list[tuple[str, str]]:
    """The LLM ordinals a resume reserves: only unfinished steps, only
    ordinals without an answer (spec §5.1; the 2026-10-31 decision on #272).

    While pages are left, the new observations are unknown, so every
    unanswered ordinal of filter and tag (and ``cluster:1`` if unanswered)
    is reserved as before. On a fully fetched scan:

    * filter -- its frozen but unanswered batches, plus enough free
      ordinals for the groups no batch holds (``ceil(groups / 30)``);
    * tag -- unanswered table entries, plus free ordinals for the relevant
      untagged observations outside the table while it has room (all of
      them while filter still has work: more may turn relevant);
    * cluster -- ``cluster:1`` unless answered or superseded by a completed
      recluster (#353), when anything is or may turn relevant;
    * discover (Issue #276) -- unanswered table entries, plus, while the
      step never froze a table, free ordinals for the advertisers it would
      judge (at most ``max_discover_advertisers``).
    """
    from app.competitors.clusters import superseded_by_recluster  # clusters imports this module

    names = llm_call_names(params)
    by_step: dict[str, list[str]] = {}
    for step, name in names:
        by_step.setdefault(step, []).append(name)
    plan: list[tuple[str, str]] = []

    assigned, free = _unanswered(evidence, "filter", by_step.get("filter", []))
    if pages_left:
        filter_names = assigned + free
    else:
        filter_names = assigned + free[:math.ceil(backlog.unjudged_groups / MAX_ADS_PER_PAGE)]
    plan += [("filter", name) for name in sorted(filter_names, key=_ordinal)]
    filter_open = pages_left or bool(filter_names)

    tag_state = evidence.get("tag") or {}
    frozen_vocabulary = tag_state.get("vocabulary")
    if frozen_vocabulary is None or (frozen_vocabulary.get("factors") or []):
        assigned, free = _unanswered(evidence, "tag", by_step.get("tag", []))
        room = max(0, params.max_tagged_ads - len(tag_state.get("assignments") or {}))
        wanted = room if filter_open else min(room, backlog.untagged_candidates)
        plan += [("tag", name) for name in sorted(assigned + free[:wanted], key=_ordinal)]

    cluster_answered = ("cluster:1" in ((evidence.get("cluster") or {}).get("answered") or [])
                        or superseded_by_recluster(evidence))
    if not cluster_answered and (filter_open or backlog.relevant > 0):
        plan.append(("cluster", "cluster:1"))

    if by_step.get("discover"):
        assigned, free = _unanswered(evidence, "discover", by_step["discover"])
        if not pages_left:
            discover_state = evidence.get("discover") or {}
            room = max(0, params.max_discover_advertisers - len(discover_state.get("assignments") or {}))
            free = free[:min(room, backlog.undiscovered_advertisers)]
        plan += [("discover", name) for name in sorted(assigned + free, key=_ordinal)]
    return plan


def _ordinal(call_name: str) -> int:
    return int(call_name.rsplit(":", 1)[1])


def resume_plan(params: ScanParams, evidence: Mapping[str, Any], backlog: LlmBacklog | None = None) -> CallPlan:
    """What is left: remaining pages, the unused refresh quota, and the LLM
    ordinals of unfinished steps that have no answer (:func:`llm_backlog_names`)."""
    media = evidence.get("media") or {}
    used = len(media.get("refreshes") or [])
    targets = tuple(remaining_targets(evidence.get("targets") or []))
    # A judgments-only resume refreshes media only if some is still pending.
    media_pending = media.get("status") not in ("completed", "partial") or any(
        item.get("status") == "pending" for outcomes in (media.get("items") or {}).values() for item in outcomes
    )
    refreshes = max(0, params.max_media_refreshes - used) if targets or media_pending else 0
    return CallPlan(
        targets=targets,
        refreshes=tuple(range(1, refreshes + 1)),
        llm=tuple(llm_backlog_names(params, evidence, backlog or LlmBacklog(), pages_left=bool(targets))),
    )


def recluster_plan() -> CallPlan:
    """A recluster's one call: ``cluster:1`` of its own Job (spec §5.2)."""
    return CallPlan(targets=(), refreshes=(), llm=(("cluster", "cluster:1"),))


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------


def _usd(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def market_data_quote(
    counts: Mapping[str, int], *, mode: str, provider: str, prices: Mapping[str, MarketDataPrice] | None,
    pricing_version: str,
) -> dict[str, Any]:
    """Line (a): ``calls`` x credits x credit price per operation (simulated under Mock)."""
    lines, total = [], Decimal("0")
    for operation in MARKET_DATA_OPERATIONS:
        calls = int(counts.get(operation) or 0)
        if not calls:
            continue
        price = prices.get(operation) if prices else None
        usd = price.unit_usd * calls if price else None
        if usd is not None:
            total += usd
        lines.append({
            "operation": operation, "calls": calls, "credits": (price.credits if price else 1) * calls,
            "usd": _usd(usd) if mode == "byok" else "0",
        })
    return {
        "provider": provider,
        "max_credits": sum(line["credits"] for line in lines),
        "max_usd": _usd(total) if mode == "byok" else "0",
        "simulated_usd": _usd(total) if mode == "mock" and prices else None,
        "pricing_version": pricing_version if prices else None,
        "lines": lines,
        "total": total if prices else None,
    }


def llm_quote(counts: Mapping[str, int], *, mode: str) -> dict[str, Any]:
    """Line (b): each step's call names at ``quote_llm_chat``'s maximum."""
    lines, real, simulated, provider, model = [], Decimal("0"), Decimal("0"), "", ""
    for step in LLM_STEPS:
        calls = int(counts.get(step) or 0)
        budget = LLM_BUDGETS[step]
        quote = quote_llm_chat(
            execution_mode=mode, max_output_tokens=budget.max_output_tokens, input_tokens=budget.max_input_tokens,
        )
        provider, model = quote.provider, quote.model
        if not calls:
            continue
        amount = quote.amount * calls
        simulated_amount = (quote.simulated_amount or Decimal("0")) * calls
        real += amount
        simulated += simulated_amount
        lines.append({
            "step": step, "calls": calls,
            "max_input_tokens": budget.max_input_tokens, "max_output_tokens": budget.max_output_tokens,
            "usd": _usd(amount), "simulated_usd": _usd(simulated_amount) if mode == "mock" else None,
        })
    return {
        "provider": provider,
        "model": model,
        "calls": sum(line["calls"] for line in lines),
        "max_usd": _usd(real),
        "simulated_usd": _usd(simulated) if mode == "mock" else None,
        "price_rule_version": LLM_PRICE_RULE_VERSION,
        "lines": lines,
        "total": real if mode == "byok" else simulated,
    }


def combine(market_data: dict[str, Any], llm: dict[str, Any], *, mode: str) -> dict[str, Any]:
    """Both lines and their sum; ``total`` keys are internal and dropped."""
    md_total, llm_total = market_data.pop("total"), llm.pop("total")
    if mode == "byok":
        return {
            "market_data": market_data, "llm": llm,
            "max_usd": _usd((md_total or Decimal("0")) + llm_total), "simulated_usd": None,
        }
    simulated = llm_total + md_total if md_total is not None else None
    return {"market_data": market_data, "llm": llm, "max_usd": "0", "simulated_usd": _usd(simulated)}
