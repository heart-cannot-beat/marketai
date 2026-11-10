"""Competitor scans: API commands and reads (Issue #270, spec §5, §6).

Authorization: any Workspace member (Owner or Member) previews, starts and
resumes a scan and reads its observations (§6: the scan is paid by the
member's Workspace); marking an ``uncertain`` page by hand is Owner-only.
Everything is Workspace-scoped: another Workspace's scan, product or
competitor is "not found", never "forbidden" (no existence oracle).

Admission (create and resume) happens in one transaction: the scan row (or
its update), one Operation, one ``competitor_scan.run`` Job and every
reservation of :mod:`app.competitors.scan_plan` -- the market-data calls
in one ``reserve_market_data_calls`` and each LLM call name with
``reserve_llm_chat``. The Spend Guardrail sees both lines: the outstanding,
rolling and period limits through each reservation, the per-Operation limit
on their sum (checked here, since each reserve call checks only its own
amount). Any refusal rolls the whole command back.

A real scan needs the ``market_data`` key, a complete price table and the
AI key ``llm_chat`` uses; with any of them missing only Controlled Mock is
offered, for the whole scan (fetch and judgments), and the two never mix.

Idempotency: create replays by ``Idempotency-Key`` (never by date); a
resume or a reconciliation replays by the key recorded on the scan.

Tags, clusters and snapshots (Issue #272): members confirm or change an
observation's tags (a new revision against the scan's frozen vocabulary),
refreeze a scan (free and pure: an unchanged hash returns the existing
snapshot) and recluster it (paid: quoted, then ``cluster:1`` reserved for a
Job of its own that appends a clusters revision and freezes once). The
summary is read from a snapshot. ``products.competitor_snapshot_in_drafts``
is any member's to switch. A resume also finishes the judgment steps of a
fully fetched scan: it reserves only the unfinished steps' unanswered
ordinals (:func:`app.competitors.scan_plan.llm_backlog_names`).
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import func, select

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import ProductRecord, new_id, utcnow
from app.competitors import discovery
from app.competitors import relevance as judgments
from app.competitors import scan_media, scan_plan, snapshots, tags
from app.competitors.models import (
    CompetitorAdOriginRecord,
    CompetitorAdRecord,
    CompetitorRecord,
    CompetitorScanRecord,
)
from app.competitors.scan_plan import CallPlan, ScanParams
from app.competitors.service import CompetitorProblem, _country, _iso, latest_confirmation, observed_page_names
from app.competitors.sources.mock import MOCK_PROVIDER
from app.entitlements.cost_models import SpendReservationRecord
from app.generation.credential_binding import resolve_generation_credential
from app.market_data.calls import MarketDataCallError, reserve_market_data_calls
from app.market_data.credentials import SCRAPECREATORS, resolve_market_data_credential
from app.market_data.pricing import MarketDataPricingError, price_table
from app.platform import audit
from app.platform.jobs import enqueue_in_session
from app.platform.operations import OperationResultRef, OperationState, create_operation
from app.platform.pagination import InvalidCursorError, decode_offset_cursor, encode_cursor
from app.platform.tenancy import UI_LANGUAGES, TenantContext
from app.providers.llm_chat import LlmChatError, reserve_llm_chat
from app.providers.llm_registry import OPENAI_PROVIDER

JOB_TYPE = "competitor_scan.run"
COMMAND_TYPE = "competitor_scan"
RESUME_COMMAND_TYPE = "competitor_scan_resume"
RECONCILE_COMMAND_TYPE = "competitor_scan_reconcile"
RECLUSTER_COMMAND_TYPE = "competitor_scan_recluster"
SETTINGS_EVENT = "product.competitor_settings_updated"
RESOURCE_TYPE = "competitor_scan"
#: Waiting for another market-data job on the same key re-queues the Job
#: every 10 seconds (§5.1); each wait uses one attempt (about 15 minutes).
SCAN_JOB_MAX_ATTEMPTS = 90

FINISHED = frozenset({"completed", "partial", "failed", "uncertain"})
LIST_LIMIT = 50
#: Failures a resume may get past without anyone changing anything first.
RETRYABLE_FAILURES = frozenset({
    "market_data_rate_limited", "market_data_unreachable", "market_data_turn_unavailable",
    "market_data_provider_error",
})


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _key_hash(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _require_member(ctx: TenantContext) -> None:
    if ctx.role not in ("owner", "member"):
        raise CompetitorProblem(403, "competitor_forbidden", "only Workspace members manage competitors")


def lock_scan(session, ctx: TenantContext, scan_id: str) -> CompetitorScanRecord | None:
    """The scan row, locked for this transaction (PostgreSQL; SQLite has one
    writer, and SQLAlchemy leaves the lock out of its SQL)."""
    statement = select(CompetitorScanRecord).where(
        CompetitorScanRecord.workspace_id == ctx.workspace_id, CompetitorScanRecord.id == scan_id,
    ).with_for_update()
    return session.scalars(statement).one_or_none()


def _load(session, ctx: TenantContext, scan_id: str, *, lock: bool = False) -> CompetitorScanRecord:
    record = lock_scan(session, ctx, scan_id) if lock else session.scalars(
        select(CompetitorScanRecord).where(
            CompetitorScanRecord.workspace_id == ctx.workspace_id, CompetitorScanRecord.id == scan_id,
        )
    ).one_or_none()
    if record is None:
        raise CompetitorProblem(404, "competitor_scan_not_found", "no such scan in this Workspace")
    return record


# ---------------------------------------------------------------------------
# Modes: both keys or Mock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanModes:
    available: tuple[str, ...]
    default: str
    byok_unavailable_code: str | None
    byok_unavailable_params: dict
    credentials: list[dict]
    market_data_version_id: str | None
    prices: dict | None = field(default=None, compare=False)


def scan_modes(session, ctx: TenantContext) -> ScanModes:
    market_data = resolve_market_data_credential(session, ctx, SCRAPECREATORS)
    ai = resolve_generation_credential(session, ctx, OPENAI_PROVIDER)
    prices, pricing_error = None, None
    try:
        prices = price_table(SCRAPECREATORS)
    except MarketDataPricingError as exc:
        pricing_error = exc
    md_entry = {"binding_scope": "market_data", "provider": SCRAPECREATORS, "ready": True,
                "code": None, "params": {}}
    if not market_data.get("active"):
        md_entry.update(ready=False, code="market_data_credential_missing", params={"provider": SCRAPECREATORS})
    elif pricing_error is not None:
        md_entry.update(ready=False, code=pricing_error.code, params=dict(pricing_error.params))
    ai_entry = {"binding_scope": "generation", "provider": OPENAI_PROVIDER, "ready": True, "code": None, "params": {}}
    if not ai.get("active"):
        ai_entry.update(ready=False, code="competitor_scan_ai_credential_missing")
    credentials = [md_entry, ai_entry]
    blocking = next((entry for entry in credentials if not entry["ready"]), None)
    if blocking is not None:
        return ScanModes(("mock",), "mock", blocking["code"], dict(blocking["params"]), credentials, None, prices)
    return ScanModes(("byok", "mock"), "byok", None, {}, credentials, market_data["version_id"], prices)


def _choose_mode(modes: ScanModes, requested: str | None) -> str:
    mode = requested or modes.default
    if mode not in modes.available:
        raise CompetitorProblem(
            409, modes.byok_unavailable_code or "market_data_credential_missing",
            "real scans need the market-data key, a configured price table and the AI key",
            params=dict(modes.byok_unavailable_params) or {"provider": SCRAPECREATORS},
        )
    return mode


# ---------------------------------------------------------------------------
# Parameters and targets
# ---------------------------------------------------------------------------


def _params(ctx: TenantContext, raw: Mapping[str, Any]) -> ScanParams:
    return ScanParams(
        competitor_ids=scan_plan.unique(raw.get("competitor_ids") or ()),
        keywords=scan_plan.normalize_keywords(raw.get("keywords") or ()),
        country=_country(raw.get("country") or "US"),
        max_pages_per_competitor=int(raw["max_pages_per_competitor"]),
        keyword_pages=int(raw["keyword_pages"]),
        max_media_refreshes=int(raw["max_media_refreshes"]),
        max_tagged_ads=int(raw["max_tagged_ads"]),
        max_discover_advertisers=int(raw["max_discover_advertisers"]),
        language=ctx.ui_language if ctx.ui_language in UI_LANGUAGES else "en",
    )


def _targets(session, ctx: TenantContext, product_id: str, params: ScanParams) -> list[dict]:
    """The frozen fetch paths: each competitor's confirmed page as of now, then the keywords."""
    product = session.scalars(
        select(ProductRecord.id).where(ProductRecord.workspace_id == ctx.workspace_id, ProductRecord.id == product_id)
    ).one_or_none()
    if product is None:
        raise CompetitorProblem(404, "product_not_found", "no such product in this Workspace")
    targets = []
    for competitor_id in params.competitor_ids:
        competitor = session.scalars(
            select(CompetitorRecord).where(
                CompetitorRecord.workspace_id == ctx.workspace_id, CompetitorRecord.id == competitor_id,
            )
        ).one_or_none()
        if competitor is None:
            raise CompetitorProblem(404, "competitor_not_found", "no such competitor in this Workspace",
                                    params={"competitor_id": competitor_id})
        if competitor.product_id != product_id:
            raise CompetitorProblem(422, "competitor_scan_product_mismatch",
                                    "the competitor belongs to another product", params={"competitor_id": competitor_id})
        if competitor.status != "active":
            raise CompetitorProblem(409, "competitor_archived", "the competitor is archived",
                                    params={"competitor_id": competitor_id})
        confirmation = latest_confirmation(session, ctx, competitor_id)
        if confirmation is None:
            raise CompetitorProblem(422, "competitor_scan_competitor_unconfirmed",
                                    "only competitors with a confirmed page can be scanned",
                                    params={"competitor_id": competitor_id})
        targets.append({
            "origin": f"page:{competitor_id}", "kind": "page", "competitor_id": competitor_id,
            "role": competitor.role, "page_id": confirmation.page_id, "page_alias": confirmation.page_alias,
            "page_name": confirmation.page_name, "confirmation_revision": confirmation.revision,
            "keyword": None, "keyword_index": None,
            "pages_planned": params.max_pages_per_competitor,
        })
    for index, keyword in enumerate(params.keywords, start=1):
        targets.append({
            "origin": f"keyword:{keyword}", "kind": "keyword", "competitor_id": None, "role": None,
            "page_id": None, "page_alias": None, "page_name": None, "confirmation_revision": None,
            "keyword": keyword, "keyword_index": index, "pages_planned": params.keyword_pages,
        })
    for target in targets:
        target.update(pages_fetched=0, cursor=None, exhausted=False, status="pending", failure=None,
                      ads_seen=0, pages=[])
    return targets


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------


