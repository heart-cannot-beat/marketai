"""Competitor discovery: recommendations and a member's decisions (spec §8; Issue #276).

Authorization: any Workspace member (Owner or Member) reads a scan's
recommendations and accepts or rejects them; another Workspace's scan is
"not found", never "forbidden" (no existence oracle).

A recommendation is an advertiser the scan's discover step judged
``relevant`` or ``adjacent`` (:mod:`app.competitors.discovery`). Its state
follows the product's decisions and list: ``accepted`` (the latest decision
accepted it and that competitor still has the page on the list),
``competitor`` (an active competitor has that page, added another way),
``rejected`` or ``open``. An accept whose competitor was archived no longer
holds (Issue #330): archiving takes the page off the list, so it is
recommended again and ``open``, with that accept as its ``decision``;
accepting it again adds a new competitor and appends a new decision.

Decisions (``competitor_discovery_decisions``, append-only, per product):

* accept -- in one transaction, a competitor (``role=competitor``) added as
  the advertiser's Ad Library link and confirmed by the page ID from the
  ads (a ``link`` confirmation, as C-13 confirms a pasted link): no
  resolve, no resolve charge. The next scan fetches that page.
* reject -- later scans leave the advertiser out of discovery.

Idempotency: the ``Idempotency-Key`` replays the same decision; a different
body under the same key is ``409 idempotency_key_reused``. Deciding what is
already in effect (rejecting a rejected advertiser, accepting an accepted
one) answers the decision in effect instead of appending. An advertiser
already on the product's list cannot be decided on (``409
competitor_discovery_already_competitor``). Decisions on one advertiser of
one product are serialized (PostgreSQL advisory lock).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import select, text

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id, utcnow
from app.competitors import discovery
from app.competitors.models import CompetitorDiscoveryDecisionRecord, CompetitorRecord, CompetitorScanRecord
from app.competitors.scan_service import _load as load_scan
from app.competitors.service import CompetitorProblem, _iso, append_confirmation, competitor_view
from app.platform import audit
from app.platform.tenancy import TenantContext

RESOURCE_TYPE = "competitor_scan"
DECIDED_EVENT = "competitor_discovery.decided"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _key_hash(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _require_member(ctx: TenantContext) -> None:
    if ctx.role not in ("owner", "member"):
        raise CompetitorProblem(403, "competitor_forbidden", "only Workspace members manage competitors")


def _lock_advertiser(session, ctx: TenantContext, product_id: str, page_id: str) -> None:
    """Serialize decisions on one advertiser of one product (PostgreSQL; SQLite has one writer)."""
    if session.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"competitor_discovery:{ctx.workspace_id}:{product_id}:{page_id}".encode()).digest()
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(digest[:8], "big", signed=True)},
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _recommendations(record: CompetitorScanRecord) -> list[tuple[str, dict, dict]]:
    """``(call_name, table entry, answer)`` of every advertiser judged relevant or adjacent, by ordinal."""
    state = (record.evidence_json or {}).get(discovery.STEP) or {}
    calls = state.get("calls") or {}
    answered = set(state.get("answered") or [])
    found = []
    for name, entry in (state.get("assignments") or {}).items():
        call = calls.get(name) or {}
        if name in answered and call.get("relevance") in discovery.RECOMMENDED:
            found.append((name, entry, call))
    found.sort(key=lambda item: int(item[0].rsplit(":", 1)[1]))
    return found


def decision_view(row: CompetitorDiscoveryDecisionRecord) -> dict:
    return {
        "decision_id": row.id,
        "scan_id": row.scan_id,
        "advertiser_page_id": row.advertiser_page_id,
        "decision": row.decision,
        "reason": row.reason,
        "competitor_id": row.competitor_id,
        "decided_by": row.decided_by,
        "decided_at": _iso(row.decided_at),
    }


def _in_effect(latest: CompetitorDiscoveryDecisionRecord, pages: Mapping[str, str]) -> bool:
    """A reject holds; an accept holds while the competitor it created has the page on the list (#330)."""
    return latest.decision == "reject" or (
        latest.competitor_id is not None and latest.competitor_id == pages.get(latest.advertiser_page_id)
    )


def _state(page_id: str, decisions: Mapping[str, CompetitorDiscoveryDecisionRecord],
           pages: Mapping[str, str]) -> str:
    latest = decisions.get(page_id)
    if latest is not None and latest.decision == "accept" and _in_effect(latest, pages):
        return "accepted"
    if page_id in pages:
        return "competitor"
    if latest is not None and latest.decision == "reject":
        return "rejected"
    # Never decided, or the accepted competitor no longer has the page (archived): decided again.
    return "open"


def _item_view(record: CompetitorScanRecord, call_name: str, entry: Mapping[str, Any], call: Mapping[str, Any], *,
               decisions: Mapping[str, CompetitorDiscoveryDecisionRecord], pages: Mapping[str, str]) -> dict:
    page_id = entry["page_id"]
    latest = decisions.get(page_id)
    return {
        "advertiser_page_id": page_id,
        "advertiser_name": entry.get("advertiser_name"),
        "categories": list(entry.get("categories") or []),
        "observation_count": int(entry.get("observations") or 0),
        "landing_domains": list(entry.get("landing_domains") or []),
        "keywords": list(entry.get("keywords") or []),
        "relevance": call["relevance"],
        "reason": call.get("reason"),
        "language": call.get("language"),
        "library_url": discovery.library_url(page_id, record.market),
        "call_name": call_name,
        "state": _state(page_id, decisions, pages),
        "decision": decision_view(latest) if latest is not None else None,
        "competitor_id": pages.get(page_id),
    }


def _summary(record: CompetitorScanRecord) -> dict:
    state = (record.evidence_json or {}).get(discovery.STEP) or {}
    if not (record.params_json or {}).get("keywords"):
        status, reason = "skipped", "no_keywords"
    else:
        status, reason = state.get("status") or "pending", state.get("reason")
    calls = state.get("calls") or {}
    answered = [name for name in state.get("answered") or [] if name in (state.get("assignments") or {})]
    return {
        "status": status,
        "reason": reason,
        "advertisers": len(state.get("assignments") or {}),
        "judged": len(answered),
        "unrelated": sum(1 for name in answered if (calls.get(name) or {}).get("relevance") == "unrelated"),
        "over_cap": int(state.get("over_cap") or 0),
    }


def list_discoveries(ctx: TenantContext, scan_id: str) -> dict:
    """The scan's recommendations, by ordinal (most observations first), with their state."""
    _require_member(ctx)
    with tenant_session(ctx) as session:
        record = load_scan(session, ctx, scan_id)
        decisions = discovery.latest_decisions(session, ctx, record.product_id)
        pages = discovery.competitor_pages(session, ctx, record.product_id)
        items = [
            _item_view(record, name, entry, call, decisions=decisions, pages=pages)
            for name, entry, call in _recommendations(record)
        ]
        return {
            "scan_id": record.id,
            "product_id": record.product_id,
            "execution_mode": record.execution_mode,
            "language": (record.params_json or {}).get("language"),
            **_summary(record),
            "recommended": len(items),
            "items": items,
        }


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def _replay(session, ctx: TenantContext, record: CompetitorScanRecord, row: CompetitorDiscoveryDecisionRecord, *,
            entry: tuple[str, dict, dict]) -> dict:
    decisions = discovery.latest_decisions(session, ctx, record.product_id)
    pages = discovery.competitor_pages(session, ctx, record.product_id)
    competitor = None
    if row.competitor_id:
        competitor = session.scalars(select(CompetitorRecord).where(
            CompetitorRecord.workspace_id == ctx.workspace_id, CompetitorRecord.id == row.competitor_id,
        )).one_or_none()
    name, table_entry, call = entry
    return {
        "discovery": _item_view(record, name, table_entry, call, decisions=decisions, pages=pages),
        "decision": decision_view(row),
        "competitor": competitor_view(session, ctx, competitor) if competitor is not None else None,
    }


def decide(ctx: TenantContext, scan_id: str, page_id: str, *, decision: str, reason: str | None,
           idempotency_key: str) -> dict:
    """Accept (add and confirm the competitor by the ads' page ID) or reject one recommendation."""
    _require_member(ctx)
    reason = " ".join((reason or "").split()) or None
    key_hash = _key_hash(idempotency_key)
    fingerprint = _sha({"scan_id": scan_id, "page_id": page_id, "decision": decision, "reason": reason})
    with tenant_session(ctx) as session:
        record = load_scan(session, ctx, scan_id)
        _lock_advertiser(session, ctx, record.product_id, page_id)
        entry = next((item for item in _recommendations(record) if item[1]["page_id"] == page_id), None)
        replay = session.scalars(select(CompetitorDiscoveryDecisionRecord).where(
            CompetitorDiscoveryDecisionRecord.workspace_id == ctx.workspace_id,
            CompetitorDiscoveryDecisionRecord.product_id == record.product_id,
            CompetitorDiscoveryDecisionRecord.idempotency_key_hash == key_hash,
        )).one_or_none()
        if replay is not None and replay.request_fingerprint != fingerprint:
            raise CompetitorProblem(409, "idempotency_key_reused", "this Idempotency-Key was used for another decision")
        if entry is None:
            raise CompetitorProblem(404, "competitor_discovery_not_found",
                                    "the scan recommended no such advertiser", params={"page_id": page_id})
        if replay is not None:
            return {**_replay(session, ctx, record, replay, entry=entry), "replayed": True}
        call_name, table_entry, call = entry
        latest = discovery.latest_decisions(session, ctx, record.product_id).get(page_id)
        pages = discovery.competitor_pages(session, ctx, record.product_id)
        if latest is not None and latest.decision == decision and _in_effect(latest, pages):
            # What is already in effect: answered, not appended again.
            return {**_replay(session, ctx, record, latest, entry=entry), "replayed": True}
        if page_id in pages:
            raise CompetitorProblem(409, "competitor_discovery_already_competitor",
                                    "the advertiser is already on the product's list",
                                    params={"competitor_id": pages[page_id]})

        now = utcnow()
        decision_id = new_id("cdd")
        competitor = None
        link = discovery.library_url(page_id, record.market)
        name = (table_entry.get("advertiser_name") or "")[:300] or None
        evidence = {
            "call_name": call_name, "job_id": call.get("job_id"), "attempt_id": call.get("attempt_id"),
            "relevance": call["relevance"], "reason": call.get("reason"), "language": call.get("language"),
            "execution_mode": record.execution_mode, "observations": int(table_entry.get("observations") or 0),
            "landing_domains": list(table_entry.get("landing_domains") or []),
            "categories": list(table_entry.get("categories") or []),
        }
        if decision == "accept":
            competitor = CompetitorRecord(
                id=new_id("cmp"), workspace_id=ctx.workspace_id, product_id=record.product_id, role="competitor",
                status="active", name=None, website_domain=None, input_kind="ad_library", input_text=link,
                input_page_alias=None, idempotency_key_hash=_sha({"discovery_accept": decision_id}),
                request_fingerprint=fingerprint, created_by=ctx.initiator_id, created_at=now, updated_at=now,
            )
            session.add(competitor)
            session.flush()
            audit.append_event(
                session, event_type="competitor.created", resource_type="competitor", resource_id=competitor.id,
                details={"product_id": record.product_id, "role": "competitor", "input_kind": "ad_library",
                         "has_website": False, "discovery_scan_id": record.id},
            )
            append_confirmation(
                session, ctx, competitor, page_id=page_id, page_alias=None, page_name=name, source="link",
                resolve_job_id=None,
                evidence={"link_kind": "ad_library", "link": link,
                          "discovery": {"scan_id": record.id, "decision_id": decision_id, **evidence}},
                idempotency_key_hash=_sha({"discovery_confirm": decision_id}), request_fingerprint=fingerprint,
                confirmed_by=ctx.initiator_id,
            )
        row = CompetitorDiscoveryDecisionRecord(
            id=decision_id, workspace_id=ctx.workspace_id, product_id=record.product_id, scan_id=record.id,
            advertiser_page_id=page_id, advertiser_name=name, decision=decision, reason=reason,
            competitor_id=competitor.id if competitor is not None else None, evidence_json=evidence,
            idempotency_key_hash=key_hash, request_fingerprint=fingerprint, decided_by=ctx.initiator_id,
            decided_at=now,
        )
        session.add(row)
        session.flush()
        audit.append_event(
            session, event_type=DECIDED_EVENT, resource_type=RESOURCE_TYPE, resource_id=record.id,
            details={"decision_id": row.id, "product_id": record.product_id, "advertiser_page_id": page_id,
                     "decision": decision, "competitor_id": row.competitor_id, "call_name": call_name,
                     "relevance": call["relevance"]},
        )
        session.flush()
        return {**_replay(session, ctx, record, row, entry=entry), "replayed": False}
