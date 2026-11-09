"""The product-intake Worker job: fetch -> extract -> images (Issue #161).

``app.worker_runtime.WorkerRuntime._products_intake`` owns the Job lifecycle
(lease, retry, Operation folding); :func:`run_product_intake` owns the three
steps and their evidence. Each step records ``{status, started_at,
finished_at, duration_ms, attempts, ...}`` under ``evidence_json[step]`` and
appends one ``product_intake.step`` audit event, in short tenant
transactions -- no transaction is held open across network or blob I/O.

Retries are safe by construction:

* **fetch** -- skipped once it succeeded (its committed image candidates and
  the bounded page digest are on the row); a transient failure (timeout,
  5xx, 429, DNS, connection) is retried by the Job and only becomes
  ``fetch_failed`` on the last attempt; a refusal (blocked address, 403,
  404, wrong content type) is final at once.
* **extract** -- one governed ``llm_chat`` call keyed by (Job, ``extract``):
  a re-run Job replays the recorded answer or failure instead of paying
  again, so LLM failures are final for this intake (the user starts a new
  one; failed intakes are not replayed by the 24h input dedupe).
* **images** -- gating is idempotent per content hash (reference objects
  are reused, a released one is not re-gated).
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import io
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import ProductIntakeRecord, ReferenceObjectRecord, utcnow
from app.platform import audit
from app.platform.blob_store import BlobStore, BlobStoreError, CommittedBlobRef, GeneratedArtifactSpec, extension_for_mime
from app.platform.tenancy import TenantContext
from app.providers.llm_chat import LlmChatClient, LlmChatError, LlmChatRequest, LlmImage, llm_call_evidence

from . import intake_extraction as extraction
from .intake_fetch import MAX_IMAGES, FetchRefused, SafeFetcher, sniff_image
from .intake_html import ImageCandidateRef, page_digest_text, parse_product_page
from app.platform.runtime_role import WORKER_ROLE, runs_as

#: Images that go through the reference gate (and can be bound on confirm).
MAX_GATED_IMAGES = 8
MIN_IMAGE_SIDE = 200
MAX_IMAGE_ASPECT = 3.0
IMAGE_FETCH_CONCURRENCY = 4
MIN_PAGE_TEXT_CHARS = 200
IMAGE_PURPOSE = "product_intake_image"

#: LLM answers that exist but cannot be used: the step's "schema failure".
_UNUSABLE_ANSWER_CODES = frozenset({
    "schema_validation_failed", "invalid_json", "malformed_response", "model_refusal",
    "mock_fixture_invalid",
})

#: Statuses after which the Job has nothing left to do.
TERMINAL_JOB_STATUSES = frozenset({"ready", "fetch_failed", "extraction_failed", "failed", "confirmed"})


@dataclass(frozen=True)
class IntakeOutcome:
    """What the Job fence does next: succeed, retry, fail or mark uncertain."""

    kind: str  # ready | retry | failed | uncertain
    code: str = ""
    detail: str = ""


Heartbeat = Callable[[], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _load(session, ctx: TenantContext, intake_id: str) -> ProductIntakeRecord:
    record = session.get(ProductIntakeRecord, intake_id)
    if record is None or record.workspace_id != ctx.workspace_id:
        raise LookupError(f"product intake {intake_id!r} not found in this Workspace")
    return record


def _set_evidence(record: ProductIntakeRecord, step: str, payload: dict) -> None:
    evidence = dict(record.evidence_json or {})
    evidence[step] = payload
    record.evidence_json = evidence


def _audit_step(session, intake_id: str, step: str, payload: dict) -> None:
    details = {
        "step": step,
        "outcome": payload.get("status"),
        "duration_ms": payload.get("duration_ms"),
        "code": payload.get("code"),
        "attempt": payload.get("attempts"),
    }
    if payload.get("blocked_category"):
        details["blocked_category"] = payload["blocked_category"]
    audit.append_event(
        session,
        event_type="product_intake.step",
        resource_type="product_intake",
        resource_id=intake_id,
        details={key: value for key, value in details.items() if value is not None},
    )


def _audit_refusal(session, intake_id: str, *, step: str, url: str, refused: FetchRefused) -> None:
    """An SSRF refusal: the address *category*, never the resolved address."""
    audit.append_event(
        session,
        event_type="product_intake.fetch_refused",
        resource_type="product_intake",
        resource_id=intake_id,
        details={
            "step": step,
            "code": refused.code,
            "blocked_category": refused.category,
            "host": _host_of(url),
        },
    )


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url).hostname or "")[:255]
    except ValueError:
        return ""


def _finish(
    ctx: TenantContext,
    intake_id: str,
    *,
    status: str,
    step: str,
    step_payload: dict,
    failure_code: str | None = None,
    failure_detail: str | None = None,
) -> None:
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        _set_evidence(record, step, step_payload)
        record.status = status
        record.current_step = None if status in TERMINAL_JOB_STATUSES else step
        record.failure_code = failure_code
        record.failure_detail = (failure_detail or None) and failure_detail[:2000]
        now = utcnow()
        record.updated_at = now
        if status in TERMINAL_JOB_STATUSES:
            record.finished_at = now
        _audit_step(session, intake_id, step, step_payload)


def _begin(ctx: TenantContext, intake_id: str, *, status: str, step: str) -> dict:
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        record.status = status
        record.current_step = step
        record.updated_at = utcnow()
        previous = dict((record.evidence_json or {}).get(step) or {})
    return previous


def _step_payload(previous: dict, started: datetime, started_mono: float, **fields) -> dict:
    payload = {
        "attempts": int(previous.get("attempts") or 0) + 1,
        "started_at": _iso(started),
        "finished_at": _iso(_now()),
        "duration_ms": int((time.monotonic() - started_mono) * 1000),
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    return payload


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@runs_as(WORKER_ROLE)
async def run_product_intake(
    ctx: TenantContext,
    *,
    intake_id: str,
    job_id: str,
    operation_id: str,
    fetcher: SafeFetcher,
    llm_client: LlmChatClient,
    blob_store: BlobStore,
    final_attempt: bool,
    heartbeat: Heartbeat | None = None,
) -> IntakeOutcome:
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        status = record.status
        fetch_done = (record.evidence_json or {}).get("fetch", {}).get("status") in ("succeeded", "skipped")
        has_url = bool(record.source_url)
    if status in TERMINAL_JOB_STATUSES:
        return _outcome_for_status(ctx, intake_id)

    if not fetch_done:
        if has_url:
            outcome = await _fetch_step(ctx, intake_id, fetcher, blob_store, final_attempt=final_attempt)
            if outcome is not None:
                return outcome
        else:
            _finish(
                ctx, intake_id, status="fetching", step="fetch",
                step_payload={"status": "skipped", "reason": "no_url"},
            )
    if heartbeat is not None:
        await heartbeat()

    outcome = await _extract_step(
        ctx, intake_id, llm_client, job_id=job_id, operation_id=operation_id,
    )
    if outcome is not None:
        return outcome
    if heartbeat is not None:
        await heartbeat()

    await _images_step(ctx, intake_id, blob_store)
    return IntakeOutcome("ready")


@runs_as(WORKER_ROLE)
def mark_intake_failed(ctx: TenantContext, intake_id: str, *, code: str, detail: str) -> None:
    """Record an unexpected handler fault on the intake (best effort)."""
    try:
        with tenant_session(ctx) as session:
            record = _load(session, ctx, intake_id)
            if record.status in TERMINAL_JOB_STATUSES:
                return
            step = record.current_step or "fetch"
            record.status = "failed"
            record.failure_code = code
            record.failure_detail = detail[:2000]
            now = utcnow()
            record.updated_at = now
            record.finished_at = now
            _audit_step(session, intake_id, step, {"status": "failed", "code": code})
        _release_unused_reservation(ctx, intake_id)
    except Exception:  # noqa: BLE001 -- the Job row still carries the failure
        pass


def _release_unused_reservation(ctx: TenantContext, intake_id: str) -> None:
    """The intake ended before its LLM call: give the reserved maximum back.

    Admission reserved the extraction call's maximum cost; an intake that
    stops at the fetch step never makes that call, so the reservation would
    otherwise hold Spend Guardrail exposure forever. Only a reservation no
    attempt consumed is released (``release_llm_chat_reservation``).
    """
    from app.providers.llm_chat import release_llm_chat_reservation

    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        if record.job_id and record.execution_mode == "byok":
            released = release_llm_chat_reservation(
                session, ctx, job_id=record.job_id, call_name=extraction.CALL_NAME,
            )
            if released:
                audit.append_event(
                    session,
                    event_type="product_intake.reservation_released",
                    resource_type="product_intake",
                    resource_id=intake_id,
                    details={"reason": record.failure_code or record.status},
                )


def _outcome_for_status(ctx: TenantContext, intake_id: str) -> IntakeOutcome:
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        if record.status in ("ready", "confirmed"):
            return IntakeOutcome("ready")
        return IntakeOutcome("failed", record.failure_code or record.status, record.failure_detail or "")


# ---------------------------------------------------------------------------
# Step 1: fetch
# ---------------------------------------------------------------------------


async def _fetch_step(
    ctx: TenantContext,
    intake_id: str,
    fetcher: SafeFetcher,
    blob_store: BlobStore,
    *,
    final_attempt: bool,
) -> IntakeOutcome | None:
    previous = _begin(ctx, intake_id, status="fetching", step="fetch")
    started, started_mono = _now(), time.monotonic()
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        url = record.source_url or ""
        has_other_input = bool(record.input_text) or bool(record.upload_intent_ids)
        existing_candidates = list(record.images_json or [])

    try:
        page = await fetcher.fetch_page(url)
    except FetchRefused as refused:
        payload = _step_payload(
            previous, started, started_mono,
            status="failed" if (final_attempt or not refused.retryable) else "retrying",
            **{key: value for key, value in refused.as_evidence().items() if key != "detail"},
            detail=refused.detail,
        )
        if refused.retryable and not final_attempt:
            with tenant_session(ctx) as session:
                record = _load(session, ctx, intake_id)
                _set_evidence(record, "fetch", payload)
                record.updated_at = utcnow()
                _audit_step(session, intake_id, "fetch", payload)
            return IntakeOutcome("retry", refused.code, refused.detail)
        _finish(
            ctx, intake_id, status="fetch_failed", step="fetch", step_payload=payload,
            failure_code=refused.code, failure_detail=refused.detail,
        )
        _release_unused_reservation(ctx, intake_id)
        if refused.code == "blocked_address":
            with tenant_session(ctx) as session:
                _audit_refusal(session, intake_id, step="fetch", url=url, refused=refused)
        return IntakeOutcome("failed", refused.code, refused.detail)

    refs: list[ImageCandidateRef]
    digest_text = ""
    page_summary: dict = {}
    if page.kind == "image":
        refs = []
        direct_image = page
    else:
        direct_image = None
        html = page.body.decode(_charset(page.body), errors="replace")
        parsed = parse_product_page(html, page.final_url)
        page_summary = parsed.summary()
        digest_text = page_digest_text(parsed, max_chars=extraction.MAX_USER_PROMPT_BYTES // 2)
        refs = parsed.images
        thin = (
            not parsed.has_product_signal
            and len(parsed.main_text) < MIN_PAGE_TEXT_CHARS
            and not parsed.images
        )
        if thin and not has_other_input:
            payload = _step_payload(
                previous, started, started_mono, status="failed", code="not_product_page",
                final_url=page.final_url, http_status=page.status_code,
                content_type=page.content_type, body_sha256=page.sha256,
                body_bytes=len(page.body), truncated=page.truncated,
                redirects=max(0, len(page.hops) - 1), page=page_summary,
            )
            detail = "the page has no product information we can read (it may need JavaScript)"
            _finish(
                ctx, intake_id, status="fetch_failed", step="fetch", step_payload=payload,
                failure_code="not_product_page", failure_detail=detail,
            )
            _release_unused_reservation(ctx, intake_id)
            return IntakeOutcome("failed", "not_product_page", detail)

    candidates, image_report = await _download_images(
        ctx, intake_id, fetcher, blob_store, refs=refs, direct_image=direct_image,
        existing=existing_candidates, page_url=page.final_url,
    )
    payload = _step_payload(
        previous, started, started_mono, status="succeeded",
        final_url=page.final_url, http_status=page.status_code, content_type=page.content_type,
        body_sha256=page.sha256, body_bytes=len(page.body), truncated=page.truncated,
        redirects=max(0, len(page.hops) - 1), page=page_summary or None,
        images=image_report, digest_text=digest_text or None,
    )
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        record.images_json = candidates
        _set_evidence(record, "fetch", payload)
        record.updated_at = utcnow()
        _audit_step(session, intake_id, "fetch", payload)
        for refusal in image_report.get("blocked", []):
            audit.append_event(
                session,
                event_type="product_intake.fetch_refused",
                resource_type="product_intake",
                resource_id=intake_id,
                details={"step": "fetch_image", "code": "blocked_address",
                         "blocked_category": refusal["blocked_category"], "host": refusal["host"]},
            )
    return None


_CHARSET = re.compile(rb"""charset\s*=\s*["']?([A-Za-z0-9_.:-]{1,40})""", re.IGNORECASE)


