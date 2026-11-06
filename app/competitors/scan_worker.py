"""The ``competitor_scan.run`` Job (spec §5.1, §5.2): one run per admission (scan or resume).

Steps, each recorded in ``evidence_json[step]`` (``status``, ``started_at``,
``finished_at``, ``attempts``) with a ``competitor_scan.step`` audit event;
no transaction is held across a network call or a blob write:

1. **turn** -- byok waits for its key's turn (§5.1, shared with resolves)
   and records each wait; taking it marks the scan ``running`` with the
   key version this run uses (a resume may use a newer one).
2. **reconcile** -- a resume first settles any ``uncertain`` page by the
   free credit balance (:mod:`app.competitors.scan_reconcile`); a page it
   cannot decide stops the run in ``uncertain`` for the Owner.
3. **fetch** -- every competitor's confirmed page first, then the keywords,
   page by page from the stored cursor: each page one ProviderCallAttempt
   behind the dispatch fence (``step_kind='market_data'``), its ads and
   origins written in the same transaction as the attempt's success. An ad
   already seen in this scan only gains an origin row. 429 and 5xx are
   retried twice inside the call; after that, or on any other 4xx, the
   target fails and the next one goes on. 402 and 401/403 stop the fetch.
   An uncertain page stops it too and is never resent.
4. **media** -- each card's image and video poster: downloaded, stored once
   per SHA-256, 512px derivative. An expired link is refreshed with one
   paid ``ad_refresh`` call while the quota lasts (not after a stopped or
   uncertain fetch); past it the media is "unavailable".
5. **filter** -- relevance (:mod:`app.competitors.relevance`): the §7.1
   rules judge keyword-only observations without the LLM; the rest are
   merged by ``collation_id`` into batches frozen into
   ``evidence_json.filter.assignments`` (``filter:<i>`` -> groups) before
   the first call, each batch one ``llm_chat`` call under that reserved
   name. A redelivered Job reads the frozen table and replays its calls;
   a resume assigns only observations no batch holds, to the ordinals its
   own admission reserved. Answered names go to ``filter.answered`` with
   their judgments in the same transaction; the ordinals no batch needs
   are released when the step ends.
6. **tag** -- dimension tags (:mod:`app.competitors.tags`): against the
   product's vocabulary the admission froze into ``evidence_json.tag``, the
   "``tag:<i>`` -> observation" table (relevant, longest running first, at
   most ``max_tagged_ads``) frozen there before the first call; one vision
   ``llm_chat`` call per observation; answers and ``tag.answered`` in one transaction;
   ordinals outside the table released when the step ends.
7. **cluster** -- buying-claim clusters (:mod:`app.competitors.clusters`):
   the relevant observation groups frozen into
   ``evidence_json.cluster.assignments['cluster:1']``, one text call, the
   answer appended as a new clusters revision. Once a recluster has
   completed, the step is superseded (#353): a resume skips it and leaves
   its record as it was.
8. **freeze** -- free and pure (:mod:`app.competitors.snapshots`): the
   snapshot of the revisions in effect, an existing one when the hash is
   unchanged; the scan's ``snapshot_id`` points at it.

   A refusal no later call of the same run can get past (the AI key
   rejected, say) stops filter, and tag and cluster then record it without
   calling; freeze still runs on what was judged. A resume finishes them
   (Issue #272): it reserves only the unfinished steps' unanswered
   ordinals and reads answered ones from the evidence.
9. **discover** -- with keywords only (:mod:`app.competitors.discovery`,
   Issue #276): advertisers found only by a keyword, past the §7.1 rules
   and not already listed or rejected for the product, by observation
   count; the "``discover:<i>`` -> advertiser" table (at most
   ``max_discover_advertisers``) frozen into ``evidence_json.discover``
   before the first call; one text ``llm_chat`` call per advertiser, its
   answer and ``discover.answered`` in one transaction; ordinals outside
   the table released when the step ends. It records the same stopping
   refusal as tag and cluster without calling.
10. **finish** -- every reservation of this run's admission no call holds
   is released by call name (the unused LLM ordinals, here), the status
   follows from the targets (``completed`` | ``partial`` | ``failed`` |
   ``uncertain``), and the cost is summed from the attempts of every run
   of the scan.

An ``action=reconcile`` Job settles the reservations of pages the Owner
marked by hand. An ``action=recluster`` Job runs one paid ``cluster:1`` of
its own (reserved when a member asked for it), appends the new clusters
revision and freezes once.

A Worker that dies mid-run leaves its Job to the lease recovery, which
redelivers it (every action is admitted ``safe_to_retry_after_crash``,
Issue #327): the redelivered run replays what was recorded, and a call its
dead Worker left ``dispatching`` comes back ``uncertain`` from the fence --
never resent -- so a lost page ends the run as an in-process loss does.
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Mapping

from sqlalchemy import func, select

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id, utcnow
from app.competitors import (
    clusters,
    discovery,
    relevance,
    scan_media,
    scan_reconcile,
    scan_service,
    snapshots,
    tags,
)
from app.competitors.models import CompetitorAdOriginRecord, CompetitorAdRecord, CompetitorScanRecord
from app.competitors.normalize import MalformedAdError, normalize_ad
from app.competitors.resolve_worker import default_source
from app.competitors.scan_plan import JUDGMENT_STEPS
from app.competitors.sources.base import (
    AdLibrarySource,
    AdLibrarySourceError,
    PriceOperation,
    SourceRequest,
    SourceResponse,
)
from app.competitors.sources.scrapecreators import listing_items
from app.entitlements.cost_models import ProviderCallAttemptRecord
from app.entitlements.credential_protector import CredentialProtector
from app.generation.credential_binding import open_bound_secret
from app.market_data import serial
from app.market_data.calls import (
    MARKET_DATA_STEP_KIND,
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
from app.providers.llm_chat import LLM_CHAT_STEP_KIND, LlmChatClient, LlmChatError, release_llm_chat_reservation

logger = logging.getLogger(__name__)

#: 402 and 401/403: stop the fetch and release what is left (§6).
STOPPING_CODES = frozenset({"insufficient_credits", "credential_rejected"})
#: ``llm_chat`` refusals no later batch of the same run can get past.
STOPPING_LLM_CODES = frozenset({
    "credential_unavailable", "credential_rejected", "entitlement_unavailable", "mode_unavailable",
})
#: Seconds a Job waits before asking for its key's turn again.
TURN_RETRY_SECONDS = 10.0
#: Waits before the second and third try of a call answered 429 or 5xx.
RETRY_DELAYS = (2.0, 5.0)
MEDIA_KINDS = (("image_ref", "image"), ("video_poster_ref", "video_poster"))

SourceFactory = Callable[[str], AdLibrarySource]
FetcherFactory = Callable[[str], scan_media.MediaFetcher]
BalanceChecker = Callable[[str, memoryview | None, AdLibrarySource], Awaitable[int | None]]


@dataclass(frozen=True)
class ScanOutcome:
    """``completed`` | ``partial`` | ``failed`` | ``uncertain`` | ``wait`` | ``superseded``."""

    kind: str
    code: str = ""
    detail: str = ""


@dataclass(frozen=True)
class _Stop:
    """Why the fetch stopped early: ``stopped`` (402/401/403) or ``uncertain``."""

    kind: str
    code: str
    params: Mapping[str, Any]
    detail: str = ""


async def default_balance(mode: str, credential: memoryview | None, source: AdLibrarySource) -> int | None:
    """The free credit balance (byok); the simulated one under Mock."""
    if mode == "byok":
        from app.market_data.scrapecreators_account import check_credit_balance

        check = await check_credit_balance(bytes(credential or b""))
        return check.credit_count if check.outcome != "unavailable" else None
    return getattr(source, "credits_remaining", None)


class RetryingSource:
    """Retries 429 and 5xx twice inside one call (one attempt, one reservation).

    Every try is recorded in ``last_tries`` -- its number, HTTP status,
    ``credits_charged`` when the provider reported it, latency and dispatch
    certainty -- and travels with the page's evidence and the attempt's
    usage report. A 429 is refused unbilled. A 5xx may or may not have been
    charged: a charge a retried try *reported* is added to the call's own,
    so the ledger settles it; one it did not report stays unknown:

    * the call failed anyway -> the call is ``uncertain`` (never released as
      unbilled), and reconciliation settles it like a lost page;
    * a later try succeeded -> the call succeeds, and any reconciliation
      whose window holds it leaves its page to the Owner
      (:func:`app.competitors.scan_reconcile.balance_check`).
    """

    def __init__(self, source: AdLibrarySource, delays: tuple[float, ...]) -> None:
        self._source = source
        self._delays = delays
        self.descriptor = source.descriptor
        self.last_tries: list[dict] = []

    def _record(self, *, http_status, error_code, credits_charged, latency_ms, certainty) -> None:
        self.last_tries.append({
            "try": len(self.last_tries) + 1, "http_status": http_status, "error_code": error_code,
            "credits_charged": credits_charged, "latency_ms": latency_ms, "dispatch_certainty": certainty,
        })

    async def call(self, request: SourceRequest, *, credential: memoryview | None = None) -> SourceResponse:
        self.last_tries = []
        while True:
            try:
                response = await self._source.call(request, credential=credential)
            except AdLibrarySourceError as exc:
                self._record(http_status=exc.http_status, error_code=exc.error_code,
                             credits_charged=exc.credits_charged, latency_ms=exc.latency_ms,
                             certainty=exc.dispatch_certainty)
                retryable = exc.error_code == "rate_limited" or (exc.http_status or 0) >= 500
                if not retryable or len(self.last_tries) > len(self._delays):
                    earlier = sum(int(item["credits_charged"] or 0) for item in self.last_tries[:-1])
                    if earlier:
                        exc.credits_charged = (exc.credits_charged or 0) + earlier
                        if exc.dispatch_certainty == "not_dispatched":
                            exc.dispatch_certainty = "dispatched"
                    if unrecorded_charge(self.last_tries[:-1]):
                        exc.dispatch_certainty = "uncertain"
                    raise
                await asyncio.sleep(self._delays[len(self.last_tries) - 1])
                continue
            self._record(http_status=response.http_status, error_code=None,
                         credits_charged=response.credits_charged, latency_ms=response.latency_ms,
                         certainty="dispatched")
            earlier = sum(int(item["credits_charged"] or 0) for item in self.last_tries[:-1])
            if earlier and response.credits_charged is not None:
                response = dataclasses.replace(response, credits_charged=response.credits_charged + earlier)
            return response


def unrecorded_charge(tries) -> bool:
    """Whether a try may have been charged without the provider saying so."""
    return any(item.get("dispatch_certainty") == "uncertain" for item in tries or ())


def _clip(value: str | None, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _admission_for(evidence: Mapping[str, Any], job_id: str) -> Mapping[str, Any]:
    admission = evidence.get("admission") or {}
    if admission.get("job_id") == job_id:
        return admission
    return next((entry for entry in evidence.get("resumes") or [] if entry.get("job_id") == job_id), {})


def _ordinal(call_name: str) -> int:
    """``filter:12`` -> 12."""
    return int(call_name.rsplit(":", 1)[1])


def _scan_jobs(evidence: Mapping[str, Any]) -> list[str]:
    jobs_ = [(evidence.get("admission") or {}).get("job_id")]
    jobs_ += [entry.get("job_id") for entry in evidence.get("resumes") or []]
    jobs_ += [entry.get("job_id") for entry in evidence.get("reclusters") or []]
    return [job for job in jobs_ if job]


# ---------------------------------------------------------------------------
# Writing a page's observations (inside the attempt's success transaction)
# ---------------------------------------------------------------------------


def _card_json(card) -> dict:
    return {
        "index": card.index, "title": card.title, "body": card.body, "cta_text": card.cta_text,
        "cta_type": card.cta_type, "link_url": card.link_url, "link_description": card.link_description,
        "field_sources": dict(card.field_sources),
    }


def _media_refs(ad) -> tuple[list[dict], dict[str, str]]:
    """Unsigned refs for the row, signed links for the media step."""
    refs, links = [], {}
    for card in ad.cards:
        for attribute, kind in MEDIA_KINDS:
            ref = getattr(card, attribute)
            if ref is None:
                continue
            refs.append({
                "card_index": card.index, "kind": kind, "source_field": ref.source_field,
                "source_url": scan_media.unsigned(ref.url), "expires_at": _iso(ref.expires_at),
            })
            links[f"{card.index}:{kind}"] = ref.url
    return refs, links


def _page_ads(output: SourceResponse, *, origin: str, observed_at, reference: str) -> tuple[list, int]:
    listing = listing_items(output.raw)
    ads, malformed, seen = [], 0, set()
    if listing is None:
        return ads, malformed
    key, items = listing
    for index, item in enumerate(items):
        path = f"{key}[{index}]"
        try:
            ad = normalize_ad(item, provider=output.provider, origin=origin, observed_at=observed_at,
                              raw_ref=f"{reference}#{path}", raw_path=path)
        except MalformedAdError:
            malformed += 1
            continue
        if ad.ad_archive_id not in seen:
            seen.add(ad.ad_archive_id)
            ads.append(ad)
    return ads, malformed


def store_observations(session, ctx: TenantContext, record: CompetitorScanRecord, evidence: dict, *,
                       ads: list, origin: str, attempt_id: str, observed_at) -> int:
    """Insert unseen ads, append this origin to every ad; returns how many ads were new."""
    media = evidence.setdefault("media", {})
    links = media.setdefault("links", {})
    new = 0
    for ad in ads:
        row = session.scalars(
            select(CompetitorAdRecord).where(
                CompetitorAdRecord.workspace_id == ctx.workspace_id,
                CompetitorAdRecord.scan_id == record.id,
                CompetitorAdRecord.ad_archive_id == ad.ad_archive_id,
            )
        ).one_or_none()
        refs, fresh = _media_refs(ad)
        if row is None:
            new += 1
            row = CompetitorAdRecord(
                id=new_id("cad"), workspace_id=ctx.workspace_id, scan_id=record.id, provider=ad.provider,
                ad_archive_id=ad.ad_archive_id, collation_id=ad.collation_id, collation_count=ad.collation_count,
                advertiser_page_id=ad.advertiser_page_id, advertiser_name=_clip(ad.advertiser_name, 300),
                page_profile_uri=_clip(ad.page_profile_uri, 2048), page_categories_json=list(ad.page_categories),
                is_active=ad.is_active, start_date=ad.start_date, end_date=ad.end_date,
                days_running=ad.days_running, display_format=_clip(ad.display_format, 32),
                publisher_platforms_json=list(ad.publisher_platforms),
                cards_json=[_card_json(card) for card in ad.cards], landing_domain=_clip(ad.landing_domain, 253),
                library_url=_clip(ad.library_url, 2048), media_json=refs, raw_ref=_clip(ad.raw_ref, 300),
                field_sources_json=dict(ad.field_sources), observed_at=observed_at,
            )
            session.add(row)
            session.flush()
            if fresh:
                links[row.id] = fresh
        elif fresh and row.id in links:
            # A later path saw the same ad: keep its newest signed links for media still to fetch.
            links[row.id] = {**links[row.id], **fresh}
        exists = session.scalars(
            select(CompetitorAdOriginRecord.id).where(
                CompetitorAdOriginRecord.workspace_id == ctx.workspace_id,
                CompetitorAdOriginRecord.competitor_ad_id == row.id,
                CompetitorAdOriginRecord.origin == origin,
            )
        ).first()
        if exists is None:
            session.add(CompetitorAdOriginRecord(
                id=new_id("cado"), workspace_id=ctx.workspace_id, competitor_ad_id=row.id, scan_id=record.id,
                origin=origin, attempt_id=attempt_id, observed_at=observed_at,
            ))
            session.flush()
    return new


class ScanPageRecorder:
    """Persists one page: its ads and origins, the target's cursor and the page's evidence."""

    def __init__(self, scan_id: str, target_index: int, page: int, call_name: str, job_id: str,
                 request: SourceRequest, source: RetryingSource) -> None:
        self._scan_id = scan_id
        self._target_index = target_index
        self._page = page
        self._call_name = call_name
        self._job_id = job_id
        self._request = request
        self._source = source

    def link_prepared_attempt(self, session, ctx: TenantContext, *, attempt: ProviderCallAttemptRecord) -> None:
        digest = raw_digest(self._request.evidence())
        attempt.request_fingerprint_hash = digest
        attempt.redacted_request_hash = digest

    def materialize(self, ctx: TenantContext, *, attempt_id: str, output: SourceResponse) -> str:
        return f"sha256:{raw_digest(output.raw)}"

    def persist_result(self, session, ctx: TenantContext, *, attempt: ProviderCallAttemptRecord,
                       reference: str, output: SourceResponse) -> None:
        record = scan_service.lock_scan(session, ctx, self._scan_id)
        evidence = copy.deepcopy(record.evidence_json or {})
        target = evidence["targets"][self._target_index]
        observed_at = utcnow()
        ads, malformed = _page_ads(output, origin=target["origin"], observed_at=observed_at, reference=reference)
        new = store_observations(session, ctx, record, evidence, ads=ads, origin=target["origin"],
                                 attempt_id=attempt.id, observed_at=observed_at)
        target["pages"].append({
            "page": self._page, "call_name": self._call_name, "job_id": self._job_id, "attempt_id": attempt.id,
            "status": "succeeded", "cursor": self._request.cursor, "next_cursor": output.next_cursor,
            "ads": len(ads), "new_ads": new, "malformed": malformed, "credits_charged": output.credits_charged,
            "credits_remaining": output.credits_remaining, "cached": output.cached,
            "tries": list(self._source.last_tries), "response_ref": reference,
        })
        target["pages_fetched"] = int(target.get("pages_fetched") or 0) + 1
        target["cursor"] = output.next_cursor
        target["ads_seen"] = int(target.get("ads_seen") or 0) + len(ads)
        target["exhausted"] = output.next_cursor is None
        done = target["exhausted"] or target["pages_fetched"] >= int(target["pages_planned"])
        target["status"] = "completed" if done else "fetching"
        record.evidence_json = evidence
        record.updated_at = utcnow()
        session.flush()


class RefreshRecorder:
    """Persists an ``ad_refresh``: the ad's fresh signed links for the media step."""

    def __init__(self, scan_id: str, ad_id: str, call_name: str, request: SourceRequest,
                 source: RetryingSource) -> None:
        self._scan_id = scan_id
        self._ad_id = ad_id
        self._call_name = call_name
        self._request = request
        self._source = source

    @property
    def _tries(self) -> list[dict]:
        return self._source.last_tries

    def link_prepared_attempt(self, session, ctx: TenantContext, *, attempt: ProviderCallAttemptRecord) -> None:
        digest = raw_digest(self._request.evidence())
        attempt.request_fingerprint_hash = digest
        attempt.redacted_request_hash = digest

    def materialize(self, ctx: TenantContext, *, attempt_id: str, output: SourceResponse) -> str:
        return f"sha256:{raw_digest(output.raw)}"

    def persist_result(self, session, ctx: TenantContext, *, attempt: ProviderCallAttemptRecord,
                       reference: str, output: SourceResponse) -> None:
        record = scan_service.lock_scan(session, ctx, self._scan_id)
        evidence = copy.deepcopy(record.evidence_json or {})
        media = evidence.setdefault("media", {})
        fresh: dict[str, str] = {}
        try:
            ad = normalize_ad(output.raw, provider=output.provider, origin="refresh", observed_at=utcnow())
            _refs, fresh = _media_refs(ad)
        except MalformedAdError:
            pass
        media.setdefault("refreshed_links", {})[self._ad_id] = fresh
        for entry in media.get("refreshes") or []:
            if entry["call_name"] == self._call_name and entry["ad_id"] == self._ad_id:
                entry.update(status="succeeded", attempt_id=attempt.id, credits_charged=output.credits_charged,
                             credits_remaining=output.credits_remaining, response_ref=reference,
                             tries=list(self._tries))
        record.evidence_json = evidence
        record.updated_at = utcnow()
        session.flush()