def _quote(
    *, kind: str, product_id: str, params: ScanParams, plan: CallPlan, mode: str, modes: ScanModes,
    scan_id: str | None = None,
) -> dict:
    market_data = scan_plan.market_data_quote(
        plan.counts(), mode=mode, provider=SCRAPECREATORS if mode == "byok" else MOCK_PROVIDER,
        prices=modes.prices, pricing_version=config.MARKETAI_PRICE_REGISTRY_VERSION,
    )
    llm = scan_plan.llm_quote(plan.llm_counts(), mode=mode)
    return {
        "kind": kind,
        "scan_id": scan_id,
        "product_id": product_id,
        "execution_mode": mode,
        "available_modes": list(modes.available),
        "byok_unavailable_code": modes.byok_unavailable_code,
        "byok_unavailable_params": dict(modes.byok_unavailable_params),
        "credentials": [dict(entry) for entry in modes.credentials],
        "country": params.country,
        "language": params.language,
        "competitor_count": len(params.competitor_ids),
        "keyword_count": len(params.keywords),
        **scan_plan.combine(market_data, llm, mode=mode),
        "currency": "USD",
    }


def preview_scan(ctx: TenantContext, raw: Mapping[str, Any], *, execution_mode: str | None) -> dict:
    _require_member(ctx)
    params = _params(ctx, raw)
    with tenant_session(ctx) as session:
        _targets(session, ctx, raw["product_id"], params)
        modes = scan_modes(session, ctx)
        mode = _choose_mode(modes, execution_mode)
        return _quote(kind="scan", product_id=raw["product_id"], params=params,
                      plan=scan_plan.initial_plan(params), mode=mode, modes=modes)


# ---------------------------------------------------------------------------
# Admission (shared by create and resume)
# ---------------------------------------------------------------------------


def _reserve(session, ctx: TenantContext, *, operation_id: str, job_id: str, mode: str, plan: CallPlan) -> dict:
    """Every call of ``plan`` reserved under one Operation (byok); call names only under Mock."""
    reservations: dict[str, dict[str, str | None]] = {"market_data": {}, "llm": {}}
    if mode != "byok":
        return reservations
    try:
        if plan.market_data_calls:
            reservations["market_data"] = reserve_market_data_calls(
                session, ctx, operation_id=operation_id, job_id=job_id, provider=SCRAPECREATORS,
                calls=plan.market_data_calls,
            )
        for step, call_name in plan.llm:
            budget = scan_plan.LLM_BUDGETS[step]
            reservation = reserve_llm_chat(
                session, ctx, operation_id=operation_id, job_id=job_id, call_name=call_name,
                execution_mode=mode, input_token_budget=budget.max_input_tokens,
                max_output_tokens=budget.max_output_tokens,
            )
            reservations["llm"][call_name] = reservation.reservation_id
    except MarketDataCallError as exc:
        status = {"guardrail_exceeded": 409, "entitlement_unavailable": 403}.get(exc.code, 409)
        raise CompetitorProblem(status, exc.code, str(exc), params=exc.params) from exc
    except MarketDataPricingError as exc:
        raise CompetitorProblem(409, exc.code, str(exc), params=exc.params) from exc
    except LlmChatError as exc:
        code = "competitor_scan_ai_credential_missing" if exc.code == "credential_unavailable" else exc.code
        status = {"guardrail_exceeded": 409, "entitlement_unavailable": 403}.get(exc.code, 409)
        raise CompetitorProblem(status, code, exc.detail) from exc
    _check_operation_limit(session, ctx, operation_id)
    return reservations


def _check_operation_limit(session, ctx: TenantContext, operation_id: str) -> None:
    """The per-Operation limit on both lines together (each reserve call checked only its own amount)."""
    from app.generation.spend import ensure_spend_guardrail

    guardrail = ensure_spend_guardrail(session, ctx)
    total = session.scalar(
        select(func.coalesce(func.sum(SpendReservationRecord.maximum_budget_amount), 0)).where(
            SpendReservationRecord.workspace_id == ctx.workspace_id,
            SpendReservationRecord.operation_id == operation_id,
        )
    )
    total = Decimal(str(total or 0))
    if total > guardrail.per_operation_limit:
        currency = guardrail.budget_currency or "USD"
        raise CompetitorProblem(
            409, "guardrail_exceeded",
            f"per_operation_limit: this scan would reserve {total} {currency}, "
            f"above the limit of {guardrail.per_operation_limit} {currency}",
        )


def _admission(*, mode: str, credential_version_id: str | None, quote: dict, plan: CallPlan,
               reservations: dict, operation_id: str, job_id: str) -> dict:
    return {
        "execution_mode": mode,
        "credential_version_id": credential_version_id,
        "operation_id": operation_id,
        "job_id": job_id,
        "quote": quote,
        **plan.to_json(),
        "reservations": reservations,
    }


