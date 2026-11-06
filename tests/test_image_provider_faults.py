"""Actual image request adapters with controlled HTTP, never vendor calls."""
import asyncio
import base64
import json
from unittest.mock import patch

import httpx
import pytest

from app.providers.adapter import ProviderCallRequest, ProviderDispatchError
from app.providers.gemini import GeminiProvider
from app.providers.openai_img import OpenAIImageProvider
from app.providers.seedream import SeedreamProvider
from tests.support.library_fixtures import png_bytes


PROVIDERS = [(GeminiProvider, "gemini-3.1-flash-image"),
             (SeedreamProvider, "doubao-seedream-4-5-251128"),
             (OpenAIImageProvider, "gpt-image-1.5")]


def request(model, **changes):
    return ProviderCallRequest(**{
        "prompt": "Use this exact product photo", "reference_inputs": (png_bytes(),),
        "aspect_ratio": "4:5", "image_size": "1K", "model": model,
        "idempotency_key": "fixture-intent", "credential": memoryview(b"fixture-key"),
        **changes,
    })


def execute(provider, model, handler, **changes):
    client = httpx.AsyncClient
    with patch("httpx.AsyncClient", side_effect=lambda **kw: client(transport=httpx.MockTransport(handler), **kw)):
        return asyncio.run(provider().execute(request(model, **changes)))


@pytest.mark.parametrize("ratio,expected", [("1:1", "2048x2048"), ("4:5", "1728x2160"), ("9:16", "1440x2560"), ("16:9", "2560x1440")])
def test_seedream_45_default_size_is_accepted_2k_with_attached_photo(ratio, expected):
    calls = []
    def respond(req):
        calls.append(req)
        body = json.loads(req.content)
        assert body["size"] == expected
        assert body["image"].startswith("data:image/png;base64,")
        assert base64.b64decode(body["image"].split(",", 1)[1]) == png_bytes()
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]})
    result = execute(SeedreamProvider, PROVIDERS[1][1], respond, aspect_ratio=ratio)
    assert result.output_bytes == png_bytes()
    assert len(calls) == 1


@pytest.mark.parametrize("provider,model", PROVIDERS)
@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_explicit_http_rejection_is_not_dispatched(provider, model, status):
    with pytest.raises(ProviderDispatchError) as err:
        execute(provider, model, lambda req: httpx.Response(status, text="request refused"))
    assert err.value.dispatch_certainty == "not_dispatched"


@pytest.mark.parametrize("provider,model", PROVIDERS)
@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_ambiguous_http_failure_does_not_release_exposure_by_error_word(provider, model, status):
    calls = []
    def respond(req):
        calls.append(req)
        return httpx.Response(status, text="invalid upstream safety policy response 400")
    with pytest.raises(ProviderDispatchError) as err:
        execute(provider, model, respond)
    assert err.value.dispatch_certainty == "may_have_dispatched"
    assert len(calls) == 1  # never an automatic paid retry


@pytest.mark.parametrize("provider,model", PROVIDERS)
@pytest.mark.parametrize("kind", ["timeout", "malformed", "no_image", "safety"])
def test_post_dispatch_failure_remains_uncertain(provider, model, kind):
    calls = []
    def respond(req):
        calls.append(req)
        if kind == "timeout":
            raise httpx.ReadTimeout("fixture timeout", request=req)
        if kind == "malformed":
            return httpx.Response(200, text="invalid JSON")
        if kind == "safety":
            return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
        return httpx.Response(200, json={"data": [], "candidates": []})
    with pytest.raises(ProviderDispatchError) as err:
        execute(provider, model, respond)
    assert err.value.dispatch_certainty == "may_have_dispatched"
    assert len(calls) == 1


@pytest.mark.parametrize("provider,model", PROVIDERS)
def test_invalid_reference_cannot_send_a_text_only_paid_request(provider, model):
    with patch("httpx.AsyncClient") as client:
        with pytest.raises(ProviderDispatchError) as err:
            asyncio.run(provider().execute(request(model, reference_inputs=(b"broken-image",))))
    assert err.value.dispatch_certainty == "not_dispatched"
    client.assert_not_called()


@pytest.mark.parametrize("provider,model", PROVIDERS)
def test_missing_reference_path_cannot_be_silently_omitted(provider, model, tmp_path):
    with patch("httpx.AsyncClient") as client:
        with pytest.raises(ProviderDispatchError) as err:
            asyncio.run(provider().execute(request(model, reference_inputs=(tmp_path / "missing.jpg",))))
    assert err.value.dispatch_certainty == "not_dispatched"
    client.assert_not_called()
