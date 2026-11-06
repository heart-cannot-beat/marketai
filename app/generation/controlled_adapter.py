"""ControlledGenerationAdapter for the three-phase coordinated path.

Implements ``ExternalCallAdapter[GenerationRequest, GenerationOutput]`` for
controlled-mock generation.  Same deterministic output as the inline mock
in ``worker._call_adapter``, but exercising the full ``ExternalCallAdapter``
protocol so the coordinator's three-phase dispatch fence is tested end to
end.

The controlled-mock adapter is the adapter the cross-area E2E test uses
(Issue #98 Task 1).  It carries ``execution_mode="controlled_mock"`` and
``chargeability="free_sandbox"`` (no real provider cost), but dispatches
through the coordinator's committed-prepare -> external-dispatch ->
committed-completion seam, proving that the attempt record survives a
SIGKILL between any two of those phases.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.providers.adapter import ExternalCallAdapter, ProviderCapabilities, ProviderResult


@dataclass(frozen=True)
class GenerationRequest:
    """Input to the generation adapter.

    Carries the semantic fingerprint needed to produce deterministic mock
    output, matching the same pattern as ``worker._call_adapter``.
    """

    semantic_fingerprint: str
    execution_mode: str


@dataclass(frozen=True)
class GenerationOutput:
    """Output from the generation adapter.

    A value object mirroring the dict keys that ``worker._call_adapter``
    and ``worker._persist_output`` consume.

    A real provider adapter (``app.generation.openai_adapter``) also returns
    the image bytes themselves in ``content_bytes``; the evidence recorder
    commits them to the blob store during *materialize* and the committed
    reference is what the AssetVersion records. The controlled mock leaves
    it ``None`` -- its output is synthetic and never stored.
    """

    object_key: str
    content_sha256: str
    byte_size: int
    mime_type: str
    usage: dict[str, Any] = field(default_factory=dict)
    content_bytes: bytes | None = field(default=None, repr=False, compare=False)


class ControlledGenerationAdapter:
    """``ExternalCallAdapter`` for controlled-mock generation.

    Produces deterministic mock output identical to the inline mock in
    ``worker._call_adapter``, but through the ``ExternalCallAdapter``
    protocol required by ``ExternalCallCoordinator``.
    """

    provider: str = "controlled_mock"
    capabilities: ProviderCapabilities = ProviderCapabilities(
        accepts_idempotency_key=True,
        supports_result_lookup_by_request_id=False,
    )

    async def execute(
        self,
        request: GenerationRequest,
        *,
        credential: memoryview | None,
        idempotency_key: str,
    ) -> ProviderResult[GenerationOutput]:
        content = f"mock_output_{request.semantic_fingerprint[:16]}"
        output = GenerationOutput(
            object_key=f"mock/{content}.png",
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            byte_size=1024,
            mime_type="image/png",
            usage={"input_tokens": 0, "output_tokens": 0},
        )
        return ProviderResult(
            output=output,
            provider_request_id=f"controlled_mock:{idempotency_key}",
            idempotency_key_accepted=True,
        )
