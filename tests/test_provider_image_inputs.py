"""Assert actual provider wire payloads retain the exact source bytes and alpha."""
import asyncio
import base64
import io
import json
from unittest.mock import patch

import httpx
from PIL import Image
import pytest

from app.providers.adapter import ProviderCallRequest, ProviderDispatchError
from app.providers.gemini import GeminiProvider
from app.providers.openai_img import OpenAIImageProvider
from app.providers.seedream import SeedreamProvider


def transparent_png():
    out = io.BytesIO()
    Image.new("RGBA", (7, 5), (12, 34, 56, 70)).save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("provider", [GeminiProvider(), OpenAIImageProvider(), SeedreamProvider()])
@pytest.mark.parametrize("operation", [None, "upscale"])
def test_exact_source_reaches_provider_transport(provider, operation):
    source = transparent_png()
    seen = []
    def handle(request):
        seen.append(request)
        if provider.name == "gemini":
            payload = json.loads(request.content)
            image = payload["contents"][0]["parts"][1]["inline_data"]
            assert image["mime_type"] == "image/png"
            assert base64.b64decode(image["data"]) == source
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(source).decode()}}]}}]})
        if provider.name == "openai":
            assert request.url.path.endswith("/images/edits")
            assert b"Content-Type: image/png" in request.content
            assert source in request.content
        else:
            image = json.loads(request.content)["image"]
            assert image.startswith("data:image/png;base64,")
            assert base64.b64decode(image.split(",", 1)[1]) == source
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(source).decode()}]})
    original = httpx.AsyncClient
    with patch("httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
        result = asyncio.run(provider.execute(ProviderCallRequest("Change the background", (source,), "4:5", "1K",
            provider.default_model, "edit-once", memoryview(b"workspace-secret"), edit_operation=operation)))
    assert result.output_bytes == source
    assert len(seen) == 1


@pytest.mark.parametrize("provider", [GeminiProvider(), OpenAIImageProvider(), SeedreamProvider()])
def test_invalid_source_never_falls_back_to_text_generation(provider):
    with patch("httpx.AsyncClient") as client:
        with pytest.raises(ProviderDispatchError) as error:
            asyncio.run(provider.execute(ProviderCallRequest("Edit", (b"not an image",), "1:1", "1K", provider.default_model, "edit", None)))
    assert error.value.dispatch_certainty == "not_dispatched"
    client.assert_not_called()
