"""ByteDance Seedream (Volcengine Ark) as an ``ExternalCallAdapter`` for BYOK RunItems.

The legacy provider (``app.providers.seedream``) already sent reference
images to Ark's ``/images/generations`` as data URLs, but only on the
pre-governance path. This adapter reconnects the same wire format
(``seedream_size`` / ``build_seedream_payload``) to the governed path: the
Worker's ``ExternalCallCoordinator`` reserves, dispatches with the
Workspace's own Ark key (bound as provider ``seedream``) and settles.

Translation of the :class:`CreativeRecipe`: the text prompt when no
reference is sent, otherwise the reference instruction followed by the
scene prompt, with the reference images as ``image`` (primary first).
Seedream 4.5 renders at the 2K size table for the recipe's aspect ratio.

Failure classification: 4xx -> ``not_dispatched``; timeouts, transport
errors and 5xx -> ``uncertain``; a 200 without image data -> ``uncertain``
(Ark may have billed it; reconciliation decides).
"""
from __future__ import annotations

import base64

import httpx

from app import config
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
from app.providers.seedream import build_seedream_payload, seedream_size

SEEDREAM_PROVIDER = "seedream"
ENDPOINT = "images/generations"


class SeedreamImageAdapter:
    """``ExternalCallAdapter[ByokGenerationRequest, GenerationOutput]`` for Seedream."""

    provider: str = SEEDREAM_PROVIDER
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
        self._base_url = (base_url or config.ARK_BASE_URL).rstrip("/")
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
        recipe_format = request.recipe.format
        size = seedream_size(
            request.model, recipe_format.aspect_ratio, str(recipe_format.image_size or ""),
        )
        payload = build_seedream_payload(request.model, plan.prompt, images, size)
        headers = {"Authorization": "Bearer " + key}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(
                    f"{self._base_url}/{ENDPOINT}", headers=headers, json=payload,
                )
        except httpx.HTTPError as exc:
            raise transport_failure("Seedream", exc, self._timeout) from exc
        finally:
            headers["Authorization"] = ""

        if response.status_code != 200:
            raise status_failure("Seedream", "seedream", response)
        try:
            body = response.json()
            first = body["data"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise malformed("Seedream", "no image data") from exc

        content = b""
        if first.get("b64_json"):
            try:
                content = base64.b64decode(first["b64_json"])
            except (ValueError, TypeError) as exc:
                raise malformed("Seedream", "undecodable image data") from exc
        elif first.get("url"):
            try:
                content = await download(first["url"], transport=self._transport)
            except httpx.HTTPError as exc:
                raise transport_failure("Seedream", exc, 60) from exc
        if not content:
            raise malformed("Seedream", "an empty image")

        usage = body.get("usage") if isinstance(body, dict) else None
        return ProviderResult(
            output=image_output(SEEDREAM_PROVIDER, content, usage if isinstance(usage, dict) else None),
            provider_request_id=response.headers.get("x-request-id") or body.get("id"),
            idempotency_key_accepted=False,
        )
