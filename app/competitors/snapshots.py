"""Competitor snapshots: a pure-function freeze of one scan (spec §7.1, §7.2; Issue #272).

A snapshot is what a plan cites (``competitor_snapshot_id``, C-24): the
scan's relevant observations, the judgment revisions in effect for them and
the clusters revision in effect, frozen into ``competitor_scan_snapshots``
with a hash over the whole content. Freezing calls no LLM and costs
nothing; :func:`content` is a pure function of

* the observations (append-only rows) and their origins (``role``, with
  the advertiser page and the frozen targets: :func:`roles_of`),
* each observation's newest relevance and tags revision,
* the newest clusters revision, and
* the scan's frozen parameters and tag vocabulary,

so the same revisions always give the same hash. Snapshots are unique per
(scan, hash): an unchanged refreeze returns the existing row, never a new
one (and never makes anything stale). Its parts:

* ``relevant_ad_ids`` and ``judgments`` -- each relevant observation with
  its relevance and tags revision IDs and sources;
* ``claim_clusters`` -- the cluster revision's clusters, members filtered
  by the relevance in effect now: an observation flipped away from
  ``relevant`` leaves its cluster (a cluster left empty is dropped), and
  one flipped to ``relevant`` that no cluster holds is ``unclustered``
  until the next recluster;
* ``dimension_counts`` -- per factor value of the frozen vocabulary, how
  many relevant observations carry it ``confirmed`` (a member's revision)
  and how many only as an AI ``suggested`` one;
* ``long_running_ad_ids`` (relevant, >= 60 days), ``adjacent_ad_ids``,
  ``self_ad_ids`` (the product's own page, not counted as crowding);
* ``ads`` -- every observation these lists name: advertiser, ``role``,
  days running, card titles and bodies (C-24's copy-reuse check reads
  them), library link and its judgment sources.

``source='rule'`` relevance (the §7.1 rules, C-22) is not AI inference:
counts and the summary treat it like a member's judgment (``ai_inferred``
is false), as the maintainer decided on #271.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from sqlalchemy import select

from app.adapters.persistence.models import new_id, utcnow
from app.competitors import clusters as claim_clusters
from app.competitors import relevance, tags
from app.competitors.models import (
    CompetitorAdOriginRecord,
    CompetitorAdRecord,
    CompetitorScanAssessmentRecord,
    CompetitorScanRecord,
    CompetitorScanSnapshotRecord,
)
from app.platform import audit
from app.platform.canonical import canonical_hash
from app.platform.tenancy import TenantContext

SNAPSHOT_VERSION = "competitor-snapshot/v1"
#: An ad running at least this long is "long running" (spec §7.1).
LONG_RUNNING_DAYS = 60
MAX_BODY_CHARS = 2_000
NON_AI_SOURCES = frozenset({"rule", "user"})


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def roles_of(record: CompetitorScanRecord, origins: Mapping[str, Sequence[str]],
             pages: Mapping[str, str | None]) -> dict[str, str | None]:
    """ad_id -> ``self`` | ``competitor`` | None (keyword-only).

    ``self`` when a ``page:`` origin is a ``role=self`` target or the
    advertiser page (``pages``: ad_id -> ``advertiser_page_id``) is the page
    such a target confirmed, so the product's own ads a keyword alone found
    are ``self`` too (#328); else ``competitor`` when fetched from a
    competitor's page, None when only keywords found it. The targets are the
    scan's frozen ones, so this stays a function of the scan's own rows."""
    targets = [target for target in (record.evidence_json or {}).get("targets") or [] if target.get("competitor_id")]
    by_competitor = {target["competitor_id"]: target.get("role") for target in targets}
    own_pages = {target["page_id"] for target in targets if target.get("role") == "self" and target.get("page_id")}
    roles: dict[str, str | None] = {}
    for ad_id in dict.fromkeys([*origins, *pages]):
        competitor_ids = [origin.split(":", 1)[1] for origin in origins.get(ad_id) or [] if origin.startswith("page:")]
        if pages.get(ad_id) in own_pages or any(by_competitor.get(cid) == "self" for cid in competitor_ids):
            roles[ad_id] = "self"
        elif competitor_ids:
            roles[ad_id] = "competitor"
        else:
            roles[ad_id] = None
    return roles


def load_inputs(session, ctx: TenantContext, record: CompetitorScanRecord) -> dict:
    """Everything :func:`content` reads, from this Workspace's rows only."""
    ads = {
        ad.id: ad for ad in session.scalars(
            select(CompetitorAdRecord).where(
                CompetitorAdRecord.workspace_id == ctx.workspace_id, CompetitorAdRecord.scan_id == record.id,
            )
        )
    }
    origins: dict[str, list[str]] = {ad_id: [] for ad_id in ads}
    for ad_id, origin in session.execute(
        select(CompetitorAdOriginRecord.competitor_ad_id, CompetitorAdOriginRecord.origin).where(
            CompetitorAdOriginRecord.workspace_id == ctx.workspace_id,
            CompetitorAdOriginRecord.scan_id == record.id,
        )
    ):
        origins.setdefault(ad_id, []).append(origin)
    return {
        "ads": ads,
        "roles": roles_of(record, origins, {ad_id: ad.advertiser_page_id for ad_id, ad in ads.items()}),
        "relevance": relevance.latest_by_ad(session, ctx, record.id),
        "tags": relevance.latest_by_ad(session, ctx, record.id, kind=tags.KIND),
        "clusters": claim_clusters.latest(session, ctx, record.id),
    }


# ---------------------------------------------------------------------------
# The pure function
# ---------------------------------------------------------------------------


def _distinct(values) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _ad_entry(ad: CompetitorAdRecord, role: str | None, judgment, tag_row) -> dict:
    cards = ad.cards_json or []
    tag_payload = (tag_row.payload_json or {}) if tag_row is not None else {}
    return {
        "ad_id": ad.id,
        "ad_archive_id": ad.ad_archive_id,
        "advertiser_page_id": ad.advertiser_page_id,
        "advertiser_name": ad.advertiser_name,
        "role": role,
        "days_running": ad.days_running,
        "display_format": ad.display_format,
        "library_url": ad.library_url,
        "titles": _distinct(card.get("title") for card in cards),
        "bodies": [body[:MAX_BODY_CHARS] for body in _distinct(card.get("body") for card in cards)],
        "relevance": judgment.relevance if judgment is not None else None,
        "relevance_source": judgment.source if judgment is not None else None,
        "ai_inferred": judgment is not None and judgment.source not in NON_AI_SOURCES,
        "tags": None if tag_row is None else {
            "source": tag_row.source,
            "ai_suggestion": tag_row.source not in NON_AI_SOURCES,
            "values": {item["factor_id"]: item.get("value_id") for item in tag_payload.get("factors") or []},
        },
    }


def _dimension_counts(vocabulary: Mapping[str, Any] | None, relevant: Sequence[str],
                      tag_rows: Mapping[str, Any]) -> list[dict]:
    counts = []
    for factor in (vocabulary or {}).get("factors") or []:
        values = {value["value_id"]: {"value_id": value["value_id"], "label": value["label"],
                                      "confirmed": 0, "suggested": 0}
                  for value in factor.get("values") or []}
        untagged = {"confirmed": 0, "suggested": 0, "missing": 0}
        for ad_id in relevant:
            row = tag_rows.get(ad_id)
            if row is None:
                untagged["missing"] += 1
                continue
            column = "confirmed" if row.source in NON_AI_SOURCES else "suggested"
            chosen = next((item.get("value_id") for item in (row.payload_json or {}).get("factors") or []
                           if item.get("factor_id") == factor["factor_id"]), None)
            if chosen in values:
                values[chosen][column] += 1
            else:
                untagged[column] += 1
        counts.append({"factor_id": factor["factor_id"], "label": factor["label"],
                       "values": list(values.values()), "none": untagged})
    return counts


def content(record: CompetitorScanRecord, inputs: Mapping[str, Any]) -> dict:
    """The snapshot of ``record`` given its inputs: a pure function, every list sorted."""
    ads: Mapping[str, CompetitorAdRecord] = inputs["ads"]
    roles: Mapping[str, str | None] = inputs["roles"]
    judged = inputs["relevance"]
    tag_rows = inputs["tags"]
    cluster_row: CompetitorScanAssessmentRecord | None = inputs["clusters"]
    evidence = record.evidence_json or {}

    def ranked(ids) -> list[str]:
        return sorted(ids, key=lambda ad_id: (-(ads[ad_id].days_running if ads[ad_id].days_running is not None
                                                else -1), ads[ad_id].ad_archive_id))

    value_of = {ad_id: (judged[ad_id].relevance if ad_id in judged else "unassessed") for ad_id in ads}
    relevant = ranked(ad_id for ad_id, value in value_of.items() if value == "relevant")
    adjacent = ranked(ad_id for ad_id, value in value_of.items() if value == "adjacent")
    own = ranked(ad_id for ad_id in ads if roles.get(ad_id) == "self" and value_of[ad_id] in ("relevant", "adjacent"))
    long_running = [ad_id for ad_id in relevant if (ads[ad_id].days_running or 0) >= LONG_RUNNING_DAYS]
    relevant_set = set(relevant)

    clusters_out, clustered = [], set()
    cluster_ref = None
    if cluster_row is not None:
        payload = cluster_row.payload_json or {}
        cluster_ref = {
            "assessment_id": cluster_row.id, "revision": cluster_row.revision, "source": cluster_row.source,
            "language": payload.get("language"), "conclusion": payload.get("conclusion"),
        }
        for cluster in payload.get("claim_clusters") or []:
            kept = [ad_id for ad_id in cluster.get("ad_ids") or [] if ad_id in relevant_set]
            if not kept:
                continue
            clustered.update(kept)
            clusters_out.append({
                "cluster_id": cluster["cluster_id"], "claim": cluster["claim"],
                **claim_clusters.members(kept, ads, roles),
                "library_urls": _distinct(ads[ad_id].library_url for ad_id in ranked(kept))[:5],
            })
    unclustered = [ad_id for ad_id in relevant if ad_id not in clustered]

    counts = {"observations": len(ads), "relevant": len(relevant), "adjacent": len(adjacent),
              "unrelated": sum(1 for value in value_of.values() if value == "unrelated"),
              "unassessed": sum(1 for value in value_of.values() if value == "unassessed"),
              "self": len(own)}
    sources = {"ai": 0, "rule": 0, "user": 0}
    for ad_id in relevant:
        sources[judged[ad_id].source] = sources.get(judged[ad_id].source, 0) + 1
    tag_state = evidence.get(tags.STEP) or {}
    named = sorted(set(relevant) | set(adjacent) | set(own))
    return {
        "version": SNAPSHOT_VERSION,
        "scan_id": record.id,
        "product_id": record.product_id,
        "country": record.market,
        "execution_mode": record.execution_mode,
        "mock": record.execution_mode == "mock",
        "language": (record.params_json or {}).get("language"),
        "params": dict(record.params_json or {}),
        "counts": counts,
        "relevance_sources": {
            **sources,
            "ai_inferred": sources.get("ai", 0),
            "non_ai": sum(value for key, value in sources.items() if key in NON_AI_SOURCES),
        },
        "relevant_ad_ids": relevant,
        "judgments": [
            {
                "ad_id": ad_id,
                "relevance_assessment_id": judged[ad_id].id,
                "relevance_revision": judged[ad_id].revision,
                "relevance_source": judged[ad_id].source,
                "tags_assessment_id": tag_rows[ad_id].id if ad_id in tag_rows else None,
                "tags_revision": tag_rows[ad_id].revision if ad_id in tag_rows else None,
                "tags_source": tag_rows[ad_id].source if ad_id in tag_rows else None,
            }
            for ad_id in relevant
        ],
        "cluster_revision": cluster_ref,
        "claim_clusters": clusters_out,
        "unclustered_ad_ids": unclustered,
        "vocabulary_hash": tag_state.get("vocabulary_hash"),
        "dimension_counts": _dimension_counts(tag_state.get("vocabulary"), relevant, tag_rows),
        "long_running_days": LONG_RUNNING_DAYS,
        "long_running_ad_ids": long_running,
        "adjacent_ad_ids": adjacent,
        "self_ad_ids": own,
        "ads": {ad_id: _ad_entry(ads[ad_id], roles.get(ad_id), judged.get(ad_id), tag_rows.get(ad_id))
                for ad_id in named},
    }


def content_hash(frozen: Mapping[str, Any]) -> str:
    return canonical_hash(frozen)


def freezable(inputs: Mapping[str, Any]) -> str | None:
    """Why the scan cannot be frozen yet (``None`` when it can)."""
    if not inputs["ads"]:
        return "no_observations"
    if not inputs["relevance"]:
        return "no_judgments"
    return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def by_hash(session, ctx: TenantContext, scan_id: str, digest: str) -> CompetitorScanSnapshotRecord | None:
    return session.scalars(
        select(CompetitorScanSnapshotRecord).where(
            CompetitorScanSnapshotRecord.workspace_id == ctx.workspace_id,
            CompetitorScanSnapshotRecord.scan_id == scan_id,
            CompetitorScanSnapshotRecord.hash == digest,
        )
    ).one_or_none()


def freeze(session, ctx: TenantContext, record: CompetitorScanRecord, *, job_id: str | None = None,
           created_by: str | None = None) -> tuple[CompetitorScanSnapshotRecord | None, bool, str | None]:
    """Freeze ``record`` (its row lock held): ``(snapshot, created, refusal)``.

    The same content returns the existing snapshot (``created`` false). The
    scan's ``snapshot_id`` points at the result either way, so it is always
    the snapshot of the revisions in effect now."""
    inputs = load_inputs(session, ctx, record)
    refusal = freezable(inputs)
    if refusal is not None:
        return None, False, refusal
    frozen = content(record, inputs)
    digest = content_hash(frozen)
    row = by_hash(session, ctx, record.id, digest)
    created = row is None
    if created:
        cluster = inputs["clusters"]
        row = CompetitorScanSnapshotRecord(
            id=new_id("csnap"), workspace_id=ctx.workspace_id, scan_id=record.id, product_id=record.product_id,
            market=record.market, execution_mode=record.execution_mode,
            cluster_assessment_id=cluster.id if cluster is not None else None,
            cluster_revision=cluster.revision if cluster is not None else None,
            hash=digest, summary_json=frozen, job_id=job_id, created_by=created_by, created_at=utcnow(),
        )
        session.add(row)
        session.flush()
    record.snapshot_id = row.id
    record.updated_at = utcnow()
    session.flush()
    audit.append_event(
        session,
        event_type="competitor_scan.frozen",
        resource_type="competitor_scan",
        resource_id=record.id,
        details={"snapshot_id": row.id, "hash": digest, "created": created, "job_id": job_id,
                 "cluster_revision": row.cluster_revision, "relevant": frozen["counts"]["relevant"]},
    )
    session.flush()
    return row, created, None


def pending_hash(session, ctx: TenantContext, record: CompetitorScanRecord) -> str | None:
    """The hash a refreeze would produce now (``None`` when it cannot freeze)."""
    inputs = load_inputs(session, ctx, record)
    if freezable(inputs) is not None:
        return None
    return content_hash(content(record, inputs))


def get(session, ctx: TenantContext, scan_id: str, snapshot_id: str) -> CompetitorScanSnapshotRecord | None:
    return session.scalars(
        select(CompetitorScanSnapshotRecord).where(
            CompetitorScanSnapshotRecord.workspace_id == ctx.workspace_id,
            CompetitorScanSnapshotRecord.scan_id == scan_id,
            CompetitorScanSnapshotRecord.id == snapshot_id,
        )
    ).one_or_none()


def history(session, ctx: TenantContext, scan_id: str) -> list[CompetitorScanSnapshotRecord]:
    return list(session.scalars(
        select(CompetitorScanSnapshotRecord).where(
            CompetitorScanSnapshotRecord.workspace_id == ctx.workspace_id,
            CompetitorScanSnapshotRecord.scan_id == scan_id,
        ).order_by(CompetitorScanSnapshotRecord.created_at.desc(), CompetitorScanSnapshotRecord.id.desc())
    ).all())


def latest_for_market(session, ctx: TenantContext, *, product_id: str,
                      country: str) -> CompetitorScanSnapshotRecord | None:
    """The current snapshot of the newest scan of ``product_id`` in ``country``
    that has one (spec §7.2's default; C-24 reads it). A scan's current
    snapshot is ``competitor_scans.snapshot_id``, the freeze of the
    revisions in effect when it was last frozen."""
    scan = session.scalars(
        select(CompetitorScanRecord).where(
            CompetitorScanRecord.workspace_id == ctx.workspace_id,
            CompetitorScanRecord.product_id == product_id,
            CompetitorScanRecord.market == country,
            CompetitorScanRecord.snapshot_id.is_not(None),
        ).order_by(CompetitorScanRecord.created_at.desc(), CompetitorScanRecord.id.desc())
    ).first()
    if scan is None:
        return None
    return get(session, ctx, scan.id, scan.snapshot_id)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def meta_view(row: CompetitorScanSnapshotRecord, current_id: str | None) -> dict:
    frozen = row.summary_json or {}
    return {
        "snapshot_id": row.id,
        "scan_id": row.scan_id,
        "product_id": row.product_id,
        "country": row.market,
        "execution_mode": row.execution_mode,
        "hash": row.hash,
        "cluster_revision": row.cluster_revision,
        "current": row.id == current_id,
        "counts": dict(frozen.get("counts") or {}),
        "job_id": row.job_id,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def snapshot_view(row: CompetitorScanSnapshotRecord, current_id: str | None) -> dict:
    return {**meta_view(row, current_id), "content": dict(row.summary_json or {})}


def _ad_view(frozen: Mapping[str, Any], ad_id: str) -> dict:
    entry = dict((frozen.get("ads") or {}).get(ad_id) or {})
    return {
        "ad_id": ad_id,
        "ad_archive_id": entry.get("ad_archive_id"),
        "advertiser_page_id": entry.get("advertiser_page_id"),
        "advertiser_name": entry.get("advertiser_name"),
        "role": entry.get("role"),
        "days_running": entry.get("days_running"),
        "display_format": entry.get("display_format"),
        "title": next(iter(entry.get("titles") or []), None),
        "library_url": entry.get("library_url"),
        "relevance": entry.get("relevance"),
        "relevance_source": entry.get("relevance_source"),
        "ai_inferred": bool(entry.get("ai_inferred")),
        "tags": entry.get("tags"),
    }


def summary_view(row: CompetitorScanSnapshotRecord, *, current_id: str | None, pending: str | None,
                 can_recluster: bool) -> dict:
    """What the summary shows: clusters (advertisers, observations, longest
    running), the unclustered, adjacent, long-running and own-page
    observations, the dimension counts and the AI's one-sentence conclusion
    -- the clusters and the conclusion are AI inference."""
    frozen = row.summary_json or {}
    cluster_ref = frozen.get("cluster_revision") or None
    return {
        **meta_view(row, current_id),
        "mock": bool(frozen.get("mock")),
        "language": frozen.get("language"),
        "pending_changes": pending is not None and pending != row.hash,
        "can_recluster": can_recluster,
        "relevance_sources": dict(frozen.get("relevance_sources") or {}),
        "clusters": [
            {**cluster, "ai_inference": True,
             "ads": [_ad_view(frozen, ad_id) for ad_id in cluster.get("ad_ids") or []]}
            for cluster in frozen.get("claim_clusters") or []
        ],
        "unclustered": [_ad_view(frozen, ad_id) for ad_id in frozen.get("unclustered_ad_ids") or []],
        "adjacent": [_ad_view(frozen, ad_id) for ad_id in frozen.get("adjacent_ad_ids") or []],
        "long_running": [_ad_view(frozen, ad_id) for ad_id in frozen.get("long_running_ad_ids") or []],
        "self_observations": [_ad_view(frozen, ad_id) for ad_id in frozen.get("self_ad_ids") or []],
        "long_running_days": frozen.get("long_running_days") or LONG_RUNNING_DAYS,
        "dimension_counts": list(frozen.get("dimension_counts") or []),
        "conclusion": None if not cluster_ref or not cluster_ref.get("conclusion") else {
            "text": cluster_ref["conclusion"], "ai_inference": True, "cluster_revision": cluster_ref["revision"],
            "language": cluster_ref.get("language"),
        },
    }