def _enqueue(session, ctx: TenantContext, *, scan_id: str, command_type: str, key_hash: str,
             action: str, sequence: int, reason: str):
    operation = create_operation(
        session,
        command_type=command_type,
        idempotency_key_hash=key_hash,
        initial_state=OperationState.ADMITTED,
        admission_contract_version=1,
        result_ref=OperationResultRef(resource_type=RESOURCE_TYPE, resource_id=scan_id),
        reason=reason,
    )
    job = enqueue_in_session(
        session,
        context=ctx,
        job_type=JOB_TYPE,
        payload={"scan_id": scan_id, "operation_id": operation.id, "action": action},
        idempotency_key=f"competitor_scan:{scan_id}:{action}:{sequence}",
        resource_type=RESOURCE_TYPE,
        resource_id=scan_id,
        # Each paid call sits behind its own dispatch fence: a redelivered run
        # replays what was recorded and finds a call its dead Worker left
        # ``dispatching``, which turns ``uncertain`` and is never resent. So the
        # lease recovery redelivers the Job instead of stranding the scan (#327).
        safe_to_retry_after_crash=True,
        max_attempts=SCAN_JOB_MAX_ATTEMPTS if action == "run" else 3,
    )
    job.operation_id = operation.id
    session.flush()
    return operation, job


def create_scan(ctx: TenantContext, raw: Mapping[str, Any], *, execution_mode: str | None,
                idempotency_key: str) -> dict:
    """Admit a scan: row + Operation + Job + both lines' reservations, one transaction."""
    _require_member(ctx)
    params = _params(ctx, raw)
    product_id = raw["product_id"]
    key_hash = _key_hash(idempotency_key)
    fingerprint = _sha({"product_id": product_id, "params": params.to_json(), "execution_mode": execution_mode})
    with tenant_session(ctx) as session:
        existing = session.scalars(
            select(CompetitorScanRecord).where(
                CompetitorScanRecord.workspace_id == ctx.workspace_id,
                CompetitorScanRecord.idempotency_key_hash == key_hash,
            )
        ).one_or_none()
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise CompetitorProblem(409, "idempotency_key_reused",
                                        "this Idempotency-Key was used for another scan")
            return {**scan_view(session, ctx, existing), "replayed": True}

        targets = _targets(session, ctx, product_id, params)
        modes = scan_modes(session, ctx)
        mode = _choose_mode(modes, execution_mode)
        plan = scan_plan.initial_plan(params)
        scan_id = new_id("cscan")
        quote = _quote(kind="scan", product_id=product_id, params=params, plan=plan, mode=mode, modes=modes)
        operation, job = _enqueue(session, ctx, scan_id=scan_id, command_type=COMMAND_TYPE, key_hash=key_hash,
                                  action="run", sequence=0, reason="competitor scan admitted")
        reservations = _reserve(session, ctx, operation_id=operation.id, job_id=job.id, mode=mode, plan=plan)
        credential_version_id = modes.market_data_version_id if mode == "byok" else None
        now = utcnow()
        record = CompetitorScanRecord(
            id=scan_id,
            workspace_id=ctx.workspace_id,
            product_id=product_id,
            market=params.country,
            platform="meta",
            status="queued",
            stage=None,
            params_json=params.to_json(),
            provider=SCRAPECREATORS if mode == "byok" else MOCK_PROVIDER,
            execution_mode=mode,
            initial_credential_version_id=credential_version_id,
            job_id=job.id,
            operation_id=operation.id,
            evidence_json={
                "admission": _admission(
                    mode=mode, credential_version_id=credential_version_id, quote=quote, plan=plan,
                    reservations=reservations, operation_id=operation.id, job_id=job.id,
                ),
                "targets": targets,
                "resumes": [],
                "reconciliation": [],
                "waits": [],
                # Frozen here, as the API: the Worker role cannot read every table it is built from.
                tags.STEP: _frozen_vocabulary(session, ctx, product_id, params.language),
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
            event_type="competitor_scan.requested",
            resource_type=RESOURCE_TYPE,
            resource_id=scan_id,
            details={
                "product_id": product_id, "job_id": job.id, "operation_id": operation.id,
                "execution_mode": mode, "country": params.country, "language": params.language,
                "competitors": len(params.competitor_ids), "keywords": len(params.keywords),
                "max_credits": quote["market_data"]["max_credits"], "max_usd": quote["max_usd"],
                "market_data_calls": len(plan.market_data_calls), "llm_calls": len(plan.llm),
            },
        )
        session.flush()
        return {**scan_view(session, ctx, record), "replayed": False}


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def pending_reconciliation(evidence: Mapping[str, Any]) -> list[dict]:
    return [entry for entry in evidence.get("reconciliation") or [] if entry.get("status") == "pending"]


def llm_backlog(session, ctx: TenantContext, record: CompetitorScanRecord) -> scan_plan.LlmBacklog:
    """What the judgment steps still owe, counted from the scan's rows (Issue #272)."""
    evidence = record.evidence_json or {}
    ads = session.scalars(
        select(CompetitorAdRecord).where(
            CompetitorAdRecord.workspace_id == ctx.workspace_id, CompetitorAdRecord.scan_id == record.id,
        )
    ).all()
    judged = judgments.latest_by_ad(session, ctx, record.id)
    tagged = judgments.latest_by_ad(session, ctx, record.id, kind=tags.KIND)
    batched = {
        ad_id for entry in ((evidence.get(judgments.STEP) or {}).get("assignments") or {}).values()
        for group in entry.get("groups") or [] for ad_id in group["ad_ids"]
    }
    in_table = {entry["ad_id"] for entry in ((evidence.get(tags.STEP) or {}).get("assignments") or {}).values()}
    relevant = [ad_id for ad_id, row in judged.items() if row.relevance == "relevant"]
    return scan_plan.LlmBacklog(
        unjudged_groups=len(judgments.groups_of([ad for ad in ads if ad.id not in judged and ad.id not in batched])),
        untagged_candidates=sum(1 for ad_id in relevant if ad_id not in tagged and ad_id not in in_table),
        relevant=len(relevant),
        undiscovered_advertisers=_undiscovered(session, ctx, record, ads),
    )


def _undiscovered(session, ctx: TenantContext, record: CompetitorScanRecord, ads: list[CompetitorAdRecord]) -> int:
    """The advertisers discovery would judge, while its step never froze a table (Issue #276)."""
    if not (record.params_json or {}).get("keywords") or ((record.evidence_json or {}).get(discovery.STEP) or {}).get(
            "frozen_by"):
        return 0
    origins: dict[str, list[str]] = {}
    for ad_id, origin in session.execute(
        select(CompetitorAdOriginRecord.competitor_ad_id, CompetitorAdOriginRecord.origin).where(
            CompetitorAdOriginRecord.workspace_id == ctx.workspace_id, CompetitorAdOriginRecord.scan_id == record.id,
        )
    ):
        origins.setdefault(ad_id, []).append(origin)
    excluded = discovery.excluded_pages(session, ctx, record.product_id)
    return len(discovery.advertisers(ads, origins, excluded=[*excluded["competitor_pages"],
                                                              *excluded["rejected_pages"]]))


def _frozen_vocabulary(session, ctx: TenantContext, product_id: str, language: str) -> dict:
    """The tag step's vocabulary, frozen at admission (as external creatives do, Issue #168)."""
    return {**tags.freeze_vocabulary(session, ctx, product_id, language=language),
            "vocabulary_frozen_at": utcnow().isoformat()}


def _with_vocabulary(session, ctx: TenantContext, record: CompetitorScanRecord) -> dict:
    """The scan's evidence with a frozen vocabulary (one now, for a scan admitted before #272)."""
    evidence = copy.deepcopy(record.evidence_json or {})
    state = evidence.setdefault(tags.STEP, {})
    if "vocabulary" not in state:
        state.update(_frozen_vocabulary(session, ctx, record.product_id,
                                        ScanParams.from_json(record.params_json).language))
    return evidence


def _resume_plan(session, ctx: TenantContext, record: CompetitorScanRecord) -> CallPlan:
    return scan_plan.resume_plan(ScanParams.from_json(record.params_json), _with_vocabulary(session, ctx, record),
                                 llm_backlog(session, ctx, record))


def _busy(evidence: Mapping[str, Any]) -> bool:
    """A recluster is queued or running."""
    return any(entry.get("status") in ("queued", "running") for entry in evidence.get("reclusters") or [])


def _resume_work(session, ctx: TenantContext, record: CompetitorScanRecord) -> CallPlan | None:
    """What a resume would do now: pages, unfinished judgments, pending reconciliation; ``None`` if nothing."""
    if record.status not in FINISHED or _busy(record.evidence_json or {}):
        return None
    plan = _resume_plan(session, ctx, record)
    if plan.targets or plan.llm or pending_reconciliation(record.evidence_json or {}):
        return plan
    return None


def _resumable(session, ctx: TenantContext, record: CompetitorScanRecord) -> CallPlan:
    """A ``partial``, ``failed`` or ``uncertain`` scan with anything left, or a
    ``completed`` one whose judgment steps did not finish (Issue #272)."""
    if record.status not in FINISHED:
        raise CompetitorProblem(409, "competitor_scan_not_resumable", "a queued or running scan cannot resume",
                                params={"status": record.status})
    if _busy(record.evidence_json or {}):
        raise CompetitorProblem(409, "competitor_scan_busy", "a recluster of this scan is still running")
    plan = _resume_work(session, ctx, record)
    if plan is not None:
        return plan
    if record.status == "completed":
        raise CompetitorProblem(409, "competitor_scan_not_resumable",
                                "every page is fetched and every judgment step finished",
                                params={"status": record.status})
    raise CompetitorProblem(409, "competitor_scan_nothing_to_resume",
                            "every page is fetched and every judgment step finished")


def quote_resume(ctx: TenantContext, scan_id: str) -> dict:
    _require_member(ctx)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id)
        plan = _resumable(session, ctx, record)
        modes = scan_modes(session, ctx)
        _choose_mode(modes, record.execution_mode)
        return _quote(kind="resume", product_id=record.product_id, params=ScanParams.from_json(record.params_json),
                      plan=plan, mode=record.execution_mode, modes=modes, scan_id=record.id)


