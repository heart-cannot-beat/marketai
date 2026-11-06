"""``llm_chat``: the governed way any Worker job asks an LLM for structured JSON.

Product intake, plan drafting, external-asset tagging and VLM QC all need a
model's answer. They get it through this one call semantic, which applies
the platform's paid-call invariants (CLAUDE.md, ADR 0005, front-flow
contract §6) that the legacy ``app.llm`` client skipped:

* **Workspace key only.** ``byok`` calls use the OpenAI key the Workspace
  bound in Settings (``binding_scope='generation'``, the same binding that
  pays for GPT-Image -- see ``app.generation.credential_binding``). There is
  no platform key and no silent fallback: no bound key is a structured
  ``credential_unavailable``. ``mock`` calls go to the controlled mock
  (``app.providers.llm_mock``) and cost nothing real.
* **Reserve before, settle after.** The command that admits the job reserves
  the call's maximum cost (:func:`reserve_llm_chat`, the same
  ``ReservationBook`` path as image generation -- input-token ceiling plus
  ``max_output_tokens`` at list price, times the one schema retry); the
  Worker's call settles it at the cost of the provider's reported usage
  (ledger: ``quote`` + ``reservation`` at admission, ``reported_cost`` +
  ``release`` of the remainder at completion) or releases it when nothing
  was billed.
* **Durable, idempotent, recoverable.** Every call runs through
  ``ExternalCallCoordinator`` as a ``provider_call_attempts`` row
  (``step_kind='llm_chat'``) keyed by its Job and ``call_name``: a re-run
  Job replays the stored answer instead of paying again, an unknown outcome
  stays ``uncertain`` for reconciliation, and nothing is ever blindly
  resent.
* **Evidence without secrets.** The attempt keeps request/response digests,
  image SHA-256s, model version, usage, latency and cost (the
  ``usage_reported`` event), never the key and never raw image bytes. Each
  call appends one audit event.

Typical use from a Worker job (admission already called
:func:`reserve_llm_chat` with the same ``job_id``/``call_name``)::

    result = await client.chat(
        ctx, job_id=job.id, operation_id=job.operation_id, call_name="extract",
        execution_mode="byok",
        request=LlmChatRequest.build(
            semantic="product_intake", system=..., user=..., json_schema=SCHEMA,
            max_output_tokens=1200, images=(LlmImage.from_blob(ref),),
        ),
    )
    result.data  # schema-valid dict

This module ships no business prompt; callers own theirs.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, ContextManager, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendReservationRecord
from app.entitlements.credential_protector import CredentialProtector
from app.entitlements.policy import (
    EntitlementPolicy,
    FeatureNotEntitledError,
    LeaseExpiredError,
    NoEntitlementProjectionError,
    NumericEntitlementExceededError,
    WorkspaceSuspendedError,
)
from app.entitlements.quote_engine import CostQuoteEngine
from app.entitlements.reservation_book import GuardrailExceededError, ReservationBook
from app.entitlements.types import BillableCallSpec, EntitlementRequirements, ReservationPlan
from app.generation.credential_binding import open_bound_secret, resolve_generation_credential
from app.generation.models import ProviderAttemptEventRecord
from app.platform import audit
from app.platform.blob_store import BlobStore, CommittedBlobRef
from app.platform.tenancy import TenantContext
from app.providers import json_schema
from app.providers.adapter import ExternalCallAdapter
from app.providers.dispatch import (
    ChargeabilityMismatchError,
    DispatchCoordinatorError,
    ExternalCallCoordinator,
    JobNotFoundError,
    OperationNotFoundError,
    ReservationInvalidError,
)
from app.providers.llm_registry import (
    LLM_PRICE_RULE_VERSION,
    LLM_SCHEMA_RETRIES,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_CALL,
    MOCK_PROVIDER,
    OPENAI_PROVIDER,
    LlmModelSpec,
    UnknownLlmModelError,
    call_max_cost,
    describe_models,
    estimate_input_tokens,
    model_spec,
    DEFAULT_MODEL_BY_PROVIDER,
)
from app.providers.llm_types import (
    SUPPORTED_IMAGE_TYPES,
    LlmChatOutput,
    LlmChatRequest,
    LlmImage,
    LlmMessage,
    ResolvedImage,
    ResolvedLlmChatRequest,
    canonical_digest,
    sniff_image_type,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LLM_CHAT_STEP_KIND",
    "LlmChatClient",
    "LlmChatError",
    "LlmChatRequest",
    "LlmChatReservation",
    "LlmChatResult",
    "LlmImage",
    "LlmMessage",
    "llm_call_evidence",
    "llm_call_key",
    "llm_capability",
    "release_llm_chat_reservation",
    "reserve_llm_chat",
    "resolve_llm_execution_mode",
]

LLM_CHAT_STEP_KIND = "llm_chat"

#: Execution mode -> the provider whose adapter serves it.
PROVIDER_FOR_MODE = {"byok": OPENAI_PROVIDER, "mock": MOCK_PROVIDER}
CHARGEABILITY_FOR_MODE = {"byok": "charged", "mock": "free_mock"}

_COORDINATOR_ERROR_CODES = {
    JobNotFoundError: "job_not_found",
    OperationNotFoundError: "operation_not_found",
    ReservationInvalidError: "reservation_invalid",
    ChargeabilityMismatchError: "invalid_request",
}


# ---------------------------------------------------------------------------
# Errors and results
# ---------------------------------------------------------------------------


class LlmChatError(Exception):
    """A structured, caller-facing refusal or failure of one ``llm_chat`` call.

    ``code`` is machine-readable (``credential_unavailable``,
    ``reservation_missing``, ``guardrail_exceeded``, ``rate_limited``,
    ``credential_rejected``, ``provider_timeout``, ``schema_validation_failed``,
    ...). ``retryable`` says whether the *same* intent may be tried again as
    a new call (a rate limit) rather than needing a change first (bind a
    key) or reconciliation (``dispatch_certainty == 'uncertain'``).
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
        dispatch_certainty: str = "not_dispatched",
        attempt_id: str | None = None,
        replayed: bool = False,
        usage: dict | None = None,
        cost: dict | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.dispatch_certainty = dispatch_certainty
        self.attempt_id = attempt_id
        self.replayed = replayed
        self.usage = dict(usage or {})
        self.cost = dict(cost or {})
        super().__init__(f"{code}: {detail}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "retryable": self.retryable,
            "dispatch_certainty": self.dispatch_certainty,
            "attempt_id": self.attempt_id,
        }


