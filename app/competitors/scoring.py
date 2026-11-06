"""Ranking the page candidates of a name search (spec §4.2). Pure functions.

1. Deleted pages are excluded (``page_is_deleted``).
2. A normalized name equal to the query (case, spaces, punctuation ignored)
   comes first; a name containing it (or contained in it) next.
3. Categories are filtered coarsely: commerce first, then anything not
   known either way, then the non-commercial ones (events, schools, fan
   pages, politics, "Just For Fun" ...), which are excluded from probing.
4. Likes break ties of 2 and 3 only (P0: the 273k-like "Acme Charge Fuel
   Systems" is not the 3.5k-like Acme Charge). ``entity_type`` is never read.
5. The top three eligible candidates are probed: one page of active ads
   each, whose landing domains are compared with the official site. A
   domain hit outranks every name and category signal.

When the input was a ``facebook.com/<alias>`` link, a candidate with that
alias is the page itself and ranks first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from app.competitors.inputs import normalize_name

MAX_PROBES = 3

COMMERCIAL_CATEGORIES = frozenset(category.casefold() for category in (
    "Shopping & Retail", "Automotive Parts Store", "Brand", "Product/Service",
    "Retail Company", "E-commerce Website", "Electronics", "Electronics Company",
    "Electronics Store", "Consumer Electronics", "Automotive, Aircraft & Boat",
    "Automotive Store", "Outdoor & Sporting Goods Company", "Company", "Business",
    "Commercial & Industrial", "Industrial Company", "Home Improvement",
    "Motor Vehicle Company", "Automotive Manufacturer", "Energy Company",
    "Solar Energy Company", "Computers & Internet Website", "Internet Company",
    "Tools/Equipment", "Kitchen/Cooking", "Home Decor", "Clothing (Brand)",
    "Health/Beauty", "Sporting Goods Store", "Appliances",
))
EXCLUDED_CATEGORIES = frozenset(category.casefold() for category in (
    "Event", "School", "Elementary School", "Middle School", "High School",
    "College & University", "Fan Page", "Political Organization", "Political Party",
    "Political Candidate", "Politician", "Just For Fun", "Public Figure",
    "Personal Blog", "Community",
))

NAME_TIERS = {"exact": 0, "partial": 1, "none": 2}
CATEGORY_TIERS = {"commercial": 0, "other": 1, "excluded": 2}


def category_class(category: str | None) -> str:
    folded = (category or "").strip().casefold()
    if not folded:
        return "other"
    if folded in EXCLUDED_CATEGORIES or "school" in folded.split():
        return "excluded"
    if folded in COMMERCIAL_CATEGORIES:
        return "commercial"
    return "other"


def name_match(query: str, name: str | None) -> str:
    wanted, found = normalize_name(query), normalize_name(name or "")
    if not wanted or not found:
        return "none"
    if wanted == found:
        return "exact"
    return "partial" if wanted in found or found in wanted else "none"


@dataclass(frozen=True)
class Scored:
    """One candidate's §4.2 signals; ``candidate`` is the stored card."""

    candidate: Mapping[str, Any]
    name_match: str
    alias_match: bool
    category_class: str
    excluded_reason: str | None

    @property
    def page_id(self) -> str:
        return str(self.candidate["page_id"])

    @property
    def eligible(self) -> bool:
        return self.excluded_reason is None

    def key(self, domain_hit: bool | None = None) -> tuple:
        likes = self.candidate.get("likes")
        return (
            0 if self.eligible else 1,
            0 if self.alias_match else 1,
            0 if domain_hit else 1,
            NAME_TIERS[self.name_match],
            CATEGORY_TIERS[self.category_class],
            -(likes if isinstance(likes, int) else 0),
            int(self.candidate.get("provider_rank", 0)),
        )


def score(candidates: Iterable[Mapping[str, Any]], *, query: str, alias: str | None = None) -> list[Scored]:
    """The candidates with their signals, in pre-probe order."""
    wanted_alias = (alias or "").casefold()
    scored = []
    for candidate in candidates:
        klass = category_class(candidate.get("category"))
        if candidate.get("page_is_deleted"):
            excluded = "deleted"
        elif klass == "excluded":
            excluded = "category"
        else:
            excluded = None
        scored.append(Scored(
            candidate=candidate,
            name_match=name_match(query, candidate.get("page_name")),
            alias_match=bool(wanted_alias) and (candidate.get("page_alias") or "").casefold() == wanted_alias,
            category_class=klass,
            excluded_reason=excluded,
        ))
    return sorted(scored, key=lambda item: item.key())


def probe_plan(scored: Sequence[Scored], limit: int = MAX_PROBES) -> list[str]:
    """The page ids to probe, in order: the first ``limit`` eligible candidates."""
    return [item.page_id for item in scored if item.eligible][:limit]


def domain_hit(website_domain: str | None, probe: Mapping[str, Any] | None) -> bool | None:
    """Whether a probed page's ads land on the official site; ``None`` when unknowable."""
    if not website_domain or not probe or probe.get("status") != "succeeded":
        return None
    return website_domain in (probe.get("landing_domains") or [])


def rank(
    scored: Sequence[Scored],
    *,
    probes: Mapping[str, Mapping[str, Any]],
    website_domain: str | None,
) -> list[dict[str, Any]]:
    """Final order: the stored card plus every signal, ``rank`` 0 first."""
    hits = {item.page_id: domain_hit(website_domain, probes.get(item.page_id)) for item in scored}
    ordered = sorted(scored, key=lambda item: item.key(hits[item.page_id]))
    return [
        {
            **dict(item.candidate),
            "rank": index,
            "name_match": item.name_match,
            "alias_match": item.alias_match,
            "category_class": item.category_class,
            "excluded_reason": item.excluded_reason,
            "probe": dict(probes[item.page_id]) if item.page_id in probes else None,
            "domain_hit": hits[item.page_id],
        }
        for index, item in enumerate(ordered)
    ]


def automatic_match(ranked: Sequence[Mapping[str, Any]], *, by_alias: bool) -> Mapping[str, Any] | None:
    """The one candidate that may be confirmed without asking, if exactly one qualifies.

    Spec §4.1: an exact name *and* a domain hit; for a pasted alias link,
    the candidate carrying that alias. Two qualifying pages are ambiguous,
    so neither is chosen.
    """
    if by_alias:
        found = [item for item in ranked if item["alias_match"] and item["excluded_reason"] != "deleted"]
    else:
        found = [
            item for item in ranked
            if item["excluded_reason"] is None and item["name_match"] == "exact" and item["domain_hit"]
        ]
    return found[0] if len(found) == 1 else None
