"""Provider adapter protocols for ExternalCallCoordinator dispatch.

This module has two layers:

1. **Legacy, image-generation-specific** (``UnifiedProviderAdapter``,
   ``ProviderCallRequest``, ``ProviderCallResult``) -- unchanged from before
   Task 10, still consumed by ``app.entitlements.coordinator`` and every
   concrete image provider (``app.providers.{mock,gemini,openai_img,
   seedream}``).  Kept verbatim for backward compatibility.
2. **Generic, domain-agnostic** (``ExternalCallAdapter``, ``ProviderResult``)
   -- added by Task 10 per spec §2D.  ``app.providers.dispatch`` dispatches
   against this protocol; Localization (and, eventually, Generation) supply
   typed ``RequestT``/``ResultT`` value objects and a concrete adapter.  The
   protocol never encodes what kind of call it is -- ``step_kind`` is
   metadata recorded on ``ProviderCallAttemptRecord``, never a branch here.

``ProviderDispatchError`` and ``ProviderCapabilities`` are shared by both
layers: they already carry no image-generation-specific fields.

``ProviderDispatchError`` is defined here so the providers module is
self-contained.  The coordinator (Task 14) re-exports or re-defines the
same class; when both land, a single canonical location will be chosen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Generic, Mapping, Protocol, TypeVar, runtime_checkable


# ---------------------------------------------------------------------------
# Reported cost (usage-priced settlement)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportedCost:
    """What a call actually cost, priced from the provider's own usage report.

    An adapter whose provider bills by metered usage (tokens) returns this
    with its result -- or attaches it to a ``ProviderDispatchError`` raised
    *after* the provider already billed -- so the coordinator settles the
    reservation with a ``reported_cost`` ledger entry for the real amount and
    a ``release`` entry for the unused remainder, instead of the
    ``estimated_settlement`` of the reservation's maximum it records for
    providers that report nothing usable.

    ``usage`` and ``evidence`` are opaque to the coordinator; they are
    appended verbatim to the attempt's ``usage_reported`` event. They must
    never contain a credential or raw input bytes -- digests only.
    """

    provider_amount: Decimal
    provider_currency: str
    price_rule_version: str
    usage: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    simulated_amount: Decimal | None = None


# ---------------------------------------------------------------------------
# Dispatch error
# ---------------------------------------------------------------------------


class ProviderDispatchError(Exception):
    """Raised when a provider call fails.

    ``dispatch_certainty`` is one of:

    * ``not_dispatched`` -- nothing was generated or billed (the reservation
      is released);
    * ``uncertain`` -- the outcome is unknown (the reservation stays open
      for reconciliation);
    * ``dispatched`` -- the provider answered *and billed*, but the answer is
      unusable (e.g. output that never satisfied the requested schema). The
      attempt fails terminally and the reservation is settled at
      ``reported_cost`` (or at its maximum when no cost was reported).

    ``retryable`` classifies the failure for the caller (a rate limit may be
    retried later as a new call; a rejected credential may not until the key
    is rebound); ``None`` means the adapter does not say.
    """

    def __init__(
        self,
        dispatch_certainty: str,
        message: str = "",
        error_code: str | None = None,
        *,
        retryable: bool | None = None,
        reported_cost: ReportedCost | None = None,
    ) -> None:
        self.dispatch_certainty = dispatch_certainty
        self.error_code = error_code
        self.retryable = retryable
        self.reported_cost = reported_cost
        super().__init__(message or f"provider dispatch failed ({dispatch_certainty})")


# ---------------------------------------------------------------------------
# Capability / request / result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declares what a provider natively supports."""

    accepts_idempotency_key: bool
    supports_result_lookup_by_request_id: bool


@dataclass(frozen=True)
class ProviderCallRequest:
    """Canonical input for a single provider call."""

    prompt: str
    reference_inputs: tuple[bytes, ...]  # reference image bytes
    aspect_ratio: str
    image_size: str
    model: str
    idempotency_key: str
    credential: memoryview | None  # BYOK credential or None for mock
    edit_operation: str | None = None
    edit_mask: bytes | None = None


@dataclass(frozen=True)
class ProviderCallResult:
    """Canonical output from a successful provider call."""

    output_bytes: bytes
    provider_request_id: str | None
    idempotency_key_accepted: bool
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class UnifiedProviderAdapter(Protocol):
    """Protocol that every provider must satisfy for coordinator dispatch."""

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def execute(self, request: ProviderCallRequest) -> ProviderCallResult: ...


# ---------------------------------------------------------------------------
# Generic (domain-agnostic) adapter protocol -- spec §2D
# ---------------------------------------------------------------------------
#
# Localization (and later Generation, once migrated) define their own typed
# RequestT/ResultT value objects and register an adapter satisfying this
# protocol.  ``app.providers.dispatch.ExternalCallCoordinator`` dispatches
# against ``ExternalCallAdapter`` only; it never imports a concrete request
# or result type, and it never chooses an adapter based on ``step_kind``.


RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class ProviderResult(Generic[ResultT]):
    """Canonical typed output from a successful generic external call.

    ``output`` is the caller-defined typed result value (translated
    segments, an edited image buffer, an OCR check, ...). The coordinator
    never inspects it; only the caller's ``ExternalCallEvidenceRecorder``
    does, when materializing/persisting evidence.
    """

    output: ResultT
    provider_request_id: str | None
    idempotency_key_accepted: bool
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Usage-priced cost of this call; ``None`` keeps the coordinator's
    #: estimated settlement at the reservation's maximum.
    reported_cost: ReportedCost | None = None


@runtime_checkable
class ExternalCallAdapter(Protocol[RequestT, ResultT]):
    """Generic protocol every registered external-call adapter must satisfy.

    ``provider`` and ``capabilities`` are plain attributes (not properties)
    so a simple dataclass or a class with class-level attributes can satisfy
    this Protocol without ceremony.

    An adapter that advertises
    ``capabilities.supports_result_lookup_by_request_id=True`` should also
    implement ``async def lookup_by_request_id(self, request_id: str) ->
    ProviderResult[ResultT] | None`` for capability-aware reconciliation
    (spec §3G). That method is intentionally *not* part of this Protocol's
    required surface -- adapters that never claim the capability need not
    implement it, and the coordinator only calls it (via ``getattr``) when
    the capability flag says it exists.
    """

    provider: str
    capabilities: ProviderCapabilities

    async def execute(
        self,
        request: RequestT,
        *,
        credential: memoryview | None,
        idempotency_key: str,
    ) -> ProviderResult[ResultT]: ...