@dataclass(frozen=True)
class LlmChatResult:
    """A schema-valid answer and its evidence."""

    data: dict
    usage: dict
    model: str
    model_version: str
    provider: str
    execution_mode: str
    provider_request_id: str | None
    latency_ms: int
    attempt_id: str
    replayed: bool
    cost: dict


@dataclass(frozen=True)
class LlmChatQuote:
    provider: str
    model: str
    execution_mode: str
    input_tokens: int
    max_output_tokens: int
    max_requests: int
    amount: Decimal  # real money; zero for mock
    simulated_amount: Decimal | None
    currency: str
    price_rule_version: str


@dataclass(frozen=True)
class LlmChatReservation:
    call_key: str
    reservation_id: str | None  # ``None`` for mock: nothing real is reserved
    quote: LlmChatQuote


# ---------------------------------------------------------------------------
# Keys, modes and quotes (shared by admission and the Worker)
# ---------------------------------------------------------------------------


def llm_call_key(job_id: str, call_name: str) -> str:
    """The identity of one ``llm_chat`` call: its Job and its name within the Job.

    A 64-hex SHA-256, so it fits every ``varchar(64)`` key column it lands
    in (spend reservation, attempt, and -- namespaced and hashed again by
    ``ledger_entry_key`` -- every ledger row of the call; PR #150).
    """
    if not job_id or not call_name:
        raise ValueError("an llm_chat call needs a job_id and a call_name")
    return hashlib.sha256(f"llm_chat:{job_id}:{call_name}".encode("utf-8")).hexdigest()


def resolve_llm_execution_mode(session: Session, ctx: TenantContext) -> str:
    """``byok`` when this Workspace has an active OpenAI key bound, else ``mock``.

    A default for admission (front-flow contract §4: no key -> Mock with a
    banner to bind one). It is decided *before* the call and frozen on the
    Job; a byok call that later finds no key fails ``credential_unavailable``
    rather than silently becoming a mock.
    """
    return "byok" if resolve_generation_credential(session, ctx, OPENAI_PROVIDER).get("active") else "mock"


def _provider_for(execution_mode: str) -> str:
    try:
        return PROVIDER_FOR_MODE[execution_mode]
    except KeyError:
        raise LlmChatError(
            "mode_unavailable",
            f"llm_chat supports execution modes {sorted(PROVIDER_FOR_MODE)}, not {execution_mode!r}",
        ) from None


def _spec(provider: str, model: str | None) -> LlmModelSpec:
    try:
        return model_spec(provider, model)
    except UnknownLlmModelError as exc:
        raise LlmChatError("unknown_model", str(exc)) from exc


