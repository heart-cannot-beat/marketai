"""Competitor list, resolve and page confirmation: API commands (Issue #269, spec §4).

Authorization: any Workspace member (Owner or Member) may add a competitor,
quote and start a resolve and confirm a page (§6: the resolve is paid by
the member's Workspace); archiving is Owner-only. Everything is
Workspace-scoped: another Workspace's competitor, product or resolve is
"not found", never "forbidden" (no existence oracle).

Idempotency:

* create -- the ``Idempotency-Key`` replays the same competitor; a
  different body under the same key is ``409 idempotency_key_reused``.
* resolve -- the key replays the same resolve (and its single reservation).
* confirm -- the key replays the same revision; besides, confirming the
  page that is already the latest confirmation from the same source
  (same resolve, or a link) returns that revision instead of appending.
* archive -- archiving an archived competitor answers the same view.

A resolve is a paid Job of its own (``competitor.resolve``, §4.4): its
maximum (``page_search`` x 1 + ``probe`` x 3) is reserved per call under one
Operation when it is started, Spend Guardrail included; the Worker
(:mod:`app.competitors.resolve_worker`) settles each call by the credits it
reports and releases the rest. Without a bound ``market_data`` key only
Controlled Mock is offered, and the two never mix.

Read-only projections (Issue #332): a competitor's ``observed_page_name``
is the advertiser name of its confirmed page's newest observation, and
``last_scanned_at`` the time of the newest finished scan that included it.
Both are read from the scans; neither makes a call nor touches a
confirmation revision. A candidate's ``avatar`` is the resolve's archived
copy, signed for a short while, never the provider's link.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select, text

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import ProductRecord, new_id, utcnow
from app.competitors import inputs, scan_media, scoring
from app.competitors.inputs import CompetitorInputError
from app.competitors.models import (
    SCAN_FINISHED_STATUSES,
    CompetitorAdRecord,
    CompetitorIdentityConfirmationRecord,
    CompetitorRecord,
    CompetitorResolutionRecord,
    CompetitorScanRecord,
)
from app.competitors.sources.base import ISO_3166_ALPHA2
from app.competitors.sources.mock import MOCK_PROVIDER
from app.market_data.calls import MarketDataCallError, PlannedCall, reserve_market_data_calls
from app.market_data.credentials import SCRAPECREATORS, resolve_market_data_credential
from app.market_data.pricing import MarketDataPricingError, price_table
from app.platform import audit
from app.platform.jobs import enqueue_in_session
from app.platform.operations import OperationResultRef, OperationState, create_operation
from app.platform.tenancy import TenantContext

JOB_TYPE = "competitor.resolve"
COMMAND_TYPE = "competitor_resolve"
RESOURCE_TYPE = "competitor_resolution"

#: A finished resolve may be cited by a confirmation this long (§4.4); the
#: provider's signed avatar links live about 104 hours.
REUSE_WINDOW = timedelta(hours=72)
#: Waiting for another market-data job on the same key re-queues the Job
#: (§5.1); each wait uses one attempt.
RESOLVE_JOB_MAX_ATTEMPTS = 30

SEARCH_CALL = PlannedCall("page_search:1", "page_search")
PROBE_CALLS = tuple(PlannedCall(f"probe:{index}", "probe") for index in (1, 2, 3))
RESOLVE_CALLS = (SEARCH_CALL, *PROBE_CALLS)

LIST_LIMIT = 200
FINISHED = frozenset({"completed", "failed", "uncertain"})
#: Failures a new resolve may get past without anyone changing anything first.
RETRYABLE_FAILURES = frozenset({
    "market_data_rate_limited", "market_data_unreachable", "market_data_turn_unavailable",
})


@dataclass
class CompetitorProblem(Exception):
    """A deterministic refusal the router turns into Problem Details."""

    status: int
    code: str
    message: str
    retryable: bool = False
    params: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _key_hash(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _require_member(ctx: TenantContext) -> None:
    if ctx.role not in ("owner", "member"):
        raise CompetitorProblem(403, "competitor_forbidden", "only Workspace members manage competitors")


def _input_problem(exc: CompetitorInputError) -> CompetitorProblem:
    return CompetitorProblem(422, exc.code, str(exc), params=exc.params)


def lock_competitor(session, ctx: TenantContext, competitor_id: str) -> None:
    """Serialize revision numbering of one competitor (PostgreSQL; SQLite has one writer)."""
    if session.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"competitor:{ctx.workspace_id}:{competitor_id}".encode()).digest()
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(digest[:8], "big", signed=True)},
    )


def _load(session, ctx: TenantContext, competitor_id: str) -> CompetitorRecord:
    record = session.scalars(
        select(CompetitorRecord).where(
            CompetitorRecord.workspace_id == ctx.workspace_id,
            CompetitorRecord.id == competitor_id,
        )
    ).one_or_none()
    if record is None:
        raise CompetitorProblem(404, "competitor_not_found", "no such competitor in this Workspace")
    return record


def latest_confirmation(session, ctx: TenantContext, competitor_id: str) -> CompetitorIdentityConfirmationRecord | None:
    return session.scalars(
        select(CompetitorIdentityConfirmationRecord)
        .where(
            CompetitorIdentityConfirmationRecord.workspace_id == ctx.workspace_id,
            CompetitorIdentityConfirmationRecord.competitor_id == competitor_id,
        )
        .order_by(CompetitorIdentityConfirmationRecord.revision.desc())
    ).first()


def append_confirmation(
    session,
    ctx: TenantContext,
    competitor: CompetitorRecord,
    *,
    page_id: str,
    page_alias: str | None,
    page_name: str | None,
    source: str,
    resolve_job_id: str | None,
    evidence: dict,
    idempotency_key_hash: str,
    request_fingerprint: str,
    confirmed_by: str | None,
) -> CompetitorIdentityConfirmationRecord:
    """Append the next revision (callers hold :func:`lock_competitor`)."""
    current = session.scalar(
        select(func.max(CompetitorIdentityConfirmationRecord.revision)).where(
            CompetitorIdentityConfirmationRecord.workspace_id == ctx.workspace_id,
            CompetitorIdentityConfirmationRecord.competitor_id == competitor.id,
        )
    ) or 0
    record = CompetitorIdentityConfirmationRecord(
        id=new_id("cic"),
        workspace_id=ctx.workspace_id,
        competitor_id=competitor.id,
        revision=current + 1,
        platform="meta",
        page_id=page_id,
        page_alias=page_alias,
        page_name=page_name,
        source=source,
        resolve_job_id=resolve_job_id,
        evidence_json=evidence,
        idempotency_key_hash=idempotency_key_hash,
        request_fingerprint=request_fingerprint,
        confirmed_by=confirmed_by,
        confirmed_at=utcnow(),
    )
    session.add(record)
    session.flush()
    audit.append_event(
        session,
        event_type="competitor.confirmed",
        resource_type="competitor",
        resource_id=competitor.id,
        details={
            "revision": record.revision, "page_id": page_id, "source": source,
            "resolve_job_id": resolve_job_id,
        },
    )
    return record


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


_CANDIDATE_KEYS = (
    "page_id", "page_name", "page_alias", "category", "likes", "ig_username", "ig_followers",
    "verification", "page_is_deleted", "provider_rank", "rank", "name_match", "alias_match",
    "category_class", "excluded_reason", "domain_hit",
)
_PROBE_KEYS = ("status", "country", "total_active_ads", "sampled_ads", "landing_domains", "error_code")


def candidate_view(item: dict) -> dict:
    """A card; ``avatar`` stays ``None`` until :func:`sign_avatars` signs the archived copy."""
    view = {key: item.get(key) for key in _CANDIDATE_KEYS}
    view["alias_match"] = bool(item.get("alias_match"))
    probe = item.get("probe")
    view["probe"] = {key: probe.get(key) for key in _PROBE_KEYS} if probe else None
    if view["probe"] is not None:
        view["probe"]["landing_domains"] = list(probe.get("landing_domains") or [])
        view["probe"]["sampled_ads"] = int(probe.get("sampled_ads") or 0)
    view["avatar"] = None
    return view


def avatar_refs(record: CompetitorResolutionRecord) -> dict[str, dict]:
    """The archived avatar derivative of each candidate, by page ID (#332).

    Only while the cards can still be confirmed -- running, or reusable --
    as before, when the provider's link was shown for the same window."""
    if record.status in FINISHED and not _reusable(record, utcnow()):
        return {}
    avatars = (record.evidence_json or {}).get("avatars") or {}
    return {
        page_id: outcome["thumbnail"] for page_id, outcome in avatars.items()
        if outcome.get("status") == "available" and outcome.get("thumbnail")
    }


async def sign_avatars(blob_store, candidates: list[dict], refs: dict[str, dict]) -> None:
    """A short-lived signed GET of each candidate's archived avatar; ``None`` on any failure."""
    for candidate in candidates:
        ref = refs.get(candidate["page_id"])
        if ref:
            candidate["avatar"] = await scan_media.signed_derivative(blob_store, ref)


def ranked_candidates(record: CompetitorResolutionRecord, competitor_alias: str | None = None) -> list[dict]:
    """The frozen ranking of a finished resolve, or the live one of a running resolve."""
    evidence = record.evidence_json or {}
    if "ranking" in evidence:
        return list(evidence["ranking"])
    scored = scoring.score(evidence.get("candidates") or [], query=record.query, alias=competitor_alias)
    return scoring.rank(scored, probes=evidence.get("probes") or {}, website_domain=record.website_domain)


def _reusable(record: CompetitorResolutionRecord, now: datetime) -> bool:
    expires = _aware(record.expires_at)
    return (
        record.status in FINISHED and expires is not None and now < expires
        and bool((record.evidence_json or {}).get("candidates"))
    )


def resolution_summary(record: CompetitorResolutionRecord, now: datetime) -> dict:
    return {
        "resolve_job_id": record.job_id,
        "operation_id": record.operation_id,
        "status": record.status,
        "execution_mode": record.execution_mode,
        "auto_confirmed": bool((record.evidence_json or {}).get("auto_confirmation")),
        "reusable": _reusable(record, now),
        "expires_at": _iso(record.expires_at),
        "created_at": _iso(record.created_at),
        "finished_at": _iso(record.finished_at),
    }


def resolution_view(record: CompetitorResolutionRecord, competitor: CompetitorRecord) -> dict:
    now = utcnow()
    evidence = record.evidence_json or {}
    reusable = _reusable(record, now)
    auto = evidence.get("auto_confirmation") or {}
    failure = None
    if record.failure_code:
        failure = {
            "code": record.failure_code,
            "retryable": record.failure_code in RETRYABLE_FAILURES,
            "params": dict(evidence.get("failure_params") or {}),
        }
    return {
        "competitor_id": record.competitor_id,
        "resolve_job_id": record.job_id,
        "operation_id": record.operation_id,
        "status": record.status,
        "execution_mode": record.execution_mode,
        "provider": record.provider,
        "query": record.query,
        "website_domain": record.website_domain,
        "country": record.country,
        "quote": dict(record.quote_json or {}),
        "cost": dict(record.cost_json) if record.cost_json else None,
        "candidates": [candidate_view(item) for item in ranked_candidates(record, competitor.input_page_alias)],
        "auto_confirmed": bool(auto),
        "confirmation_revision": auto.get("revision"),
        "waits": len(evidence.get("waits") or []),
        "failure": failure,
        "reusable": reusable,
        "expires_at": _iso(record.expires_at),
        "created_at": _iso(record.created_at),
        "started_at": _iso(record.started_at),
        "finished_at": _iso(record.finished_at),
    }


def confirmation_view(record: CompetitorIdentityConfirmationRecord) -> dict:
    evidence = record.evidence_json or {}
    candidate = evidence.get("candidate")
    return {
        "revision": record.revision,
        "platform": record.platform,
        "page_id": record.page_id,
        "page_alias": record.page_alias,
        "page_name": record.page_name,
        "source": record.source,
        "link_kind": evidence.get("link_kind"),
        "resolve_job_id": record.resolve_job_id,
        "domain_hit": candidate.get("domain_hit") if candidate else None,
        "candidate": candidate_view(candidate) if candidate else None,
        "confirmed_by": record.confirmed_by,
        "confirmed_at": _iso(record.confirmed_at),
    }


def resolve_query(record: CompetitorRecord) -> str | None:
    """What a resolve searches: the name, else the alias of a pasted page link."""
    return record.name or record.input_page_alias


def observed_page_names(session, ctx: TenantContext, pages) -> dict[tuple[str, str], str]:
    """The advertiser name of each ``(product_id, page_id)``'s newest observation
    in that product's scans.

    A link confirms a page without a search (§4), so that revision has no
    name; the scans have seen the page since. Read only: no call, and the
    confirmation is never rewritten."""
    pages = {(product_id, page_id) for product_id, page_id in pages if product_id and page_id}
    if not pages:
        return {}
    named = and_(
        CompetitorAdRecord.workspace_id == ctx.workspace_id,
        CompetitorScanRecord.product_id.in_(sorted({product_id for product_id, _ in pages})),
        CompetitorAdRecord.advertiser_page_id.in_(sorted({page_id for _, page_id in pages})),
        CompetitorAdRecord.advertiser_name.is_not(None),
        CompetitorAdRecord.advertiser_name != "",
    )
    scan = and_(
        CompetitorScanRecord.workspace_id == CompetitorAdRecord.workspace_id,
        CompetitorScanRecord.id == CompetitorAdRecord.scan_id,
    )
    newest = (
        select(CompetitorScanRecord.product_id.label("product_id"),
               CompetitorAdRecord.advertiser_page_id.label("page_id"),
               func.max(CompetitorAdRecord.observed_at).label("observed_at"))
        .join(CompetitorScanRecord, scan).where(named)
        .group_by(CompetitorScanRecord.product_id, CompetitorAdRecord.advertiser_page_id)
        .subquery()
    )
    rows = session.execute(
        select(CompetitorScanRecord.product_id, CompetitorAdRecord.advertiser_page_id,
               CompetitorAdRecord.advertiser_name)
        .join(CompetitorScanRecord, scan)
        .join(newest, and_(newest.c.product_id == CompetitorScanRecord.product_id,
                           newest.c.page_id == CompetitorAdRecord.advertiser_page_id,
                           newest.c.observed_at == CompetitorAdRecord.observed_at))
        .where(named)
        .order_by(CompetitorAdRecord.id.desc())
    )
    names: dict[tuple[str, str], str] = {}
    for product_id, page_id, name in rows:
        if (product_id, page_id) in pages:
            names.setdefault((product_id, page_id), name)
    return names


def last_scan_times(session, ctx: TenantContext, *, product_ids, competitor_ids) -> dict[str, datetime]:
    """When each competitor was last scanned: the newest finished scan whose
    frozen ``competitor_ids`` include it. A queued or running scan (a resume
    included) has not finished; a scan of an archived competitor still counts."""
    wanted = set(competitor_ids)
    product_ids = sorted(set(product_ids))
    if not wanted or not product_ids:
        return {}
    at = func.coalesce(CompetitorScanRecord.finished_at, CompetitorScanRecord.created_at)
    rows = session.execute(
        select(CompetitorScanRecord.params_json, at)
        .where(
            CompetitorScanRecord.workspace_id == ctx.workspace_id,
            CompetitorScanRecord.product_id.in_(product_ids),
            CompetitorScanRecord.status.in_(SCAN_FINISHED_STATUSES),
        )
        .order_by(at.desc(), CompetitorScanRecord.id.desc())
    )
    times: dict[str, datetime] = {}
    for params, finished in rows:
        for competitor_id in (params or {}).get("competitor_ids") or ():
            if competitor_id in wanted:
                times.setdefault(competitor_id, finished)
        if len(times) == len(wanted):
            break
    return times


def competitor_view(
    session,
    ctx: TenantContext,
    record: CompetitorRecord,
    *,
    confirmation: CompetitorIdentityConfirmationRecord | None | bool = False,
    resolution: CompetitorResolutionRecord | None | bool = False,
    observed_name: str | None | bool = False,
    last_scanned_at: datetime | None | bool = False,
) -> dict:
    if confirmation is False:
        confirmation = latest_confirmation(session, ctx, record.id)
    if resolution is False:
        resolution = session.scalars(
            select(CompetitorResolutionRecord)
            .where(
                CompetitorResolutionRecord.workspace_id == ctx.workspace_id,
                CompetitorResolutionRecord.competitor_id == record.id,
            )
            .order_by(CompetitorResolutionRecord.created_at.desc(), CompetitorResolutionRecord.id.desc())
        ).first()
    if observed_name is False:
        page = (record.product_id, confirmation.page_id) if confirmation else None
        observed_name = observed_page_names(session, ctx, [page]).get(page) if page else None
    if last_scanned_at is False:
        last_scanned_at = last_scan_times(
            session, ctx, product_ids=[record.product_id], competitor_ids=[record.id],
        ).get(record.id)
    return {
        "competitor_id": record.id,
        "product_id": record.product_id,
        "role": record.role,
        "status": record.status,
        "name": record.name,
        "website_domain": record.website_domain,
        "input_kind": record.input_kind,
        "input_page_alias": record.input_page_alias,
        "confirmation_status": "confirmed" if confirmation else "unconfirmed",
        "confirmation": confirmation_view(confirmation) if confirmation else None,
        "observed_page_name": observed_name if confirmation else None,
        "last_scanned_at": _iso(last_scanned_at) if last_scanned_at else None,
        "latest_resolution": resolution_summary(resolution, utcnow()) if resolution else None,
        "can_resolve": record.status == "active" and resolve_query(record) is not None,
        "created_by": record.created_by,
        "created_at": _iso(record.created_at),
        "archived_by": record.archived_by,
        "archived_at": _iso(record.archived_at),
    }


# ---------------------------------------------------------------------------
# Competitors
# ---------------------------------------------------------------------------


def create_competitor(
    ctx: TenantContext,
    *,
    product_id: str,
    input_text: str,
    website: str | None,
    role: str,
    idempotency_key: str,
) -> dict:
    """Add a competitor; a link with a page id is confirmed in the same transaction."""
    _require_member(ctx)
    try:
        parsed = inputs.parse_input(input_text)
        website_domain = inputs.normalize_website(website)
    except CompetitorInputError as exc:
        raise _input_problem(exc) from exc
    key_hash = _key_hash(idempotency_key)
    fingerprint = _sha({
        "product_id": product_id, "input": parsed.text, "website_domain": website_domain, "role": role,
    })
    with tenant_session(ctx) as session:
        existing = session.scalars(
            select(CompetitorRecord).where(
                CompetitorRecord.workspace_id == ctx.workspace_id,
                CompetitorRecord.idempotency_key_hash == key_hash,
            )
        ).one_or_none()
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise CompetitorProblem(
                    409, "idempotency_key_reused", "this Idempotency-Key was used for another competitor",
                )
            return {**competitor_view(session, ctx, existing), "replayed": True}

        product = session.scalars(
            select(ProductRecord.id).where(
                ProductRecord.workspace_id == ctx.workspace_id, ProductRecord.id == product_id,
            )
        ).one_or_none()
        if product is None:
            raise CompetitorProblem(404, "product_not_found", "no such product in this Workspace")

        now = utcnow()
        record = CompetitorRecord(
            id=new_id("cmp"),
            workspace_id=ctx.workspace_id,
            product_id=product_id,
            role=role,
            status="active",
            name=parsed.name,
            website_domain=website_domain,
            input_kind=parsed.kind,
            input_text=parsed.text,
            input_page_alias=parsed.page_alias,
            idempotency_key_hash=key_hash,
            request_fingerprint=fingerprint,
            created_by=ctx.initiator_id,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        session.flush()
        audit.append_event(
            session,
            event_type="competitor.created",
            resource_type="competitor",
            resource_id=record.id,
            details={
                "product_id": product_id, "role": role, "input_kind": parsed.kind,
                "has_website": website_domain is not None,
            },
        )
        confirmation = None
        if parsed.page_id is not None:
            confirmation = append_confirmation(
                session, ctx, record,
                page_id=parsed.page_id,
                page_alias=None,
                page_name=None,
                source="link",
                resolve_job_id=None,
                evidence={"link_kind": parsed.kind, "link": parsed.text},
                idempotency_key_hash=_sha({"link_on_create": record.id}),
                request_fingerprint=fingerprint,
                confirmed_by=ctx.initiator_id,
            )
        return {
            **competitor_view(session, ctx, record, confirmation=confirmation, resolution=None),
            "replayed": False,
        }


def list_competitors(ctx: TenantContext, *, product_id: str | None, status: str) -> dict:
    _require_member(ctx)
    with tenant_session(ctx) as session:
        statement = select(CompetitorRecord).where(CompetitorRecord.workspace_id == ctx.workspace_id)
        if product_id is not None:
            statement = statement.where(CompetitorRecord.product_id == product_id)
        if status != "all":
            statement = statement.where(CompetitorRecord.status == status)
        records = session.scalars(
            statement.order_by(CompetitorRecord.created_at.desc(), CompetitorRecord.id.desc()).limit(LIST_LIMIT)
        ).all()
        ids = [record.id for record in records]
        confirmations: dict[str, CompetitorIdentityConfirmationRecord] = {}
        resolutions: dict[str, CompetitorResolutionRecord] = {}
        if ids:
            for row in session.scalars(
                select(CompetitorIdentityConfirmationRecord)
                .where(
                    CompetitorIdentityConfirmationRecord.workspace_id == ctx.workspace_id,
                    CompetitorIdentityConfirmationRecord.competitor_id.in_(ids),
                )
                .order_by(CompetitorIdentityConfirmationRecord.revision.desc())
            ):
                confirmations.setdefault(row.competitor_id, row)
            for row in session.scalars(
                select(CompetitorResolutionRecord)
                .where(
                    CompetitorResolutionRecord.workspace_id == ctx.workspace_id,
                    CompetitorResolutionRecord.competitor_id.in_(ids),
                )
                .order_by(CompetitorResolutionRecord.created_at.desc(), CompetitorResolutionRecord.id.desc())
            ):
                resolutions.setdefault(row.competitor_id, row)
        pages = {
            record.id: (record.product_id, confirmations[record.id].page_id)
            for record in records if record.id in confirmations
        }
        names = observed_page_names(session, ctx, pages.values())
        scanned = last_scan_times(
            session, ctx, product_ids={record.product_id for record in records}, competitor_ids=ids,
        )
        return {"items": [
            competitor_view(
                session, ctx, record, confirmation=confirmations.get(record.id),
                resolution=resolutions.get(record.id), observed_name=names.get(pages.get(record.id)),
                last_scanned_at=scanned.get(record.id),
            )
            for record in records
        ]}


def get_competitor(ctx: TenantContext, competitor_id: str) -> dict:
    _require_member(ctx)
    with tenant_session(ctx) as session:
        return competitor_view(session, ctx, _load(session, ctx, competitor_id))


def archive_competitor(ctx: TenantContext, competitor_id: str) -> dict:
    """Owner only; archived competitors are neither resolved, confirmed nor scanned."""
    with tenant_session(ctx) as session:
        record = _load(session, ctx, competitor_id)
        if ctx.role != "owner":
            raise CompetitorProblem(403, "competitor_archive_forbidden", "only the Owner archives competitors")
        if record.status != "archived":
            now = utcnow()
            record.status = "archived"
            record.archived_by = ctx.initiator_id
            record.archived_at = now
            record.updated_at = now
            session.flush()
            audit.append_event(
                session, event_type="competitor.archived", resource_type="competitor", resource_id=record.id,
                details={"product_id": record.product_id},
            )
        return competitor_view(session, ctx, record)


# ---------------------------------------------------------------------------
# Resolve: quote and admission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Modes:
    available: tuple[str, ...]
    default: str
    byok_unavailable_code: str | None
    credential_version_id: str | None
    #: What ``byok_unavailable_code``'s copy interpolates: the provider, and
    #: the missing ``operations`` of an incomplete price table (#318).
    byok_unavailable_params: dict = field(default_factory=dict)


def _modes(session, ctx: TenantContext) -> _Modes:
    credential = resolve_market_data_credential(session, ctx, SCRAPECREATORS)
    if not credential.get("active"):
        return _Modes(("mock",), "mock", "market_data_credential_missing", None, {"provider": SCRAPECREATORS})
    try:
        price_table(SCRAPECREATORS)
    except MarketDataPricingError as exc:
        return _Modes(("mock",), "mock", exc.code, None, dict(exc.params))
    return _Modes(("byok", "mock"), "byok", None, credential["version_id"])


def _country(value: str) -> str:
    country = (value or "").strip().upper()
    if country not in ISO_3166_ALPHA2:
        raise CompetitorProblem(
            422, "competitor_country_invalid", "country must be an ISO 3166-1 alpha-2 code",
            params={"country": value},
        )
    return country


def _resolvable(record: CompetitorRecord) -> str:
    if record.status != "active":
        raise CompetitorProblem(409, "competitor_archived", "the competitor is archived")
    query = resolve_query(record)
    if query is None:
        raise CompetitorProblem(
            409, "competitor_resolve_needs_name",
            "this competitor was added by page id; there is no name to search",
        )
    return query


def _choose_mode(modes: _Modes, requested: str | None) -> str:
    mode = requested or modes.default
    if mode not in modes.available:
        code = modes.byok_unavailable_code or "market_data_credential_missing"
        raise CompetitorProblem(
            409, code, "real resolves need a bound market-data key and a configured price table",
            params=dict(modes.byok_unavailable_params) or {"provider": SCRAPECREATORS},
        )
    return mode


def _quote(
    competitor: CompetitorRecord, *, query: str, mode: str, modes: _Modes, country: str,
) -> dict:
    try:
        prices = price_table(SCRAPECREATORS)
    except MarketDataPricingError:
        if mode == "byok":  # pragma: no cover - _modes offers byok only with prices
            raise
        prices = None
    lines = []
    for operation, calls in (("page_search", 1), ("probe", len(PROBE_CALLS))):
        price = prices.get(operation) if prices else None
        credits = (price.credits if price else 1) * calls
        usd = price.unit_usd * calls if price else None
        lines.append({"operation": operation, "calls": calls, "credits": credits, "usd": usd})
    total_usd = sum((line["usd"] for line in lines), Decimal("0")) if prices else None
    for line in lines:
        line["usd"] = format(line["usd"], "f") if mode == "byok" else "0"
    return {
        "competitor_id": competitor.id,
        "execution_mode": mode,
        "available_modes": list(modes.available),
        "byok_unavailable_code": modes.byok_unavailable_code,
        "byok_unavailable_params": dict(modes.byok_unavailable_params),
        "provider": SCRAPECREATORS if mode == "byok" else MOCK_PROVIDER,
        "query": query,
        "website_domain": competitor.website_domain,
        "country": country,
        "max_credits": sum(line["credits"] for line in lines),
        "max_usd": format(total_usd, "f") if mode == "byok" else "0",
        "simulated_usd": format(total_usd, "f") if mode == "mock" and total_usd is not None else None,
        "currency": "USD",
        "pricing_version": config.MARKETAI_PRICE_REGISTRY_VERSION if prices else None,
        "lines": lines,
    }


def quote_resolve(ctx: TenantContext, competitor_id: str, *, execution_mode: str | None, country: str) -> dict:
    _require_member(ctx)
    country = _country(country)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, competitor_id)
        query = _resolvable(record)
        modes = _modes(session, ctx)
        mode = _choose_mode(modes, execution_mode)
        return _quote(record, query=query, mode=mode, modes=modes, country=country)


def start_resolve(
    ctx: TenantContext,
    competitor_id: str,
    *,
    execution_mode: str | None,
    country: str,
    idempotency_key: str,
) -> tuple[dict, dict]:
    """Admit a resolve: row + Operation + Job + one reservation per call, one transaction.

    Returns the view and, for a replay, the archived avatars to sign."""
    _require_member(ctx)
    country = _country(country)
    key_hash = _key_hash(idempotency_key)
    fingerprint = _sha({"competitor_id": competitor_id, "execution_mode": execution_mode, "country": country})
    with tenant_session(ctx) as session:
        competitor = _load(session, ctx, competitor_id)
        existing = session.scalars(
            select(CompetitorResolutionRecord).where(
                CompetitorResolutionRecord.workspace_id == ctx.workspace_id,
                CompetitorResolutionRecord.competitor_id == competitor.id,
                CompetitorResolutionRecord.idempotency_key_hash == key_hash,
            )
        ).one_or_none()
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise CompetitorProblem(
                    409, "idempotency_key_reused", "this Idempotency-Key was used for another resolve",
                )
            return {**resolution_view(existing, competitor), "replayed": True}, avatar_refs(existing)

        query = _resolvable(competitor)
        modes = _modes(session, ctx)
        mode = _choose_mode(modes, execution_mode)
        quote = _quote(competitor, query=query, mode=mode, modes=modes, country=country)
        resolution_id = new_id("cres")
        operation = create_operation(
            session,
            command_type=COMMAND_TYPE,
            idempotency_key_hash=key_hash,
            initial_state=OperationState.ADMITTED,
            admission_contract_version=1,
            result_ref=OperationResultRef(resource_type=RESOURCE_TYPE, resource_id=resolution_id),
            reason="competitor resolve admitted",
        )
        job = enqueue_in_session(
            session,
            context=ctx,
            job_type=JOB_TYPE,
            payload={"resolution_id": resolution_id, "operation_id": operation.id},
            idempotency_key=f"competitor_resolve:{resolution_id}",
            resource_type=RESOURCE_TYPE,
            resource_id=resolution_id,
            # A redelivered run replays its calls from the fence; one left
            # ``dispatching`` turns ``uncertain``, never resent (#327).
            safe_to_retry_after_crash=True,
            max_attempts=RESOLVE_JOB_MAX_ATTEMPTS,
        )
        job.operation_id = operation.id
        session.flush()
        credential_version_id = modes.credential_version_id if mode == "byok" else None
        reservations: dict[str, str] = {}
        if mode == "byok":
            try:
                reservations = reserve_market_data_calls(
                    session, ctx,
                    operation_id=operation.id, job_id=job.id, provider=SCRAPECREATORS, calls=RESOLVE_CALLS,
                )
            except MarketDataCallError as exc:
                status = {"guardrail_exceeded": 409, "entitlement_unavailable": 403}.get(exc.code, 409)
                raise CompetitorProblem(status, exc.code, str(exc), params=exc.params) from exc
            except MarketDataPricingError as exc:
                raise CompetitorProblem(409, exc.code, str(exc), params=exc.params) from exc
        now = utcnow()
        # Inserted once, complete: the API role may not UPDATE a resolve (the Worker advances it).
        record = CompetitorResolutionRecord(
            id=resolution_id,
            workspace_id=ctx.workspace_id,
            competitor_id=competitor.id,
            job_id=job.id,
            operation_id=operation.id,
            status="queued",
            execution_mode=mode,
            provider=quote["provider"],
            credential_version_id=credential_version_id,
            query=query,
            website_domain=competitor.website_domain,
            country=country,
            quote_json=quote,
            evidence_json={
                "admission": {
                    "execution_mode": mode,
                    "credential_version_id": credential_version_id,
                    "reservations": reservations,
                    "calls": [{"call_name": call.call_name, "operation": call.operation} for call in RESOLVE_CALLS],
                }
            },
            cost_json={},
            idempotency_key_hash=key_hash,
            request_fingerprint=fingerprint,
            created_by=ctx.initiator_id,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        session.flush()
        audit.append_event(
            session,
            event_type="competitor.resolve_requested",
            resource_type="competitor",
            resource_id=competitor.id,
            details={
                "resolution_id": resolution_id, "job_id": job.id, "operation_id": operation.id,
                "execution_mode": mode, "country": country, "max_credits": quote["max_credits"],
                "max_usd": quote["max_usd"],
            },
        )
        session.flush()
        return {**resolution_view(record, competitor), "replayed": False}, {}


def get_resolution(ctx: TenantContext, competitor_id: str, resolve_job_id: str) -> tuple[dict, dict]:
    """The resolve and the archived avatars to sign (:func:`sign_avatars`)."""
    _require_member(ctx)
    with tenant_session(ctx) as session:
        competitor = _load(session, ctx, competitor_id)
        record = _resolution(session, ctx, competitor.id, resolve_job_id)
        return resolution_view(record, competitor), avatar_refs(record)


def _resolution(session, ctx: TenantContext, competitor_id: str, resolve_job_id: str) -> CompetitorResolutionRecord:
    record = session.scalars(
        select(CompetitorResolutionRecord).where(
            CompetitorResolutionRecord.workspace_id == ctx.workspace_id,
            CompetitorResolutionRecord.competitor_id == competitor_id,
            CompetitorResolutionRecord.job_id == resolve_job_id,
        )
    ).one_or_none()
    if record is None:
        raise CompetitorProblem(404, "competitor_resolution_not_found", "no such resolve for this competitor")
    return record


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def confirmation_evidence(record: CompetitorResolutionRecord, candidate: dict) -> dict:
    """What a confirmation keeps from a resolve: the card as confirmed, no avatar."""
    kept = {key: value for key, value in candidate.items() if key not in ("avatar_url", "field_sources")}
    return {
        "candidate": kept,
        "query": record.query,
        "website_domain": record.website_domain,
        "country": record.country,
        "execution_mode": record.execution_mode,
        "resolution_id": record.id,
    }


def confirm_competitor(
    ctx: TenantContext,
    competitor_id: str,
    *,
    resolve_job_id: str | None,
    page_id: str | None,
    page_url: str | None,
    idempotency_key: str,
) -> dict:
    _require_member(ctx)
    by_resolve = resolve_job_id is not None and page_id is not None and page_url is None
    by_link = page_url is not None and resolve_job_id is None and page_id is None
    if not (by_resolve or by_link):
        raise CompetitorProblem(
            422, "competitor_confirm_target_required",
            "confirm a resolve candidate (resolve_job_id + page_id) or a page link (page_url)",
        )
    key_hash = _key_hash(idempotency_key)
    fingerprint = _sha({"resolve_job_id": resolve_job_id, "page_id": page_id, "page_url": page_url})
    with tenant_session(ctx) as session:
        competitor = _load(session, ctx, competitor_id)
        lock_competitor(session, ctx, competitor.id)
        replay = session.scalars(
            select(CompetitorIdentityConfirmationRecord).where(
                CompetitorIdentityConfirmationRecord.workspace_id == ctx.workspace_id,
                CompetitorIdentityConfirmationRecord.competitor_id == competitor.id,
                CompetitorIdentityConfirmationRecord.idempotency_key_hash == key_hash,
            )
        ).one_or_none()
        if replay is not None:
            if replay.request_fingerprint != fingerprint:
                raise CompetitorProblem(
                    409, "idempotency_key_reused", "this Idempotency-Key was used for another confirmation",
                )
            return {**competitor_view(session, ctx, competitor), "replayed": True}
        if competitor.status != "active":
            raise CompetitorProblem(409, "competitor_archived", "the competitor is archived")

        if by_link:
            try:
                parsed = inputs.parse_page_link(page_url)
            except CompetitorInputError as exc:
                raise _input_problem(exc) from exc
            target = {
                "page_id": parsed.page_id, "page_alias": None, "page_name": None, "source": "link",
                "resolve_job_id": None, "evidence": {"link_kind": parsed.kind, "link": parsed.text},
            }
        else:
            resolution = _resolution(session, ctx, competitor.id, resolve_job_id)
            if resolution.status not in FINISHED:
                raise CompetitorProblem(409, "competitor_resolution_not_ready", "the resolve has not finished")
            if not _reusable(resolution, utcnow()):
                raise CompetitorProblem(
                    409, "competitor_resolution_expired",
                    "the resolve is older than 72 hours or found no pages; resolve again",
                    params={"hours": int(REUSE_WINDOW.total_seconds() // 3600)},
                )
            candidate = next(
                (item for item in ranked_candidates(resolution, competitor.input_page_alias)
                 if item.get("page_id") == page_id),
                None,
            )
            if candidate is None:
                raise CompetitorProblem(
                    422, "competitor_candidate_not_found", "the page is not a candidate of this resolve",
                )
            if candidate.get("excluded_reason") == "deleted":
                raise CompetitorProblem(422, "competitor_candidate_deleted", "the page has been deleted")
            target = {
                "page_id": page_id, "page_alias": candidate.get("page_alias"),
                "page_name": candidate.get("page_name"), "source": "user",
                "resolve_job_id": resolution.job_id, "evidence": confirmation_evidence(resolution, candidate),
            }

        current = latest_confirmation(session, ctx, competitor.id)
        if (
            current is not None
            and current.page_id == target["page_id"]
            and current.resolve_job_id == target["resolve_job_id"]
        ):
            return {**competitor_view(session, ctx, competitor, confirmation=current), "replayed": True}
        confirmation = append_confirmation(
            session, ctx, competitor,
            page_id=target["page_id"],
            page_alias=target["page_alias"],
            page_name=target["page_name"],
            source=target["source"],
            resolve_job_id=target["resolve_job_id"],
            evidence=target["evidence"],
            idempotency_key_hash=key_hash,
            request_fingerprint=fingerprint,
            confirmed_by=ctx.initiator_id,
        )
        return {**competitor_view(session, ctx, competitor, confirmation=confirmation), "replayed": False}
