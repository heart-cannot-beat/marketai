"""OpenAI chat as the ``ExternalCallAdapter`` behind governed ``llm_chat``.

**Chat Completions, not the Responses API.** ``POST /v1/chat/completions``
with ``response_format: {type: "json_schema", strict: true}`` gives
everything ``llm_chat`` needs -- system/user messages, image parts as data
URLs, schema-constrained JSON, a token ceiling (``max_completion_tokens``,
reasoning tokens included) and a usage report -- in one stateless request.
The Responses API keeps conversation state server-side (``store`` defaults
to true there), which a one-shot, evidence-recorded call has no use for and
would have to opt out of on every request; Chat Completions is also the
surface OpenAI-compatible gateways behind a custom ``OPENAI_BASE_URL``
implement. ``store: false`` is still sent explicitly.

Authentication is the Workspace's own key, handed over by the coordinator as
an ephemeral ``memoryview``; it is never stored on the adapter, logged, or
part of an error message.

Outcomes (the coordinator's vocabulary):

* success -- a JSON answer that validates against the request's schema and
  optional pure domain validator;
  ``ProviderResult.reported_cost`` prices the reported usage, so the
  reservation settles at what the call actually cost;
* ``not_dispatched`` -- nothing billed: 4xx refusals (429 ``rate_limited``
  is ``retryable``; a 429 whose ``error.code`` or ``error.type`` is
  ``insufficient_quota`` is ``quota_exhausted`` and, like 401/403
  ``credential_rejected``, is not), or the connection never opened;
* ``uncertain`` -- no trustworthy answer (timeouts, 5xx, a dropped
  connection after the request went out): the reservation stays open for
  reconciliation, never a blind resend (no idempotency key, no lookup);
* ``dispatched`` failure -- the model answered and billed, but its answer
  was unusable twice in a row (JSON/schema and domain validation share one
  retry, with unchanged input and token ceilings), or it refused: the attempt
  fails and the reservation settles at the reported cost.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

import httpx

from app import config
from app.providers import json_schema
from app.providers.adapter import (
    ProviderCapabilities,
    ProviderDispatchError,
    ProviderResult,
    ReportedCost,
)
from app.providers.llm_registry import (
    LLM_PRICE_RULE_VERSION,
    LLM_SCHEMA_RETRIES,
    OPENAI_PROVIDER,
    model_spec,
    usage_cost,
)
from app.providers.llm_types import LlmChatOutput, LlmOutputValidationError, ResolvedLlmChatRequest

DEFAULT_TIMEOUT_SECONDS = 90.0

#: Response statuses after which nothing was generated or billed.
_NOT_DISPATCHED_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 413, 415, 422, 429})

#: How much of an unusable answer the attempt's evidence keeps (per answer).
INVALID_ANSWER_EXCERPT_CHARS = 4000


class _InvalidAnswer(Exception):
    """The model answered, but not with usable JSON."""

    def __init__(self, code: str, detail: str, *, retry: bool, raw: str = "") -> None:
        self.code = code
        self.detail = detail
        self.retry = retry
        #: A bounded excerpt of what the model actually answered, kept as
        #: evidence so a caller can show *why* an answer was unusable.
        self.raw = raw[:INVALID_ANSWER_EXCERPT_CHARS]
        super().__init__(detail)


class OpenAIChatAdapter:
    """``ExternalCallAdapter[ResolvedLlmChatRequest, LlmChatOutput]`` for OpenAI chat."""

    provider: str = OPENAI_PROVIDER
    capabilities: ProviderCapabilities = ProviderCapabilities(
        accepts_idempotency_key=False,
        supports_result_lookup_by_request_id=False,
    )

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or config.OPENAI_BASE_URL).rstrip("/")
        self._timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS
        self._transport = transport

    async def execute(
        self,
        request: ResolvedLlmChatRequest,
        *,
        credential: memoryview | None,
        idempotency_key: str,
    ) -> ProviderResult[LlmChatOutput]:
        if credential is None or len(credential) == 0:
            raise ProviderDispatchError(
                dispatch_certainty="not_dispatched",
                message="no BYOK credential was supplied for the call",
                error_code="credential_unavailable",
                retryable=False,
            )
        spec = model_spec(self.provider, request.model)
        payload = _payload(request, spec.reasoning_effort)
        headers = {"Authorization": "Bearer " + bytes(credential).decode("utf-8").strip()}

        usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
        answer_digests: list[str] = []
        answer_excerpts: list[str] = []
        started = time.monotonic()
        model_version = request.model
        request_id: str | None = None
        http_calls = 0
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                for round_no in range(1 + LLM_SCHEMA_RETRIES):
                    try:
                        response = await client.post(
                            f"{self._base_url}/chat/completions", headers=headers, json=payload,
                        )
                    except httpx.HTTPError as exc:
                        raise self._transport_failure(
                            exc, usage, spec, request, model_version, answer_digests, started, http_calls,
                        ) from exc
                    if response.status_code != 200:
                        raise self._status_failure(
                            response, usage, spec, request, model_version, answer_digests, started, http_calls,
                        )
                    http_calls += 1
                    body = _json_body(response)
                    _add_usage(usage, body)
                    model_version = str(body.get("model") or model_version)
                    request_id = response.headers.get("x-request-id") or body.get("id") or request_id
                    try:
                        data, digest = _parse_answer(body, request.json_schema, request.output_validator)
                    except _InvalidAnswer as invalid:
                        answer_digests.append(invalid.code)
                        answer_excerpts.append(invalid.raw)
                        if invalid.retry and round_no < LLM_SCHEMA_RETRIES:
                            continue
                        raise ProviderDispatchError(
                            dispatch_certainty="dispatched",
                            message=f"OpenAI answer unusable: {invalid.detail}",
                            error_code=invalid.code,
                            retryable=False,
                            reported_cost=_reported_cost(
                                spec, usage, request, model_version, started, http_calls,
                                answer_digests, invalid_excerpts=answer_excerpts,
                            ),
                        ) from invalid
                    latency_ms = _elapsed_ms(started)
                    output = LlmChatOutput(
                        semantic=request.semantic,
                        data=data,
                        usage=dict(usage),
                        model_version=model_version,
                        latency_ms=latency_ms,
                        response_digest=digest,
                        http_calls=http_calls,
                    )
                    return ProviderResult(
                        output=output,
                        provider_request_id=_bounded(request_id),
                        idempotency_key_accepted=False,
                        reported_cost=_reported_cost(
                            spec, usage, request, model_version, started, http_calls,
                            answer_digests, output=output,
                        ),
                    )
        finally:
            headers["Authorization"] = ""
        raise AssertionError("unreachable: every round returns or raises")

    # -- failure classification -------------------------------------------

    def _transport_failure(
        self, exc, usage, spec, request, model_version, answer_digests, started, http_calls,
    ) -> ProviderDispatchError:
        billed = _reported_cost(
            spec, usage, request, model_version, started, http_calls, answer_digests,
        ) if http_calls else None
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            # The connection never opened: this request was never sent.
            return ProviderDispatchError(
                dispatch_certainty="dispatched" if billed else "not_dispatched",
                message=f"OpenAI unreachable: {type(exc).__name__}",
                error_code="provider_unreachable",
                retryable=True,
                reported_cost=billed,
            )
        if isinstance(exc, httpx.TimeoutException):
            return ProviderDispatchError(
                dispatch_certainty="uncertain",
                message=f"OpenAI request timed out after {self._timeout:g}s",
                error_code="provider_timeout",
                retryable=False,
                reported_cost=billed,
            )
        return ProviderDispatchError(
            dispatch_certainty="uncertain",
            message=f"OpenAI request failed in transit: {type(exc).__name__}",
            error_code="provider_connection_lost",
            retryable=False,
            reported_cost=billed,
        )

    def _status_failure(
        self, response, usage, spec, request, model_version, answer_digests, started, http_calls,
    ) -> ProviderDispatchError:
        status = response.status_code
        detail = _error_detail(response)
        billed = _reported_cost(
            spec, usage, request, model_version, started, http_calls, answer_digests,
        ) if http_calls else None
        if status in _NOT_DISPATCHED_STATUSES:
            # An exhausted balance or quota also answers 429, but waiting
            # does not help: the key's account needs topping up.
            code = (
                "quota_exhausted" if status == 429 and "insufficient_quota" in _error_kinds(response)
                else "rate_limited" if status == 429
                else "credential_rejected" if status in (401, 403)
                else f"openai_{status}"
            )
            # A refused *retry* still leaves the first, billed answer behind.
            return ProviderDispatchError(
                dispatch_certainty="dispatched" if billed else "not_dispatched",
                message=f"OpenAI {status}: {detail}",
                error_code=code,
                retryable=code == "rate_limited",
                reported_cost=billed,
            )
        return ProviderDispatchError(
            dispatch_certainty="uncertain",
            message=f"OpenAI {status}: {detail}",
            error_code=f"openai_{status}",
            retryable=False,
            reported_cost=billed,
        )


# ---------------------------------------------------------------------------
# Request / response helpers
# ---------------------------------------------------------------------------


def _payload(request: ResolvedLlmChatRequest, reasoning_effort: str | None) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    last_user = max(
        (index for index, message in enumerate(request.messages) if message.role == "user"),
        default=-1,
    )
    for index, message in enumerate(request.messages):
        if index == last_user and request.images:
            content: Any = [{"type": "text", "text": message.text}] + [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image.mime_type};base64,"
                        + base64.b64encode(image.content).decode("ascii"),
                        "detail": "auto",
                    },
                }
                for image in request.images
            ]
        else:
            content = message.text
        messages.append({"role": message.role, "content": content})
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": request.schema_name,
                "schema": request.json_schema,
                "strict": True,
            },
        },
        "max_completion_tokens": request.max_output_tokens,
        "store": False,
        "n": 1,
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    return payload


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderDispatchError(
            dispatch_certainty="uncertain",
            message="OpenAI returned a 200 without a JSON body",
            error_code="malformed_response",
            retryable=False,
        ) from exc
    if not isinstance(body, dict):
        raise ProviderDispatchError(
            dispatch_certainty="uncertain",
            message="OpenAI returned a 200 with an unexpected body",
            error_code="malformed_response",
            retryable=False,
        )
    return body


def _add_usage(total: dict[str, int], body: dict[str, Any]) -> None:
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    total["input_tokens"] += int(usage.get("prompt_tokens") or 0)
    total["output_tokens"] += int(usage.get("completion_tokens") or 0)
    if isinstance(prompt_details, dict):
        total["cached_input_tokens"] += int(prompt_details.get("cached_tokens") or 0)
    if isinstance(completion_details, dict):
        total["reasoning_tokens"] += int(completion_details.get("reasoning_tokens") or 0)


def _parse_answer(body: dict[str, Any], schema: dict, output_validator=None) -> tuple[dict, str]:
    try:
        choice = body["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise _InvalidAnswer("malformed_response", "no choices in the answer", retry=True) from exc
    if message.get("refusal"):
        raise _InvalidAnswer(
            "model_refusal", f"the model refused: {str(message['refusal'])[:200]}", retry=False,
        )
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        reason = choice.get("finish_reason") or "empty"
        raise _InvalidAnswer("invalid_json", f"no content (finish_reason={reason})", retry=True)
    try:
        data = json.loads(content)
    except ValueError as exc:
        reason = choice.get("finish_reason")
        suffix = f" (finish_reason={reason})" if reason and reason != "stop" else ""
        raise _InvalidAnswer(
            "invalid_json", f"content is not JSON{suffix}", retry=True, raw=content,
        ) from exc
    if not isinstance(data, dict):
        raise _InvalidAnswer(
            "schema_validation_failed", "answer is not a JSON object", retry=True, raw=content,
        )
    errors = json_schema.validate(data, schema)
    if errors:
        raise _InvalidAnswer(
            "schema_validation_failed", "; ".join(errors[:5]), retry=True, raw=content,
        )
    if output_validator is not None:
        try:
            output_validator(data)
        except LlmOutputValidationError as invalid:
            raise _InvalidAnswer(invalid.code, invalid.detail, retry=True, raw=content) from invalid
    return data, hashlib.sha256(content.encode("utf-8")).hexdigest()


def _reported_cost(
    spec,
    usage: dict[str, int],
    request: ResolvedLlmChatRequest,
    model_version: str,
    started: float,
    http_calls: int,
    invalid_answers: list[str],
    *,
    output: LlmChatOutput | None = None,
    invalid_excerpts: list[str] | None = None,
) -> ReportedCost:
    evidence: dict[str, Any] = {
        "semantic": request.semantic,
        "model_version": model_version,
        "latency_ms": output.latency_ms if output is not None else _elapsed_ms(started),
        "http_calls": http_calls,
        "request_digest": request.request_digest,
        "image_sha256": [image.sha256 for image in request.images],
    }
    if invalid_answers:
        evidence["invalid_answers"] = list(invalid_answers)
    if request.output_validation_policy is not None:
        evidence["output_validation_policy"] = request.output_validation_policy
    if invalid_excerpts and any(invalid_excerpts):
        evidence["invalid_answer_excerpts"] = list(invalid_excerpts)
    if output is not None:
        evidence["response_digest"] = output.response_digest
        evidence["output"] = output.data
    return ReportedCost(
        provider_amount=usage_cost(spec, usage),
        provider_currency="USD",
        price_rule_version=LLM_PRICE_RULE_VERSION,
        usage=dict(usage),
        evidence=evidence,
    )


def _error_detail(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
        message = error.get("message") if isinstance(error, dict) else None
    except (ValueError, AttributeError):
        message = None
    return str(message or response.text or "").strip()[:300]


def _error_kinds(response: httpx.Response) -> tuple[str, ...]:
    """The refusal's structured ``error.code`` and ``error.type``; never its message text."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return ()
    if not isinstance(error, dict):
        return ()
    return tuple(value for value in (error.get("code"), error.get("type")) if isinstance(value, str))


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _bounded(request_id: Any) -> str | None:
    """``provider_call_attempts.provider_request_id`` is ``varchar(128)``."""
    if not request_id:
        return None
    return str(request_id)[:128]
