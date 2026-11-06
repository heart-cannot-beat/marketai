"""OpenAI GPT-Image as an ``ExternalCallAdapter`` for Generation RunItems.

The adapter receives the RunItem's :class:`CreativeRecipe` (inside a
:class:`ByokGenerationRequest`) and translates it itself:

* no reference images to send -> one ``POST /v1/images/generations`` with
  the text prompt (``prompting.compose_text_prompt``, unchanged from before
  the recipe existed);
* the recipe sends reference images (frozen capability decision) -> one
  ``POST /v1/images/edits`` (multipart) with the product's committed
  reference derivatives, primary first, and an instruction that keeps the
  product's appearance -- naming the confirmed must-match items -- while the
  scene follows the dimension fragments (``prompting.compose_reference_prompt``).

The call is authenticated with the Workspace's own key (the BYOK credential
the coordinator hands over as an ephemeral ``memoryview``; it is never
stored on the adapter, never logged and never part of an error message). The
response's ``b64_json`` image comes back as ``GenerationOutput.content_bytes``
for the evidence recorder to commit.

Failure classification follows the coordinator's outcomes:

* ``not_dispatched`` -- the provider answered and generated nothing
  (4xx: bad request, unauthorized, content policy, rate limit), or a
  reference image could not be read so nothing was sent. The reservation is
  released; the item fails with the reason.
* ``uncertain`` -- no trustworthy answer (timeouts, connection loss, 5xx):
  the image may or may not have been generated and billed, so the attempt
  stays ``uncertain`` for reconciliation rather than being retried blindly.

GPT-Image accepts no idempotency key and offers no lookup by request id,
so ``capabilities`` says so and ``reconcile()`` never resends on its own.
"""
from __future__ import annotations

import base64

import httpx

from app import config
from app.generation import pricing
from app.generation.controlled_adapter import GenerationOutput
from app.generation.image_call import (
    ByokGenerationRequest,
    ProviderCallPlan,
    download,
    image_output,
    load_reference_images,
    malformed,
    require_credential,
    status_failure,
    transport_failure,
)
from app.generation.prompting import compose_reference_prompt, compose_text_prompt
from app.providers.adapter import ProviderCapabilities, ProviderResult

__all__ = ["ByokGenerationRequest", "OPENAI_PROVIDER", "OpenAIImageAdapter"]

OPENAI_PROVIDER = "openai"

#: Models documented to accept ``input_fidelity=high`` on image edits (keeps
#: logos and fine product detail). Others are sent the default fidelity.
INPUT_FIDELITY_MODELS = frozenset({"gpt-image-1"})

ENDPOINT_GENERATIONS = "images/generations"
ENDPOINT_EDITS = "images/edits"


class OpenAIImageAdapter:
    """``ExternalCallAdapter[ByokGenerationRequest, GenerationOutput]`` for GPT-Image."""

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
        self._timeout = timeout if timeout is not None else config.GEN_TIMEOUT
        self._transport = transport

    def plan(self, request: ByokGenerationRequest) -> ProviderCallPlan:
        recipe = request.recipe
        references = recipe.references_to_send
        if references:
            return ProviderCallPlan(
                endpoint=ENDPOINT_EDITS,
                prompt=compose_reference_prompt(recipe, len(references)),
                references=references,
            )
        return ProviderCallPlan(endpoint=ENDPOINT_GENERATIONS, prompt=compose_text_prompt(recipe))

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
        size = pricing.openai_size(format_config)
        quality = pricing.openai_quality(format_config)
        headers = {"Authorization": "Bearer " + key}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport,
            ) as client:
                if plan.endpoint == ENDPOINT_EDITS:
                    form = {
                        "model": request.model,
                        "prompt": plan.prompt,
                        "size": size,
                        "quality": quality,
                        "n": "1",
                    }
                    if request.model in INPUT_FIDELITY_MODELS:
                        form["input_fidelity"] = "high"
                    files = [
                        ("image[]", (f"reference-{index}.{_ext(mime)}", content, mime))
                        for index, (mime, content) in enumerate(images)
                    ]
                    response = await client.post(
                        f"{self._base_url}/{ENDPOINT_EDITS}",
                        headers=headers, data=form, files=files,
                    )
                else:
                    response = await client.post(
                        f"{self._base_url}/{ENDPOINT_GENERATIONS}",
                        headers=headers,
                        json={
                            "model": request.model,
                            "prompt": plan.prompt,
                            "size": size,
                            "quality": quality,
                            "n": 1,
                        },
                    )
        except httpx.HTTPError as exc:
            raise transport_failure("OpenAI", exc, self._timeout) from exc
        finally:
            headers["Authorization"] = ""

        if response.status_code != 200:
            raise status_failure("OpenAI", "openai", response)

        try:
            body = response.json()
            first = body["data"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise malformed("OpenAI", "no image data") from exc

        content = b""
        if first.get("b64_json"):
            try:
                content = base64.b64decode(first["b64_json"])
            except (ValueError, TypeError) as exc:
                raise malformed("OpenAI", "undecodable image data") from exc
        elif first.get("url"):
            try:
                content = await download(first["url"], transport=self._transport)
            except httpx.HTTPError as exc:
                raise transport_failure("OpenAI", exc, 60) from exc
        if not content:
            raise malformed("OpenAI", "an empty image")

        usage = body.get("usage") if isinstance(body, dict) else None
        return ProviderResult(
            output=image_output(OPENAI_PROVIDER, content, usage if isinstance(usage, dict) else None),
            provider_request_id=response.headers.get("x-request-id"),
            idempotency_key_accepted=False,
        )


def _ext(mime: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