def _check_max_output(spec: LlmModelSpec, max_output_tokens: int) -> None:
    if not isinstance(max_output_tokens, int) or isinstance(max_output_tokens, bool) \
            or not 1 <= max_output_tokens <= spec.max_output_tokens:
        raise LlmChatError(
            "invalid_request",
            f"max_output_tokens must be an integer in 1..{spec.max_output_tokens} for {spec.model}",
        )


def quote_llm_chat(
    *,
    execution_mode: str,
    max_output_tokens: int,
    input_tokens: int,
    model: str | None = None,
) -> LlmChatQuote:
    """Maximum cost of one call: ``input_tokens`` + ``max_output_tokens`` at list price,
    for the first answer and its one schema retry."""
    provider = _provider_for(execution_mode)
    spec = _spec(provider, model)
    _check_max_output(spec, max_output_tokens)
    if input_tokens < 0:
        raise LlmChatError("invalid_request", "input_tokens must not be negative")
    maximum = call_max_cost(spec, input_tokens=input_tokens, max_output_tokens=max_output_tokens)
    is_mock = execution_mode == "mock"
    return LlmChatQuote(
        provider=provider,
        model=spec.model,
        execution_mode=execution_mode,
        input_tokens=input_tokens,
        max_output_tokens=max_output_tokens,
        max_requests=1 + LLM_SCHEMA_RETRIES,
        amount=Decimal("0") if is_mock else maximum,
        simulated_amount=maximum if is_mock else None,
        currency="USD",
        price_rule_version=LLM_PRICE_RULE_VERSION,
    )


def estimate_request_input_tokens(request: LlmChatRequest, *, execution_mode: str) -> int:
    """The input-token ceiling of a fully known request (no blob reads needed)."""
    spec = _spec(_provider_for(execution_mode), request.model)
    return estimate_input_tokens(
        spec,
        texts=tuple(message.text for message in request.messages),
        schema_json=_schema_json(request.json_schema),
        image_count=len(request.images),
    )


# ---------------------------------------------------------------------------
# Admission: reserve (and abandon) a call's maximum cost
# ---------------------------------------------------------------------------


def reserve_llm_chat(
    session: Session,
    ctx: TenantContext,
    *,
    operation_id: str,
    job_id: str,
    call_name: str,
    execution_mode: str,
    max_output_tokens: int | None = None,
    input_token_budget: int | None = None,
    request: LlmChatRequest | None = None,
    model: str | None = None,
) -> LlmChatReservation:
    """Reserve one ``llm_chat`` call's maximum cost in the caller's transaction.

    Called by the command that creates ``job_id`` (the API role creates
    reservations; the Worker only settles them). The input side of the quote
    is ``request``'s own ceiling when the request is already known, otherwise
    ``input_token_budget`` -- an upper bound the Worker then enforces (a
    request whose ceiling exceeds what was reserved is refused, unbilled).
    Mock reserves nothing real and only reports a simulated quote.

    Raises :class:`LlmChatError` (``credential_unavailable``,
    ``entitlement_unavailable``, ``guardrail_exceeded``, ``invalid_request``);
    the caller's transaction then rolls back the whole command.
    """
    if request is not None:
        input_tokens = estimate_request_input_tokens(request, execution_mode=execution_mode)
        model = request.model if model is None else model
        max_output_tokens = request.max_output_tokens
    elif input_token_budget is not None:
        input_tokens = int(input_token_budget)
    elif execution_mode == "mock":
        input_tokens = 0  # nothing real is reserved; the simulated quote is output-only
    else:
        raise LlmChatError(
            "invalid_request", "reserve_llm_chat needs the request or an input_token_budget",
        )
    if max_output_tokens is None:
        raise LlmChatError("invalid_request", "reserve_llm_chat needs max_output_tokens")
    quote = quote_llm_chat(
        execution_mode=execution_mode,
        max_output_tokens=max_output_tokens,
        input_tokens=input_tokens,
        model=model,
    )
    key = llm_call_key(job_id, call_name)
    if execution_mode == "mock":
        return LlmChatReservation(call_key=key, reservation_id=None, quote=quote)

    if not resolve_generation_credential(session, ctx, quote.provider).get("active"):
        raise LlmChatError(
            "credential_unavailable",
            f"no active {quote.provider} key is bound to this Workspace; bind one in Settings",
            retryable=False,
        )

    from app.generation.spend import ensure_spend_guardrail

    try:
        entitlement_ref = EntitlementPolicy().authorize(
            session, ctx,
            EntitlementRequirements(feature_keys=(), numeric_requirements=()),
            _utcnow(),
        )
    except (
        NoEntitlementProjectionError,
        LeaseExpiredError,
        WorkspaceSuspendedError,
        FeatureNotEntitledError,
        NumericEntitlementExceededError,
    ) as exc:
        raise LlmChatError("entitlement_unavailable", str(exc)) from exc

    guardrail = ensure_spend_guardrail(session, ctx)
    budget_currency = guardrail.budget_currency or "USD"
    registry = {quote.provider: {quote.model: quote.amount}}
    quote_bundle = CostQuoteEngine(registry, LLM_PRICE_RULE_VERSION).bundle(
        [BillableCallSpec(
            call_semantic_key_hash=key, provider=quote.provider, model=quote.model, quantity=1,
        )],
        execution_mode=execution_mode,
        budget_currency=budget_currency,
    )
    try:
        bundle = ReservationBook().reserve(session, ctx, ReservationPlan(
            quote_bundle=quote_bundle,
            capacity_requests=(),
            entitlement_ref=entitlement_ref,
            operation_id=operation_id,
            job_ids_by_call_key={key: job_id},
        ))
    except GuardrailExceededError as exc:
        raise LlmChatError(
            "guardrail_exceeded",
            f"{exc.limit_name}: this call would bring spend to {exc.proposed_value} "
            f"{budget_currency}, above the limit of {exc.limit_value} {budget_currency}",
        ) from exc
    return LlmChatReservation(
        call_key=key,
        reservation_id=bundle.spend_reservation_by_call_key[key],
        quote=quote,
    )


