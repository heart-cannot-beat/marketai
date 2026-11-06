"""Tests for the unified provider adapter protocol (Task 16).

Verifies that every image-generation provider exposes the
``UnifiedProviderAdapter`` protocol surface -- ``capabilities`` property and
``execute()`` method -- and that failures raise ``ProviderDispatchError`` with
the correct ``dispatch_certainty``.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.providers.adapter import (
    ProviderCallRequest,
    ProviderCallResult,
    ProviderCapabilities,
    ProviderDispatchError,
    UnifiedProviderAdapter,
)
from app.providers.mock import MockProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(**overrides) -> ProviderCallRequest:
    defaults = {
        "prompt": "a red shoe on white background",
        "reference_inputs": (),
        "aspect_ratio": "1:1",
        "image_size": "1K",
        "model": "mock-v1",
        "idempotency_key": "idem-key-001",
        "credential": None,
    }
    defaults.update(overrides)
    return ProviderCallRequest(**defaults)


# ---------------------------------------------------------------------------
# Mock adapter capabilities
# ---------------------------------------------------------------------------


class TestMockAdapterCapabilities:
    def test_mock_adapter_capabilities(self):
        provider = MockProvider()
        caps = provider.capabilities
        assert isinstance(caps, ProviderCapabilities)
        assert caps.accepts_idempotency_key is True
        assert caps.supports_result_lookup_by_request_id is False


# ---------------------------------------------------------------------------
# Mock execute returns result
# ---------------------------------------------------------------------------


class TestMockExecuteReturnsResult:
    def test_mock_execute_returns_result(self):
        provider = MockProvider()
        request = _make_request()
        result = asyncio.run(provider.execute(request))
        assert isinstance(result, ProviderCallResult)
        assert isinstance(result.output_bytes, bytes)
        assert len(result.output_bytes) > 0
        assert result.idempotency_key_accepted is True
        assert result.provider_request_id is None


# ---------------------------------------------------------------------------
# Mock execute raises dispatch error on failure
# ---------------------------------------------------------------------------


class TestMockExecuteRaisesDispatchErrorOnFailure:
    def test_mock_execute_raises_dispatch_error_on_failure(self):
        provider = MockProvider()
        request = _make_request()
        with patch.object(
            provider, "generate", new_callable=AsyncMock, side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(ProviderDispatchError) as exc_info:
                asyncio.run(provider.execute(request))
            assert exc_info.value.dispatch_certainty == "not_dispatched"


# ---------------------------------------------------------------------------
# All providers implement protocol
# ---------------------------------------------------------------------------


class TestAllProvidersImplementProtocol:
    def test_all_providers_implement_protocol(self):
        from app.providers.gemini import GeminiProvider
        from app.providers.openai_img import OpenAIImageProvider
        from app.providers.seedream import SeedreamProvider

        for cls in (MockProvider, GeminiProvider, OpenAIImageProvider, SeedreamProvider):
            instance = cls()
            assert isinstance(instance, UnifiedProviderAdapter), (
                f"{cls.__name__} does not satisfy UnifiedProviderAdapter"
            )


# ---------------------------------------------------------------------------
# ProviderDispatchError has certainty
# ---------------------------------------------------------------------------


class TestProviderDispatchErrorHasCertainty:
    @pytest.mark.parametrize(
        "certainty",
        ["not_dispatched", "may_have_dispatched", "dispatched"],
    )
    def test_provider_dispatch_error_has_certainty(self, certainty: str):
        err = ProviderDispatchError(
            dispatch_certainty=certainty,
            message="test error",
            error_code="TEST_ERR",
        )
        assert err.dispatch_certainty == certainty
        assert err.error_code == "TEST_ERR"
        assert "test error" in str(err)


@pytest.mark.parametrize("provider_name", ["openai", "gemini", "seedream"])
def test_real_adapter_wire_contains_the_supplied_product_photo(provider_name):
    import base64
    import hashlib
    import json
    import httpx
    from app.providers.openai_img import OpenAIImageProvider
    from app.providers.gemini import GeminiProvider
    from app.providers.seedream import SeedreamProvider
    from tests.support.library_fixtures import png_bytes
    source = png_bytes(colour=(20, 70, 150))
    requests = []
    def respond(request):
        requests.append(request)
        if provider_name == 'openai':
            assert request.url.path.endswith('/images/edits')
            assert b'name="image[]"' in request.content
            assert b'Content-Type: image/png' in request.content
            assert source in request.content
        else:
            payload = json.loads(request.content)
            if provider_name == 'gemini':
                refs = [p['inline_data'] for p in payload['contents'][0]['parts'] if 'inline_data' in p]
                assert len(refs) == 1
                assert base64.b64decode(refs[0]['data']) == source
            else:
                refs = [payload['image']] if isinstance(payload['image'], str) else payload['image']
                assert len(refs) == 1
                assert refs[0].startswith('data:image/png;base64,')
                assert base64.b64decode(refs[0].split(',', 1)[1]) == source
        encoded = base64.b64encode(png_bytes()).decode()
        body = {'candidates':[{'content':{'parts':[{'inlineData':{'data':encoded}}]}}]} if provider_name == 'gemini' else {'data':[{'b64_json':encoded}]}
        return httpx.Response(200, headers={'x-request-id':'req-real-wire-fixture'}, json=body)
    client = httpx.AsyncClient
    providers = {'openai':OpenAIImageProvider, 'gemini':GeminiProvider, 'seedream':SeedreamProvider}
    with patch('httpx.AsyncClient', side_effect=lambda **kw: client(transport=httpx.MockTransport(respond), **kw)):
        result = asyncio.run(providers[provider_name]().execute(_make_request(reference_inputs=(source,), credential=memoryview(b'test-only'))))
    assert len(requests) == 1
    assert result.provider_request_id == 'req-real-wire-fixture'
    assert result.metadata['reference_input_sha256'] == [hashlib.sha256(source).hexdigest()]


def test_invalid_reference_bytes_never_fall_back_to_a_paid_text_only_image():
    from app.providers.openai_img import OpenAIImageProvider
    with patch('httpx.AsyncClient') as client:
        with pytest.raises(ProviderDispatchError) as error:
            asyncio.run(OpenAIImageProvider().execute(_make_request(reference_inputs=(b'broken-image',), credential=memoryview(b'test-only'))))
    assert error.value.dispatch_certainty == 'not_dispatched'
    client.assert_not_called()


def test_mock_corrections_with_same_visible_prefix_and_palette_have_distinct_content():
    import hashlib
    common = 'Product facts: ' + ('shared confirmed product description ' * 90)
    first = common + ' original execution'
    palette = int(hashlib.md5(first.encode()).hexdigest(), 16) % 6
    second = next(common + f' revised execution {i}' for i in range(100)
                  if int(hashlib.md5((common + f' revised execution {i}').encode()).hexdigest(), 16) % 6 == palette)
    async def generate():
        provider = MockProvider()
        return [await provider.generate(prompt, [], '4:5', '1K', 'mock-v1') for prompt in (first, second, second)]
    original, corrected, replay = asyncio.run(generate())
    assert original != corrected
    assert corrected == replay