def _charset(body: bytes) -> str:
    """The page's declared charset (``<meta charset>``), else UTF-8."""
    match = _CHARSET.search(body[:4096])
    if match:
        name = match.group(1).decode("ascii", errors="ignore")
        try:
            return codecs.lookup(name).name
        except LookupError:
            pass
    return "utf-8"


def _image_dimensions(content: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            return int(image.width), int(image.height)
    except Exception:  # noqa: BLE001 -- an undecodable image is simply skipped
        return None


def _committed_key(workspace_id: str, sha256_hex: str, mime: str) -> str:
    now = utcnow()
    return (
        f"committed/{workspace_id}/{IMAGE_PURPOSE}/{now:%Y}/{now:%m}/{now:%d}/"
        f"{sha256_hex}.{extension_for_mime(mime)}"
    )


async def _commit_bytes(blob_store: BlobStore, workspace_id: str, content: bytes, mime: str) -> CommittedBlobRef:
    digest = hashlib.sha256(content)
    with tempfile.TemporaryDirectory(prefix="intake-img-") as tmp:
        path = Path(tmp) / "image"
        path.write_bytes(content)
        return await blob_store.commit_generated_artifact(
            path,
            GeneratedArtifactSpec(
                committed_key=_committed_key(workspace_id, digest.hexdigest(), mime),
                content_type=mime,
                expected_sha256_b64=base64.b64encode(digest.digest()).decode("ascii"),
            ),
        )


async def _download_images(
    ctx: TenantContext,
    intake_id: str,
    fetcher: SafeFetcher,
    blob_store: BlobStore,
    *,
    refs: list[ImageCandidateRef],
    direct_image,
    existing: list[dict],
    page_url: str,
) -> tuple[list[dict], dict]:
    """Download (≤ 12), filter, de-duplicate and commit the page's images.

    Upload candidates already on the row (from the API) keep their place
    first; fetched ones follow in page priority order.
    """
    candidates = [dict(item) for item in existing if item.get("source") == "upload"]
    seen = {item["sha256"] for item in candidates}
    report: dict = {"found": len(refs), "attempted": 0, "kept": 0, "skipped": [], "blocked": []}

    async def fetch_one(ref: ImageCandidateRef):
        try:
            return ref, await fetcher.fetch_image(ref.url), None
        except FetchRefused as refused:
            return ref, None, refused

    results: list = []
    if direct_image is not None:
        results.append((ImageCandidateRef(url=direct_image.final_url, source="direct"), direct_image, None))
    attempts = refs[:MAX_IMAGES]
    report["attempted"] = len(attempts) + (1 if direct_image is not None else 0)
    semaphore = asyncio.Semaphore(IMAGE_FETCH_CONCURRENCY)

    async def bounded(ref):
        async with semaphore:
            return await fetch_one(ref)

    results.extend(await asyncio.gather(*(bounded(ref) for ref in attempts)))

    fetched_count = 0
    for ref, result, refused in results:
        if refused is not None:
            entry = {"url": ref.url[:500], "code": refused.code}
            if refused.code == "blocked_address":
                entry["blocked_category"] = refused.category
                report["blocked"].append({"blocked_category": refused.category, "host": _host_of(ref.url)})
            report["skipped"].append(entry)
            continue
        mime = sniff_image(result.body)
        dimensions = _image_dimensions(result.body)
        if mime is None or dimensions is None:
            report["skipped"].append({"url": ref.url[:500], "code": "undecodable"})
            continue
        width, height = dimensions
        if min(width, height) < MIN_IMAGE_SIDE:
            report["skipped"].append({"url": ref.url[:500], "code": "too_small"})
            continue
        if max(width, height) / max(1, min(width, height)) > MAX_IMAGE_ASPECT:
            report["skipped"].append({"url": ref.url[:500], "code": "bad_aspect"})
            continue
        sha256_hex = hashlib.sha256(result.body).hexdigest()
        if sha256_hex in seen:
            report["skipped"].append({"url": ref.url[:500], "code": "duplicate"})
            continue
        if fetched_count >= MAX_IMAGES:
            break
        try:
            committed = await _commit_bytes(blob_store, ctx.workspace_id, result.body, mime)
        except BlobStoreError as exc:
            report["skipped"].append({"url": ref.url[:500], "code": "storage_unavailable",
                                      "detail": type(exc).__name__})
            continue
        seen.add(sha256_hex)
        fetched_count += 1
        candidates.append({
            "source": "product_page",
            "page_source": ref.source,
            "source_url": ref.url[:2048],
            "page_url": page_url[:2048],
            "alt": ref.alt,
            "sha256": sha256_hex,
            "mime_type": mime,
            "byte_size": len(result.body),
            "width": width,
            "height": height,
            "committed_key": committed.committed_key,
            "storage_version_id": committed.storage_version_id,
        })
    report["kept"] = fetched_count
    report["skipped"] = report["skipped"][:40]
    return _number(candidates), report


def _number(candidates: list[dict]) -> list[dict]:
    for index, candidate in enumerate(candidates, start=1):
        candidate["image_id"] = f"img_{index}"
    return candidates


# ---------------------------------------------------------------------------
# Step 2: extract
# ---------------------------------------------------------------------------


def _blob_ref(candidate: dict) -> CommittedBlobRef:
    return CommittedBlobRef(
        committed_key=candidate["committed_key"],
        storage_version_id=candidate.get("storage_version_id") or "",
        size_bytes=int(candidate.get("byte_size") or 0),
        checksum_sha256_b64=base64.b64encode(bytes.fromhex(candidate["sha256"])).decode("ascii"),
        content_type_verified=candidate.get("mime_type") or "",
    )


def _image_label(item: dict) -> dict:
    """How the prompt names one candidate: id, provenance, page URL, alt text."""
    return {
        "image_id": item["image_id"],
        "source": item.get("source", "upload"),
        "url": item.get("source_url") or "",
        "alt": item.get("alt", ""),
    }


async def _extract_step(
    ctx: TenantContext,
    intake_id: str,
    llm_client: LlmChatClient,
    *,
    job_id: str,
    operation_id: str,
) -> IntakeOutcome | None:
    previous = _begin(ctx, intake_id, status="extracting", step="extract")
    started, started_mono = _now(), time.monotonic()
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        mode = record.execution_mode
        candidates = list(record.images_json or [])
        fetch_evidence = dict((record.evidence_json or {}).get("fetch") or {})
        user_text = record.input_text or ""
        source_url = record.source_url
        only_url = bool(source_url) and not user_text and not record.upload_intent_ids

    shown = candidates[: extraction.MAX_LLM_IMAGES]
    prompt = extraction.build_user_prompt(
        user_text=user_text,
        page_url=fetch_evidence.get("final_url") or source_url,
        page_digest=fetch_evidence.get("digest_text"),
        image_labels=[_image_label(item) for item in shown],
        other_image_labels=[_image_label(item) for item in candidates[extraction.MAX_LLM_IMAGES:]],
    )
    request = LlmChatRequest.build(
        semantic=extraction.SEMANTIC,
        system=extraction.SYSTEM_PROMPT,
        user=prompt,
        json_schema=extraction.PRODUCT_INTAKE_SCHEMA,
        schema_name=extraction.SCHEMA_NAME,
        max_output_tokens=extraction.MAX_OUTPUT_TOKENS,
        images=tuple(LlmImage.from_blob(_blob_ref(item)) for item in shown),
    )
    try:
        result = await llm_client.chat(
            ctx, job_id=job_id, operation_id=operation_id, call_name=extraction.CALL_NAME,
            request=request, execution_mode=mode,
        )
    except LlmChatError as error:
        return _extraction_error(ctx, intake_id, previous, started, started_mono, error)

    call = {
        "attempt_id": result.attempt_id,
        "provider": result.provider,
        "model": result.model,
        "model_version": result.model_version,
        "execution_mode": result.execution_mode,
        "usage": result.usage,
        "cost": result.cost,
        "replayed": result.replayed,
        "images_sent": len(shown),
    }
    try:
        card = extraction.normalize_extraction(
            result.data, candidate_ids=[item["image_id"] for item in candidates],
        )
    except extraction.ExtractionInvalid as invalid:
        payload = _step_payload(
            previous, started, started_mono, status="failed", code="extraction_invalid",
            problems=invalid.problems, **call,
        )
        with tenant_session(ctx) as session:
            record = _load(session, ctx, intake_id)
            record.extraction_json = {"raw_output": result.data, "problems": invalid.problems, **call}
        detail = "the model's answer cannot become a product card: " + "; ".join(invalid.problems)
        _finish(
            ctx, intake_id, status="extraction_failed", step="extract", step_payload=payload,
            failure_code="extraction_invalid", failure_detail=detail,
        )
        return IntakeOutcome("failed", "extraction_invalid", detail)

    if not card["is_product"] and only_url:
        payload = _step_payload(previous, started, started_mono, status="failed", code="not_product_page", **call)
        with tenant_session(ctx) as session:
            record = _load(session, ctx, intake_id)
            record.extraction_json = {"output": result.data, **call}
        detail = "the page does not describe a product"
        _finish(
            ctx, intake_id, status="fetch_failed", step="extract", step_payload=payload,
            failure_code="not_product_page", failure_detail=detail,
        )
        return IntakeOutcome("failed", "not_product_page", detail)

    payload = _step_payload(previous, started, started_mono, status="succeeded", **call)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        record.extraction_json = {"output": result.data, **call}
        draft = {key: value for key, value in card.items() if key not in ("image_ranking",)}
        draft["image_ranking"] = card["image_ranking"]
        draft["source_url"] = source_url
        record.card_json = draft
        _set_evidence(record, "extract", payload)
        record.updated_at = utcnow()
        _audit_step(session, intake_id, "extract", payload)
    return None


def _extraction_error(
    ctx: TenantContext,
    intake_id: str,
    previous: dict,
    started: datetime,
    started_mono: float,
    error: LlmChatError,
) -> IntakeOutcome:
    raw_excerpts: list[str] = []
    if error.attempt_id:
        with tenant_session(ctx) as session:
            evidence = llm_call_evidence(session, ctx, error.attempt_id)
        raw_excerpts = [text for text in evidence.get("invalid_answer_excerpts") or [] if text]
    unusable = error.code in _UNUSABLE_ANSWER_CODES
    payload = _step_payload(
        previous, started, started_mono, status="failed", code=error.code,
        retryable=error.retryable, dispatch_certainty=error.dispatch_certainty,
        attempt_id=error.attempt_id,
    )
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        record.extraction_json = {
            "error": error.as_dict(),
            "raw_output": raw_excerpts or None,
        }
    status = "extraction_failed" if unusable else "failed"
    _finish(
        ctx, intake_id, status=status, step="extract", step_payload=payload,
        failure_code=error.code, failure_detail=error.detail,
    )
    if error.dispatch_certainty == "uncertain":
        return IntakeOutcome("uncertain", error.code, error.detail)
    return IntakeOutcome("failed", error.code, error.detail)


# ---------------------------------------------------------------------------
# Step 3: images (rank, then the reference gate)
# ---------------------------------------------------------------------------


def rank_candidates(candidates: list[dict], ranking: list[dict]) -> list[dict]:
    """Model ranking first (``irrelevant`` last and flagged), then the rest in order."""
    by_id = {item["image_id"]: dict(item) for item in candidates}
    ordered: list[dict] = []
    irrelevant: list[dict] = []
    for entry in ranking:
        item = by_id.pop(entry["image_id"], None)
        if item is None:
            continue
        item["role"] = entry["role"]
        (irrelevant if entry["role"] == "irrelevant" else ordered).append(item)
    for item in candidates:
        if item["image_id"] in by_id:
            leftover = by_id.pop(item["image_id"])
            leftover.setdefault("role", "unranked")
            ordered.append(leftover)
    ordered.extend(irrelevant)
    for rank, item in enumerate(ordered, start=1):
        item["rank"] = rank
    return ordered


async def _images_step(ctx: TenantContext, intake_id: str, blob_store: BlobStore) -> None:
    from app.library.reference_commands import quarantine_committed_blob
    from app.library.reference_gate_worker import run_reference_gate

    previous = _begin(ctx, intake_id, status="preparing_images", step="images")
    started, started_mono = _now(), time.monotonic()
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        ranked = rank_candidates(list(record.images_json or []), list((record.card_json or {}).get("image_ranking") or []))
        source_url = record.source_url

    counts = {"gated": 0, "released": 0, "rejected": 0, "unavailable": 0, "not_gated": 0}
    for item in ranked:
        if item.get("role") == "irrelevant" or counts["gated"] >= MAX_GATED_IMAGES:
            item["gate_state"] = "not_gated"
            counts["not_gated"] += 1
            continue
        counts["gated"] += 1
        with tenant_session(ctx) as session:
            reference, _reused = quarantine_committed_blob(
                session, ctx,
                committed_key=item["committed_key"],
                storage_version_id=item.get("storage_version_id") or "",
                content_sha256=item["sha256"],
                declared_mime=item.get("mime_type") or "",
                byte_size=int(item.get("byte_size") or 0),
                filename=f"{item['image_id']}.{extension_for_mime(item.get('mime_type') or '')}",
                provenance={
                    "source": item.get("source"),
                    "source_url": item.get("source_url") or source_url,
                    "product_intake_id": intake_id,
                },
            )
            object_id = reference.id
        try:
            outcome = await run_reference_gate(ctx, blob_store, object_id)
            state, code = outcome.kind, outcome.code
        except Exception as exc:  # noqa: BLE001 -- one image never sinks the intake
            state, code = "unavailable", type(exc).__name__
        item["reference_object_id"] = object_id
        item["gate_state"] = "released" if state == "released" else ("rejected" if state == "rejected" else "unavailable")
        if code:
            item["gate_code"] = code
        counts[item["gate_state"]] += 1

    with tenant_session(ctx) as session:
        released = {
            row.id: row for row in session.scalars(
                select(ReferenceObjectRecord).where(
                    ReferenceObjectRecord.workspace_id == ctx.workspace_id,
                    ReferenceObjectRecord.id.in_([i["reference_object_id"] for i in ranked if i.get("reference_object_id")] or [""]),
                )
            )
        }
        for item in ranked:
            row = released.get(item.get("reference_object_id") or "")
            if row is not None and row.state == "released":
                if not item.get("width") and row.width:
                    item["width"], item["height"] = int(row.width), int(row.height)
                item["derivative"] = {
                    "mime_type": row.derivative_mime,
                    "width": row.derivative_width,
                    "height": row.derivative_height,
                    "byte_size": row.derivative_byte_size,
                }
    payload = _step_payload(previous, started, started_mono, status="succeeded", **counts)
    _finish_ready(ctx, intake_id, ranked, payload)


def _finish_ready(ctx: TenantContext, intake_id: str, ranked: list[dict], payload: dict) -> None:
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        record.images_json = ranked
        _set_evidence(record, "images", payload)
        record.status = "ready"
        record.current_step = None
        record.failure_code = None
        record.failure_detail = None
        now = utcnow()
        record.updated_at = now
        record.finished_at = now
        _audit_step(session, intake_id, "images", payload)
        audit.append_event(
            session,
            event_type="product_intake.ready",
            resource_type="product_intake",
            resource_id=intake_id,
            details={
                "images": len(ranked),
                "released_images": sum(1 for item in ranked if item.get("gate_state") == "released"),
                "variants": len((record.card_json or {}).get("model_variants") or []),
            },
        )