def resume_scan(ctx: TenantContext, scan_id: str, *, idempotency_key: str) -> dict:
    """Same scan, a new Job reserving only what is left; it may use a newer key."""
    _require_member(ctx)
    key_hash = _key_hash(idempotency_key)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id, lock=True)
        evidence = dict(record.evidence_json or {})
        resumes = list(evidence.get("resumes") or [])
        replay = next((entry for entry in resumes if entry.get("idempotency_key_hash") == key_hash), None)
        if replay is not None:
            return {**scan_view(session, ctx, record), "replayed": True}
        plan = _resumable(session, ctx, record)
        evidence = _with_vocabulary(session, ctx, record)
        modes = scan_modes(session, ctx)
        mode = _choose_mode(modes, record.execution_mode)
        params = ScanParams.from_json(record.params_json)
        quote = _quote(kind="resume", product_id=record.product_id, params=params, plan=plan, mode=mode,
                       modes=modes, scan_id=record.id)
        operation, job = _enqueue(session, ctx, scan_id=record.id, command_type=RESUME_COMMAND_TYPE,
                                  key_hash=key_hash, action="run", sequence=len(resumes) + 1,
                                  reason="competitor scan resume admitted")
        reservations = _reserve(session, ctx, operation_id=operation.id, job_id=job.id, mode=mode, plan=plan)
        credential_version_id = modes.market_data_version_id if mode == "byok" else None
        now = utcnow()
        resumes.append({
            **_admission(mode=mode, credential_version_id=credential_version_id, quote=quote, plan=plan,
                         reservations=reservations, operation_id=operation.id, job_id=job.id),
            "idempotency_key_hash": key_hash,
            "requested_by": ctx.initiator_id,
            "requested_at": now.isoformat(),
        })
        evidence["resumes"] = resumes
        record.evidence_json = evidence
        record.status = "queued"
        record.job_id = job.id
        record.operation_id = operation.id
        record.failure_code = None
        record.failure_detail = None
        record.finished_at = None
        record.updated_at = now
        session.flush()
        audit.append_event(
            session,
            event_type="competitor_scan.resume_requested",
            resource_type=RESOURCE_TYPE,
            resource_id=record.id,
            details={
                "job_id": job.id, "operation_id": operation.id, "execution_mode": mode,
                "credential_version_id": credential_version_id, "resume": len(resumes),
                "max_credits": quote["market_data"]["max_credits"], "max_usd": quote["max_usd"],
                "market_data_calls": len(plan.market_data_calls), "llm_calls": len(plan.llm),
                "pending_reconciliation": len(pending_reconciliation(evidence)),
            },
        )
        session.flush()
        return {**scan_view(session, ctx, record), "replayed": False}


# ---------------------------------------------------------------------------
# Reconcile (Owner)
# ---------------------------------------------------------------------------


def terminal_status(evidence: Mapping[str, Any]) -> str:
    """``uncertain`` while a page awaits reconciliation; else by how the pages went."""
    if pending_reconciliation(evidence):
        return "uncertain"
    targets = evidence.get("targets") or []
    if not any(int(target.get("pages_fetched") or 0) for target in targets):
        return "failed"
    complete = all(
        target.get("exhausted") or int(target.get("pages_fetched") or 0) >= int(target.get("pages_planned") or 0)
        for target in targets
    )
    return "completed" if complete else "partial"


def reconcile_scan(ctx: TenantContext, scan_id: str, *, attempt_id: str, outcome: str, idempotency_key: str) -> dict:
    """Record the Owner's verdict on a pending page; a Worker Job settles its reservation."""
    key_hash = _key_hash(idempotency_key)
    fingerprint = _sha({"attempt_id": attempt_id, "outcome": outcome})
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id, lock=True)
        if ctx.role != "owner":
            raise CompetitorProblem(403, "competitor_scan_reconcile_forbidden",
                                    "only the Owner reconciles a scan's pages")
        evidence = dict(record.evidence_json or {})
        entries = [dict(entry) for entry in evidence.get("reconciliation") or []]
        replay = next((entry for entry in entries if entry.get("decision_key_hash") == key_hash), None)
        if replay is not None:
            if replay.get("decision_fingerprint") != fingerprint:
                raise CompetitorProblem(409, "idempotency_key_reused",
                                        "this Idempotency-Key was used for another reconciliation")
            return {**scan_view(session, ctx, record), "replayed": True}
        entry = next((item for item in entries if item.get("attempt_id") == attempt_id), None)
        if entry is None or entry.get("status") != "pending" or record.status not in FINISHED:
            raise CompetitorProblem(409, "competitor_scan_reconcile_not_applicable",
                                    "this page is not waiting for reconciliation", params={"attempt_id": attempt_id})
        operation, job = _enqueue(session, ctx, scan_id=record.id, command_type=RECONCILE_COMMAND_TYPE,
                                  key_hash=key_hash, action="reconcile",
                                  sequence=sum(1 for item in entries if item.get("source") == "owner") + 1,
                                  reason="competitor scan reconciliation admitted")
        now = utcnow()
        entry.update(
            status=outcome, source="owner", decided_by=ctx.initiator_id, decided_at=now.isoformat(),
            settled=False, decision_key_hash=key_hash, decision_fingerprint=fingerprint,
            operation_id=operation.id, job_id=job.id,
        )
        evidence["reconciliation"] = [entry if item.get("attempt_id") == attempt_id else item for item in entries]
        record.evidence_json = evidence
        record.status = terminal_status(evidence)
        record.updated_at = now
        session.flush()
        audit.append_event(
            session,
            event_type="competitor_scan.reconciled",
            resource_type=RESOURCE_TYPE,
            resource_id=record.id,
            details={"attempt_id": attempt_id, "outcome": outcome, "source": "owner",
                     "job_id": job.id, "status": record.status},
        )
        session.flush()
        return {**scan_view(session, ctx, record), "replayed": False}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _failure(record: CompetitorScanRecord) -> dict | None:
    if not record.failure_code:
        return None
    return {
        "code": record.failure_code,
        "retryable": record.failure_code in RETRYABLE_FAILURES,
        "params": dict((record.evidence_json or {}).get("failure_params") or {}),
    }