# ---------------------------------------------------------------------------
# Finishing (shared by the run and the unexpected-fault path)
# ---------------------------------------------------------------------------


def _outcome_of(record: CompetitorScanRecord) -> ScanOutcome:
    if record.status in ("completed", "partial"):
        return ScanOutcome(record.status, record.failure_code or "", record.failure_detail or "")
    return ScanOutcome(record.status, record.failure_code or "competitor_scan_failed", record.failure_detail or "")


def scan_cost(session, ctx: TenantContext, record: CompetitorScanRecord) -> dict:
    """Both lines, summed from the attempts of every run of the scan (simulated under Mock)."""
    job_ids = _scan_jobs(record.evidence_json or {})
    attempts = session.scalars(
        select(ProviderCallAttemptRecord).where(
            ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            ProviderCallAttemptRecord.job_id.in_(job_ids or [""]),
            ProviderCallAttemptRecord.step_kind.in_((MARKET_DATA_STEP_KIND, LLM_CHAT_STEP_KIND)),
        )
    ).all()
    lines = {kind: {"calls": 0, "credits": 0, "usd": Decimal("0"), "simulated": Decimal("0"), "priced": False}
             for kind in (MARKET_DATA_STEP_KIND, LLM_CHAT_STEP_KIND)}
    for attempt in attempts:
        line = lines[attempt.step_kind]
        line["calls"] += 1
        usage = call_usage(session, ctx, attempt.id)
        line["credits"] += int((usage.get("usage") or {}).get("credits_charged") or 0)
        cost = usage.get("cost") or {}
        line["usd"] += Decimal(cost.get("provider_amount") or "0")
        if cost.get("simulated_amount") is not None:
            line["simulated"] += Decimal(cost["simulated_amount"])
            line["priced"] = True
    mock = record.execution_mode == "mock"

    def simulated(line) -> str | None:
        return format(line["simulated"], "f") if mock and line["priced"] else None

    market_data, llm = lines[MARKET_DATA_STEP_KIND], lines[LLM_CHAT_STEP_KIND]
    total_simulated = market_data["simulated"] + llm["simulated"]
    return {
        "market_data": {"calls": market_data["calls"], "credits_charged": market_data["credits"],
                        "usd": format(market_data["usd"], "f"), "simulated_usd": simulated(market_data)},
        "llm": {"calls": llm["calls"], "usd": format(llm["usd"], "f"), "simulated_usd": simulated(llm)},
        "usd": format(market_data["usd"] + llm["usd"], "f"),
        "simulated_usd": format(total_simulated, "f") if mock and (market_data["priced"] or llm["priced"]) else None,
    }


