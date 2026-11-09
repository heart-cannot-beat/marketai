"""The ``competitor.resolve`` Job (spec §4.2, §4.4): search, probe, rank, maybe confirm.

Steps, each call one ProviderCallAttempt behind the dispatch fence
(:mod:`app.market_data.calls`):

1. **turn** -- byok waits for its key's turn (§5.1) and records each wait;
2. **search** -- ``page_search:1``: the page candidates, stored with their
   signed avatar links in the same transaction as the attempt's success;
   **avatars** -- each link downloaded once, free (Meta's CDN over HTTPS
   only, never the provider's API) and archived by SHA-256 like scan media
   (Issue #332); a failed download leaves the card its placeholder, and
   the browser only ever gets a signed link to the archived copy;
3. **probe plan** -- the top three eligible candidates by §4.2, frozen into
   the evidence before the first probe so a retried Job probes the same
   pages; reservations of probes the plan does not need are released;
4. **probes** -- ``probe:i``: one page of each planned page's active ads,
   reduced to the provider's ad count and the landing domains;
5. **finish** -- the final ranking is frozen, the cost summed from the
   attempts' usage, and the competitor confirmed when exactly one candidate
   qualifies (§4.1) and nothing is confirmed yet.

A 402 or 401/403 stops the resolve: no further calls, every unused
reservation released, ``failed`` with the candidates found so far kept. An
uncertain call stops it too (``uncertain``; nothing is resent, its own
reservation stays open for reconciliation). A retried Job replays every
call it already made from the attempts and the stored evidence; so does one
the lease recovery redelivers after its Worker died (Issue #327), where a
call left ``dispatching`` comes back ``uncertain`` and is never resent.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping

from sqlalchemy import select

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import utcnow
from app.competitors import scan_media, scoring, service
from app.competitors.models import CompetitorRecord, CompetitorResolutionRecord
from app.competitors.normalize import MalformedAdError, normalize_ad, normalize_page_candidates
from app.competitors.sources.base import AdLibrarySource, PriceOperation, SourceRequest, SourceResponse
from app.competitors.sources.scrapecreators import listing_items
from app.entitlements.cost_models import ProviderCallAttemptRecord
from app.entitlements.credential_protector import CredentialProtector
from app.generation.credential_binding import open_bound_secret
from app.market_data import serial
from app.market_data.calls import (
    AdLibraryCallAdapter,
    call_usage,
    execute_market_data_call,
    interface_failure,
    raw_digest,
    release_market_data_call,
)
from app.market_data.credentials import SCRAPECREATORS, resolve_market_data_credential
from app.market_data.pricing import MarketDataPricingError, price_table
from app.platform import audit, jobs
from app.platform.tenancy import TenantContext
from app.providers.dispatch import DispatchCoordinatorError, ExternalCallCoordinator
from app.platform.runtime_role import WORKER_ROLE, runs_as

logger = logging.getLogger(__name__)

#: 402 and 401/403: stop the resolve and release what is left (§6).
STOPPING_CODES = frozenset({"insufficient_credits", "credential_rejected"})
#: Seconds a Job waits before asking for its key's turn again.
TURN_RETRY_SECONDS = 10.0
#: Avatar downloads in flight at once (each one is free but may take a while).
AVATAR_DOWNLOADS = 4

SourceFactory = Callable[[str], AdLibrarySource]
FetcherFactory = Callable[[str], scan_media.MediaFetcher]


@dataclass(frozen=True)
class ResolveOutcome:
    """``completed`` | ``failed`` | ``uncertain`` | ``wait`` (re-queue for the key's turn)."""

    kind: str
    code: str = ""
    detail: str = ""


def default_source(execution_mode: str) -> AdLibrarySource:
    """ScrapeCreators for byok; a fresh Controlled Mock (simulated credits) otherwise."""
    if execution_mode == "byok":
        from app.competitors.sources.scrapecreators import ScrapeCreatorsAdLibrarySource

        return ScrapeCreatorsAdLibrarySource(base_url=config.SCRAPECREATORS_BASE_URL)
    from app.competitors.sources.mock import MockAdLibrarySource

    return MockAdLibrarySource()


def _load(session, ctx: TenantContext, resolution_id: str) -> CompetitorResolutionRecord:
    return session.scalars(
        select(CompetitorResolutionRecord).where(
            CompetitorResolutionRecord.workspace_id == ctx.workspace_id,
            CompetitorResolutionRecord.id == resolution_id,
        )
    ).one()


def _update_evidence(record: CompetitorResolutionRecord, **changes: Any) -> None:
    evidence = dict(record.evidence_json or {})
    evidence.update(changes)
    record.evidence_json = evidence
    record.updated_at = utcnow()


def _search_candidates(response: SourceResponse) -> list[dict]:
    """The stored card of each candidate; ``avatar_url`` is the provider's signed link."""
    listing = listing_items(response.raw)
    items = listing[1] if listing else []
    cards = []
    for candidate in normalize_page_candidates(response):
        index = int(candidate.raw_path.rsplit("[", 1)[1].rstrip("]"))
        raw = items[index] if index < len(items) and isinstance(items[index], Mapping) else {}
        avatar = raw.get("image_uri")
        cards.append({
            "page_id": candidate.page_id,
            "page_name": candidate.name,
            "page_alias": candidate.page_alias,
            "category": candidate.category,
            "likes": candidate.likes,
            "ig_username": candidate.ig_username,
            "ig_followers": candidate.ig_followers,
            "verification": candidate.verification,
            "page_is_deleted": candidate.page_is_deleted,
            "country": candidate.country,
            "avatar_url": avatar if isinstance(avatar, str) and avatar.startswith("https://") else None,
            "provider_rank": candidate.rank,
            "raw_path": candidate.raw_path,
        })
    return cards


def _probe_summary(response: SourceResponse, country: str) -> dict:
    """The provider's ad count and the registrable landing domains of the sampled ads."""
    listing = listing_items(response.raw)
    domains: set[str] = set()
    sampled = 0
    for index, item in enumerate(listing[1] if listing else []):
        try:
            ad = normalize_ad(item, provider=response.provider, origin="probe", observed_at=utcnow(),
                              raw_path=f"{listing[0]}[{index}]")
        except MalformedAdError:
            continue
        sampled += 1
        if ad.landing_domain:
            domains.add(ad.landing_domain)
    total = response.raw.get("searchResultsCount")
    return {
        "status": "succeeded",
        "country": country,
        "total_active_ads": total if isinstance(total, int) and not isinstance(total, bool) else None,
        "sampled_ads": sampled,
        "landing_domains": sorted(domains),
        "error_code": None,
    }


class ResolveEvidenceRecorder:
    """Writes a call's result into the resolve, in the attempt's own success transaction."""

    def __init__(self, resolution_id: str, call_name: str, request: SourceRequest, *,
                 page_id: str | None = None, country: str = "") -> None:
        self._resolution_id = resolution_id
        self._call_name = call_name
        self._request = request
        self._page_id = page_id
        self._country = country

    def link_prepared_attempt(self, session, ctx: TenantContext, *, attempt: ProviderCallAttemptRecord) -> None:
        digest = raw_digest(self._request.evidence())
        attempt.request_fingerprint_hash = digest
        attempt.redacted_request_hash = digest

    def materialize(self, ctx: TenantContext, *, attempt_id: str, output: SourceResponse) -> str:
        return f"sha256:{raw_digest(output.raw)}"

    def persist_result(self, session, ctx: TenantContext, *, attempt: ProviderCallAttemptRecord,
                       reference: str, output: SourceResponse) -> None:
        record = _load(session, ctx, self._resolution_id)
        call = {
            "call_name": self._call_name, "attempt_id": attempt.id, "status": "succeeded",
            "credits_charged": output.credits_charged, "credits_remaining": output.credits_remaining,
            "cached": output.cached, "response_ref": reference,
        }
        if self._page_id is None:
            candidates = _search_candidates(output)
            _update_evidence(record, search={**call, "candidate_count": len(candidates)}, candidates=candidates)
        else:
            probes = dict((record.evidence_json or {}).get("probes") or {})
            probes[self._page_id] = {**call, **_probe_summary(output, self._country)}
            _update_evidence(record, probes=probes)


# ---------------------------------------------------------------------------
# Finishing (shared by the run and the unexpected-fault path)
# ---------------------------------------------------------------------------


def _outcome_of(record: CompetitorResolutionRecord) -> ResolveOutcome:
    if record.status == "completed":
        return ResolveOutcome("completed")
    return ResolveOutcome(record.status, record.failure_code or "competitor_resolve_failed", record.failure_detail or "")


def _cost(session, ctx: TenantContext, job_id: str, execution_mode: str) -> dict:
    """Credits and money the Job's calls reported (simulated under Mock)."""
    attempts = session.scalars(
        select(ProviderCallAttemptRecord.id).where(
            ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            ProviderCallAttemptRecord.job_id == job_id,
        )
    ).all()
    credits, usd, simulated, priced = 0, Decimal("0"), Decimal("0"), False
    for attempt_id in attempts:
        usage = call_usage(session, ctx, attempt_id)
        credits += int((usage.get("usage") or {}).get("credits_charged") or 0)
        cost = usage.get("cost") or {}
        usd += Decimal(cost.get("provider_amount") or "0")
        if cost.get("simulated_amount") is not None:
            simulated += Decimal(cost["simulated_amount"])
            priced = True
    return {
        "calls": len(attempts),
        "credits_charged": credits,
        "usd": format(usd, "f"),
        "simulated_usd": format(simulated, "f") if execution_mode == "mock" and priced else None,
    }


def _auto_confirm(session, ctx: TenantContext, record: CompetitorResolutionRecord, competitor: CompetitorRecord,
                  ranking: list[dict]) -> None:
    """Confirm the one qualifying candidate, unless someone already confirmed a page."""
    by_alias = competitor.name is None and competitor.input_page_alias is not None
    match = scoring.automatic_match(ranking, by_alias=by_alias)
    if match is None or competitor.status != "active":
        return
    service.lock_competitor(session, ctx, competitor.id)
    if service.latest_confirmation(session, ctx, competitor.id) is not None:
        return
    confirmation = service.append_confirmation(
        session, ctx, competitor,
        page_id=match["page_id"],
        page_alias=match.get("page_alias"),
        page_name=match.get("page_name"),
        source="auto",
        resolve_job_id=record.job_id,
        evidence={
            **service.confirmation_evidence(record, dict(match)),
            "rule": "alias" if by_alias else "exact_name_and_domain",
        },
        idempotency_key_hash=hashlib.sha256(f"auto:{record.job_id}".encode()).hexdigest(),
        request_fingerprint=hashlib.sha256(f"auto:{record.job_id}:{match['page_id']}".encode()).hexdigest(),
        confirmed_by=record.created_by,
    )
    _update_evidence(record, auto_confirmation={"page_id": match["page_id"], "revision": confirmation.revision})


def finish_resolution(session, ctx: TenantContext, record: CompetitorResolutionRecord, *, status: str,
                      code: str = "", detail: str = "", params: Mapping[str, Any] | None = None) -> ResolveOutcome:
    """Release every unused reservation, freeze the ranking and cost, maybe confirm."""
    released = list((record.evidence_json or {}).get("released") or [])
    for call in service.RESOLVE_CALLS:
        if release_market_data_call(session, ctx, job_id=record.job_id, call_name=call.call_name):
            released.append(call.call_name)
    competitor = session.get(CompetitorRecord, record.competitor_id)
    evidence = record.evidence_json or {}
    scored = scoring.score(
        evidence.get("candidates") or [], query=record.query,
        alias=competitor.input_page_alias if competitor is not None else None,
    )
    ranking = scoring.rank(scored, probes=evidence.get("probes") or {}, website_domain=record.website_domain)
    now = utcnow()
    record.status = status
    record.failure_code = None if status == "completed" else (code or "competitor_resolve_failed")
    record.failure_detail = None if status == "completed" else (detail[:2000] or None)
    record.finished_at = now
    record.expires_at = now + service.REUSE_WINDOW
    record.cost_json = _cost(session, ctx, record.job_id, record.execution_mode)
    _update_evidence(record, ranking=ranking, released=released, failure_params=dict(params or {}))
    session.flush()
    if status == "completed" and competitor is not None:
        _auto_confirm(session, ctx, record, competitor, ranking)
    audit.append_event(
        session,
        event_type="competitor.resolve_finished",
        resource_type="competitor",
        resource_id=record.competitor_id,
        details={
            "resolution_id": record.id, "job_id": record.job_id, "status": status,
            "failure_code": record.failure_code, "candidate_count": len(ranking),
            "credits_charged": record.cost_json.get("credits_charged"),
            "auto_confirmed": bool((record.evidence_json or {}).get("auto_confirmation")),
        },
    )
    session.flush()
    return _outcome_of(record)


# ---------------------------------------------------------------------------
# One run of the Job
# ---------------------------------------------------------------------------


class ResolveRun:
    """One execution of a resolve Job (fresh or retried)."""

    def __init__(
        self,
        ctx: TenantContext,
        *,
        resolution_id: str,
        job_id: str,
        operation_id: str,
        coordinator: ExternalCallCoordinator,
        protector: CredentialProtector,
        source_factory: SourceFactory = default_source,
        blob_store=None,
        fetcher_factory: FetcherFactory = scan_media.default_fetcher,
        final_attempt: bool = False,
    ) -> None:
        self.ctx = ctx
        self.resolution_id = resolution_id
        self.job_id = job_id
        self.operation_id = operation_id
        self.coordinator = coordinator
        self.protector = protector
        self.source_factory = source_factory
        #: Where avatars are archived; without one the cards keep their placeholders.
        self.blob_store = blob_store
        self.fetcher_factory = fetcher_factory
        self.final_attempt = final_attempt

    async def run(self) -> ResolveOutcome:
        started = self._start()
        if isinstance(started, ResolveOutcome):
            return started
        mode, version_id = started
        secret = bytearray()
        try:
            if mode == "byok":
                with tenant_session(self.ctx) as session:
                    secret = bytearray(open_bound_secret(session, self.ctx, version_id, self.protector))
            try:
                prices = price_table(SCRAPECREATORS)
            except MarketDataPricingError:
                prices = None  # Mock without a price table: no simulated amount
            adapter = AdLibraryCallAdapter(
                self.source_factory(mode), execution_mode=mode, prices=prices,
                pricing_version=config.MARKETAI_PRICE_REGISTRY_VERSION,
            )
            return await self._steps(adapter, mode, memoryview(secret) if secret else None, version_id)
        finally:
            for index in range(len(secret)):
                secret[index] = 0

    def _start(self):
        """Replay a finished resolve, wait for the key's turn, or mark it running."""
        with tenant_session(self.ctx) as session:
            record = _load(session, self.ctx, self.resolution_id)
            if record.status in service.FINISHED:
                return _outcome_of(record)
            version_id = None
            if record.execution_mode == "byok":
                credential = resolve_market_data_credential(session, self.ctx, SCRAPECREATORS)
                if not credential.get("active"):
                    return finish_resolution(
                        session, self.ctx, record, status="failed",
                        code="market_data_credential_unavailable", detail="no active market-data key is bound",
                    )
                version_id = credential["version_id"]
                holder = serial.turn_holder(session, self.ctx, credential_version_id=version_id, job_id=self.job_id)
                if holder is not None:
                    waits = list((record.evidence_json or {}).get("waits") or [])
                    waits.append({"at": utcnow().isoformat(), "holder_job_id": holder})
                    _update_evidence(record, waits=waits)
                    if self.final_attempt:
                        return finish_resolution(
                            session, self.ctx, record, status="failed", code="market_data_turn_unavailable",
                            detail="another market-data job held the key until this Job ran out of attempts",
                        )
                    return ResolveOutcome("wait", code="market_data_turn_busy", detail=f"queued behind {holder}")
                # The key this resolve runs with: rotation since admission is recorded, not hidden.
                record.credential_version_id = version_id
            record.status = "running"
            record.started_at = record.started_at or utcnow()
            record.updated_at = utcnow()
            session.flush()
            return record.execution_mode, version_id

    async def _call(self, adapter, mode, credential, version_id, call_name, request, recorder):
        try:
            return await execute_market_data_call(
                self.coordinator, self.ctx,
                job_id=self.job_id, operation_id=self.operation_id, call_name=call_name,
                request=request, adapter=adapter, evidence_recorder=recorder, execution_mode=mode,
                credential=credential, credential_version_id=version_id, session_factory=tenant_session,
            )
        except DispatchCoordinatorError as exc:
            # No open reservation (released, or never made): refused before any request.
            logger.warning("competitor_resolve_dispatch_refused", extra={"job_id": self.job_id, "error": str(exc)})
            return None

    def _evidence(self) -> tuple[dict, CompetitorResolutionRecord]:
        with tenant_session(self.ctx) as session:
            record = _load(session, self.ctx, self.resolution_id)
            session.expunge(record)
            return dict(record.evidence_json or {}), record

    def _record(self, *, search: dict | None = None, probe: tuple[str, dict] | None = None) -> None:
        with tenant_session(self.ctx) as session:
            record = _load(session, self.ctx, self.resolution_id)
            if search is not None:
                _update_evidence(record, search=search)
            if probe is not None:
                probes = dict((record.evidence_json or {}).get("probes") or {})
                probes[probe[0]] = probe[1]
                _update_evidence(record, probes=probes)

    def _finish(self, status: str, code: str = "", detail: str = "", params: Mapping[str, Any] | None = None,
                ) -> ResolveOutcome:
        with tenant_session(self.ctx) as session:
            record = _load(session, self.ctx, self.resolution_id)
            return finish_resolution(session, self.ctx, record, status=status, code=code, detail=detail,
                                     params=params)

    def _failed_call(self, result) -> ResolveOutcome:
        kind = "uncertain" if result.dispatch_certainty == "uncertain" else "failed"
        code, params = interface_failure(result.error_code, result.dispatch_certainty)
        return self._finish(kind, code, result.error_detail or "", params)

    async def _steps(self, adapter, mode, credential, version_id) -> ResolveOutcome:
        evidence, record = self._evidence()
        if (evidence.get("search") or {}).get("status") != "succeeded":
            call_name = service.SEARCH_CALL.call_name
            request = SourceRequest.search_pages(record.query)
            result = await self._call(adapter, mode, credential, version_id, call_name, request,
                                      ResolveEvidenceRecorder(self.resolution_id, call_name, request))
            if result is None:
                return self._finish("failed", "reservation_missing", f"no open reservation for {call_name}")
            if not result.success:
                kind = "uncertain" if result.dispatch_certainty == "uncertain" else "failed"
                self._record(search={"call_name": call_name, "attempt_id": result.attempt_id,
                                     "status": kind, "error_code": result.error_code})
                return self._failed_call(result)

        await self._avatars(mode)
        for index, page_id in enumerate(self._probe_plan(record.query), start=1):
            evidence, _ = self._evidence()
            if ((evidence.get("probes") or {}).get(page_id) or {}).get("status") in ("succeeded", "failed"):
                continue
            call_name = f"probe:{index}"
            request = SourceRequest.ads_for_page(page_id, country=record.country, operation=PriceOperation.PROBE)
            result = await self._call(
                adapter, mode, credential, version_id, call_name, request,
                ResolveEvidenceRecorder(self.resolution_id, call_name, request, page_id=page_id,
                                        country=record.country),
            )
            if result is None:
                return self._finish("failed", "reservation_missing", f"no open reservation for {call_name}")
            if result.success:
                continue
            kind = "uncertain" if result.dispatch_certainty == "uncertain" else "failed"
            self._record(probe=(page_id, {
                "call_name": call_name, "attempt_id": result.attempt_id, "status": kind,
                "country": record.country, "error_code": result.error_code,
                "landing_domains": [], "sampled_ads": 0, "total_active_ads": None,
            }))
            if kind == "uncertain" or result.error_code in STOPPING_CODES:
                return self._failed_call(result)
        return self._finish("completed")

    async def _avatars(self, mode: str) -> None:
        """Each candidate's avatar archived once (``evidence.avatars``, by page ID).

        Free: the download reaches Meta's CDN (``HttpMediaFetcher``: HTTPS and
        ``*.fbcdn.net`` only, no redirects) or the Mock's placeholders, never
        the provider's API, so nothing is reserved. A retried Job keeps what
        was recorded; a failure is recorded as ``unavailable`` with its reason."""
        evidence, _ = self._evidence()
        if "avatars" in evidence or self.blob_store is None:
            return
        fetcher = self.fetcher_factory(mode)
        gate = asyncio.Semaphore(AVATAR_DOWNLOADS)
        stored: dict[str, dict] = {}  # sha256 -> archived refs: one object per image

        async def archive(url: str) -> dict:
            async with gate:
                try:
                    fetched = await fetcher.fetch(url)
                except Exception:  # noqa: BLE001 -- one avatar, not the resolve
                    fetched = scan_media.MediaFetch(reason="fetch_failed")
                if fetched.content is None:
                    return {"status": "unavailable", "reason": fetched.reason or "fetch_failed"}
                sha256 = hashlib.sha256(fetched.content).hexdigest()
                refs = stored.get(sha256)
                if refs is None:
                    try:
                        refs = await scan_media.archive(self.blob_store, self.ctx.workspace_id, fetched.content)
                    except Exception as exc:  # noqa: BLE001 -- one avatar, not the resolve
                        logger.warning("competitor_avatar_unavailable", extra={
                            "resolution_id": self.resolution_id, "error_type": type(exc).__name__,
                        })
                        refs = {"reason": "store_failed"}
                    if "reason" not in refs:
                        stored[sha256] = refs
                if "reason" in refs:
                    return {"status": "unavailable", "reason": refs["reason"]}
                return {"status": "available", "reason": None, "fetched_at": utcnow().isoformat(), **refs}

        pages = [
            (candidate["page_id"], candidate["avatar_url"]) for candidate in evidence.get("candidates") or []
            if candidate.get("page_id") and candidate.get("avatar_url")
        ]
        outcomes = await asyncio.gather(*(archive(url) for _, url in pages))
        with tenant_session(self.ctx) as session:
            record = _load(session, self.ctx, self.resolution_id)
            _update_evidence(record, avatars={page_id: outcome for (page_id, _), outcome in zip(pages, outcomes)})

    def _probe_plan(self, query: str) -> list[str]:
        """The pages to probe, decided once; unneeded probe reservations are released."""
        with tenant_session(self.ctx) as session:
            record = _load(session, self.ctx, self.resolution_id)
            evidence = record.evidence_json or {}
            if "probe_plan" in evidence:
                return list(evidence["probe_plan"])
            competitor = session.get(CompetitorRecord, record.competitor_id)
            scored = scoring.score(
                evidence.get("candidates") or [], query=query,
                alias=competitor.input_page_alias if competitor is not None else None,
            )
            plan = scoring.probe_plan(scored)
            released = [
                call.call_name for call in service.PROBE_CALLS[len(plan):]
                if release_market_data_call(session, self.ctx, job_id=self.job_id, call_name=call.call_name)
            ]
            _update_evidence(record, probe_plan=plan, released=released)
            return plan


# ---------------------------------------------------------------------------
# Job fence
# ---------------------------------------------------------------------------


def mark_failed(ctx: TenantContext, resolution_id: str, *, code: str, detail: str) -> None:
    """Record an unexpected fault on the resolve and release what it reserved."""
    with tenant_session(ctx) as session:
        record = _load(session, ctx, resolution_id)
        if record.status not in service.FINISHED:
            finish_resolution(session, ctx, record, status="failed", code=code, detail=detail)


def _protector(runtime) -> CredentialProtector:
    protector = getattr(runtime, "_market_data_protector", None)
    if protector is None:
        from app.entitlements.credential_protector import LocalKmsAdapter

        protector = LocalKmsAdapter.local_only()
        runtime._market_data_protector = protector
    return protector


@runs_as(WORKER_ROLE)
async def handle_resolve_job(runtime, job) -> None:
    """Fence for one resolve run; the aggregated Operation folds from the Job.

    ``completed`` succeeds; ``failed`` fails (not retried: a new resolve is
    a new paid command); ``uncertain`` is never retried blindly; a wait for
    the key's turn re-queues the Job.
    """
    resolution_id = str(job.payload.get("resolution_id") or "")
    operation_id = str(job.payload.get("operation_id") or "")
    if not resolution_id or not operation_id:
        jobs.fail(job.context, job.id, runtime.owner, code="missing_input",
                  detail="resolution_id or operation_id missing", retryable=False)
        return
    jobs.mark_running(job.context, job.id, runtime.owner)
    heartbeat = asyncio.create_task(runtime._heartbeat(job.context, job.id))
    try:
        outcome = await ResolveRun(
            job.context,
            resolution_id=resolution_id,
            job_id=job.id,
            operation_id=operation_id,
            coordinator=runtime._localization_coordinator,
            protector=_protector(runtime),
            source_factory=getattr(runtime, "ad_library_source", None) or default_source,
            blob_store=runtime.blob_store,
            fetcher_factory=getattr(runtime, "competitor_media_fetcher", None) or scan_media.default_fetcher,
            final_attempt=job.attempt >= job.max_attempts,
        ).run()
    except Exception as exc:  # noqa: BLE001 -- recorded on the resolve, never a crash loop
        logger.exception("competitor_resolve_failed",
                         extra={"workspace_id": job.context.workspace_id, "resolution_id": resolution_id})
        mark_failed(job.context, resolution_id, code="internal_error", detail=type(exc).__name__)
        jobs.fail(job.context, job.id, runtime.owner, code="competitor_resolve_failed",
                  detail=str(exc)[:500], retryable=False)
        return
    finally:
        heartbeat.cancel()
    if outcome.kind == "completed":
        jobs.succeed(job.context, job.id, runtime.owner)
    elif outcome.kind == "wait":
        jobs.fail(job.context, job.id, runtime.owner, code=outcome.code, detail=outcome.detail,
                  retryable=True, retry_after_seconds=TURN_RETRY_SECONDS)
    elif outcome.kind == "uncertain":
        jobs.mark_uncertain(job.context, job.id, runtime.owner, code=outcome.code, detail=outcome.detail)
    else:
        jobs.fail(job.context, job.id, runtime.owner, code=outcome.code or "competitor_resolve_failed",
                  detail=outcome.detail, retryable=False)