_TARGET_KEYS = (
    "origin", "kind", "competitor_id", "role", "page_id", "page_name", "confirmation_revision", "keyword",
    "status", "pages_planned", "pages_fetched", "ads_seen", "exhausted", "failure",
)


def _steps(evidence: Mapping[str, Any]) -> list[dict]:
    steps = []
    for step in ("fetch", "media", *scan_plan.JUDGMENT_STEPS):
        state = evidence.get(step) or {}
        steps.append({
            "step": step,
            "status": state.get("status") or "pending",
            "reason": state.get("reason"),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "attempts": int(state.get("attempts") or 0),
        })
    return steps


def _media_summary(record: CompetitorScanRecord, evidence: Mapping[str, Any], ads: list[CompetitorAdRecord]) -> dict:
    media = evidence.get("media") or {}
    items = media.get("items") or {}
    counts = {"planned": 0, "available": 0, "unavailable": 0, "pending": 0}
    for ad in ads:
        outcomes = {(item["card_index"], item["kind"]): item for item in items.get(ad.id) or []}
        for ref in ad.media_json or []:
            counts["planned"] += 1
            status = (outcomes.get((ref["card_index"], ref["kind"])) or {}).get("status") or "pending"
            counts[status if status in counts else "pending"] += 1
    return {
        **counts,
        "refreshes_used": len(media.get("refreshes") or []),
        "refresh_quota": int((record.params_json or {}).get("max_media_refreshes") or 0),
    }


def _reconciliation_view(entry: Mapping[str, Any]) -> dict:
    return {
        "attempt_id": entry["attempt_id"],
        "call_name": entry["call_name"],
        "origin": entry["origin"],
        "page": int(entry["page"]),
        "credential_version_id": entry.get("credential_version_id"),
        "status": entry["status"],
        "source": entry.get("source"),
        "balance_check": entry.get("balance_check"),
        "decided_by": entry.get("decided_by"),
        "decided_at": entry.get("decided_at"),
    }


def _resume_view(entry: Mapping[str, Any]) -> dict:
    quote = entry.get("quote") or {}
    return {
        "operation_id": entry["operation_id"],
        "job_id": entry["job_id"],
        "credential_version_id": entry.get("credential_version_id"),
        "max_credits": int((quote.get("market_data") or {}).get("max_credits") or 0),
        "max_usd": quote.get("max_usd") or "0",
        "requested_by": entry.get("requested_by"),
        "requested_at": entry.get("requested_at"),
    }


def _recluster_view(entry: Mapping[str, Any]) -> dict:
    quote = entry.get("quote") or {}
    return {
        "operation_id": entry["operation_id"],
        "job_id": entry["job_id"],
        "status": entry.get("status") or "queued",
        "max_usd": quote.get("max_usd") or "0",
        "simulated_usd": quote.get("simulated_usd"),
        "cluster_revision": entry.get("cluster_revision"),
        "snapshot_id": entry.get("snapshot_id"),
        "failure": entry.get("failure"),
        "requested_by": entry.get("requested_by"),
        "requested_at": entry.get("requested_at"),
        "finished_at": entry.get("finished_at"),
    }


def _target_views(session, ctx: TenantContext, record: CompetitorScanRecord, targets: list[dict]) -> list[dict]:
    """The frozen targets; a page's ``observed_page_name`` is projected from the product's scans (#332)."""
    names = observed_page_names(
        session, ctx, [(record.product_id, target.get("page_id")) for target in targets if target.get("page_id")],
    )
    return [
        {**{key: target.get(key) for key in _TARGET_KEYS},
         "observed_page_name": names.get((record.product_id, target.get("page_id")))}
        for target in targets
    ]


def scan_view(session, ctx: TenantContext, record: CompetitorScanRecord) -> dict:
    evidence = record.evidence_json or {}
    ads = session.scalars(
        select(CompetitorAdRecord).where(
            CompetitorAdRecord.workspace_id == ctx.workspace_id, CompetitorAdRecord.scan_id == record.id,
        )
    ).all()
    can_resume = _resume_work(session, ctx, record) is not None
    return {
        "scan_id": record.id,
        "product_id": record.product_id,
        "operation_id": record.operation_id,
        "job_id": record.job_id,
        "status": record.status,
        "stage": record.stage,
        "execution_mode": record.execution_mode,
        "provider": record.provider,
        "platform": record.platform,
        "country": record.market,
        "params": dict(record.params_json or {}),
        "targets": _target_views(session, ctx, record, evidence.get("targets") or []),
        "steps": _steps(evidence),
        "quote": dict((evidence.get("admission") or {}).get("quote") or {}),
        "cost": dict(record.cost_json) if record.cost_json else None,
        "initial_credential_version_id": record.initial_credential_version_id,
        "resumes": [_resume_view(entry) for entry in evidence.get("resumes") or []],
        "reconciliation": [_reconciliation_view(entry) for entry in evidence.get("reconciliation") or []],
        "media": _media_summary(record, evidence, ads),
        "observation_count": len(ads),
        "waits": len(evidence.get("waits") or []),
        "failure": _failure(record),
        "can_resume": can_resume,
        "reclusters": [_recluster_view(entry) for entry in evidence.get("reclusters") or []],
        "snapshot_id": record.snapshot_id,
        "created_by": record.created_by,
        "created_at": _iso(record.created_at),
        "started_at": _iso(record.started_at),
        "finished_at": _iso(record.finished_at),
    }


def get_scan(ctx: TenantContext, scan_id: str) -> dict:
    _require_member(ctx)
    with tenant_session(ctx) as session:
        return scan_view(session, ctx, _load(session, ctx, scan_id))


def list_scans(ctx: TenantContext, *, product_id: str | None, status: str | None) -> dict:
    _require_member(ctx)
    with tenant_session(ctx) as session:
        statement = select(CompetitorScanRecord).where(CompetitorScanRecord.workspace_id == ctx.workspace_id)
        if product_id is not None:
            statement = statement.where(CompetitorScanRecord.product_id == product_id)
        if status is not None:
            statement = statement.where(CompetitorScanRecord.status == status)
        records = session.scalars(
            statement.order_by(CompetitorScanRecord.created_at.desc(), CompetitorScanRecord.id.desc()).limit(LIST_LIMIT)
        ).all()
        counts = dict(session.execute(
            select(CompetitorAdRecord.scan_id, func.count()).where(
                CompetitorAdRecord.workspace_id == ctx.workspace_id,
                CompetitorAdRecord.scan_id.in_([record.id for record in records] or [""]),
            ).group_by(CompetitorAdRecord.scan_id)
        ).all())
        return {"items": [
            {
                "scan_id": record.id, "product_id": record.product_id, "operation_id": record.operation_id,
                "job_id": record.job_id, "status": record.status, "stage": record.stage,
                "execution_mode": record.execution_mode, "country": record.market,
                "competitor_ids": list((record.params_json or {}).get("competitor_ids") or []),
                "keywords": list((record.params_json or {}).get("keywords") or []),
                "observation_count": int(counts.get(record.id) or 0),
                "created_by": record.created_by, "created_at": _iso(record.created_at),
                "finished_at": _iso(record.finished_at),
            }
            for record in records
        ]}


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


def _origin_view(row: CompetitorAdOriginRecord) -> dict:
    kind, _, value = row.origin.partition(":")
    return {
        "origin": row.origin,
        "kind": kind,
        "competitor_id": value if kind == "page" else None,
        "keyword": value if kind == "keyword" else None,
        "attempt_id": row.attempt_id,
        "observed_at": _iso(row.observed_at),
    }