def release_unused(session, ctx: TenantContext, evidence: Mapping[str, Any], job_id: str) -> list[str]:
    """Release every reservation of ``job_id``'s admission that no call holds, by call name."""
    admission = _admission_for(evidence, job_id)
    released = [
        call["call_name"] for call in admission.get("market_data_calls") or []
        if release_market_data_call(session, ctx, job_id=job_id, call_name=call["call_name"])
    ]
    released += [
        call["call_name"] for call in admission.get("llm_calls") or []
        if release_llm_chat_reservation(session, ctx, job_id=job_id, call_name=call["call_name"])
    ]
    return released


def _stage(evidence: Mapping[str, Any]) -> str | None:
    stage = None
    for step in ("fetch", "media", *JUDGMENT_STEPS):
        if (evidence.get(step) or {}).get("status") in ("completed", "partial"):
            stage = step
    return stage


def finish_scan(session, ctx: TenantContext, record: CompetitorScanRecord, *, job_id: str,
                code: str = "", detail: str = "", params: Mapping[str, Any] | None = None,
                status: str | None = None) -> ScanOutcome:
    """Release what this run did not use, settle the status, sum the cost."""
    evidence = copy.deepcopy(record.evidence_json or {})
    released = dict(evidence.get("released") or {})
    released[job_id] = sorted(set(released.get(job_id) or []) | set(release_unused(session, ctx, evidence, job_id)))
    evidence["released"] = released
    status = status or scan_service.terminal_status(evidence)
    if status == "completed":
        code, params = "", {}
    elif not code:
        failed = next((target for target in evidence.get("targets") or [] if target.get("failure")), None)
        if failed is not None:
            code, params = failed["failure"]["code"], failed["failure"].get("params") or {}
        elif status == "uncertain":
            code, params = "market_data_outcome_unknown", {}
    now = utcnow()
    evidence["failure_params"] = dict(params or {})
    record.evidence_json = evidence
    record.status = status
    record.stage = _stage(evidence)
    if status == "completed":
        record.failure_code = None
    else:
        record.failure_code = code or ("competitor_scan_failed" if status == "failed" else None)
    record.failure_detail = (detail[:2000] or None) if record.failure_code else None
    record.finished_at = now
    record.updated_at = now
    record.cost_json = scan_cost(session, ctx, record)
    session.flush()
    audit.append_event(
        session,
        event_type="competitor_scan.finished",
        resource_type=scan_service.RESOURCE_TYPE,
        resource_id=record.id,
        details={
            "job_id": job_id, "status": status, "stage": record.stage, "failure_code": record.failure_code,
            "released": len(released[job_id]),
            "credits_charged": record.cost_json["market_data"]["credits_charged"],
            "usd": record.cost_json["usd"],
        },
    )
    session.flush()
    return _outcome_of(record)


def _step_event(session, record: CompetitorScanRecord, step: str, state: Mapping[str, Any], job_id: str) -> None:
    audit.append_event(
        session,
        event_type="competitor_scan.step",
        resource_type=scan_service.RESOURCE_TYPE,
        resource_id=record.id,
        details={"step": step, "status": state.get("status"), "reason": state.get("reason"),
                 "attempts": state.get("attempts"), "job_id": job_id,
                 **{key: state[key] for key in ("pages", "ads", "available", "unavailable", "refresh_calls",
                                                "batches", "judged", "unjudged", "advertisers", "recommended")
                    if key in state}},
    )


# ---------------------------------------------------------------------------
# One run of the Job
# ---------------------------------------------------------------------------


