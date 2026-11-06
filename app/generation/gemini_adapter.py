"""Google Gemini image as an ``ExternalCallAdapter`` for BYOK Generation RunItems.

The legacy provider (``app.providers.gemini``) already spoke
``generateContent`` with inline reference images, but only on the
pre-governance path: a platform-wide key, no reservation, no attempt
evidence and an empty reference list. This adapter reconnects the same wire
format (``build_generate_content_payload`` / ``extract_inline_image``) to the
governed path: the Worker's ``ExternalCallCoordinator`` reserves, dispatches
with the Workspace's own Gemini key (bound as provider ``gemini``) and
settles, exactly like OpenAI.

Translation of the :class:`CreativeRecipe`: the text prompt when no
reference is sent, otherwise the reference instruction followed by the
scene prompt, with the reference images inline after it (primary first).
Aspect ratio goes to ``imageConfig.aspectRatio`` and the size tier
(``1K`` unless the format says otherwise) to ``imageConfig.imageSize``.

Failure classification:

* 4xx (bad request, key rejected, quota) -> ``not_dispatched``;
* timeouts, transport errors, 5xx -> ``uncertain``;
* a 200 whose prompt was blocked (``promptFeedback.blockReason``) ->
  ``not_dispatched`` (nothing was generated);
* any other 200 without an image -> ``dispatched``: the model answered and
  billed its tokens, but produced nothing usable; the attempt fails
  terminally and the reservation is settled at its maximum.
"""
from __future__ import annotations

import httpx

from app import config
from app.generation import pricing
from app.generation.controlled_adapter import GenerationOutput
from app.generation.image_call import (
    ByokGenerationRequest,
    ProviderCallPlan,
    image_output,
    load_reference_images,
    malformed,
    require_credential,
    status_failure,
    transport_failure,
)
from app.generation.prompting import compose_reference_prompt, compose_text_prompt
from app.providers.adapter import ProviderCapabilities, ProviderDispatchError, ProviderResult
from app.providers.gemini import build_generate_content_payload, extract_inline_image, no_image_reason

GEMINI_PROVIDER = "gemini"
ENDPOINT = "generateContent"


class GeminiImageAdapter:
    """``ExternalCallAdapter[ByokGenerationRequest, GenerationOutput]`` for Gemini image."""

    provider: str = GEMINI_PROVIDER
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
        self._base_url = (base_url or config.GEMINI_BASE_URL).rstrip("/")
        self._timeout = timeout if timeout is not None else config.GEN_TIMEOUT
        self._transport = transport

    def plan(self, request: ByokGenerationRequest) -> ProviderCallPlan:
        recipe = request.recipe
        references = recipe.references_to_send
        if references:
            return ProviderCallPlan(
                endpoint=ENDPOINT,
                prompt=compose_reference_prompt(recipe, len(references)),
                references=references,
            )
        return ProviderCallPlan(endpoint=ENDPOINT, prompt=compose_text_prompt(recipe))

    async def execute(
        self,
        request: ByokGenerationRequest,
        *,
        credential: memoryview | None,
        idempotency_key: str,
    ) -> ProviderResult[GenerationOutput]:
        key = require_credential(credential)
        plan = self.plan(request)
        images = await load_reference_images(request, plan.references)
        format_config = request.recipe.format.as_format_config()
        payload = build_generate_content_payload(
            plan.prompt,
            images,
            request.recipe.format.aspect_ratio,
            pricing.gemini_image_size(format_config),
        )
        headers = {"x-goog-api-key": key}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(
                    f"{self._base_url}/models/{request.model}:{ENDPOINT}",
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise transport_failure("Gemini", exc, self._timeout) from exc
        finally:
            headers["x-goog-api-key"] = ""

        if response.status_code != 200:
            raise status_failure("Gemini", "gemini", response)
        try:
            body = response.json()
        except ValueError as exc:
            raise malformed("Gemini", "a body that is not JSON") from exc
        if not isinstance(body, dict):
            raise malformed("Gemini", "an unexpected body")

        found = None
        try:
            found = extract_inline_image(body)
        except (ValueError, TypeError) as exc:
            raise malformed("Gemini", "undecodable image data") from exc
        if found is None or not found[0]:
            reason = no_image_reason(body) or "no image in the answer"
            blocked = isinstance(body.get("promptFeedback"), dict) and body["promptFeedback"].get("blockReason")
            raise ProviderDispatchError(
                dispatch_certainty="not_dispatched" if blocked else "dispatched",
                message=f"Gemini returned no image ({reason})",
                error_code="provider_refused" if blocked else "no_image_returned",
            )
        usage = body.get("usageMetadata")
        return ProviderResult(
            output=image_output(GEMINI_PROVIDER, found[0], usage if isinstance(usage, dict) else None),
            provider_request_id=response.headers.get("x-goog-request-id") or body.get("responseId"),
            idempotency_key_accepted=False,
        )