def release_llm_chat_reservation(
    session: Session, ctx: TenantContext, *, job_id: str, call_name: str,
) -> bool:
    """Release a reserved call that will never be made (its Job was cancelled).

    Only a reservation no attempt has consumed is released; one an attempt
    holds is settled or reconciled by the coordinator. Returns whether
    anything was released.
    """
    key = llm_call_key(job_id, call_name)
    reservation = _open_reservation(session, ctx, key)
    if reservation is None:
        return False
    in_use = session.execute(
        select(ProviderCallAttemptRecord.id).where(
            ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            ProviderCallAttemptRecord.spend_reservation_id == reservation.id,
        )
    ).first()
    if in_use is not None:
        return False
    now = _utcnow()
    reservation.state = "released"
    reservation.outstanding_budget_amount = 0
    reservation.released_at = now
    reservation.updated_at = now
    session.flush()
    return True


# ---------------------------------------------------------------------------
# Worker: execute one call
# ---------------------------------------------------------------------------


AdapterRegistry = Mapping[str, ExternalCallAdapter]


def default_llm_adapters() -> dict[str, ExternalCallAdapter]:
    from app.providers.llm_mock import MockLlmChatAdapter
    from app.providers.openai_chat import OpenAIChatAdapter

    return {OPENAI_PROVIDER: OpenAIChatAdapter(), MOCK_PROVIDER: MockLlmChatAdapter()}