def _media_view(ref: Mapping[str, Any], outcome: Mapping[str, Any] | None) -> dict:
    outcome = outcome or {}
    return {
        "kind": ref["kind"],
        "card_index": ref["card_index"],
        "source_field": ref["source_field"],
        "source_url": ref.get("source_url"),
        "expires_at": ref.get("expires_at"),
        "status": outcome.get("status") or "pending",
        "reason": outcome.get("reason"),
        "sha256": outcome.get("sha256"),
        "byte_size": outcome.get("byte_size"),
        "mime_type": outcome.get("mime_type"),
        "width": outcome.get("width"),
        "height": outcome.get("height"),
        "fetched_at": outcome.get("fetched_at"),
        "refreshed": bool(outcome.get("refreshed")),
    }


def _ad_view(ad: CompetitorAdRecord, origins: list[CompetitorAdOriginRecord], role: str | None,
             outcomes: list[dict], judgment=None, tag_row=None) -> tuple[dict, dict | None]:
    by_media = {(item["card_index"], item["kind"]): item for item in outcomes}
    media = {(ref["card_index"], ref["kind"]): _media_view(ref, by_media.get((ref["card_index"], ref["kind"])))
             for ref in ad.media_json or []}
    cards = []
    for card in ad.cards_json or []:
        index = card["index"]
        cards.append({
            **{key: card.get(key) for key in ("index", "title", "body", "cta_text", "cta_type", "link_url",
                                              "link_description")},
            "image": media.get((index, "image")),
            "video_poster": media.get((index, "video_poster")),
        })
    statuses = {item["status"] for item in media.values()}
    media_status = (
        "none" if not media else "available" if "available" in statuses
        else "pending" if "pending" in statuses else "unavailable"
    )
    thumbnail = next(
        (by_media[key].get("thumbnail") for key in sorted(media)
         if (by_media.get(key) or {}).get("status") == "available" and by_media[key].get("thumbnail")),
        None,
    )
    origin_views = [_origin_view(row) for row in sorted(origins, key=lambda row: (row.observed_at, row.id))]
    competitor_ids = [view["competitor_id"] for view in origin_views if view["kind"] == "page"]
    return {
        "ad_id": ad.id,
        "scan_id": ad.scan_id,
        "provider": ad.provider,
        "ad_archive_id": ad.ad_archive_id,
        "collation_id": ad.collation_id,
        "collation_count": ad.collation_count,
        "advertiser_page_id": ad.advertiser_page_id,
        "advertiser_name": ad.advertiser_name,
        "page_profile_uri": ad.page_profile_uri,
        "page_categories": list(ad.page_categories_json or []),
        "is_active": ad.is_active,
        "start_date": _iso(ad.start_date),
        "end_date": _iso(ad.end_date),
        "days_running": ad.days_running,
        "display_format": ad.display_format,
        "publisher_platforms": list(ad.publisher_platforms_json or []),
        "cards": cards,
        "landing_domain": ad.landing_domain,
        "library_url": ad.library_url,
        "observed_at": _iso(ad.observed_at),
        "origins": origin_views,
        "competitor_ids": competitor_ids,
        "role": role,
        "media_status": media_status,
        "thumbnail": None,
        "field_sources": dict(ad.field_sources_json or {}),
        "relevance": judgments.summary(judgment),
        "tags": tags.summary(tag_row),
    }, thumbnail


def list_ads(
    ctx: TenantContext,
    scan_id: str,
    *,
    competitor_id: str | None,
    role: str | None,
    origin_kind: str | None,
    min_days_running: int | None,
    display_format: str | None,
    cursor: str | None,
    limit: int,
    relevance: str | None = None,
) -> tuple[dict, dict]:
    """One page of observations (longest running first) and the thumbnail refs to sign.

    ``relevance`` keeps the observations whose revision in effect says so;
    ``unassessed`` keeps the ones nothing judged yet."""
    _require_member(ctx)
    filters = {
        "scan_id": scan_id, "competitor_id": competitor_id, "role": role, "origin_kind": origin_kind,
        "min_days_running": min_days_running, "display_format": (display_format or "").upper() or None,
        "relevance": relevance,
    }
    filters_key = json.dumps(filters, sort_keys=True)
    start = 0
    if cursor:
        try:
            start = decode_offset_cursor(cursor, ctx.workspace_id, filters_key)
        except InvalidCursorError as exc:
            raise CompetitorProblem(400, "invalid_cursor", str(exc)) from exc
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id)
        items = (((record.evidence_json or {}).get("media") or {}).get("items")) or {}
        ads = session.scalars(
            select(CompetitorAdRecord).where(
                CompetitorAdRecord.workspace_id == ctx.workspace_id, CompetitorAdRecord.scan_id == scan_id,
            )
        ).all()
        origins: dict[str, list[CompetitorAdOriginRecord]] = {}
        for row in session.scalars(
            select(CompetitorAdOriginRecord).where(
                CompetitorAdOriginRecord.workspace_id == ctx.workspace_id,
                CompetitorAdOriginRecord.scan_id == scan_id,
            )
        ):
            origins.setdefault(row.competitor_ad_id, []).append(row)
        latest = judgments.latest_by_ad(session, ctx, scan_id)
        tagged = judgments.latest_by_ad(session, ctx, scan_id, kind=tags.KIND)
        # The snapshot's projection, so the board's self group is the summary's (#328).
        roles = snapshots.roles_of(
            record, {ad_id: [row.origin for row in rows] for ad_id, rows in origins.items()},
            {ad.id: ad.advertiser_page_id for ad in ads},
        )
        views = []
        for ad in ads:
            view, thumbnail = _ad_view(ad, origins.get(ad.id, []), roles.get(ad.id), items.get(ad.id) or [],
                                       latest.get(ad.id), tagged.get(ad.id))
            if competitor_id is not None and competitor_id not in view["competitor_ids"]:
                continue
            if role is not None and view["role"] != role:
                continue
            if origin_kind is not None and not any(item["kind"] == origin_kind for item in view["origins"]):
                continue
            if min_days_running is not None and (view["days_running"] or 0) < min_days_running:
                continue
            if filters["display_format"] is not None and view["display_format"] != filters["display_format"]:
                continue
            if relevance is not None and (view["relevance"] or {}).get("relevance", "unassessed") != relevance:
                continue
            views.append((view, thumbnail))
    views.sort(key=lambda pair: (-(pair[0]["days_running"] or -1), pair[0]["ad_archive_id"]))
    page = views[start:start + limit]
    next_cursor = (
        encode_cursor(ctx.workspace_id, filters_key, (start + limit,)) if start + limit < len(views) else None
    )
    refs = {view["ad_id"]: thumbnail for view, thumbnail in page if thumbnail}
    return {"items": [view for view, _ in page], "next_cursor": next_cursor, "total": len(views)}, refs


async def sign_thumbnails(blob_store, items: list[dict], refs: Mapping[str, Mapping[str, Any]]) -> None:
    """A short-lived signed GET of each page item's 512px derivative; ``None`` on any failure."""
    for item in items:
        ref = refs.get(item["ad_id"])
        if ref:
            item["thumbnail"] = await scan_media.signed_derivative(blob_store, ref)


# ---------------------------------------------------------------------------
# Relevance: flips and history (Issue #271, spec §7.1)
# ---------------------------------------------------------------------------


def _load_ad(session, ctx: TenantContext, scan_id: str, ad_id: str) -> CompetitorAdRecord:
    ad = session.scalars(
        select(CompetitorAdRecord).where(
            CompetitorAdRecord.workspace_id == ctx.workspace_id,
            CompetitorAdRecord.scan_id == scan_id,
            CompetitorAdRecord.id == ad_id,
        )
    ).one_or_none()
    if ad is None:
        raise CompetitorProblem(404, "competitor_ad_not_found", "no such observation in this scan")
    return ad