class ScanRun:
    """One execution of a scan Job (fresh, retried, or a resume's)."""

    def __init__(
        self,
        ctx: TenantContext,
        *,
        scan_id: str,
        job_id: str,
        coordinator: ExternalCallCoordinator,
        protector: CredentialProtector,
        blob_store,
        source_factory: SourceFactory = default_source,
        fetcher_factory: FetcherFactory = scan_media.default_fetcher,
        balance_checker: BalanceChecker = default_balance,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        final_attempt: bool = False,
        llm_client: LlmChatClient | None = None,
    ) -> None:
        self.ctx = ctx
        self.scan_id = scan_id
        self.job_id = job_id
        self.coordinator = coordinator
        self.protector = protector
        self.blob_store = blob_store
        self.source_factory = source_factory
        self.fetcher_factory = fetcher_factory
        self.balance_checker = balance_checker
        self.retry_delays = retry_delays
        self.final_attempt = final_attempt
        self.llm_client = llm_client or LlmChatClient(
            coordinator=coordinator, session_factory=tenant_session, protector=protector, blob_store=blob_store,
        )
        self.operation_id = ""

    # -- state ---------------------------------------------------------------

    def _evidence(self) -> tuple[dict, CompetitorScanRecord]:
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            session.expunge(record)
            return copy.deepcopy(record.evidence_json or {}), record

    def _update(self, change: Callable[[dict], None]) -> None:
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            change(evidence)
            record.evidence_json = evidence
            record.updated_at = utcnow()

    def _step(self, step: str, **changes: Any) -> None:
        def change(evidence: dict) -> None:
            state = dict(evidence.get(step) or {})
            state.update(changes)
            evidence[step] = state

        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            change(evidence)
            record.evidence_json = evidence
            record.updated_at = utcnow()
            if changes.get("finished_at"):
                _step_event(session, record, step, evidence[step], self.job_id)

    def _begin(self, step: str) -> None:
        evidence, _ = self._evidence()
        state = evidence.get(step) or {}
        self._step(step, status="running", started_at=utcnow().isoformat(), finished_at=None,
                   attempts=int(state.get("attempts") or 0) + 1)

    def _finish(self, *, code: str = "", detail: str = "", params: Mapping[str, Any] | None = None,
                status: str | None = None) -> ScanOutcome:
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            return finish_scan(session, self.ctx, record, job_id=self.job_id, code=code, detail=detail,
                               params=params, status=status)

    # -- run -----------------------------------------------------------------

    async def run(self) -> ScanOutcome:
        started = self._start()
        if isinstance(started, ScanOutcome):
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
            source = self.source_factory(mode)
            retrying = RetryingSource(source, self.retry_delays)
            adapter = AdLibraryCallAdapter(
                retrying, execution_mode=mode, prices=prices, pricing_version=config.MARKETAI_PRICE_REGISTRY_VERSION,
            )
            credential = memoryview(secret) if secret else None
            if not await self._reconcile_pending(mode, credential, version_id, source, prices):
                return self._finish(status="uncertain", code="market_data_outcome_unknown",
                                    detail="an uncertain page could not be reconciled automatically")
            stop = await self._fetch(adapter, retrying, mode, credential, version_id)
            await self._media(adapter, retrying, mode, credential, version_id, allow_refresh=stop is None)
            llm_stop = await self._filter(mode)
            llm_stop = await self._tag(mode, llm_stop)
            await self._cluster(mode, llm_stop)
            self._freeze()
            await self._discover(mode, llm_stop)
            if stop is not None:
                return self._finish(code=stop.code, detail=stop.detail, params=stop.params)
            return self._finish()
        finally:
            for index in range(len(secret)):
                secret[index] = 0

    def _start(self):
        """Replay a finished run, wait for the key's turn, or mark the scan running."""
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            if record is None:
                return ScanOutcome("failed", "competitor_scan_not_found", "the scan no longer exists")
            if record.job_id != self.job_id:
                return ScanOutcome("superseded", detail=f"the scan now runs as {record.job_id}")
            if record.status in scan_service.FINISHED:
                return _outcome_of(record)
            self.operation_id = record.operation_id
            evidence = copy.deepcopy(record.evidence_json or {})
            version_id = None
            if record.execution_mode == "byok":
                credential = resolve_market_data_credential(session, self.ctx, SCRAPECREATORS)
                if not credential.get("active"):
                    self._fail_remaining(record, "market_data_credential_unavailable")
                    return finish_scan(session, self.ctx, record, job_id=self.job_id,
                                       code="market_data_credential_unavailable",
                                       detail="no active market-data key is bound", params={"provider": SCRAPECREATORS})
                version_id = credential["version_id"]
                holder = serial.turn_holder(session, self.ctx, credential_version_id=version_id, job_id=self.job_id)
                if holder is not None:
                    evidence.setdefault("waits", []).append({
                        "at": utcnow().isoformat(), "holder_job_id": holder, "job_id": self.job_id,
                    })
                    record.evidence_json = evidence
                    record.updated_at = utcnow()
                    if self.final_attempt:
                        return finish_scan(
                            session, self.ctx, record, job_id=self.job_id, code="market_data_turn_unavailable",
                            detail="another market-data job held the key until this Job ran out of attempts",
                        )
                    return ScanOutcome("wait", code="market_data_turn_busy", detail=f"queued behind {holder}")
            runs = evidence.setdefault("runs", [])
            if not any(run.get("job_id") == self.job_id for run in runs):
                runs.append({"job_id": self.job_id, "started_at": utcnow().isoformat(),
                             "credential_version_id": version_id})
                for target in evidence.get("targets") or []:
                    if target.get("status") in ("failed", "uncertain", "fetching"):
                        target.update(status="pending", failure=None)
            evidence["run_credential_version_id"] = version_id
            record.evidence_json = evidence
            record.status = "running"
            record.started_at = record.started_at or utcnow()
            record.updated_at = utcnow()
            session.flush()
            return record.execution_mode, version_id

    @staticmethod
    def _fail_remaining(record: CompetitorScanRecord, code: str) -> None:
        evidence = copy.deepcopy(record.evidence_json or {})
        for target in evidence.get("targets") or []:
            done = target.get("exhausted") or int(target.get("pages_fetched") or 0) >= int(target["pages_planned"])
            if not done:
                target.update(status="failed", failure={"code": code, "params": {"provider": SCRAPECREATORS}})
        record.evidence_json = evidence

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
            logger.warning("competitor_scan_dispatch_refused", extra={"job_id": self.job_id, "error": str(exc)})
            return None

    # -- reconcile -----------------------------------------------------------

    async def _reconcile_pending(self, mode, credential, version_id, source, prices) -> bool:
        evidence, _ = self._evidence()
        pending = scan_service.pending_reconciliation(evidence)
        if not pending:
            return True
        balance = None
        if mode == "byok" and any(entry.get("credential_version_id") == version_id for entry in pending):
            try:
                balance = await self.balance_checker(mode, credential, source)
            except Exception:  # noqa: BLE001 -- an unreadable balance leaves the page to the Owner
                logger.warning("competitor_scan_balance_unavailable", extra={"job_id": self.job_id})
                balance = None
        decided = True
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            for entry in evidence.get("reconciliation") or []:
                if entry.get("status") != "pending":
                    continue
                if mode == "mock":
                    # Controlled Mock charges nothing real: settled as not charged, no balance to read.
                    check = {"outcome": "not_charged", "reason": None, "difference": None,
                             "baseline_credits_remaining": None, "known_charges": None, "balance": None,
                             "checked_at": utcnow().isoformat()}
                else:
                    price = (prices or {}).get(entry.get("operation") or "page")
                    check = scan_reconcile.balance_check(
                        session, self.ctx, attempt_id=entry["attempt_id"], balance=balance,
                        run_credential_version_id=version_id, expected_charge=price.credits if price else 1,
                    )
                entry["balance_check"] = check
                if check["outcome"] == "manual_required":
                    decided = False
                    continue
                scan_reconcile.settle_uncertain_call(
                    session, self.ctx, attempt_id=entry["attempt_id"], charged=check["outcome"] == "charged",
                    prices=prices, pricing_version=config.MARKETAI_PRICE_REGISTRY_VERSION, source="auto",
                )
                entry.update(status=check["outcome"], source="auto", decided_at=check["checked_at"], settled=True)
                audit.append_event(
                    session, event_type="competitor_scan.reconciled", resource_type=scan_service.RESOURCE_TYPE,
                    resource_id=record.id,
                    details={"attempt_id": entry["attempt_id"], "outcome": check["outcome"], "source": "auto",
                             "difference": check.get("difference"), "job_id": self.job_id},
                )
            record.evidence_json = evidence
            record.updated_at = utcnow()
        return decided

    # -- fetch ---------------------------------------------------------------

    def _request(self, target: Mapping[str, Any], country: str) -> SourceRequest:
        if target["kind"] == "page":
            return SourceRequest.ads_for_page(target["page_id"], country=country, cursor=target.get("cursor"),
                                              operation=PriceOperation.PAGE)
        return SourceRequest.search_ads(target["keyword"], country=country, cursor=target.get("cursor"))

    @staticmethod
    def _call_name(target: Mapping[str, Any], page: int) -> str:
        if target["kind"] == "page":
            return f"page:{target['competitor_id']}:{page}"
        return f"keyword:{target['keyword_index']}:{page}"

    def _record_failure(self, index: int, page_entry: dict, *, status: str, failure: dict | None,
                        reconciliation: dict | None = None) -> None:
        def change(evidence: dict) -> None:
            target = evidence["targets"][index]
            if not any(entry.get("call_name") == page_entry["call_name"] and entry.get("job_id") == self.job_id
                       for entry in target["pages"]):
                target["pages"].append(page_entry)
            target["status"] = status
            target["failure"] = failure
            if reconciliation is not None:
                entries = evidence.setdefault("reconciliation", [])
                if not any(entry["attempt_id"] == reconciliation["attempt_id"] for entry in entries):
                    entries.append(reconciliation)

        self._update(change)

    async def _fetch(self, adapter, retrying, mode, credential, version_id) -> _Stop | None:
        self._begin("fetch")
        evidence, record = self._evidence()
        country = record.market
        stop = None
        for index in range(len(evidence.get("targets") or [])):
            while stop is None:
                evidence, _ = self._evidence()
                target = evidence["targets"][index]
                fetched = int(target.get("pages_fetched") or 0)
                if target.get("exhausted") or fetched >= int(target["pages_planned"]):
                    if target.get("status") != "completed":
                        self._update(lambda e, i=index: e["targets"][i].update(status="completed", failure=None))
                    break
                if target.get("status") == "failed":
                    break
                page = fetched + 1
                call_name = self._call_name(target, page)
                request = self._request(target, country)
                result = await self._call(
                    adapter, mode, credential, version_id, call_name, request,
                    ScanPageRecorder(self.scan_id, index, page, call_name, self.job_id, request, retrying),
                )
                if result is None:
                    self._record_failure(index, {
                        "page": page, "call_name": call_name, "job_id": self.job_id, "status": "failed",
                        "error_code": "reservation_missing",
                    }, status="failed", failure={"code": "reservation_missing", "params": {}})
                    break
                if result.success:
                    continue
                uncertain = result.dispatch_certainty == "uncertain"
                code, params = interface_failure(result.error_code, result.dispatch_certainty)
                page_entry = {
                    "page": page, "call_name": call_name, "job_id": self.job_id, "attempt_id": result.attempt_id,
                    "status": "uncertain" if uncertain else "failed", "cursor": request.cursor,
                    "error_code": result.error_code, "tries": list(retrying.last_tries),
                }
                if uncertain:
                    self._record_failure(index, page_entry, status="uncertain", failure={"code": code, "params": params},
                                         reconciliation={
                        "attempt_id": result.attempt_id, "call_name": call_name, "job_id": self.job_id,
                        "origin": target["origin"], "page": page, "operation": request.operation.value,
                        "credential_version_id": version_id, "status": "pending", "source": None,
                        "balance_check": None,
                    })
                    stop = _Stop("uncertain", code, params, result.error_detail or "")
                    break
                self._record_failure(index, page_entry, status="failed", failure={"code": code, "params": params})
                if result.error_code in STOPPING_CODES:
                    stop = _Stop("stopped", code, params, result.error_detail or "")
                break
            if stop is not None:
                break
        evidence, _ = self._evidence()
        targets = evidence.get("targets") or []
        failed = any(target.get("status") in ("failed", "uncertain") for target in targets)
        pending = any(target.get("status") in ("pending", "fetching") for target in targets)
        self._step(
            "fetch", status="partial" if failed or pending else "completed", finished_at=utcnow().isoformat(),
            pages=sum(int(target.get("pages_fetched") or 0) for target in targets),
            ads=sum(int(target.get("ads_seen") or 0) for target in targets),
        )
        return stop

    # -- media ---------------------------------------------------------------

    async def _media(self, adapter, retrying, mode, credential, version_id, *, allow_refresh: bool) -> None:
        self._begin("media")
        fetcher = self.fetcher_factory(mode)
        with tenant_session(self.ctx) as session:
            ads = [
                (row.id, row.ad_archive_id, list(row.media_json or []))
                for row in session.scalars(
                    select(CompetitorAdRecord)
                    .where(CompetitorAdRecord.workspace_id == self.ctx.workspace_id,
                           CompetitorAdRecord.scan_id == self.scan_id)
                    .order_by(CompetitorAdRecord.observed_at, CompetitorAdRecord.id)
                )
            ]
        for ad_id, ad_archive_id, refs in ads:
            if not refs:
                continue
            evidence, _ = self._evidence()
            media = evidence.get("media") or {}
            done = {(item["card_index"], item["kind"]) for item in (media.get("items") or {}).get(ad_id) or []
                    if item.get("status") in ("available", "unavailable")}
            todo = [ref for ref in refs if (ref["card_index"], ref["kind"]) not in done]
            if not todo:
                continue
            links = dict((media.get("links") or {}).get(ad_id) or {})
            outcomes, expired = await self._download(fetcher, todo, links, media)
            if expired and not allow_refresh:
                outcomes += [{**self._ref_key(ref), "status": "pending", "reason": "expired"} for ref in expired]
            elif expired:
                kind, fresh = await self._refresh(adapter, retrying, mode, credential, version_id, ad_id,
                                                  ad_archive_id)
                if kind == "ok":
                    evidence, _ = self._evidence()
                    refreshed, still = await self._download(fetcher, expired, fresh, evidence.get("media") or {})
                    outcomes += [{**item, "refreshed": True} for item in refreshed]
                    outcomes += [{**self._ref_key(ref), "status": "unavailable", "reason": "expired",
                                  "refreshed": True} for ref in still]
                elif kind == "stop":
                    # 402/401/403 or an uncertain refresh: no further paid call in this run.
                    allow_refresh = False
                    outcomes += [{**self._ref_key(ref), "status": "pending", "reason": "expired"} for ref in expired]
                else:
                    reason = "refresh_quota_exhausted" if kind == "quota" else "refresh_failed"
                    outcomes += [{**self._ref_key(ref), "status": "unavailable", "reason": reason}
                                 for ref in expired]
            self._store_media(ad_id, outcomes)
        evidence, _ = self._evidence()
        items = (evidence.get("media") or {}).get("items") or {}
        statuses = [item["status"] for outcomes in items.values() for item in outcomes]
        self._step(
            "media", finished_at=utcnow().isoformat(),
            status="completed" if all(status == "available" for status in statuses) else "partial",
            available=statuses.count("available"), unavailable=statuses.count("unavailable"),
            refresh_calls=len((evidence.get("media") or {}).get("refreshes") or []),
        )

    @staticmethod
    def _ref_key(ref: Mapping[str, Any]) -> dict:
        return {"card_index": ref["card_index"], "kind": ref["kind"]}

    async def _download(self, fetcher, refs, links, media) -> tuple[list[dict], list[dict]]:
        """Each ref downloaded and archived; the ones whose link expired (or is gone) come back apart."""
        outcomes, expired = [], []
        blobs = dict(media.get("blobs") or {})
        for ref in refs:
            url = links.get(f"{ref['card_index']}:{ref['kind']}")
            if url is None:
                expired.append(ref)
                continue
            fetched = await fetcher.fetch(url)
            if fetched.content is None:
                if fetched.reason == "expired":
                    expired.append(ref)
                else:
                    outcomes.append({**self._ref_key(ref), "status": "unavailable", "reason": fetched.reason})
                continue
            sha256 = hashlib.sha256(fetched.content).hexdigest()
            stored = blobs.get(sha256)
            if stored is None:
                try:
                    stored = await scan_media.archive(self.blob_store, self.ctx.workspace_id, fetched.content)
                except Exception as exc:  # noqa: BLE001 -- one image, not the scan
                    logger.warning("competitor_media_unavailable", extra={
                        "scan_id": self.scan_id, "error_type": type(exc).__name__,
                    })
                    stored = {"reason": "store_failed"}
                if "reason" not in stored:
                    blobs[sha256] = stored
            if "reason" in stored:
                outcomes.append({**self._ref_key(ref), "status": "unavailable", "reason": stored["reason"]})
                continue
            outcomes.append({**self._ref_key(ref), "status": "available", "reason": None,
                             "fetched_at": utcnow().isoformat(), **stored})
        media["blobs"] = blobs
        return outcomes, expired

    def _store_media(self, ad_id: str, outcomes: list[dict]) -> None:
        def change(evidence: dict) -> None:
            media = evidence.setdefault("media", {})
            blobs = media.setdefault("blobs", {})
            for item in outcomes:
                if item.get("status") == "available" and item.get("sha256"):
                    blobs.setdefault(item["sha256"], {
                        key: item[key] for key in ("sha256", "mime_type", "byte_size", "width", "height",
                                                    "object_key", "storage_version_id", "thumbnail")
                    })
            items = media.setdefault("items", {})
            merged = {(item["card_index"], item["kind"]): item for item in items.get(ad_id) or []}
            for item in outcomes:
                merged[(item["card_index"], item["kind"])] = item
            items[ad_id] = list(merged.values())
            if all(item["status"] != "pending" for item in items[ad_id]):
                (media.get("links") or {}).pop(ad_id, None)
                (media.get("refreshed_links") or {}).pop(ad_id, None)

        self._update(change)

    async def _refresh(self, adapter, retrying, mode, credential, version_id, ad_id,
                       ad_archive_id) -> tuple[str, dict]:
        """Fresh signed links via one paid ``ad_refresh``: ``("ok", links)``, or ``"quota"``
        (none left), ``"stop"`` (402/401/403 or uncertain) or ``"failed"`` (any other refusal)."""
        evidence, record = self._evidence()
        media = evidence.get("media") or {}
        refreshes = list(media.get("refreshes") or [])
        quota = int((record.params_json or {}).get("max_media_refreshes") or 0)
        mine = [entry for entry in refreshes if entry["job_id"] == self.job_id]
        replay = next((entry for entry in mine if entry["ad_id"] == ad_id), None)
        if replay is None:
            if len(refreshes) >= quota:
                return "quota", {}
            replay = {"index": len(mine) + 1, "ad_id": ad_id, "job_id": self.job_id,
                      "call_name": f"ad_refresh:{len(mine) + 1}", "status": "pending"}
            self._update(lambda e: e.setdefault("media", {}).setdefault("refreshes", []).append(dict(replay)))
        if replay.get("status") == "succeeded":
            return "ok", dict((media.get("refreshed_links") or {}).get(ad_id) or {})
        request = SourceRequest.ad_detail(ad_archive_id)
        result = await self._call(adapter, mode, credential, version_id, replay["call_name"], request,
                                  RefreshRecorder(self.scan_id, ad_id, replay["call_name"], request, retrying))
        if result is not None and result.success:
            evidence, _ = self._evidence()
            return "ok", dict(((evidence.get("media") or {}).get("refreshed_links") or {}).get(ad_id) or {})
        uncertain = result is not None and result.dispatch_certainty == "uncertain"
        error_code = result.error_code if result is not None else "reservation_missing"

        def change(evidence: dict) -> None:
            for entry in (evidence.get("media") or {}).get("refreshes") or []:
                if entry["call_name"] == replay["call_name"] and entry["job_id"] == self.job_id:
                    entry.update(status="uncertain" if uncertain else "failed", error_code=error_code,
                                 attempt_id=result.attempt_id if result is not None else None,
                                 tries=list(retrying.last_tries))
            if uncertain:
                entries = evidence.setdefault("reconciliation", [])
                if not any(entry["attempt_id"] == result.attempt_id for entry in entries):
                    entries.append({
                        "attempt_id": result.attempt_id, "call_name": replay["call_name"], "job_id": self.job_id,
                        "origin": f"media:{ad_archive_id}", "page": 0, "operation": "ad_refresh",
                        "credential_version_id": version_id, "status": "pending", "source": None,
                        "balance_check": None,
                    })

        self._update(change)
        return ("stop" if uncertain or error_code in STOPPING_CODES else "failed"), {}

    # -- filter (relevance) ------------------------------------------------------

    def _filter_names(self, evidence: Mapping[str, Any]) -> list[str]:
        """The ``filter:<i>`` names this run's admission reserved, by ordinal."""
        names = [call["call_name"] for call in _admission_for(evidence, self.job_id).get("llm_calls") or []
                 if call.get("step") == relevance.STEP]
        return sorted(names, key=_ordinal)

    async def _filter(self, mode: str) -> str | None:
        """Rules, then one ``llm_chat`` call per frozen batch; unused ordinals
        released. Returns the refusal that stopped it, if any."""
        self._begin(relevance.STEP)
        pending = self._freeze_filter()
        stop = None
        for call_name in pending:
            code = await self._judge(mode, call_name)
            if code in STOPPING_LLM_CODES:
                stop = code
                break
        self._close_filter(stop)
        return stop

    def _freeze_filter(self) -> list[str]:
        """Judge by rule, then freeze batches for what no batch holds yet; the
        assigned names this run still has to answer, in order."""
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            state = dict(evidence.get(relevance.STEP) or {})
            if "product" not in state:
                state["product"] = relevance.product_context(session, self.ctx, record.product_id)
            language = state.setdefault("language", (record.params_json or {}).get("language") or "en")
            ads = session.scalars(
                select(CompetitorAdRecord)
                .where(CompetitorAdRecord.workspace_id == self.ctx.workspace_id,
                       CompetitorAdRecord.scan_id == self.scan_id)
                .order_by(CompetitorAdRecord.id)
            ).all()
            origins: dict[str, list[str]] = {}
            for ad_id, origin in session.execute(
                select(CompetitorAdOriginRecord.competitor_ad_id, CompetitorAdOriginRecord.origin).where(
                    CompetitorAdOriginRecord.workspace_id == self.ctx.workspace_id,
                    CompetitorAdOriginRecord.scan_id == self.scan_id,
                )
            ):
                origins.setdefault(ad_id, []).append(origin)
            judged = set(relevance.latest_by_ad(session, self.ctx, self.scan_id))
            assignments = dict(state.get("assignments") or {})
            assigned = {ad_id for entry in assignments.values() for group in entry["groups"]
                        for ad_id in group["ad_ids"]}
            rules = dict(state.get("rules") or {})
            waiting = []
            for ad in ads:
                if ad.id in judged or ad.id in assigned:
                    continue
                verdict = relevance.rule_verdict(ad, origins.get(ad.id, ()))
                if verdict is None:
                    waiting.append(ad)
                    continue
                relevance.append(
                    session, self.ctx, scan_id=self.scan_id, ad_id=ad.id, source="rule", relevance="unrelated",
                    payload={"relevance": "unrelated", "confidence": None, "reason": None, "language": language,
                             "rule": verdict["rule"]},
                    evidence={**verdict, "job_id": self.job_id},
                    key_hash=relevance.natural_key(self.scan_id, ad.id, "rule"),
                    fingerprint=relevance.natural_key(verdict["rule"], verdict["matched"]),
                )
                rules[verdict["rule"]] = int(rules.get(verdict["rule"]) or 0) + 1
            batches = relevance.batches_of(relevance.groups_of(waiting))
            free = [name for name in self._filter_names(evidence) if name not in assignments]
            now = utcnow().isoformat()
            for name, batch in zip(free, batches):
                assignments[name] = {"groups": batch, "job_id": self.job_id, "assigned_at": now}
            left = batches[len(free):]
            state.update(
                assignments=dict(sorted(assignments.items(), key=lambda item: _ordinal(item[0]))),
                rules=rules, answered=list(state.get("answered") or []), calls=dict(state.get("calls") or {}),
                unassigned=sum(len(group["ad_ids"]) for batch in left for group in batch),
            )
            evidence[relevance.STEP] = state
            record.evidence_json = evidence
            record.updated_at = utcnow()
            mine = set(self._filter_names(evidence))
            return [name for name in state["assignments"] if name not in state["answered"] and name in mine]

    async def _judge(self, mode: str, call_name: str) -> str | None:
        """One batch: its call (or replay), then its judgments and ``answered`` in one transaction."""
        evidence, _ = self._evidence()
        state = evidence[relevance.STEP]
        groups = state["assignments"][call_name]["groups"]
        with tenant_session(self.ctx) as session:
            ads = {ad.id: ad for ad in session.scalars(select(CompetitorAdRecord).where(
                CompetitorAdRecord.workspace_id == self.ctx.workspace_id,
                CompetitorAdRecord.id.in_([group["representative"] for group in groups]),
            ))}
            for ad in ads.values():
                session.expunge(ad)
        try:
            request, items = relevance.build_request(groups, ads, state["product"], language=state["language"],
                                                     execution_mode=mode)
            result = await self.llm_client.chat(
                self.ctx, job_id=self.job_id, operation_id=self.operation_id, call_name=call_name,
                request=request, execution_mode=mode,
            )
        except (LlmChatError, relevance.RelevanceBudgetError) as exc:
            code = getattr(exc, "code", "competitor_relevance_budget")
            failure = {"status": "failed", "job_id": self.job_id, "error_code": code,
                       "retryable": bool(getattr(exc, "retryable", False)),
                       "dispatch_certainty": getattr(exc, "dispatch_certainty", "not_dispatched"),
                       "attempt_id": getattr(exc, "attempt_id", None)}
            logger.warning("competitor_relevance_failed", extra={"scan_id": self.scan_id, "call_name": call_name,
                                                                  "error_code": code})
            self._update(lambda e: e[relevance.STEP].setdefault("calls", {}).__setitem__(call_name, failure))
            return code
        answers = relevance.normalize(result.data, [item["id"] for item in items])
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            state = evidence[relevance.STEP]
            judged = set(relevance.latest_by_ad(session, self.ctx, self.scan_id))
            written = 0
            for index, group in enumerate(groups):
                answer = answers.get(relevance.item_id(index))
                if answer is None:
                    continue
                for ad_id in group["ad_ids"]:
                    if ad_id in judged:  # a member flipped it first, or an earlier run wrote it
                        continue
                    relevance.append(
                        session, self.ctx, scan_id=self.scan_id, ad_id=ad_id, source="ai",
                        relevance=answer["relevance"], payload={**answer, "language": state["language"]},
                        evidence={
                            "job_id": self.job_id, "call_name": call_name, "item": relevance.item_id(index),
                            "collation_key": group["key"], "representative_ad_id": group["representative"],
                            "variants": len(group["ad_ids"]), "execution_mode": mode, "provider": result.provider,
                            "model": result.model, "model_version": result.model_version,
                            "prompt_version": relevance.PROMPT_VERSION, "replayed": result.replayed,
                        },
                        attempt_id=result.attempt_id, key_hash=relevance.natural_key(self.scan_id, ad_id, "ai"),
                        fingerprint=relevance.natural_key(answer["relevance"], call_name),
                    )
                    written += 1
            if call_name not in state["answered"]:
                state["answered"].append(call_name)
            state.setdefault("calls", {})[call_name] = {
                "status": "answered", "job_id": self.job_id, "attempt_id": result.attempt_id,
                "items": len(items), "answers": len(answers), "judged": written, "replayed": result.replayed,
            }
            record.evidence_json = evidence
            record.updated_at = utcnow()
        return None

    def _close_filter(self, stop: str | None) -> None:
        """Release the ordinals no batch needs, then record how the step went."""
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            state = evidence[relevance.STEP]
            assignments = state.get("assignments") or {}
            released = [
                name for name in self._filter_names(evidence)
                if name not in assignments
                and release_llm_chat_reservation(session, self.ctx, job_id=self.job_id, call_name=name)
            ]
            state["released"] = sorted(set(state.get("released") or []) | set(released), key=_ordinal)
            ledger = dict(evidence.get("released") or {})
            ledger[self.job_id] = sorted(set(ledger.get(self.job_id) or []) | set(released))
            evidence["released"] = ledger
            sources: dict[str, int] = {}
            for row in relevance.latest_by_ad(session, self.ctx, self.scan_id).values():
                sources[row.source] = sources.get(row.source, 0) + 1
            observations = session.scalar(
                select(func.count()).select_from(CompetitorAdRecord).where(
                    CompetitorAdRecord.workspace_id == self.ctx.workspace_id,
                    CompetitorAdRecord.scan_id == self.scan_id,
                )
            ) or 0
            record.evidence_json = evidence
            record.updated_at = utcnow()
        unanswered = [name for name in assignments if name not in state["answered"]]
        unjudged = observations - sum(sources.values())
        if not unanswered and not state.get("unassigned"):
            status, reason = "completed", None
        else:
            status = "partial" if sum(sources.values()) else "failed"
            reason = stop or ("calls_failed" if unanswered else "ordinals_exhausted")
        self._step(relevance.STEP, status=status, reason=reason, finished_at=utcnow().isoformat(),
                   batches=len(assignments), judged=sources, unjudged=unjudged)

    # -- shared by tag and cluster ---------------------------------------------

    def _step_names(self, evidence: Mapping[str, Any], step: str) -> list[str]:
        """The ``<step>:<i>`` names this run's admission reserved, by ordinal."""
        names = [call["call_name"] for call in _admission_for(evidence, self.job_id).get("llm_calls") or []
                 if call.get("step") == step]
        return sorted(names, key=_ordinal)

    def _release_outside(self, session, evidence: dict, step: str, keep) -> list[str]:
        """Release this run's ``step`` ordinals not in ``keep``; record them on the step and the ledger."""
        released = [
            name for name in self._step_names(evidence, step)
            if name not in keep and release_llm_chat_reservation(session, self.ctx, job_id=self.job_id,
                                                                 call_name=name)
        ]
        state = evidence.setdefault(step, {})
        state["released"] = sorted(set(state.get("released") or []) | set(released), key=_ordinal)
        ledger = dict(evidence.get("released") or {})
        ledger[self.job_id] = sorted(set(ledger.get(self.job_id) or []) | set(released))
        evidence["released"] = ledger
        return released

    def _relevant_ads(self, session) -> list[CompetitorAdRecord]:
        judged = relevance.latest_by_ad(session, self.ctx, self.scan_id)
        ids = [ad_id for ad_id, row in judged.items() if row.relevance == "relevant"]
        return list(session.scalars(select(CompetitorAdRecord).where(
            CompetitorAdRecord.workspace_id == self.ctx.workspace_id,
            CompetitorAdRecord.scan_id == self.scan_id,
            CompetitorAdRecord.id.in_(ids or [""]),
        )).all())

    def _roles(self, session, record: CompetitorScanRecord) -> dict[str, str | None]:
        origins: dict[str, list[str]] = {}
        for ad_id, origin in session.execute(
            select(CompetitorAdOriginRecord.competitor_ad_id, CompetitorAdOriginRecord.origin).where(
                CompetitorAdOriginRecord.workspace_id == self.ctx.workspace_id,
                CompetitorAdOriginRecord.scan_id == self.scan_id,
            )
        ):
            origins.setdefault(ad_id, []).append(origin)
        pages = dict(session.execute(
            select(CompetitorAdRecord.id, CompetitorAdRecord.advertiser_page_id).where(
                CompetitorAdRecord.workspace_id == self.ctx.workspace_id,
                CompetitorAdRecord.scan_id == self.scan_id,
            )
        ).all())
        return snapshots.roles_of(record, origins, pages)

    @staticmethod
    def _llm_failure(exc: Exception, job_id: str, budget_code: str) -> dict:
        return {"status": "failed", "job_id": job_id, "error_code": getattr(exc, "code", budget_code),
                "retryable": bool(getattr(exc, "retryable", False)),
                "dispatch_certainty": getattr(exc, "dispatch_certainty", "not_dispatched"),
                "attempt_id": getattr(exc, "attempt_id", None)}

    # -- tag -----------------------------------------------------------------------

    async def _tag(self, mode: str, prior_stop: str | None) -> str | None:
        """One vision call per frozen table entry; ordinals outside the table released."""
        self._begin(tags.STEP)
        if prior_stop is not None:
            # The same refusal would stop every call; the table is frozen when they can run.
            self._close_tag(prior_stop, skipped_calls=True)
            return prior_stop
        pending = self._freeze_tag()
        stop = None
        for call_name in pending:
            code = await self._tag_one(mode, call_name)
            if code in STOPPING_LLM_CODES:
                stop = code
                break
        self._close_tag(stop)
        return stop

    def _freeze_tag(self) -> list[str]:
        """Freeze the table: this run adds the relevant observations no entry
        holds (longest running first) to its own free ordinals, once per Job;
        the names this run still has to answer, in order."""
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            state = dict(evidence.get(tags.STEP) or {})
            # Admission froze it (the API reads every table it is built from; the Worker cannot).
            vocabulary = state.get("vocabulary") or {}
            state.setdefault("language", (record.params_json or {}).get("language") or "en")
            assignments = dict(state.get("assignments") or {})
            frozen_by = list(state.get("frozen_by") or [])
            if (vocabulary.get("factors") or []) and self.job_id not in frozen_by:
                tagged = set(relevance.latest_by_ad(session, self.ctx, self.scan_id, kind=tags.KIND))
                assigned = {entry["ad_id"] for entry in assignments.values()}
                room = max(0, int((record.params_json or {}).get("max_tagged_ads") or 0) - len(assignments))
                waiting = [ad for ad in tags.rank(self._relevant_ads(session))
                           if ad.id not in assigned and ad.id not in tagged][:room]
                free = [name for name in self._step_names(evidence, tags.STEP) if name not in assignments]
                now = utcnow().isoformat()
                for name, ad in zip(free, waiting):
                    assignments[name] = {"ad_id": ad.id, "ad_archive_id": ad.ad_archive_id,
                                         "days_running": ad.days_running, "job_id": self.job_id, "assigned_at": now}
                state["unassigned"] = max(0, len(waiting) - len(free))
                frozen_by.append(self.job_id)
            state.update(
                assignments=dict(sorted(assignments.items(), key=lambda item: _ordinal(item[0]))),
                frozen_by=frozen_by, answered=list(state.get("answered") or []), calls=dict(state.get("calls") or {}),
            )
            evidence[tags.STEP] = state
            record.evidence_json = evidence
            record.updated_at = utcnow()
            mine = set(self._step_names(evidence, tags.STEP))
            return [name for name in state["assignments"] if name not in state["answered"] and name in mine]

    async def _tag_one(self, mode: str, call_name: str) -> str | None:
        """One observation: its call (or replay), then its tags revision and ``answered`` in one transaction."""
        evidence, _ = self._evidence()
        state = evidence[tags.STEP]
        ad_id = state["assignments"][call_name]["ad_id"]
        with tenant_session(self.ctx) as session:
            ad = session.scalars(select(CompetitorAdRecord).where(
                CompetitorAdRecord.workspace_id == self.ctx.workspace_id, CompetitorAdRecord.id == ad_id,
            )).one()
            session.expunge(ad)
            already = ad_id in relevance.latest_by_ad(session, self.ctx, self.scan_id, kind=tags.KIND)
        if already:
            # A member tagged it first: nothing to ask, so the ordinal is answered and freed unbilled.
            with tenant_session(self.ctx) as session:
                record = scan_service.lock_scan(session, self.ctx, self.scan_id)
                evidence = copy.deepcopy(record.evidence_json or {})
                step = evidence[tags.STEP]
                if call_name not in step["answered"]:
                    step["answered"].append(call_name)
                step.setdefault("calls", {})[call_name] = {"status": "superseded", "job_id": self.job_id}
                release_llm_chat_reservation(session, self.ctx, job_id=self.job_id, call_name=call_name)
                record.evidence_json = evidence
                record.updated_at = utcnow()
            return None
        outcomes = ((evidence.get("media") or {}).get("items") or {}).get(ad_id) or []
        try:
            request, images = tags.build_request(ad, state["vocabulary"], tags.card_images(ad, outcomes),
                                                 language=state["language"], execution_mode=mode)
            result = await self.llm_client.chat(
                self.ctx, job_id=self.job_id, operation_id=self.operation_id, call_name=call_name,
                request=request, execution_mode=mode,
            )
        except (LlmChatError, tags.TagBudgetError) as exc:
            failure = self._llm_failure(exc, self.job_id, "competitor_tag_budget")
            logger.warning("competitor_tag_failed", extra={"scan_id": self.scan_id, "call_name": call_name,
                                                            "error_code": failure["error_code"]})
            self._update(lambda e: e[tags.STEP].setdefault("calls", {}).__setitem__(call_name, failure))
            return failure["error_code"]
        answer = tags.normalize(result.data, state["vocabulary"])
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            step = evidence[tags.STEP]
            written = 0
            if ad_id not in relevance.latest_by_ad(session, self.ctx, self.scan_id, kind=tags.KIND):
                relevance.append(
                    session, self.ctx, scan_id=self.scan_id, ad_id=ad_id, kind=tags.KIND, source="ai", relevance=None,
                    payload={**answer, "language": step["language"], "vocabulary_hash": step["vocabulary_hash"],
                             "images": [{"card_index": image["card_index"], "derivative": image["derivative"]}
                                        for image in images]},
                    evidence={
                        "job_id": self.job_id, "call_name": call_name, "execution_mode": mode,
                        "provider": result.provider, "model": result.model, "model_version": result.model_version,
                        "prompt_version": tags.PROMPT_VERSION, "replayed": result.replayed,
                    },
                    attempt_id=result.attempt_id, key_hash=tags.natural_key(self.scan_id, ad_id, "ai"),
                    fingerprint=tags.fingerprint(answer["factors"]),
                )
                written = 1
            if call_name not in step["answered"]:
                step["answered"].append(call_name)
            step.setdefault("calls", {})[call_name] = {
                "status": "answered", "job_id": self.job_id, "attempt_id": result.attempt_id,
                "images": len(images), "tagged": written, "replayed": result.replayed,
            }
            record.evidence_json = evidence
            record.updated_at = utcnow()
        return None

    def _close_tag(self, stop: str | None, *, skipped_calls: bool = False) -> None:
        """Release the ordinals outside the table, then record how the step went."""
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            state = evidence.setdefault(tags.STEP, {})
            assignments = state.get("assignments") or {}
            if not skipped_calls:
                self._release_outside(session, evidence, tags.STEP, assignments)
            tagged = relevance.latest_by_ad(session, self.ctx, self.scan_id, kind=tags.KIND)
            record.evidence_json = evidence
            record.updated_at = utcnow()
        answered = set(state.get("answered") or [])
        unanswered = [name for name in assignments if name not in answered]
        vocabulary = state.get("vocabulary")
        if skipped_calls:
            status, reason = "failed", stop
        elif vocabulary is None:
            status, reason = "skipped", "vocabulary_not_frozen"  # admitted by a release before #272
        elif not (vocabulary.get("factors") or []):
            status, reason = "skipped", "no_vocabulary"
        elif not assignments:
            status, reason = "skipped", "no_relevant_observations"
        elif not unanswered and not state.get("unassigned"):
            status, reason = "completed", None
        else:
            status = "partial" if answered & set(assignments) else "failed"
            reason = stop or ("calls_failed" if unanswered else "ordinals_exhausted")
        self._step(tags.STEP, status=status, reason=reason, finished_at=utcnow().isoformat(),
                   entries=len(assignments), tagged=len(tagged), untagged=len(unanswered))

    # -- cluster ---------------------------------------------------------------------

    async def _cluster(self, mode: str, prior_stop: str | None) -> None:
        """``cluster:1`` over the frozen relevant groups; the answer is a new clusters revision."""
        evidence, _ = self._evidence()
        if clusters.superseded_by_recluster(evidence):
            # A completed recluster's revision stands (#353): the step's record is left as it was,
            # and a ``cluster:1`` this run's admission reserved is released when the run finishes.
            return
        self._begin(clusters.STEP)
        if prior_stop is not None:
            self._close_cluster("failed", prior_stop)
            return
        evidence, _ = self._evidence()
        state = evidence.get(clusters.STEP) or {}
        if clusters.CALL_NAME in (state.get("answered") or []):
            # An earlier run answered it: read from the evidence, never asked again (a recluster asks anew).
            self._close_cluster("completed", "answered", release=True)
            return
        if clusters.CALL_NAME not in self._step_names(evidence, clusters.STEP):
            self._close_cluster("failed", "reservation_missing")
            return
        groups = self._freeze_cluster()
        if not groups:
            self._close_cluster("skipped", "no_relevant_observations", release=True)
            return
        code = await self._clusters_call(
            mode, job_id=self.job_id, operation_id=self.operation_id, groups=groups, trigger="scan",
            created_by=None,
        )
        if code is None:
            def answered(evidence: dict) -> None:
                if clusters.CALL_NAME not in evidence[clusters.STEP]["answered"]:
                    evidence[clusters.STEP]["answered"].append(clusters.CALL_NAME)

            self._update(answered)
            self._close_cluster("completed", None)
        else:
            self._close_cluster("failed", code)

    def _freeze_cluster(self) -> list[dict]:
        """The relevant groups ``cluster:1`` sends, frozen before the first call."""
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            state = dict(evidence.get(clusters.STEP) or {})
            state.setdefault("language", (record.params_json or {}).get("language") or "en")
            assignments = dict(state.get("assignments") or {})
            if clusters.CALL_NAME not in assignments:
                groups = clusters.cluster_groups(self._relevant_ads(session))
                if groups:
                    assignments[clusters.CALL_NAME] = {"groups": groups, "job_id": self.job_id,
                                                       "assigned_at": utcnow().isoformat()}
            state.update(assignments=assignments, answered=list(state.get("answered") or []),
                         calls=dict(state.get("calls") or {}))
            evidence[clusters.STEP] = state
            record.evidence_json = evidence
            record.updated_at = utcnow()
            return list((assignments.get(clusters.CALL_NAME) or {}).get("groups") or [])

    async def _clusters_call(self, mode: str, *, job_id: str, operation_id: str, groups: list[dict],
                             trigger: str, created_by: str | None) -> str | None:
        """Run (or replay) ``cluster:1`` of ``job_id`` and append its revision; the failing code, if any."""
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            language = (record.params_json or {}).get("language") or "en"
            ids = [ad_id for group in groups for ad_id in group["ad_ids"]]
            ads = {ad.id: ad for ad in session.scalars(select(CompetitorAdRecord).where(
                CompetitorAdRecord.workspace_id == self.ctx.workspace_id, CompetitorAdRecord.id.in_(ids or [""]),
            ))}
            roles = self._roles(session, record)
            for ad in ads.values():
                session.expunge(ad)
        try:
            request, items = clusters.build_request(groups, ads, roles, language=language, execution_mode=mode)
            result = await self.llm_client.chat(
                self.ctx, job_id=job_id, operation_id=operation_id, call_name=clusters.CALL_NAME,
                request=request, execution_mode=mode,
            )
        except (LlmChatError, clusters.ClusterBudgetError) as exc:
            failure = self._llm_failure(exc, job_id, "competitor_cluster_budget")
            # Only the unknown-ID refusal's detail is logged: it names item IDs, never ad content (#345).
            detail = (getattr(exc, "detail", "") if failure["error_code"] == "competitor_clusters_items_mismatch"
                      else "")
            logger.warning("competitor_cluster_failed error_code=%s%s", failure["error_code"],
                           f" {detail}" if detail else "",
                           extra={"scan_id": self.scan_id, "job_id": job_id, "trigger": trigger,
                                  "error_code": failure["error_code"], "error_detail": detail or None})
            if trigger == "scan":
                self._update(lambda e: e[clusters.STEP].setdefault("calls", {}).__setitem__(clusters.CALL_NAME,
                                                                                         failure))
            return failure["error_code"]
        found = clusters.claim_clusters(result.data, groups, ads, roles)
        repeated = clusters.repeated_items(result.data, groups)
        if repeated:
            answered = len(result.data.get("clusters") or [])
            logger.warning("competitor_clusters_deduplicated repeated_items=%d answer_clusters=%d clusters=%d",
                           len(repeated), answered, len(found),
                           extra={"scan_id": self.scan_id, "job_id": job_id, "trigger": trigger,
                                  "repeated_items": len(repeated), "answer_clusters": answered,
                                  "clusters": len(found), "replayed": result.replayed})
        with tenant_session(self.ctx) as session:
            scan_service.lock_scan(session, self.ctx, self.scan_id)
            key = clusters.natural_key(self.scan_id, job_id, clusters.CALL_NAME)
            row = clusters.by_key(session, self.ctx, self.scan_id, key)
            if row is None:
                row = clusters.append(
                    session, self.ctx, scan_id=self.scan_id, job_id=job_id, attempt_id=result.attempt_id,
                    payload={"claim_clusters": found, "conclusion": clusters.conclusion(result.data),
                             "language": language, "items": len(items),
                             "ad_ids": sorted(ids)},
                    evidence={"job_id": job_id, "call_name": clusters.CALL_NAME, "trigger": trigger,
                              "execution_mode": mode, "provider": result.provider, "model": result.model,
                              "model_version": result.model_version, "prompt_version": clusters.PROMPT_VERSION,
                              "validation_policy": clusters.VALIDATION_POLICY, "deduplicated_items": repeated,
                              "replayed": result.replayed},
                    key_hash=key, fingerprint=key, created_by=created_by,
                )
            if trigger == "scan":
                record = scan_service.lock_scan(session, self.ctx, self.scan_id)
                evidence = copy.deepcopy(record.evidence_json or {})
                evidence[clusters.STEP].setdefault("calls", {})[clusters.CALL_NAME] = {
                    "status": "answered", "job_id": job_id, "attempt_id": result.attempt_id, "items": len(items),
                    "clusters": len(found), "revision": row.revision, "replayed": result.replayed,
                }
                record.evidence_json = evidence
                record.updated_at = utcnow()
        return None

    def _close_cluster(self, status: str, reason: str | None, *, release: bool = False) -> None:
        if release:
            with tenant_session(self.ctx) as session:
                record = scan_service.lock_scan(session, self.ctx, self.scan_id)
                evidence = copy.deepcopy(record.evidence_json or {})
                self._release_outside(session, evidence, clusters.STEP, ())
                record.evidence_json = evidence
                record.updated_at = utcnow()
        with tenant_session(self.ctx) as session:
            latest = clusters.latest(session, self.ctx, self.scan_id)
        self._step(clusters.STEP, status=status, reason=reason, finished_at=utcnow().isoformat(),
                   revision=latest.revision if latest is not None else None)

    # -- freeze ------------------------------------------------------------------------

    def _freeze(self) -> None:
        """Free and pure: the snapshot of the revisions in effect (an existing one if unchanged)."""
        step = "freeze"
        self._begin(step)
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            row, created, refusal = snapshots.freeze(session, self.ctx, record, job_id=self.job_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            state = dict(evidence.get(step) or {})
            if row is None:
                state.update(status="skipped", reason=refusal)
            else:
                state.update(status="completed", reason=None, snapshot_id=row.id, hash=row.hash, created=created)
            state["finished_at"] = utcnow().isoformat()
            evidence[step] = state
            record.evidence_json = evidence
            record.updated_at = utcnow()
            _step_event(session, record, step, state, self.job_id)

    # -- discover (keywords only) --------------------------------------------------------

    async def _discover(self, mode: str, prior_stop: str | None) -> None:
        """One text call per advertiser of the frozen table; ordinals outside it released."""
        evidence, record = self._evidence()
        if not (record.params_json or {}).get("keywords"):
            return  # no keyword, no discovery: nothing was reserved for it
        if not self._step_names(evidence, discovery.STEP) and (evidence.get(discovery.STEP) or {}).get("frozen_by"):
            return  # an earlier run's table stands, and this run reserved nothing for it
        self._begin(discovery.STEP)
        if prior_stop is not None:
            # The same refusal would stop every call; the table is frozen when they can run.
            self._close_discover(prior_stop, skipped_calls=True)
            return
        pending = self._freeze_discover()
        stop = None
        for call_name in pending:
            code = await self._discover_one(mode, call_name)
            if code in STOPPING_LLM_CODES:
                stop = code
                break
        self._close_discover(stop)

    def _freeze_discover(self) -> list[str]:
        """Freeze the table: this run adds the advertisers no entry holds (most
        observations first) to its own free ordinals, once per Job; the names
        this run still has to answer, in order."""
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            state = dict(evidence.get(discovery.STEP) or {})
            state.setdefault("language", (record.params_json or {}).get("language") or "en")
            if "product" not in state:
                state["product"] = (evidence.get(relevance.STEP) or {}).get("product") or relevance.product_context(
                    session, self.ctx, record.product_id)
            assignments = dict(state.get("assignments") or {})
            frozen_by = list(state.get("frozen_by") or [])
            if self.job_id not in frozen_by:
                excluded = discovery.excluded_pages(session, self.ctx, record.product_id)
                ads = session.scalars(select(CompetitorAdRecord).where(
                    CompetitorAdRecord.workspace_id == self.ctx.workspace_id,
                    CompetitorAdRecord.scan_id == self.scan_id,
                )).all()
                origins: dict[str, list[str]] = {}
                for ad_id, origin in session.execute(
                    select(CompetitorAdOriginRecord.competitor_ad_id, CompetitorAdOriginRecord.origin).where(
                        CompetitorAdOriginRecord.workspace_id == self.ctx.workspace_id,
                        CompetitorAdOriginRecord.scan_id == self.scan_id,
                    )
                ):
                    origins.setdefault(ad_id, []).append(origin)
                candidates = discovery.advertisers(
                    ads, origins, excluded=[*excluded["competitor_pages"], *excluded["rejected_pages"]],
                )
                assigned = {entry["page_id"] for entry in assignments.values()}
                room = max(0, int((record.params_json or {}).get("max_discover_advertisers") or 0) - len(assignments))
                fresh = [candidate for candidate in candidates if candidate["page_id"] not in assigned]
                waiting = fresh[:room]
                free = [name for name in self._step_names(evidence, discovery.STEP) if name not in assignments]
                now = utcnow().isoformat()
                for name, candidate in zip(free, waiting):
                    assignments[name] = {**candidate, "job_id": self.job_id, "assigned_at": now}
                state.update(
                    candidates=len(candidates), over_cap=len(fresh) - len(waiting),
                    unassigned=max(0, len(waiting) - len(free)),
                    excluded={key: len(value) for key, value in excluded.items()},
                )
                frozen_by.append(self.job_id)
            state.update(
                assignments=dict(sorted(assignments.items(), key=lambda item: _ordinal(item[0]))),
                frozen_by=frozen_by, answered=list(state.get("answered") or []), calls=dict(state.get("calls") or {}),
            )
            evidence[discovery.STEP] = state
            record.evidence_json = evidence
            record.updated_at = utcnow()
            mine = set(self._step_names(evidence, discovery.STEP))
            return [name for name in state["assignments"] if name not in state["answered"] and name in mine]

    async def _discover_one(self, mode: str, call_name: str) -> str | None:
        """One advertiser: its call (or replay), then its answer and ``answered`` in one transaction."""
        evidence, _ = self._evidence()
        state = evidence[discovery.STEP]
        entry = state["assignments"][call_name]
        with tenant_session(self.ctx) as session:
            ads = list(session.scalars(select(CompetitorAdRecord).where(
                CompetitorAdRecord.workspace_id == self.ctx.workspace_id,
                CompetitorAdRecord.id.in_(entry["ad_ids"] or [""]),
            )).all())
            for ad in ads:
                session.expunge(ad)
        try:
            request = discovery.build_request(entry, ads, state["product"], language=state["language"],
                                              execution_mode=mode)
            result = await self.llm_client.chat(
                self.ctx, job_id=self.job_id, operation_id=self.operation_id, call_name=call_name,
                request=request, execution_mode=mode,
            )
        except (LlmChatError, discovery.DiscoveryBudgetError) as exc:
            failure = self._llm_failure(exc, self.job_id, "competitor_discovery_budget")
            logger.warning("competitor_discovery_failed", extra={"scan_id": self.scan_id, "call_name": call_name,
                                                                  "error_code": failure["error_code"]})
            self._update(lambda e: e[discovery.STEP].setdefault("calls", {}).__setitem__(call_name, failure))
            return failure["error_code"]
        answer = discovery.normalize(result.data)

        def answered(evidence: dict) -> None:
            step = evidence[discovery.STEP]
            if call_name not in step["answered"]:
                step["answered"].append(call_name)
            step.setdefault("calls", {})[call_name] = {
                "status": "answered" if answer is not None else "invalid", "job_id": self.job_id,
                "attempt_id": result.attempt_id, "relevance": (answer or {}).get("relevance"),
                "reason": (answer or {}).get("reason"), "language": step["language"], "execution_mode": mode,
                "provider": result.provider, "model": result.model, "model_version": result.model_version,
                "prompt_version": discovery.PROMPT_VERSION, "replayed": result.replayed,
            }

        self._update(answered)
        return None

    def _close_discover(self, stop: str | None, *, skipped_calls: bool = False) -> None:
        """Release the ordinals outside the table, then record how the step went."""
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            state = evidence.setdefault(discovery.STEP, {})
            assignments = state.get("assignments") or {}
            if not skipped_calls:
                self._release_outside(session, evidence, discovery.STEP, assignments)
            record.evidence_json = evidence
            record.updated_at = utcnow()
        answered = set(state.get("answered") or [])
        calls = state.get("calls") or {}
        unanswered = [name for name in assignments if name not in answered]
        recommended = sum(1 for name in answered if (calls.get(name) or {}).get("relevance") in discovery.RECOMMENDED)
        if skipped_calls:
            status, reason = "failed", stop
        elif not assignments:
            status, reason = "skipped", "no_candidates" if not state.get("candidates") else "ordinals_exhausted"
        elif not unanswered and not state.get("unassigned"):
            status, reason = "completed", None
        else:
            status = "partial" if answered & set(assignments) else "failed"
            reason = stop or ("calls_failed" if unanswered else "ordinals_exhausted")
        self._step(discovery.STEP, status=status, reason=reason, finished_at=utcnow().isoformat(),
                   advertisers=len(assignments), judged=len(answered & set(assignments)), recommended=recommended)

    # -- recluster (its own Job) ---------------------------------------------------------

    async def recluster(self) -> ScanOutcome:
        """The member's paid recluster: ``cluster:1`` of this Job over the
        relevant groups frozen into its entry, a new revision, one freeze."""
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            if record is None:
                return ScanOutcome("failed", "competitor_scan_not_found", "the scan no longer exists")
            evidence = copy.deepcopy(record.evidence_json or {})
            entry = next((item for item in evidence.get("reclusters") or [] if item.get("job_id") == self.job_id), None)
            if entry is None:
                return ScanOutcome("failed", "competitor_scan_not_found", "no recluster was admitted for this Job")
            if entry.get("status") == "completed":
                return ScanOutcome("completed")
            if entry.get("status") == "failed":
                return ScanOutcome("failed", (entry.get("failure") or {}).get("code") or "competitor_scan_failed")
            if not entry.get("groups"):
                entry["groups"] = clusters.cluster_groups(self._relevant_ads(session))
            entry.update(status="running", started_at=entry.get("started_at") or utcnow().isoformat())
            record.evidence_json = evidence
            record.updated_at = utcnow()
            mode, groups = record.execution_mode, list(entry["groups"])
            operation_id, requested_by = entry["operation_id"], entry.get("requested_by")
        self.operation_id = operation_id
        if groups:
            code = await self._clusters_call(mode, job_id=self.job_id, operation_id=operation_id, groups=groups,
                                             trigger="recluster", created_by=requested_by)
        else:
            code = "competitor_scan_nothing_to_cluster"
        with tenant_session(self.ctx) as session:
            record = scan_service.lock_scan(session, self.ctx, self.scan_id)
            snapshot = None
            if code is None:
                snapshot, created, _refusal = snapshots.freeze(session, self.ctx, record, job_id=self.job_id)
            evidence = copy.deepcopy(record.evidence_json or {})
            entry = next(item for item in evidence["reclusters"] if item.get("job_id") == self.job_id)
            released = [call["call_name"] for call in entry.get("llm_calls") or []
                        if release_llm_chat_reservation(session, self.ctx, job_id=self.job_id,
                                                        call_name=call["call_name"])]
            latest = clusters.latest(session, self.ctx, self.scan_id)
            entry.update(
                status="completed" if code is None else "failed", finished_at=utcnow().isoformat(),
                failure=None if code is None else {"code": code},
                cluster_assessment_id=latest.id if code is None and latest is not None else None,
                cluster_revision=latest.revision if code is None and latest is not None else None,
                snapshot_id=snapshot.id if snapshot is not None else None, released=released,
            )
            record.evidence_json = evidence
            record.updated_at = utcnow()
            session.flush()
            record.cost_json = scan_cost(session, self.ctx, record)
            audit.append_event(
                session, event_type="competitor_scan.reclustered", resource_type=scan_service.RESOURCE_TYPE,
                resource_id=record.id,
                details={"job_id": self.job_id, "status": entry["status"], "failure_code": code,
                         "cluster_revision": entry["cluster_revision"], "snapshot_id": entry["snapshot_id"]},
            )
        return ScanOutcome("completed") if code is None else ScanOutcome("failed", code)


# ---------------------------------------------------------------------------
# Owner reconciliation
# ---------------------------------------------------------------------------


def settle_owner_marks(ctx: TenantContext, scan_id: str, job_id: str) -> ScanOutcome:
    """Settle every page the Owner marked and nobody settled yet; re-sum the cost."""
    try:
        prices = price_table(SCRAPECREATORS)
    except MarketDataPricingError:
        prices = None
    with tenant_session(ctx) as session:
        record = scan_service.lock_scan(session, ctx, scan_id)
        if record is None:
            return ScanOutcome("failed", "competitor_scan_not_found")
        evidence = copy.deepcopy(record.evidence_json or {})
        for entry in evidence.get("reconciliation") or []:
            if entry.get("source") != "owner" or entry.get("settled") or entry.get("status") == "pending":
                continue
            scan_reconcile.settle_uncertain_call(
                session, ctx, attempt_id=entry["attempt_id"], charged=entry["status"] == "charged",
                prices=prices, pricing_version=config.MARKETAI_PRICE_REGISTRY_VERSION, source="owner",
            )
            entry["settled"] = True
        record.evidence_json = evidence
        record.updated_at = utcnow()
        session.flush()
        record.cost_json = scan_cost(session, ctx, record)
        session.flush()
        return ScanOutcome("completed")


# ---------------------------------------------------------------------------
# Job fence
# ---------------------------------------------------------------------------


def mark_failed(ctx: TenantContext, scan_id: str, job_id: str, *, code: str, detail: str) -> None:
    """Record an unexpected fault on the scan and release what this run reserved."""
    with tenant_session(ctx) as session:
        record = scan_service.lock_scan(session, ctx, scan_id)
        if record is not None and record.job_id == job_id and record.status not in scan_service.FINISHED:
            finish_scan(session, ctx, record, job_id=job_id, code=code, detail=detail)


def mark_recluster_failed(ctx: TenantContext, scan_id: str, job_id: str, *, code: str) -> None:
    """Record an unexpected fault on a recluster and release its reservation."""
    with tenant_session(ctx) as session:
        record = scan_service.lock_scan(session, ctx, scan_id)
        if record is None:
            return
        evidence = copy.deepcopy(record.evidence_json or {})
        for entry in evidence.get("reclusters") or []:
            if entry.get("job_id") == job_id and entry.get("status") in ("queued", "running"):
                for call in entry.get("llm_calls") or []:
                    release_llm_chat_reservation(session, ctx, job_id=job_id, call_name=call["call_name"])
                entry.update(status="failed", failure={"code": code}, finished_at=utcnow().isoformat())
        record.evidence_json = evidence
        record.updated_at = utcnow()


def _protector(runtime) -> CredentialProtector:
    protector = getattr(runtime, "_market_data_protector", None)
    if protector is None:
        from app.entitlements.credential_protector import LocalKmsAdapter

        protector = LocalKmsAdapter.local_only()
        runtime._market_data_protector = protector
    return protector


async def handle_scan_job(runtime, job) -> None:
    """Fence for one scan run; the aggregated Operation folds from the Job.

    ``completed`` and ``partial`` succeed (the scan holds what was found);
    ``failed`` fails (a resume is a new paid command); ``uncertain`` is never
    retried blindly; a wait for the key's turn re-queues the Job.
    """
    scan_id = str(job.payload.get("scan_id") or "")
    action = str(job.payload.get("action") or "run")
    if not scan_id or action not in ("run", "reconcile", "recluster"):
        jobs.fail(job.context, job.id, runtime.owner, code="missing_input",
                  detail="scan_id missing or action unknown", retryable=False)
        return
    jobs.mark_running(job.context, job.id, runtime.owner)
    heartbeat = asyncio.create_task(runtime._heartbeat(job.context, job.id))
    try:
        if action == "reconcile":
            outcome = settle_owner_marks(job.context, scan_id, job.id)
        elif action == "recluster":
            outcome = await ScanRun(
                job.context, scan_id=scan_id, job_id=job.id, coordinator=runtime._localization_coordinator,
                protector=_protector(runtime), blob_store=runtime.blob_store, llm_client=runtime.llm_chat_client(),
            ).recluster()
        else:
            outcome = await ScanRun(
                job.context,
                scan_id=scan_id,
                job_id=job.id,
                coordinator=runtime._localization_coordinator,
                protector=_protector(runtime),
                blob_store=runtime.blob_store,
                source_factory=getattr(runtime, "ad_library_source", None) or default_source,
                fetcher_factory=getattr(runtime, "competitor_media_fetcher", None) or scan_media.default_fetcher,
                balance_checker=getattr(runtime, "market_data_balance_checker", None) or default_balance,
                retry_delays=getattr(runtime, "competitor_scan_retry_delays", None) or RETRY_DELAYS,
                final_attempt=job.attempt >= job.max_attempts,
                llm_client=runtime.llm_chat_client(),
            ).run()
    except Exception as exc:  # noqa: BLE001 -- recorded on the scan, never a crash loop
        logger.exception("competitor_scan_failed", extra={"workspace_id": job.context.workspace_id, "scan_id": scan_id})
        if action == "run":
            mark_failed(job.context, scan_id, job.id, code="internal_error", detail=type(exc).__name__)
        elif action == "recluster":
            mark_recluster_failed(job.context, scan_id, job.id, code="internal_error")
        jobs.fail(job.context, job.id, runtime.owner, code="competitor_scan_failed",
                  detail=str(exc)[:500], retryable=False)
        return
    finally:
        heartbeat.cancel()
    if outcome.kind in ("completed", "partial", "superseded"):
        jobs.succeed(job.context, job.id, runtime.owner)
    elif outcome.kind == "wait":
        jobs.fail(job.context, job.id, runtime.owner, code=outcome.code, detail=outcome.detail,
                  retryable=True, retry_after_seconds=TURN_RETRY_SECONDS)
    elif outcome.kind == "uncertain":
        jobs.mark_uncertain(job.context, job.id, runtime.owner, code=outcome.code, detail=outcome.detail)
    else:
        jobs.fail(job.context, job.id, runtime.owner, code=outcome.code or "competitor_scan_failed",
                  detail=outcome.detail, retryable=False)