class LlmChatClient:
    """Executes ``llm_chat`` calls from Worker jobs through the coordinator."""

    def __init__(
        self,
        *,
        coordinator: ExternalCallCoordinator,
        session_factory: Callable[[TenantContext], ContextManager[Session]],
        protector: CredentialProtector,
        adapters: AdapterRegistry | None = None,
        blob_store: BlobStore | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._session_factory = session_factory
        self._protector = protector
        self._adapters = dict(adapters) if adapters is not None else default_llm_adapters()
        self._blob_store = blob_store

    async def chat(
        self,
        ctx: TenantContext,
        *,
        job_id: str,
        operation_id: str,
        call_name: str,
        request: LlmChatRequest,
        execution_mode: str,
    ) -> LlmChatResult:
        """Run (or replay) one governed call; raise :class:`LlmChatError` on failure."""
        provider = _provider_for(execution_mode)
        spec = _spec(provider, request.model)
        _validate_request(request, spec)
        key = llm_call_key(job_id, call_name)

        with self._session_factory(ctx) as session:
            replay = _replay(session, ctx, job_id=job_id, call_key=key, execution_mode=execution_mode)
        if replay is not None:
            return replay

        adapter = self._adapters.get(provider)
        if adapter is None:
            raise LlmChatError("mode_unavailable", f"no llm_chat adapter is deployed for {provider!r}")

        images = await self._resolve_images(ctx, request.images)
        resolved = _resolve(request, spec, provider, images)

        reservation_id: str | None = None
        credential_version_id: str | None = None
        secret = bytearray()
        with self._session_factory(ctx) as session:
            if execution_mode == "byok":
                evidence = resolve_generation_credential(session, ctx, provider)
                if not evidence.get("active"):
                    raise LlmChatError(
                        "credential_unavailable",
                        f"no active {provider} key is bound to this Workspace; bind one in Settings",
                        retryable=False,
                    )
                reservation = _open_reservation(session, ctx, key)
                if reservation is None and not _has_attempt(session, ctx, job_id=job_id, call_key=key):
                    raise LlmChatError(
                        "reservation_missing",
                        "no open spend reservation for this call; its admitting command must "
                        "reserve it (reserve_llm_chat) before the Worker dispatches",
                    )
                if reservation is not None:
                    maximum = call_max_cost(
                        spec,
                        input_tokens=resolved.estimated_input_tokens,
                        max_output_tokens=resolved.max_output_tokens,
                    )
                    if maximum > Decimal(str(reservation.maximum_provider_amount)):
                        raise LlmChatError(
                            "reservation_insufficient",
                            f"this request can cost up to {maximum} USD but only "
                            f"{reservation.maximum_provider_amount} was reserved; reserve a larger "
                            "input_token_budget",
                        )
                    reservation_id = reservation.id
                credential_version_id = evidence["version_id"]
                secret = bytearray(open_bound_secret(
                    session, ctx, credential_version_id, self._protector,
                ))

        recorder = LlmChatEvidenceRecorder(resolved, job_id=job_id, call_name=call_name)
        try:
            result = await self._coordinator.execute(
                ctx,
                job_id=job_id,
                operation_id=operation_id,
                call_semantic_key_hash=key,
                step_kind=LLM_CHAT_STEP_KIND,
                execution_mode=execution_mode,
                chargeability=CHARGEABILITY_FOR_MODE[execution_mode],
                provider=provider,
                request=resolved,
                adapter=adapter,
                evidence_recorder=recorder,
                credential=memoryview(secret) if secret else None,
                spend_reservation_id=reservation_id,
                model=spec.model,
                credential_version_id=credential_version_id,
            )
        except DispatchCoordinatorError as exc:
            # The Job, Operation or reservation is not this Workspace's (or no
            # longer valid): refuse structurally, before any provider call.
            raise LlmChatError(_COORDINATOR_ERROR_CODES.get(type(exc), "dispatch_refused"), str(exc)) from exc
        finally:
            for index in range(len(secret)):
                secret[index] = 0

        if result.success and result.result is not None:
            output: LlmChatOutput = result.result
            with self._session_factory(ctx) as session:
                cost = _usage_payload(session, ctx, result.attempt_id).get("cost") or {}
            return LlmChatResult(
                data=output.data,
                usage=dict(output.usage),
                model=spec.model,
                model_version=output.model_version,
                provider=provider,
                execution_mode=execution_mode,
                provider_request_id=result.provider_request_id,
                latency_ms=output.latency_ms,
                attempt_id=result.attempt_id,
                replayed=False,
                cost=cost,
            )
        if result.replayed:
            # The coordinator found an attempt left by an earlier run of this
            # Job (window 3/4/5); answer from what it recorded.
            with self._session_factory(ctx) as session:
                replay = _replay(session, ctx, job_id=job_id, call_key=key, execution_mode=execution_mode)
            if replay is not None:
                return replay

        with self._session_factory(ctx) as session:
            payload = _usage_payload(session, ctx, result.attempt_id)
        error = LlmChatError(
            result.error_code or "provider_error",
            result.error_detail or "llm_chat call failed",
            retryable=_attempt_retryable(self._session_factory, ctx, result.attempt_id),
            dispatch_certainty=result.dispatch_certainty,
            attempt_id=result.attempt_id,
            usage=payload.get("usage"),
            cost=payload.get("cost"),
        )
        self._audit_failure(ctx, resolved, job_id=job_id, call_name=call_name, error=error)
        raise error

    async def _resolve_images(
        self, ctx: TenantContext, images: tuple[LlmImage, ...],
    ) -> tuple[ResolvedImage, ...]:
        resolved: list[ResolvedImage] = []
        for image in images:
            if image.content is not None:
                content = image.content
            elif image.blob is not None:
                content = await self._read_blob(ctx, image.blob)
            else:
                raise LlmChatError("invalid_request", "an llm_chat image needs bytes or a blob ref")
            if len(content) > MAX_IMAGE_BYTES:
                raise LlmChatError(
                    "invalid_request", f"an input image is larger than {MAX_IMAGE_BYTES} bytes",
                )
            mime = sniff_image_type(content)
            if mime is None or mime not in SUPPORTED_IMAGE_TYPES:
                raise LlmChatError(
                    "invalid_request",
                    f"input images must be one of {SUPPORTED_IMAGE_TYPES} (by content)",
                )
            resolved.append(ResolvedImage(
                content=content, mime_type=mime, sha256=hashlib.sha256(content).hexdigest(),
            ))
        return tuple(resolved)

    async def _read_blob(self, ctx: TenantContext, ref: CommittedBlobRef) -> bytes:
        # A committed key names its Workspace (``committed/<workspace>/...``):
        # a ref from another Workspace is refused before any read.
        if not ref.committed_key.startswith(f"committed/{ctx.workspace_id}/"):
            raise LlmChatError(
                "image_not_in_workspace",
                "an input image must be a committed object of this Workspace",
            )
        if self._blob_store is None:
            raise LlmChatError("invalid_request", "blob image refs need a BlobStore")
        chunks: list[bytes] = []
        size = 0
        async for chunk in self._blob_store.open_committed(ref):
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise LlmChatError(
                    "invalid_request", f"an input image is larger than {MAX_IMAGE_BYTES} bytes",
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _audit_failure(
        self,
        ctx: TenantContext,
        request: ResolvedLlmChatRequest,
        *,
        job_id: str,
        call_name: str,
        error: LlmChatError,
    ) -> None:
        try:
            with self._session_factory(ctx) as session:
                audit.append_event(
                    session,
                    event_type="llm_chat.call_failed",
                    resource_type="provider_call_attempt",
                    resource_id=error.attempt_id or "",
                    details={
                        "semantic": request.semantic,
                        "provider": request.provider,
                        "model": request.model,
                        "job_id": job_id,
                        "call_name": call_name,
                        "error_code": error.code,
                        "dispatch_certainty": error.dispatch_certainty,
                        "retryable": error.retryable,
                        "request_digest": request.request_digest,
                    },
                )
        except Exception:  # noqa: BLE001 -- the attempt row already holds the outcome
            logger.exception(
                "llm_chat_failure_audit_failed",
                extra={"workspace_id": ctx.workspace_id, "attempt_id": error.attempt_id},
            )


# ---------------------------------------------------------------------------
# Evidence recorder
# ---------------------------------------------------------------------------


class LlmChatEvidenceRecorder:
    """``ExternalCallEvidenceRecorder[LlmChatOutput]`` for ``llm_chat``.

    The answer itself and its usage/cost evidence are recorded by the
    coordinator on the attempt (``usage_reported``); this recorder stamps the
    request digests on the attempt at prepare time and audits the success in
    the completing transaction. Nothing goes to object storage: the reference
    is the response digest.
    """

    def __init__(self, request: ResolvedLlmChatRequest, *, job_id: str, call_name: str) -> None:
        self._request = request
        self._job_id = job_id
        self._call_name = call_name

    def link_prepared_attempt(
        self, session: Session, ctx: TenantContext, *, attempt: ProviderCallAttemptRecord,
    ) -> None:
        attempt.request_fingerprint_hash = self._request.request_digest
        attempt.redacted_request_hash = self._request.redacted_request_digest

    def materialize(self, ctx: TenantContext, *, attempt_id: str, output: LlmChatOutput) -> str:
        return f"sha256:{output.response_digest}"

    def persist_result(
        self,
        session: Session,
        ctx: TenantContext,
        *,
        attempt: ProviderCallAttemptRecord,
        reference: str,
        output: LlmChatOutput,
    ) -> None:
        audit.append_event(
            session,
            event_type="llm_chat.call_succeeded",
            resource_type="provider_call_attempt",
            resource_id=attempt.id,
            details={
                "semantic": output.semantic,
                "provider": attempt.provider,
                "model": attempt.model,
                "model_version": output.model_version,
                "execution_mode": attempt.execution_mode,
                "job_id": self._job_id,
                "call_name": self._call_name,
                "usage": dict(output.usage),
                "latency_ms": output.latency_ms,
                "request_digest": self._request.request_digest,
                "response_digest": output.response_digest,
            },
        )


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def llm_capability(session: Session, ctx: TenantContext) -> dict[str, Any]:
    """The ``llm`` entry of ``GET /generation/capabilities``."""
    byok_ready = bool(resolve_generation_credential(session, ctx, OPENAI_PROVIDER).get("active"))
    return {
        "semantic": LLM_CHAT_STEP_KIND,
        "modes": ["mock", "byok"] if byok_ready else ["mock"],
        "default_mode": "byok" if byok_ready else "mock",
        "byok_ready": byok_ready,
        "credential_binding_scope": "generation",
        "providers": {
            OPENAI_PROVIDER: {
                "execution_mode": "byok",
                "default_model": DEFAULT_MODEL_BY_PROVIDER[OPENAI_PROVIDER],
                "models": describe_models(OPENAI_PROVIDER),
            },
            MOCK_PROVIDER: {
                "execution_mode": "mock",
                "default_model": DEFAULT_MODEL_BY_PROVIDER[MOCK_PROVIDER],
                "models": describe_models(MOCK_PROVIDER),
            },
        },
        "price_rule_version": LLM_PRICE_RULE_VERSION,
        "max_images_per_call": MAX_IMAGES_PER_CALL,
        "schema_retries": LLM_SCHEMA_RETRIES,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _schema_json(schema: Mapping[str, Any]) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate_request(request: LlmChatRequest, spec: LlmModelSpec) -> None:
    if (request.output_validator is None) != (request.output_validation_policy is None) or (
        request.output_validator is not None and (
            not callable(request.output_validator)
            or not isinstance(request.output_validation_policy, str)
            or not request.output_validation_policy.strip()
            or len(request.output_validation_policy) > 128
        )
    ):
        raise LlmChatError("invalid_request", "an output validator requires a named validation policy")
    if not request.semantic or len(request.semantic) > 64:
        raise LlmChatError("invalid_request", "semantic must be a tag of 1..64 characters")
    if not any(message.role == "user" for message in request.messages):
        raise LlmChatError("invalid_request", "an llm_chat request needs a user message")
    try:
        json_schema.check_schema(dict(request.json_schema))
    except json_schema.SchemaDefinitionError as exc:
        raise LlmChatError("invalid_schema", str(exc)) from exc
    _check_max_output(spec, request.max_output_tokens)
    if len(request.images) > MAX_IMAGES_PER_CALL:
        raise LlmChatError("invalid_request", f"at most {MAX_IMAGES_PER_CALL} images per call")
    if request.images and not spec.vision:
        raise LlmChatError("invalid_request", f"{spec.model} does not read images")
    if not request.schema_name or len(request.schema_name) > 64:
        raise LlmChatError("invalid_request", "schema_name must be 1..64 characters")


def _resolve(
    request: LlmChatRequest,
    spec: LlmModelSpec,
    provider: str,
    images: tuple[ResolvedImage, ...],
) -> ResolvedLlmChatRequest:
    schema = json.loads(_schema_json(request.json_schema))
    schema_digest = canonical_digest(schema)
    shape = {
        "semantic": request.semantic,
        "provider": provider,
        "model": spec.model,
        "schema_name": request.schema_name,
        "schema_sha256": schema_digest,
        "max_output_tokens": request.max_output_tokens,
        "images": [[image.mime_type, image.sha256] for image in images],
        "roles": [message.role for message in request.messages],
    }
    full = {
        **shape,
        "messages_sha256": [
            hashlib.sha256(message.text.encode("utf-8")).hexdigest() for message in request.messages
        ],
    }
    if request.output_validation_policy is not None:
        shape["output_validation_policy"] = request.output_validation_policy
        full["output_validation_policy"] = request.output_validation_policy
    return ResolvedLlmChatRequest(
        semantic=request.semantic,
        provider=provider,
        model=spec.model,
        messages=tuple(request.messages),
        images=images,
        json_schema=schema,
        schema_name=request.schema_name,
        max_output_tokens=request.max_output_tokens,
        estimated_input_tokens=estimate_input_tokens(
            spec,
            texts=tuple(message.text for message in request.messages),
            schema_json=_schema_json(schema),
            image_count=len(images),
        ),
        request_digest=canonical_digest(full),
        redacted_request_digest=canonical_digest(shape),
        output_validation_policy=request.output_validation_policy,
        output_validator=request.output_validator,
    )


def _open_reservation(session: Session, ctx: TenantContext, call_key: str) -> SpendReservationRecord | None:
    return session.execute(
        select(SpendReservationRecord).where(
            SpendReservationRecord.workspace_id == ctx.workspace_id,
            SpendReservationRecord.semantic_key_hash == call_key,
            SpendReservationRecord.state == "reserved",
        )
    ).scalars().first()


def _latest_attempt(
    session: Session, ctx: TenantContext, *, job_id: str, call_key: str,
) -> ProviderCallAttemptRecord | None:
    return session.execute(
        select(ProviderCallAttemptRecord)
        .where(
            ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            ProviderCallAttemptRecord.job_id == job_id,
            ProviderCallAttemptRecord.call_semantic_key_hash == call_key,
        )
        .order_by(ProviderCallAttemptRecord.attempt_no.desc())
    ).scalars().first()


def _has_attempt(session: Session, ctx: TenantContext, *, job_id: str, call_key: str) -> bool:
    return _latest_attempt(session, ctx, job_id=job_id, call_key=call_key) is not None


def _usage_payload(session: Session, ctx: TenantContext, attempt_id: str) -> dict[str, Any]:
    event = session.execute(
        select(ProviderAttemptEventRecord)
        .where(
            ProviderAttemptEventRecord.workspace_id == ctx.workspace_id,
            ProviderAttemptEventRecord.provider_call_attempt_id == attempt_id,
            ProviderAttemptEventRecord.event_type == "usage_reported",
        )
        .order_by(ProviderAttemptEventRecord.sequence.desc())
    ).scalars().first()
    return dict(event.payload_json or {}) if event is not None else {}


def llm_call_evidence(session: Session, ctx: TenantContext, attempt_id: str) -> dict[str, Any]:
    """The evidence an attempt's ``usage_reported`` event recorded (``{}`` if none).

    For a billed but unusable answer (``schema_validation_failed``,
    ``invalid_json``) this includes ``invalid_answers`` (the failure codes)
    and ``invalid_answer_excerpts`` (bounded raw text of what the model
    said), so a caller can keep the raw output as its own evidence. Scoped
    to ``ctx``'s Workspace like every other read here.
    """
    if not attempt_id:
        return {}
    return dict(_usage_payload(session, ctx, attempt_id).get("evidence") or {})


def _failure_payload(session: Session, ctx: TenantContext, attempt_id: str) -> dict[str, Any]:
    event = session.execute(
        select(ProviderAttemptEventRecord)
        .where(
            ProviderAttemptEventRecord.workspace_id == ctx.workspace_id,
            ProviderAttemptEventRecord.provider_call_attempt_id == attempt_id,
            ProviderAttemptEventRecord.event_type == "failure_classified",
        )
        .order_by(ProviderAttemptEventRecord.sequence.desc())
    ).scalars().first()
    return dict(event.payload_json or {}) if event is not None else {}


def _attempt_retryable(
    session_factory: Callable[[TenantContext], ContextManager[Session]],
    ctx: TenantContext,
    attempt_id: str,
) -> bool:
    with session_factory(ctx) as session:
        return bool(_failure_payload(session, ctx, attempt_id).get("retryable", False))


def _replay(
    session: Session, ctx: TenantContext, *, job_id: str, call_key: str, execution_mode: str,
) -> LlmChatResult | None:
    """Answer from an earlier run of this call, or raise its recorded failure.

    ``None`` when there is nothing terminal to replay (no attempt yet, or one
    the coordinator must resume itself).
    """
    attempt = _latest_attempt(session, ctx, job_id=job_id, call_key=call_key)
    if attempt is None or attempt.dispatch_state in ("prepared", "dispatching"):
        return None
    if attempt.dispatch_state == "succeeded":
        payload = _usage_payload(session, ctx, attempt.id)
        evidence = payload.get("evidence") or {}
        if "output" not in evidence:
            raise LlmChatError(
                "evidence_missing",
                "the call succeeded but its recorded answer is missing; reconcile the attempt",
                attempt_id=attempt.id,
                dispatch_certainty="dispatched",
                replayed=True,
            )
        return LlmChatResult(
            data=dict(evidence["output"]),
            usage=dict(payload.get("usage") or {}),
            model=attempt.model or "",
            model_version=str(evidence.get("model_version") or attempt.model or ""),
            provider=attempt.provider,
            execution_mode=attempt.execution_mode,
            provider_request_id=attempt.provider_request_id,
            latency_ms=int(evidence.get("latency_ms") or 0),
            attempt_id=attempt.id,
            replayed=True,
            cost=dict(payload.get("cost") or {}),
        )
    failure = _failure_payload(session, ctx, attempt.id)
    payload = _usage_payload(session, ctx, attempt.id)
    raise LlmChatError(
        attempt.error_code or ("dispatch_uncertain" if attempt.dispatch_state == "uncertain" else "provider_error"),
        attempt.error_detail or "an earlier run of this call failed",
        retryable=bool(failure.get("retryable", False)),
        dispatch_certainty="uncertain" if attempt.dispatch_state == "uncertain" else (
            "dispatched" if failure.get("billed") else "not_dispatched"
        ),
        attempt_id=attempt.id,
        replayed=True,
        usage=payload.get("usage"),
        cost=payload.get("cost"),
    )