def flip_relevance(ctx: TenantContext, scan_id: str, ad_id: str, *, relevance: str, idempotency_key: str) -> dict:
    """A member's verdict on one observation: a new ``user`` revision, never an edit.

    Flipping back is one more revision, so the history keeps every step; the
    same ``Idempotency-Key`` replays the revision it made. The scan row lock
    orders a flip against the Worker writing the scan's judgments.
    """
    _require_member(ctx)
    key_hash = _key_hash(idempotency_key)
    fingerprint = _sha({"relevance": relevance})
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id, lock=True)
        ad = _load_ad(session, ctx, record.id, ad_id)
        replay = judgments.by_key(session, ctx, ad.id, key_hash)
        if replay is not None:
            if replay.source != "user" or replay.request_fingerprint != fingerprint:
                raise CompetitorProblem(409, "idempotency_key_reused",
                                        "this Idempotency-Key was used for another flip")
            return {**judgments.view(replay), "replayed": True}
        revisions = judgments.history(session, ctx, ad.id)
        current = revisions[-1] if revisions else None
        previous = None if current is None else {
            "revision": current.revision, "relevance": current.relevance, "source": current.source,
        }
        row = judgments.append(
            session, ctx, scan_id=record.id, ad_id=ad.id, source="user", relevance=relevance,
            payload={"relevance": relevance, "confidence": None, "reason": None, "language": None,
                     "previous": previous},
            evidence={}, key_hash=key_hash, fingerprint=fingerprint, created_by=ctx.initiator_id,
        )
        audit.append_event(
            session,
            event_type="competitor_ad.relevance_flipped",
            resource_type="competitor_ad",
            resource_id=ad.id,
            details={"scan_id": record.id, "revision": row.revision, "relevance": relevance,
                     "previous": previous},
        )
        session.flush()
        return {**judgments.view(row), "replayed": False}


def list_assessments(ctx: TenantContext, scan_id: str, ad_id: str) -> dict:
    """Every relevance revision of one observation, oldest first; the last is in effect."""
    _require_member(ctx)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id)
        ad = _load_ad(session, ctx, record.id, ad_id)
        items = [judgments.view(row) for row in judgments.history(session, ctx, ad.id)]
        return {"scan_id": record.id, "ad_id": ad.id, "kind": judgments.KIND, "items": items,
                "current": items[-1] if items else None}


# ---------------------------------------------------------------------------
# Tags: confirmations and history (Issue #272, spec §7.1)
# ---------------------------------------------------------------------------


def _vocabulary(record: CompetitorScanRecord) -> dict | None:
    """The scan's frozen tag vocabulary (``None`` before the tag step froze one)."""
    return ((record.evidence_json or {}).get(tags.STEP) or {}).get("vocabulary")


def confirm_tags(ctx: TenantContext, scan_id: str, ad_id: str, *, values: list[dict], idempotency_key: str) -> dict:
    """A member confirms or changes one observation's tags: a new ``user``
    revision against the scan's frozen vocabulary, never an edit. Factors
    left out are "none"; the same ``Idempotency-Key`` replays the revision."""
    _require_member(ctx)
    key_hash = _key_hash(idempotency_key)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id, lock=True)
        ad = _load_ad(session, ctx, record.id, ad_id)
        vocabulary = _vocabulary(record)
        replay = judgments.by_key(session, ctx, ad.id, key_hash, kind=tags.KIND)
        if replay is not None:
            try:
                fingerprint = tags.fingerprint(tags.resolve(vocabulary or {}, values))
            except tags.TagsInvalid:
                fingerprint = None
            if replay.source != "user" or replay.request_fingerprint != fingerprint:
                raise CompetitorProblem(409, "idempotency_key_reused",
                                        "this Idempotency-Key was used for other tags")
            return {**tags.view(replay, vocabulary), "replayed": True}
        if not vocabulary or not vocabulary.get("factors"):
            raise CompetitorProblem(409, "competitor_tags_unavailable",
                                    "this scan has no frozen dimension vocabulary to tag against")
        try:
            factors = tags.resolve(vocabulary, values)
        except tags.TagsInvalid as exc:
            raise CompetitorProblem(422, exc.code, str(exc), params=exc.params) from exc
        revisions = judgments.history(session, ctx, ad.id, kind=tags.KIND)
        current = revisions[-1] if revisions else None
        previous = None if current is None else {"revision": current.revision, "source": current.source}
        state = (record.evidence_json or {}).get(tags.STEP) or {}
        row = judgments.append(
            session, ctx, scan_id=record.id, ad_id=ad.id, kind=tags.KIND, source="user", relevance=None,
            payload={"factors": factors, "summary": None, "language": None,
                     "vocabulary_hash": state.get("vocabulary_hash"), "previous": previous},
            evidence={}, key_hash=key_hash, fingerprint=tags.fingerprint(factors), created_by=ctx.initiator_id,
        )
        audit.append_event(
            session,
            event_type="competitor_ad.tags_confirmed",
            resource_type="competitor_ad",
            resource_id=ad.id,
            details={"scan_id": record.id, "revision": row.revision, "previous": previous,
                     "set": sum(1 for item in factors if item["value_id"]),
                     "accepted_ai": None if current is None or current.source != "ai" else sum(
                         1 for mine, theirs in zip(factors, (current.payload_json or {}).get("factors") or [])
                         if mine["value_id"] == theirs.get("value_id"))},
        )
        session.flush()
        return {**tags.view(row, vocabulary), "replayed": False}


def list_tags(ctx: TenantContext, scan_id: str, ad_id: str) -> dict:
    """Every tags revision of one observation, oldest first, and the frozen vocabulary."""
    _require_member(ctx)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id)
        ad = _load_ad(session, ctx, record.id, ad_id)
        vocabulary = _vocabulary(record)
        items = [tags.view(row, vocabulary) for row in judgments.history(session, ctx, ad.id, kind=tags.KIND)]
        return {"scan_id": record.id, "ad_id": ad.id, "kind": tags.KIND,
                "vocabulary": tags.public_vocabulary(vocabulary), "items": items,
                "current": items[-1] if items else None}


# ---------------------------------------------------------------------------
# Snapshots: refreeze, reads, summary (Issue #272, spec §7.1)
# ---------------------------------------------------------------------------


def _relevant_count(session, ctx: TenantContext, scan_id: str) -> int:
    return sum(1 for row in judgments.latest_by_ad(session, ctx, scan_id).values() if row.relevance == "relevant")


def _require_idle(record: CompetitorScanRecord) -> None:
    if record.status not in FINISHED or _busy(record.evidence_json or {}):
        raise CompetitorProblem(409, "competitor_scan_busy", "the scan is running; try again when it has finished",
                                params={"status": record.status})


def refreeze_scan(ctx: TenantContext, scan_id: str, *, idempotency_key: str) -> dict:
    """Free and pure: freeze the revisions in effect now. An unchanged hash
    returns the existing snapshot (no new row, nothing made stale); the same
    ``Idempotency-Key`` replays the snapshot it answered."""
    _require_member(ctx)
    key_hash = _key_hash(idempotency_key)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id, lock=True)
        evidence = copy.deepcopy(record.evidence_json or {})
        freezes = list(evidence.get("freezes") or [])
        replay = next((entry for entry in freezes if entry.get("idempotency_key_hash") == key_hash), None)
        if replay is not None:
            row = snapshots.get(session, ctx, record.id, replay["snapshot_id"])
            return {**snapshots.snapshot_view(row, record.snapshot_id), "created": False, "replayed": True}
        _require_idle(record)
        row, created, refusal = snapshots.freeze(session, ctx, record, created_by=ctx.initiator_id)
        if row is None:
            raise CompetitorProblem(409, "competitor_scan_nothing_to_freeze",
                                    "the scan has no judged observation to freeze", params={"reason": refusal})
        freezes.append({"idempotency_key_hash": key_hash, "snapshot_id": row.id, "created": created,
                        "requested_by": ctx.initiator_id, "requested_at": utcnow().isoformat()})
        evidence["freezes"] = freezes
        record.evidence_json = evidence
        record.updated_at = utcnow()
        session.flush()
        return {**snapshots.snapshot_view(row, record.snapshot_id), "created": created, "replayed": False}


