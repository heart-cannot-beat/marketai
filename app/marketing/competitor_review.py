"""Competitor snapshots in plan drafting and review (spec §7.2, §9; Issue #273; ADR 0014).

A plan draft reads one frozen competitor snapshot: the latest one of the same
product and the same market. The plan's ``market`` is free text, so "the same
market" is the country :func:`country_for` gives it through the deployment's
alias table (``MARKETAI_MARKET_COUNTRY_ALIASES_JSON``), compared with the
scan's ``country``. A first draft has no market until the planner writes one,
so it reads the product's newest snapshot and keeps it only when the market
the planner wrote names that snapshot's country; a redraft reads the snapshot
of its previous plan's market. ``products.competitor_snapshot_in_drafts``
switched off, no product, no snapshot or a market that names no country: the
draft reads nothing, and the planner's input, prompt and output are exactly
what they were before competitor scans.

The planner sees a bounded text summary (:func:`planner_input`): claim
clusters with their advertiser counts, longest running days and library
links, and counts for the unclustered, long-running and adjacent
observations -- no media and no ad copy. It names the clusters each angle
shares a buying claim with (``Angle.competitor_overlap``, AI inference).

From the cited snapshot alone the server then derives, deterministically and
on every write, three ``source=system`` review items (:func:`review_items`):

* ``competitor_crowded`` (advisory): the clusters an angle cites, merged,
  hold at least three advertisers other than the product's own page;
* ``competitor_gap`` (advisory): an angle cites no cluster -- maybe white
  space, maybe something others tried without success;
* ``competitor_copy_reuse`` (blocking): a headline repeats a relevant
  observation's title, or a headline or body copy shares a run of words or
  characters with a relevant observation's title or body. The product's own
  page (``role=self``) is not compared: its copy is the user's to reuse.

None of this is a product fact or delivery evidence: an observation says what
others publicly run, and how long it runs says only that they keep paying.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlalchemy import select

from app import config
from app.adapters.persistence.models import ProductRecord
from app.competitors.models import CompetitorScanRecord, CompetitorScanSnapshotRecord
from .contracts import COMPETITOR_KINDS, ReviewItem, ReviewObservation

#: Merged advertisers (own page excluded) at which an angle's claim is crowded (spec §7.2).
CROWDED_ADVERTISERS = 3
#: The planner reads each cluster in at most this many characters (spec §7.2).
MAX_CLAIM_CHARS = 200
MAX_LIBRARY_URLS = 5
#: Copy reuse (spec §7.2): a whole headline of at least 3 words or 6 CJK characters,
#: or a shared run of at least 8 words or 12 CJK characters.
HEADLINE_WORDS, HEADLINE_CJK = 3, 6
RUN_WORDS, RUN_CJK = 8, 12
LABELS = {"zh-CN": "竞品观察", "en-US": "Competitor observation"}

COUNTRY = re.compile(r"[A-Za-z]{2}")
CJK_CHARS = "぀-ヿ㐀-䶿一-鿿가-힯豈-﫿"
TOKEN = re.compile(rf"[{CJK_CHARS}]|[^\W_{CJK_CHARS}]+")
CJK = re.compile(rf"[{CJK_CHARS}]")
# Words that never count towards reused copy: calls to action and certification
# names (spec §7.2's allow list from configuration). The product's own name and
# its own page names are added per plan from the understanding and the snapshot.
CTA_TERMS = (
    "shop now", "buy now", "order now", "learn more", "see more", "get yours", "get it now", "get offer",
    "sign up", "subscribe", "book now", "contact us", "send message", "download", "apply now", "get started",
    "立即购买", "马上购买", "立即下单", "了解更多", "查看详情", "立即咨询", "马上抢购", "立即订购",
)
CERTIFICATION_TERMS = (
    "ul", "ul listed", "etl", "etl listed", "etl certified", "fcc", "ce", "rohs", "ukca", "tuv", "csa", "nrtl",
    "energy star", "ip54", "ip55", "ip65", "ip66", "ip67", "ip68", "iso 9001",
)
NAME_STOPWORDS = frozenset({"a", "an", "and", "by", "for", "in", "of", "on", "or", "the", "to", "with"})


# ---------------------------------------------------------------------------
# Same market
# ---------------------------------------------------------------------------


def _alias_key(text: Any) -> str:
    """Lower-cased with every whitespace removed, on both sides of the table."""
    return "".join(str(text or "").split()).casefold()


def aliases() -> dict[str, str]:
    """The deployment's alias table; anything that is not a name -> two-letter code pair is ignored."""
    try:
        table = json.loads(config.MARKET_COUNTRY_ALIASES_JSON or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(table, dict):
        return {}
    return {_alias_key(name): code.strip().upper() for name, code in table.items()
            if isinstance(code, str) and COUNTRY.fullmatch(code.strip())}


def country_for(market: str | None) -> str | None:
    """The two-letter country a plan's free-text market names, or ``None`` when it names none."""
    text = (market or "").strip()
    if COUNTRY.fullmatch(text):
        return text.upper()
    return aliases().get(_alias_key(text)) if text else None


def market_unmatched(plan: Mapping[str, Any] | None) -> bool:
    return bool(plan) and country_for(plan.get("market")) is None


# ---------------------------------------------------------------------------
# Which snapshot a draft reads
# ---------------------------------------------------------------------------


def drafts_enabled(session, ctx, product_id: str | None) -> bool:
    if not product_id:
        return False
    product = session.scalar(select(ProductRecord).where(
        ProductRecord.workspace_id == ctx.workspace_id, ProductRecord.id == product_id))
    # A deferred column: reading the attribute loads it in this session.
    return bool(product is not None and product.competitor_snapshot_in_drafts)


def _current_snapshot(session, ctx, product_id: str, country: str | None):
    """The current snapshot (``competitor_scans.snapshot_id``) of the product's newest scan that has one,
    in ``country`` or in any market."""
    statement = select(CompetitorScanRecord).where(
        CompetitorScanRecord.workspace_id == ctx.workspace_id, CompetitorScanRecord.product_id == product_id,
        CompetitorScanRecord.snapshot_id.is_not(None))
    if country is not None:
        statement = statement.where(CompetitorScanRecord.market == country)
    scan = session.scalars(statement.order_by(CompetitorScanRecord.created_at.desc(),
                                              CompetitorScanRecord.id.desc())).first()
    return None if scan is None else snapshot(session, ctx, scan.snapshot_id, product_id)


def snapshot(session, ctx, snapshot_id: str | None, product_id: str | None):
    if not snapshot_id or not product_id:
        return None
    return session.scalar(select(CompetitorScanSnapshotRecord).where(
        CompetitorScanSnapshotRecord.workspace_id == ctx.workspace_id,
        CompetitorScanSnapshotRecord.product_id == product_id,
        CompetitorScanSnapshotRecord.id == snapshot_id))


def draft_snapshot(session, ctx, row, previous_plan: Mapping[str, Any] | None):
    """The snapshot a new plan draft of ``row`` reads, or ``None``.

    A redraft keeps its previous plan's market: the latest snapshot of the
    country that market names, none when it names no country. A first draft
    has no market yet: the product's newest snapshot, kept only if the
    planner's market turns out to name its country (:func:`cited`)."""
    if not drafts_enabled(session, ctx, row.product_id):
        return None
    if previous_plan and previous_plan.get("market"):
        country = country_for(previous_plan["market"])
        return _current_snapshot(session, ctx, row.product_id, country) if country else None
    return _current_snapshot(session, ctx, row.product_id, None)


def _summary(frozen: Mapping[str, Any], ad_ids) -> dict:
    ads = frozen.get("ads") or {}
    entries = [ads[ad_id] for ad_id in ad_ids if ad_id in ads]
    days = [entry["days_running"] for entry in entries if entry.get("days_running") is not None]
    return {"observation_count": len(entries),
            "advertiser_count": len({_advertiser(entry) for entry in entries if entry.get("role") != "self"}),
            "max_days_running": max(days) if days else None}


def planner_input(row: CompetitorScanSnapshotRecord) -> dict:
    """What the planner reads of a snapshot: counts and AI-inferred claims, no media, no ad copy."""
    frozen = row.summary_json or {}
    clusters = frozen.get("claim_clusters") or []
    long_running = frozen.get("long_running_ad_ids") or []
    return {
        "snapshot_id": row.id,
        "country": row.market,
        "language": frozen.get("language"),
        "mock": bool(frozen.get("mock")),
        "claim_clusters": [{
            "cluster_id": cluster["cluster_id"],
            "claim": str(cluster.get("claim") or "")[:MAX_CLAIM_CHARS],
            "advertiser_count": cluster.get("advertiser_count", 0),
            "observation_count": cluster.get("observation_count", len(cluster.get("ad_ids") or [])),
            "self_included": bool(cluster.get("self_included")),
            "max_days_running": cluster.get("max_days_running"),
            "library_urls": list(cluster.get("library_urls") or [])[:MAX_LIBRARY_URLS],
        } for cluster in clusters],
        "unclustered": _summary(frozen, frozen.get("unclustered_ad_ids") or []),
        "long_running": {
            **_summary(frozen, long_running),
            "min_days_running": frozen.get("long_running_days"),
            "cluster_ids": [cluster["cluster_id"] for cluster in clusters
                            if set(cluster.get("ad_ids") or []) & set(long_running)],
        },
        "adjacent": _summary(frozen, frozen.get("adjacent_ad_ids") or []),
    }


# ---------------------------------------------------------------------------
# The citation a plan carries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """A plan's cited snapshot and the names its copy may share with competitors."""
    snapshot_id: str
    country: str
    content: Mapping[str, Any]
    allowed: tuple[str, ...] = field(default=())

    @property
    def cluster_ids(self) -> set[str]:
        return {cluster["cluster_id"] for cluster in self.content.get("claim_clusters") or []}


def citation(row: CompetitorScanSnapshotRecord | None, understanding: Mapping[str, Any] | None) -> Citation | None:
    if row is None:
        return None
    frozen = row.summary_json or {}
    names = [str((understanding or {}).get("product_name") or "")]
    names += [str(entry.get("advertiser_name") or "") for entry in (frozen.get("ads") or {}).values()
              if entry.get("role") == "self"]
    return Citation(row.id, row.market, frozen, allowed=tuple(name for name in names if name.strip()))


def cited(row: CompetitorScanSnapshotRecord | None, market: str | None) -> CompetitorScanSnapshotRecord | None:
    """``row`` when the plan's market names its country, else ``None`` (spec §7.2's "same market")."""
    return row if row is not None and country_for(market) == row.market else None


def stored_citation(session, ctx, row, plan) -> Citation | None:
    """The snapshot ``row``'s stored plan cites, while ``plan``'s market still names its country.

    The server owns the reference: a client can neither point a plan at another snapshot nor keep one
    after changing the market to another country."""
    stored = (row.plan_json or {}).get("competitor_snapshot_id")
    return citation(cited(snapshot(session, ctx, stored, row.product_id), plan.market), row.understanding_json)


def flags(session, ctx, row) -> dict:
    """The read projection's competitor state of ``row``'s plan (spec §7.2)."""
    plan = row.plan_json or None
    cited_row = snapshot(session, ctx, (plan or {}).get("competitor_snapshot_id"), row.product_id)
    return {"competitor_snapshot_stale": stale(session, ctx, cited_row), "competitor_market_unmatched": market_unmatched(plan)}


def with_citation(plan, cite: Citation | None, stored: Mapping[str, Any] | None = None):
    """The plan citing ``cite`` (or nothing): unknown cluster IDs dropped, and every citing angle's rationale
    labelled as a competitor observation. An angle a client sent without ``competitor_overlap`` keeps the
    stored one: a client predating competitor review neither reads nor sends it."""
    kept = {angle["id"]: angle.get("competitor_overlap", []) for angle in (stored or {}).get("angles", [])}
    label = LABELS["zh-CN" if plan.strategy_language == "zh-CN" else "en-US"]
    known = cite.cluster_ids if cite else set()
    angles = []
    for angle in plan.angles:
        overlap = angle.competitor_overlap if "competitor_overlap" in angle.model_fields_set else kept.get(angle.id, [])
        overlap = list(dict.fromkeys(cluster_id for cluster_id in overlap if cluster_id in known))
        update = {"competitor_overlap": overlap}
        if overlap and label.casefold() not in angle.rationale.casefold():
            note = f"（{label}：{'、'.join(overlap)}）" if label == LABELS["zh-CN"] else f" ({label}: {', '.join(overlap)})"
            update["rationale"] = angle.rationale[:2000 - len(note)] + note
        angles.append(angle.model_copy(update=update))
    return plan.model_copy(update={"angles": angles, "competitor_snapshot_id": cite.snapshot_id if cite else None})


def compact(document: dict) -> dict:
    """A plan without a citation is stored and read exactly as before competitor review."""
    if document.get("competitor_snapshot_id") is None:
        document.pop("competitor_snapshot_id", None)
        for angle in document.get("angles", []):
            angle.pop("competitor_overlap", None)
    for item in document.get("plan_review", []):
        if not item.get("observations"):
            item.pop("observations", None)
    return document


def legacy_projection(plan: Mapping[str, Any] | None):
    """Retain the pre-competitor-review wire shape for deployed strict clients."""
    if plan is None:
        return None
    result = dict(plan)
    result.pop("competitor_snapshot_id", None)
    if "angles" in plan:
        result["angles"] = [{key: value for key, value in angle.items() if key != "competitor_overlap"}
                            for angle in plan["angles"]]
    if "plan_review" in plan:
        result["plan_review"] = [{key: value for key, value in item.items() if key != "observations"}
                                 for item in plan["plan_review"] if item.get("kind") not in COMPETITOR_KINDS]
    return result


def stale(session, ctx, row: CompetitorScanSnapshotRecord | None) -> bool:
    """A snapshot of the same product and market, created later, with another hash (spec §7.2)."""
    if row is None:
        return False
    return session.scalar(select(CompetitorScanSnapshotRecord.id).where(
        CompetitorScanSnapshotRecord.workspace_id == ctx.workspace_id,
        CompetitorScanSnapshotRecord.product_id == row.product_id,
        CompetitorScanSnapshotRecord.market == row.market,
        CompetitorScanSnapshotRecord.created_at > row.created_at,
        CompetitorScanSnapshotRecord.hash != row.hash).limit(1)) is not None


# ---------------------------------------------------------------------------
# Copy reuse
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    """Lower case, punctuation dropped, whitespace merged: words, and CJK one character at a time."""
    return TOKEN.findall(unicodedata.normalize("NFKC", text or "").casefold())


def _phrases(allowed) -> dict[str, list[tuple[str, ...]]]:
    """The allowed phrases by their first token, longest first."""
    phrases = {tuple(_tokens(term)) for term in (*CTA_TERMS, *CERTIFICATION_TERMS)}
    for name in allowed:
        words = _tokens(name)
        phrases.add(tuple(words))
        phrases.update((word,) for word in words if not CJK.fullmatch(word) and word not in NAME_STOPWORDS)
        run: list[str] = []
        for word in [*words, ""]:  # a CJK run in a name is a phrase, not loose characters
            if word and CJK.fullmatch(word):
                run.append(word)
            elif run:
                phrases.add(tuple(run))
                run = []
    indexed: dict[str, list[tuple[str, ...]]] = {}
    for phrase in sorted((phrase for phrase in phrases if phrase), key=len, reverse=True):
        indexed.setdefault(phrase[0], []).append(phrase)
    return indexed


def _counted(tokens: list[str], phrases) -> list[bool]:
    """Per token, whether it counts: words of an allowed name or term do not."""
    counted = [True] * len(tokens)
    for start, token in enumerate(tokens):
        for phrase in phrases.get(token, ()):
            if tuple(tokens[start:start + len(phrase)]) == phrase:
                counted[start:start + len(phrase)] = [False] * len(phrase)
    return counted


def _lengths(tokens, counted) -> tuple[int, int]:
    """(words, CJK characters) that count."""
    cjk = sum(1 for token, keep in zip(tokens, counted) if keep and CJK.fullmatch(token))
    return sum(1 for keep in counted if keep) - cjk, cjk


def _runs(tokens, counted) -> set[tuple[str, ...]]:
    """Every run of 8 counted words or 12 counted CJK characters; allowed words are left out first."""
    kept = [token for token, keep in zip(tokens, counted) if keep]
    found = set()
    for size, script in ((RUN_WORDS, False), (RUN_CJK, True)):
        for start in range(len(kept) - size + 1):
            window = kept[start:start + size]
            if all(bool(CJK.fullmatch(token)) == script for token in window):
                found.add(tuple(window))
    return found


def _advertiser(entry: Mapping[str, Any]) -> str:
    return entry.get("advertiser_page_id") or f"name:{entry.get('advertiser_name') or ''}"


def _observation(ad_id: str, entry: Mapping[str, Any], cluster_id: str | None = None) -> ReviewObservation:
    return ReviewObservation(ad_id=ad_id, cluster_id=cluster_id,
                             advertiser_name=str(entry.get("advertiser_name") or "")[:300],
                             library_url=str(entry.get("library_url") or "")[:2000],
                             days_running=entry.get("days_running"))


@dataclass
class CopyHit:
    combination_index: int
    fields: list[str]
    ad_ids: list[str]


def copy_reuse(plan, cite: Citation | None) -> list[CopyHit]:
    """Per combination, the relevant competitor observations its copy repeats (spec §7.2 rules a and b)."""
    if cite is None:
        return []
    phrases = _phrases(cite.allowed)
    ads = cite.content.get("ads") or {}
    titles: dict[tuple[str, ...], list[str]] = {}
    runs: dict[tuple[str, ...], list[str]] = {}
    for ad_id in cite.content.get("relevant_ad_ids") or []:
        entry = ads.get(ad_id) or {}
        if entry.get("role") == "self":
            continue  # the product's own page: its copy is the user's to reuse
        for text in entry.get("titles") or []:
            tokens = _tokens(text)
            if tokens:
                titles.setdefault(tuple(tokens), []).append(ad_id)
        for text in [*(entry.get("titles") or []), *(entry.get("bodies") or [])]:
            tokens = _tokens(text)
            for run in _runs(tokens, _counted(tokens, phrases)):
                runs.setdefault(run, []).append(ad_id)
    hits = []
    for index, combo in enumerate(plan.combinations):
        fields, matched = [], []
        headline = _tokens(combo.headline)
        counted = _counted(headline, phrases)
        words, cjk = _lengths(headline, counted)
        if tuple(headline) in titles and (words >= HEADLINE_WORDS or cjk >= HEADLINE_CJK):
            fields.append("headline")
            matched += titles[tuple(headline)]
        for name, text in (("headline", combo.headline), ("body_copy", combo.body_copy)):
            tokens = _tokens(text)
            shared = [ad_id for run in _runs(tokens, _counted(tokens, phrases)) for ad_id in runs.get(run, [])]
            if shared:
                fields.append(name)
                matched += shared
        if fields:
            hits.append(CopyHit(index, list(dict.fromkeys(fields)), list(dict.fromkeys(matched))))
    return hits


def copy_fields(plan, cite: Citation | None) -> list[str]:
    return [f"body.plan.combinations.{hit.combination_index}.{name}" for hit in copy_reuse(plan, cite) for name in hit.fields]


# ---------------------------------------------------------------------------
# Review items
# ---------------------------------------------------------------------------


def _clip(text: str) -> str:
    return text[:2000]


def review_items(plan, cite: Citation | None) -> list[ReviewItem]:
    """The competitor findings of ``plan`` against its cited snapshot: copy reuse first (blocking), then
    crowded and gap angles (advisory). Numbers and observations come from the snapshot only."""
    if cite is None:
        return []
    zh = plan.strategy_language == "zh-CN"
    content = cite.content
    ads = content.get("ads") or {}
    clusters = {cluster["cluster_id"]: cluster for cluster in content.get("claim_clusters") or []}
    mock = ("（竞品数据为受控 Mock 模拟。）" if zh else " (The competitor data is a Controlled Mock simulation.)") \
        if content.get("mock") else ""
    items = []
    for n, hit in enumerate(copy_reuse(plan, cite), 1):
        combo = plan.combinations[hit.combination_index]
        advertisers = list(dict.fromkeys(str(ads[ad_id].get("advertiser_name") or ad_id) for ad_id in hit.ad_ids))[:3]
        where = ("、".join({"headline": "标题", "body_copy": "正文"}[f] for f in hit.fields) if zh
                 else " and ".join({"headline": "headline", "body_copy": "body copy"}[f] for f in hit.fields))
        names = ("、".join(f"「{a[:80]}」" for a in advertisers) if zh else ", ".join(f'"{a[:80]}"' for a in advertisers))
        items.append(ReviewItem(id=f"system-competitor-copy-reuse-{n}", kind="competitor_copy_reuse", severity="blocking",
            source="system", target_ids=[combo.id],
            finding=_clip((f"组合「{combo.headline[:120]}」的{where}照搬了竞品快照中 {names} 在投广告的文案"
                           f"（整句相同的标题，或连续 {RUN_WORDS} 个以上相同的词 / {RUN_CJK} 个以上相同的字）。" if zh
                           else f'The {where} of the combination "{combo.headline[:120]}" repeats in-market copy of {names} '
                           f"from the competitor snapshot (the same whole headline, or a run of {RUN_WORDS}+ identical words "
                           f"or {RUN_CJK}+ identical CJK characters).") + mock),
            suggestion=("用自己的话重写；竞品文案不能复用，产品名、品牌名、认证名和常用行动号召不计入，自家主页的在投文案不受此限。" if zh
                        else "Rewrite it in your own words. Competitor copy may not be reused; product, brand and certification "
                        "names and common calls to action do not count, and your own page's ads are not checked."),
            observations=[_observation(ad_id, ads[ad_id]) for ad_id in hit.ad_ids[:10]]))
    crowded = gaps = 0
    for angle in plan.angles:
        proposition = angle.proposition[:120]
        cited_clusters = [clusters[cluster_id] for cluster_id in angle.competitor_overlap if cluster_id in clusters]
        if not cited_clusters:
            gaps += 1
            items.append(ReviewItem(id=f"system-competitor-gap-{gaps}", kind="competitor_gap", severity="advisory",
                source="system", target_ids=[angle.id],
                finding=_clip((f"角度「{proposition}」没有对应竞品快照中的任何购买主张簇（{content.get('counts', {}).get('relevant', 0)} 条相关观察，"
                               f"{cite.country}）。可能是空白，也可能别人试过无效。" if zh
                               else f'The angle "{proposition}" matches no buying-claim cluster in the competitor snapshot '
                               f"({content.get('counts', {}).get('relevant', 0)} relevant observations, {cite.country}). "
                               "This may be white space, or others may have tried it without success.") + mock),
                suggestion=("把它作为待验证的假设；竞品没讲不说明它有效，也不提升它的优先级。" if zh
                            else "Treat it as a hypothesis to test; competitors not making a claim says nothing about whether it works "
                            "and does not raise its priority.")))
            continue
        advertisers: dict[str, dict] = {}
        observations, seen = [], set()
        for cluster in cited_clusters:
            for advertiser in cluster.get("advertisers") or []:
                if advertiser.get("role") != "self":
                    advertisers.setdefault(_advertiser(advertiser), advertiser)
            members = sorted((ad_id for ad_id in cluster.get("ad_ids") or [] if ad_id in ads),
                             key=lambda ad_id: (-(ads[ad_id].get("days_running") or 0), ad_id))
            for ad_id in members:
                key = _advertiser(ads[ad_id])
                if ads[ad_id].get("role") != "self" and key not in seen:
                    seen.add(key)
                    observations.append(_observation(ad_id, ads[ad_id], cluster["cluster_id"]))
        if len(advertisers) < CROWDED_ADVERTISERS:
            continue
        crowded += 1
        # What competitors run: the own page's observations are left out of the counts as well.
        theirs = {ad_id for cluster in cited_clusters for ad_id in cluster.get("ad_ids") or []
                  if ad_id in ads and ads[ad_id].get("role") != "self"}
        count = len(theirs)
        days = max((ads[ad_id].get("days_running") or 0 for ad_id in theirs), default=0)
        ids = ", ".join(cluster["cluster_id"] for cluster in cited_clusters)
        items.append(ReviewItem(id=f"system-competitor-crowded-{crowded}", kind="competitor_crowded", severity="advisory",
            source="system", target_ids=[angle.id],
            finding=_clip((f"角度「{proposition}」引用的竞品主张簇（{ids}）里，已有 {len(advertisers)} 家非自家广告主在讲同一购买理由"
                           f"（{count} 条观察，最长在投 {days} 天，{cite.country}）。这只说明别人在讲，不说明有效。" if zh
                           else f'In the competitor claim clusters the angle "{proposition}" cites ({ids}), {len(advertisers)} advertisers '
                           f"other than your own page already make this buying claim ({count} observations, longest running {days} days, "
                           f"{cite.country}). This shows what others say, not what works.") + mock),
            suggestion=("考虑差异化的讲法或补充更强的证据；这只是提醒，不改变该角度的优先级。" if zh
                        else "Consider a differentiated way of telling or stronger evidence. This is a reminder only and does not "
                        "change the angle's priority."),
            observations=observations[:10]))
    return items