def list_snapshots(ctx: TenantContext, scan_id: str) -> dict:
    _require_member(ctx)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id)
        return {"scan_id": record.id, "current_snapshot_id": record.snapshot_id,
                "items": [snapshots.meta_view(row, record.snapshot_id)
                          for row in snapshots.history(session, ctx, record.id)]}


def _load_snapshot(session, ctx: TenantContext, record: CompetitorScanRecord, snapshot_id: str | None):
    row = snapshots.get(session, ctx, record.id, snapshot_id) if snapshot_id else None
    if row is None:
        raise CompetitorProblem(404, "competitor_snapshot_not_found", "no such snapshot of this scan")
    return row


def get_snapshot(ctx: TenantContext, scan_id: str, snapshot_id: str) -> dict:
    _require_member(ctx)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id)
        return snapshots.snapshot_view(_load_snapshot(session, ctx, record, snapshot_id), record.snapshot_id)


def get_summary(ctx: TenantContext, scan_id: str, *, snapshot_id: str | None = None) -> dict:
    """The summary of a snapshot (the scan's current one unless named), and
    whether a refreeze would change it (``pending_changes``)."""
    _require_member(ctx)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id)
        row = _load_snapshot(session, ctx, record, snapshot_id or record.snapshot_id)
        idle = record.status in FINISHED and not _busy(record.evidence_json or {})
        return snapshots.summary_view(
            row, current_id=record.snapshot_id, pending=snapshots.pending_hash(session, ctx, record),
            can_recluster=idle and _relevant_count(session, ctx, record.id) > 0,
        )


# ---------------------------------------------------------------------------
# Recluster: quote and admission (Issue #272, spec §6, §7.1)
# ---------------------------------------------------------------------------


def _recluster_quote(session, ctx: TenantContext, record: CompetitorScanRecord) -> dict:
    """``cluster:1`` at ``quote_llm_chat``'s maximum, in the scan's own mode (never mixed)."""
    _require_idle(record)
    relevant = _relevant_count(session, ctx, record.id)
    if not relevant:
        raise CompetitorProblem(409, "competitor_scan_nothing_to_cluster", "no observation of this scan is relevant")
    mode = record.execution_mode
    if mode == "byok" and not resolve_generation_credential(session, ctx, OPENAI_PROVIDER).get("active"):
        raise CompetitorProblem(409, "competitor_scan_ai_credential_missing",
                                "a real scan reclusters with the Workspace's AI key, which is not bound")
    llm = scan_plan.llm_quote(scan_plan.recluster_plan().llm_counts(), mode=mode)
    llm.pop("total")
    return {
        "kind": "recluster", "scan_id": record.id, "product_id": record.product_id, "execution_mode": mode,
        "relevant_count": relevant, "llm": llm, "max_usd": llm["max_usd"] if mode == "byok" else "0",
        "simulated_usd": llm["simulated_usd"], "currency": "USD",
    }


def quote_recluster(ctx: TenantContext, scan_id: str) -> dict:
    _require_member(ctx)
    with tenant_session(ctx) as session:
        return _recluster_quote(session, ctx, _load(session, ctx, scan_id))


def recluster_scan(ctx: TenantContext, scan_id: str, *, idempotency_key: str) -> dict:
    """Paid and separate (spec §6): an Operation, a ``competitor_scan.run``
    Job (``action=recluster``) and its ``cluster:1`` reservation in one
    transaction; the Job appends a new clusters revision and freezes once.
    The same ``Idempotency-Key`` replays the same recluster."""
    _require_member(ctx)
    key_hash = _key_hash(idempotency_key)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, scan_id, lock=True)
        evidence = copy.deepcopy(record.evidence_json or {})
        reclusters = list(evidence.get("reclusters") or [])
        if any(entry.get("idempotency_key_hash") == key_hash for entry in reclusters):
            return {**scan_view(session, ctx, record), "replayed": True}
        quote = _recluster_quote(session, ctx, record)
        mode = record.execution_mode
        operation, job = _enqueue(session, ctx, scan_id=record.id, command_type=RECLUSTER_COMMAND_TYPE,
                                  key_hash=key_hash, action="recluster", sequence=len(reclusters) + 1,
                                  reason="competitor scan recluster admitted")
        plan = scan_plan.recluster_plan()
        reservations = _reserve(session, ctx, operation_id=operation.id, job_id=job.id, mode=mode, plan=plan)
        now = utcnow()
        reclusters.append({
            **_admission(mode=mode, credential_version_id=None, quote=quote, plan=plan, reservations=reservations,
                         operation_id=operation.id, job_id=job.id),
            "idempotency_key_hash": key_hash, "requested_by": ctx.initiator_id, "requested_at": now.isoformat(),
            "status": "queued", "groups": None,
        })
        evidence["reclusters"] = reclusters
        record.evidence_json = evidence
        record.updated_at = now
        session.flush()
        audit.append_event(
            session,
            event_type="competitor_scan.recluster_requested",
            resource_type=RESOURCE_TYPE,
            resource_id=record.id,
            details={"job_id": job.id, "operation_id": operation.id, "execution_mode": mode,
                     "relevant": quote["relevant_count"], "max_usd": quote["max_usd"],
                     "simulated_usd": quote["simulated_usd"]},
        )
        session.flush()
        return {**scan_view(session, ctx, record), "replayed": False}


# ---------------------------------------------------------------------------
# The product's drafting switch (Issue #272, spec §7.2)
# ---------------------------------------------------------------------------


def _product_for(session, ctx: TenantContext, product_id: str, *, lock: bool = False) -> ProductRecord:
    statement = select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
                                            ProductRecord.id == product_id)
    if lock:
        statement = statement.with_for_update()
    product = session.scalars(statement).one_or_none()
    if product is None:
        raise CompetitorProblem(404, "product_not_found", "no such product in this Workspace")
    return product


def _settings_view(product: ProductRecord) -> dict:
    return {"product_id": product.id, "competitor_snapshot_in_drafts": bool(product.competitor_snapshot_in_drafts),
            "updated_at": _iso(product.updated_at)}


def get_product_settings(ctx: TenantContext, product_id: str) -> dict:
    _require_member(ctx)
    with tenant_session(ctx) as session:
        return _settings_view(_product_for(session, ctx, product_id))


def update_product_settings(ctx: TenantContext, product_id: str, *, competitor_snapshot_in_drafts: bool,
                            idempotency_key: str) -> dict:
    """Any member switches whether drafting cites the latest competitor
    snapshot; audited with the actor. The same ``Idempotency-Key`` replays
    (the audit trail holds the key's hash), and is refused for another value."""
    from app.adapters.persistence.models import AuditEventRecord

    _require_member(ctx)
    key_hash = _key_hash(idempotency_key)
    value = bool(competitor_snapshot_in_drafts)
    with tenant_session(ctx) as session:
        product = _product_for(session, ctx, product_id, lock=True)
        for event in session.scalars(
            select(AuditEventRecord).where(
                AuditEventRecord.workspace_id == ctx.workspace_id,
                AuditEventRecord.event_type == SETTINGS_EVENT,
                AuditEventRecord.resource_type == "product",
                AuditEventRecord.resource_id == product.id,
            )
        ):
            details = event.details_json or {}
            if details.get("idempotency_key_hash") == key_hash:
                if details.get("competitor_snapshot_in_drafts") != value:
                    raise CompetitorProblem(409, "idempotency_key_reused",
                                            "this Idempotency-Key was used for another setting")
                return {**_settings_view(product), "replayed": True}
        previous = bool(product.competitor_snapshot_in_drafts)
        product.competitor_snapshot_in_drafts = value
        product.updated_at = utcnow()
        session.flush()
        audit.append_event(
            session,
            event_type=SETTINGS_EVENT,
            resource_type="product",
            resource_id=product.id,
            details={"competitor_snapshot_in_drafts": value, "previous": previous,
                     "idempotency_key_hash": key_hash},
        )
        session.flush()
        return {**_settings_view(product), "replayed": False}
