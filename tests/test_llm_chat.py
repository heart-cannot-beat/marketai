"""Governed ``llm_chat`` (Issue #160): BYOK OpenAI chat with reservation and settlement.

Covers:

* the JSON-schema subset validator and the token price table;
* the OpenAI chat adapter's HTTP contract against a fake transport --
  success, 429, 401, timeout, unreachable, 5xx, invalid JSON re-asked once
  then failed, schema violations, refusals, a missing key;
* the controlled mock -- registered fixtures, schema synthesis, a fixture
  that drifts from its schema;
* the governed path end to end through ``ExternalCallCoordinator`` in both
  modes: ``mock`` (no real reservation, simulated cost, usage on the
  attempt) and ``byok`` (quote + reservation at admission, reported cost +
  release at settlement, the Workspace's own key on the wire);
* failure semantics on the attempt and the reservation (released when
  nothing was billed, settled when it was, left open when uncertain);
* ``credential_unavailable``, missing/insufficient reservations, guardrails;
* replay of a finished call without a second provider request;
* every written key fitting its PostgreSQL ``varchar`` column (PR #150);
* negative tenant isolation: jobs, keys, credential versions and blob
  images of another Workspace are never usable.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import unittest
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, new_id
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendReservationRecord,
)
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import LocalKmsAdapter
from app.entitlements.models import EntitlementProjectionRecord
from app.generation.models import ProviderAttemptEventRecord, ProviderAttemptProjectionRecord
from app.platform.blob_store import CommittedBlobRef
from app.platform.operations import create_operation
from app.providers import json_schema
from app.providers.adapter import ProviderDispatchError
from app.providers.dispatch import ExternalCallCoordinator
from app.providers.llm_chat import (
    LlmChatClient,
    LlmChatError,
    LlmChatRequest,
    LlmImage,
    estimate_request_input_tokens,
    llm_call_key,
    llm_capability,
    quote_llm_chat,
    release_llm_chat_reservation,
    reserve_llm_chat,
    resolve_llm_execution_mode,
)
from app.providers.llm_mock import (
    EXAMPLE_SUMMARY_SCHEMA,
    EXAMPLE_SUMMARY_SEMANTIC,
    MockLlmChatAdapter,
    register_llm_mock_fixture,
    synthesize_from_schema,
    unregister_llm_mock_fixture,
)
from app.providers.llm_registry import (
    LLM_PRICE_RULE_VERSION,
    call_max_cost,
    model_spec,
    request_max_cost,
    usage_cost,
)
from app.providers.llm_types import LlmMessage, LlmOutputValidationError, ResolvedImage, ResolvedLlmChatRequest
from app.providers.openai_chat import OpenAIChatAdapter
from tests.support.library_fixtures import LibraryFixtures

PNG = (
    b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "selling_points": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "price": {"anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}]},
    },
    "required": ["name", "selling_points", "price"],
    "additionalProperties": False,
}
ANSWER = {"name": "Charger X", "selling_points": ["fast", "small"], "price": 19.9}
#: 1,200 prompt tokens (200 cached), 300 completion tokens (50 reasoning).
USAGE = {
    "prompt_tokens": 1200,
    "completion_tokens": 300,
    "prompt_tokens_details": {"cached_tokens": 200},
    "completion_tokens_details": {"reasoning_tokens": 50},
}
# gpt-5-mini: 1,000 x 0.25 + 200 x 0.025 + 300 x 2.00 per 1M tokens.
ANSWER_COST = Decimal("0.000855")


def _completion(content: str, *, usage: dict | None = None, refusal: str | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if refusal is not None:
        message = {"role": "assistant", "content": None, "refusal": refusal}
    return {
        "id": "chatcmpl-test",
        "model": "gpt-5-mini-2025-08-07",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": usage if usage is not None else USAGE,
    }


def _ok(content: str = json.dumps(ANSWER), **kwargs) -> httpx.Response:
    return httpx.Response(200, headers={"x-request-id": "req_llm_1"}, json=_completion(content, **kwargs))


class _Script:
    """A fake OpenAI: answers each request with the next scripted response."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def adapter(self) -> OpenAIChatAdapter:
        return OpenAIChatAdapter(
            base_url="https://openai.test/v1", timeout=5, transport=httpx.MockTransport(self),
        )


def _resolved(**overrides) -> ResolvedLlmChatRequest:
    values = dict(
        semantic="product_intake",
        provider="openai",
        model="gpt-5-mini",
        messages=(LlmMessage("system", "be precise"), LlmMessage("user", "describe the charger")),
        images=(),
        json_schema=SCHEMA,
        schema_name="product",
        max_output_tokens=800,
        estimated_input_tokens=500,
        request_digest="d" * 64,
        redacted_request_digest="r" * 64,
    )
    values.update(overrides)
    return ResolvedLlmChatRequest(**values)


def _request(**overrides) -> LlmChatRequest:
    values = dict(
        semantic="product_intake",
        system="You extract product facts.",
        user="Charger X, 65W, USD 19.90",
        json_schema=SCHEMA,
        max_output_tokens=800,
    )
    values.update(overrides)
    return LlmChatRequest.build(**values)


# ---------------------------------------------------------------------------
# JSON schema subset
# ---------------------------------------------------------------------------


class TestJsonSchema(unittest.TestCase):
    def test_valid_instance_has_no_errors(self):
        self.assertEqual(json_schema.validate(ANSWER, SCHEMA), [])
        self.assertEqual(json_schema.validate({**ANSWER, "price": None}, SCHEMA), [])

    def test_violations_are_reported_with_paths(self):
        errors = json_schema.validate(
            {"name": "", "selling_points": [], "price": -1, "extra": 1}, SCHEMA,
        )
        joined = "\n".join(errors)
        self.assertIn("$.name: shorter than 1", joined)
        self.assertIn("$.selling_points: fewer than 1 items", joined)
        self.assertIn("$.price: matches none of the anyOf", joined)
        self.assertIn("unexpected property 'extra'", joined)
        self.assertIn("missing required property 'name'", "\n".join(json_schema.validate({}, SCHEMA)))

    def test_refs_and_type_checks(self):
        schema = {
            "type": "object",
            "$defs": {"tag": {"type": "string", "enum": ["a", "b"]}},
            "properties": {"tags": {"type": "array", "items": {"$ref": "#/$defs/tag"}}},
            "required": ["tags"],
            "additionalProperties": False,
        }
        json_schema.check_schema(schema)
        self.assertEqual(json_schema.validate({"tags": ["a"]}, schema), [])
        self.assertIn("not one of", json_schema.validate({"tags": ["c"]}, schema)[0])
        self.assertIn("expected integer", json_schema.validate(True, {"type": "integer"})[0])

    def test_unusable_schemas_are_rejected_up_front(self):
        with self.assertRaises(json_schema.SchemaDefinitionError):
            json_schema.check_schema({"type": "array"})
        with self.assertRaises(json_schema.SchemaDefinitionError):
            json_schema.check_schema({"type": "object", "properties": {"x": {"$ref": "http://x"}}})
        with self.assertRaises(json_schema.SchemaDefinitionError):
            json_schema.check_schema({"type": "object", "properties": {"x": {"type": "date"}}})


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


class TestLlmPricing(unittest.TestCase):
    def test_quote_is_input_plus_max_output_for_the_answer_and_its_retry(self):
        spec = model_spec("openai", None)
        self.assertEqual(spec.model, "gpt-5-mini")
        one = request_max_cost(spec, input_tokens=4000, max_output_tokens=1000)
        # 4,000 x 0.25 + 1,000 x 2.00 per 1M tokens.
        self.assertEqual(one, Decimal("0.003"))
        self.assertEqual(call_max_cost(spec, input_tokens=4000, max_output_tokens=1000), Decimal("0.006"))
        quote = quote_llm_chat(execution_mode="byok", max_output_tokens=1000, input_tokens=4000)
        self.assertEqual(quote.amount, Decimal("0.006"))
        self.assertEqual(quote.max_requests, 2)
        self.assertEqual(quote.price_rule_version, LLM_PRICE_RULE_VERSION)

    def test_mock_quotes_zero_real_and_a_simulated_amount(self):
        quote = quote_llm_chat(execution_mode="mock", max_output_tokens=1000, input_tokens=4000)
        self.assertEqual(quote.amount, Decimal("0"))
        self.assertEqual(quote.simulated_amount, Decimal("0.006"))

    def test_usage_cost_bills_cached_input_at_its_own_rate(self):
        spec = model_spec("openai", "gpt-5-mini")
        cost = usage_cost(spec, {"input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 300})
        self.assertEqual(cost, ANSWER_COST)

    def test_unknown_models_and_out_of_range_ceilings_are_refused(self):
        with self.assertRaises(LlmChatError) as raised:
            quote_llm_chat(execution_mode="byok", max_output_tokens=100, input_tokens=1, model="gpt-9")
        self.assertEqual(raised.exception.code, "unknown_model")
        with self.assertRaises(LlmChatError) as raised:
            quote_llm_chat(execution_mode="byok", max_output_tokens=10**6, input_tokens=1)
        self.assertEqual(raised.exception.code, "invalid_request")
        with self.assertRaises(LlmChatError) as raised:
            quote_llm_chat(execution_mode="sandbox", max_output_tokens=10, input_tokens=1)
        self.assertEqual(raised.exception.code, "mode_unavailable")

    def test_input_estimate_is_an_upper_bound_including_images(self):
        text_only = estimate_request_input_tokens(_request(), execution_mode="byok")
        with_image = estimate_request_input_tokens(
            _request(images=(LlmImage.from_bytes(PNG),)), execution_mode="byok",
        )
        self.assertGreater(text_only, len("Charger X, 65W, USD 19.90"))
        self.assertEqual(with_image - text_only, model_spec("openai", None).image_input_tokens_ceiling)


# ---------------------------------------------------------------------------
# OpenAI chat adapter (fake HTTP)
# ---------------------------------------------------------------------------


class TestOpenAIChatAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_a_domain_retry_timeout_is_uncertain_and_never_retried_again(self):
        def reject(_data):
            raise LlmOutputValidationError("draft_invalid", "unknown factor reference")

        script = _Script(_ok(), httpx.ReadTimeout("timeout"))
        request = _resolved(output_validation_policy="test/v1", output_validator=reject)
        with self.assertRaises(ProviderDispatchError) as raised:
            await script.adapter().execute(request, credential=memoryview(b"k"), idempotency_key="k")
        self.assertEqual(len(script.requests), 2)
        self.assertEqual(raised.exception.dispatch_certainty, "uncertain")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.reported_cost.provider_amount, ANSWER_COST)
        self.assertEqual(json.loads(script.requests[0].content), json.loads(script.requests[1].content))

    async def test_success_sends_strict_schema_and_prices_reported_usage(self):
        script = _Script(_ok())
        image = ResolvedImage(content=PNG, mime_type="image/png", sha256="a" * 64)
        result = await script.adapter().execute(
            _resolved(images=(image,)), credential=memoryview(b"sk-ws-key"), idempotency_key="k",
        )
        sent = script.requests[0]
        body = json.loads(sent.content)
        self.assertEqual(str(sent.url), "https://openai.test/v1/chat/completions")
        self.assertEqual(sent.headers["Authorization"], "Bearer sk-ws-key")
        self.assertEqual(body["model"], "gpt-5-mini")
        self.assertEqual(body["max_completion_tokens"], 800)
        self.assertIs(body["store"], False)
        self.assertEqual(body["reasoning_effort"], "minimal")
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertIs(body["response_format"]["json_schema"]["strict"], True)
        self.assertEqual(body["response_format"]["json_schema"]["schema"], SCHEMA)
        self.assertEqual(body["messages"][0], {"role": "system", "content": "be precise"})
        parts = body["messages"][1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "describe the charger"})
        self.assertEqual(
            parts[1]["image_url"]["url"], "data:image/png;base64," + base64.b64encode(PNG).decode(),
        )

        self.assertEqual(result.output.data, ANSWER)
        self.assertEqual(result.output.model_version, "gpt-5-mini-2025-08-07")
        self.assertEqual(result.output.http_calls, 1)
        self.assertEqual(result.provider_request_id, "req_llm_1")
        self.assertEqual(
            result.output.usage,
            {"input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 300, "reasoning_tokens": 50},
        )
        cost = result.reported_cost
        self.assertEqual(cost.provider_amount, ANSWER_COST)
        self.assertEqual(cost.price_rule_version, LLM_PRICE_RULE_VERSION)
        self.assertEqual(cost.evidence["output"], ANSWER)
        self.assertEqual(cost.evidence["image_sha256"], ["a" * 64])
        self.assertNotIn("sk-ws-key", json.dumps(cost.evidence))

    async def test_rate_limit_is_unbilled_and_retryable(self):
        script = _Script(httpx.Response(429, json={"error": {"message": "slow down"}}))
        with self.assertRaises(ProviderDispatchError) as raised:
            await script.adapter().execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
        self.assertEqual(raised.exception.dispatch_certainty, "not_dispatched")
        self.assertEqual(raised.exception.error_code, "rate_limited")
        self.assertIs(raised.exception.retryable, True)
        self.assertIsNone(raised.exception.reported_cost)

    async def test_exhausted_quota_is_unbilled_and_not_retried(self):
        for name, error in (
            ("code", {"message": "You exceeded your current quota", "type": "insufficient_quota",
                      "code": "insufficient_quota"}),
            ("type only", {"message": "no quota left", "type": "insufficient_quota", "code": None}),
        ):
            with self.subTest(name):
                script = _Script(httpx.Response(429, json={"error": error}))
                with self.assertRaises(ProviderDispatchError) as raised:
                    await script.adapter().execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
                self.assertEqual(len(script.requests), 1)
                self.assertEqual(raised.exception.dispatch_certainty, "not_dispatched")
                self.assertEqual(raised.exception.error_code, "quota_exhausted")
                self.assertIs(raised.exception.retryable, False)
                self.assertIsNone(raised.exception.reported_cost)

    async def test_a_429_without_the_quota_code_stays_a_rate_limit(self):
        for body in ({"error": {"message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"}},
                     {"error": {"message": "You exceeded your current quota"}},
                     {"error": "insufficient_quota"}):
            with self.subTest(body=body):
                script = _Script(httpx.Response(429, json=body))
                with self.assertRaises(ProviderDispatchError) as raised:
                    await script.adapter().execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
                self.assertEqual(raised.exception.error_code, "rate_limited")
                self.assertIs(raised.exception.retryable, True)
        script = _Script(httpx.Response(429, text="<html>insufficient_quota</html>"))
        with self.assertRaises(ProviderDispatchError) as raised:
            await script.adapter().execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
        self.assertEqual(raised.exception.error_code, "rate_limited")

    async def test_exhausted_quota_on_a_schema_retry_keeps_the_billed_first_answer(self):
        quota = httpx.Response(429, json={"error": {"type": "insufficient_quota", "code": "insufficient_quota"}})
        script = _Script(_ok("not json"), quota)
        with self.assertRaises(ProviderDispatchError) as raised:
            await script.adapter().execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
        self.assertEqual(raised.exception.error_code, "quota_exhausted")
        self.assertEqual(raised.exception.dispatch_certainty, "dispatched")
        self.assertIs(raised.exception.retryable, False)
        self.assertIsNotNone(raised.exception.reported_cost)

    async def test_rejected_key_is_unbilled_and_not_retryable(self):
        script = _Script(httpx.Response(401, json={"error": {"message": "bad key"}}))
        with self.assertRaises(ProviderDispatchError) as raised:
            await script.adapter().execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
        self.assertEqual(raised.exception.dispatch_certainty, "not_dispatched")
        self.assertEqual(raised.exception.error_code, "credential_rejected")
        self.assertIs(raised.exception.retryable, False)
        self.assertNotIn("Bearer", str(raised.exception))

    async def test_timeouts_and_server_errors_are_uncertain(self):
        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        def bad_gateway(request):
            return httpx.Response(502, text="bad")

        for handler, code in ((timeout, "provider_timeout"), (bad_gateway, "openai_502")):
            with self.subTest(code=code):
                adapter = OpenAIChatAdapter(
                    base_url="https://openai.test/v1", timeout=5, transport=httpx.MockTransport(handler),
                )
                with self.assertRaises(ProviderDispatchError) as raised:
                    await adapter.execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
                self.assertEqual(raised.exception.dispatch_certainty, "uncertain")
                self.assertEqual(raised.exception.error_code, code)
                self.assertIs(raised.exception.retryable, False)

    async def test_a_connection_that_never_opened_is_unbilled(self):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        adapter = OpenAIChatAdapter(base_url="https://openai.test/v1", transport=httpx.MockTransport(refuse))
        with self.assertRaises(ProviderDispatchError) as raised:
            await adapter.execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
        self.assertEqual(raised.exception.dispatch_certainty, "not_dispatched")
        self.assertEqual(raised.exception.error_code, "provider_unreachable")

    async def test_invalid_json_is_asked_again_once(self):
        script = _Script(_ok("not json {"), _ok())
        result = await script.adapter().execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
        self.assertEqual(len(script.requests), 2)
        self.assertEqual(result.output.data, ANSWER)
        self.assertEqual(result.output.http_calls, 2)
        self.assertEqual(result.output.usage["input_tokens"], 2400)
        self.assertEqual(result.reported_cost.provider_amount, ANSWER_COST * 2)
        self.assertEqual(result.reported_cost.evidence["invalid_answers"], ["invalid_json"])

    async def test_invalid_json_twice_fails_billed(self):
        script = _Script(_ok("nope"), _ok("still nope"))
        with self.assertRaises(ProviderDispatchError) as raised:
            await script.adapter().execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
        self.assertEqual(len(script.requests), 2)
        self.assertEqual(raised.exception.dispatch_certainty, "dispatched")
        self.assertEqual(raised.exception.error_code, "invalid_json")
        self.assertEqual(raised.exception.reported_cost.provider_amount, ANSWER_COST * 2)

    async def test_schema_violations_fail_after_the_retry(self):
        wrong = json.dumps({"name": "X", "selling_points": "fast", "price": 1})
        script = _Script(_ok(wrong), _ok(wrong))
        with self.assertRaises(ProviderDispatchError) as raised:
            await script.adapter().execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
        self.assertEqual(raised.exception.error_code, "schema_validation_failed")
        self.assertIn("selling_points", str(raised.exception))

    async def test_refusals_are_not_asked_again(self):
        script = _Script(_ok(refusal="I can't help with that"))
        with self.assertRaises(ProviderDispatchError) as raised:
            await script.adapter().execute(_resolved(), credential=memoryview(b"k"), idempotency_key="k")
        self.assertEqual(len(script.requests), 1)
        self.assertEqual(raised.exception.error_code, "model_refusal")
        self.assertEqual(raised.exception.dispatch_certainty, "dispatched")

    async def test_missing_credential_never_calls_out(self):
        script = _Script()
        with self.assertRaises(ProviderDispatchError) as raised:
            await script.adapter().execute(_resolved(), credential=None, idempotency_key="k")
        self.assertEqual(raised.exception.error_code, "credential_unavailable")
        self.assertEqual(script.requests, [])


# ---------------------------------------------------------------------------
# Controlled mock
# ---------------------------------------------------------------------------


class TestMockLlmChatAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_example_fixture_answers_its_semantic_deterministically(self):
        request = _resolved(
            semantic=EXAMPLE_SUMMARY_SEMANTIC, provider="mock", model="mock-llm-v1",
            json_schema=EXAMPLE_SUMMARY_SCHEMA,
        )
        first = await MockLlmChatAdapter().execute(request, credential=None, idempotency_key="k")
        second = await MockLlmChatAdapter().execute(request, credential=None, idempotency_key="k")
        self.assertEqual(first.output.data, second.output.data)
        self.assertEqual(first.output.data["title"], "[mock] describe the charger")
        self.assertEqual(first.reported_cost.provider_amount, Decimal("0"))
        self.assertGreater(first.reported_cost.simulated_amount, Decimal("0"))
        self.assertEqual(first.reported_cost.evidence["mock_fixture"], "registered")

    async def test_unregistered_semantics_get_a_schema_synthesized_answer(self):
        # ``asset_tagging`` registered its own fixture (Issue #168); a tag no
        # shipped caller owns still gets the schema-synthesized answer.
        request = _resolved(semantic="vlm_qc.unregistered", provider="mock", model="mock-llm-v1")
        result = await MockLlmChatAdapter().execute(request, credential=None, idempotency_key="k")
        self.assertEqual(json_schema.validate(result.output.data, SCHEMA), [])
        self.assertEqual(result.reported_cost.evidence["mock_fixture"], "schema_synthesized")
        self.assertEqual(synthesize_from_schema(SCHEMA), result.output.data)

    async def test_callers_register_fixtures_and_drift_fails_loudly(self):
        # A tag no shipped caller owns: ``test_plan_draft`` now has its real
        # fixture (Issue #164), which unregistering here would remove.
        semantic = "example.caller"
        register_llm_mock_fixture(semantic, {"name": "Plan", "selling_points": ["a"], "price": None})
        try:
            ok = await MockLlmChatAdapter().execute(
                _resolved(semantic=semantic, provider="mock", model="mock-llm-v1"),
                credential=None, idempotency_key="k",
            )
            self.assertEqual(ok.output.data["name"], "Plan")
            register_llm_mock_fixture(semantic, {"name": "Plan"})
            with self.assertRaises(ProviderDispatchError) as raised:
                await MockLlmChatAdapter().execute(
                    _resolved(semantic=semantic, provider="mock", model="mock-llm-v1"),
                    credential=None, idempotency_key="k",
                )
            self.assertEqual(raised.exception.error_code, "mock_fixture_invalid")
            self.assertEqual(raised.exception.dispatch_certainty, "not_dispatched")
        finally:
            unregister_llm_mock_fixture(semantic)


# ---------------------------------------------------------------------------
# Governed path: admission + Worker through the coordinator
# ---------------------------------------------------------------------------


class _BlobStore:
    """Serves committed objects from memory, like the bucket adapter would."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads: list[str] = []

    async def _chunks(self, key: str):
        self.reads.append(key)
        yield self.objects[key]

    def open_committed(self, ref: CommittedBlobRef):
        return self._chunks(ref.committed_key)


class GovernedLlmChatTests(LibraryFixtures):
    PROTECTOR = LocalKmsAdapter.local_only()

    def setUp(self) -> None:
        self.ctx = self.owner_ctx(self._make_context(role="owner", actor="owner_a"))
        self._entitle(self.ctx)
        self.worker_ctx = self.ctx.for_system_execution()
        self.coordinator = ExternalCallCoordinator(session_factory=tenant_session)

    # -- helpers ----------------------------------------------------------

    def _entitle(self, ctx) -> None:
        with tenant_session(ctx) as s:
            s.merge(EntitlementProjectionRecord(
                workspace_id=ctx.workspace_id,
                entitlement_version=1, lease_sequence=1, projection_version=1,
                semantic_hash="hash_llm", resolved_entitlements_json={"features": ["generation"], "storage.bytes.max": 10 * 1024 ** 3},
            ))

    def _bind_openai_key(self, ctx=None, plaintext: bytes = b"sk-ws-own-key") -> str:
        ctx = ctx or self.ctx
        admin = CredentialAdmin(self.PROTECTOR)
        with tenant_session(ctx) as s:
            meta = admin.create(
                s, ctx, credential_key="OPENAI_API_KEY", provider="openai", plaintext=plaintext,
            )
            admin.bind(
                s, ctx, binding_scope="generation", provider="openai",
                credential_version_id=meta.credential_version_id,
            )
            return meta.credential_version_id

    def _admit(self, *, mode: str, request: LlmChatRequest | None = None, ctx=None,
               call_name: str = "extract", input_token_budget: int | None = None,
               max_output_tokens: int = 800):
        """What an admitting command does: Operation + Job + reservation, one transaction."""
        ctx = ctx or self.ctx
        with tenant_session(ctx) as s:
            operation = create_operation(s, command_type="llm_chat.test", context=ctx)
            job = JobRecord(
                id=new_id("job"), workspace_id=ctx.workspace_id, initiator_id=ctx.initiator_id,
                initiator_role="owner", type="llm_chat.test", resource_type="test",
                resource_id="t", payload_json={}, idempotency_key=new_id("ik"), state="queued",
                operation_id=operation.id,
            )
            s.add(job)
            s.flush()
            reservation = reserve_llm_chat(
                s, ctx, operation_id=operation.id, job_id=job.id, call_name=call_name,
                execution_mode=mode, request=request, input_token_budget=input_token_budget,
                max_output_tokens=max_output_tokens,
            )
            return operation.id, job.id, reservation

    def _client(self, script: _Script | None = None, *, blob_store=None) -> LlmChatClient:
        adapters = {"mock": MockLlmChatAdapter()}
        adapters["openai"] = (script or _Script()).adapter()
        return LlmChatClient(
            coordinator=self.coordinator, session_factory=tenant_session,
            protector=self.PROTECTOR, adapters=adapters, blob_store=blob_store,
        )

    def _chat(self, client, *, job_id, operation_id, mode, request=None, call_name="extract", ctx=None):
        return asyncio.run(client.chat(
            ctx or self.worker_ctx, job_id=job_id, operation_id=operation_id,
            call_name=call_name, request=request or _request(), execution_mode=mode,
        ))

    def _attempts(self, s, job_id):
        return s.execute(
            select(ProviderCallAttemptRecord).where(ProviderCallAttemptRecord.job_id == job_id)
        ).scalars().all()

    def _ledger(self, s, operation_id):
        return s.execute(
            select(ProviderCostLedgerEntryRecord)
            .where(ProviderCostLedgerEntryRecord.operation_id == operation_id)
            .order_by(ProviderCostLedgerEntryRecord.created_at, ProviderCostLedgerEntryRecord.id)
        ).scalars().all()

    def _events(self, s, attempt_id, event_type):
        return s.execute(
            select(ProviderAttemptEventRecord).where(
                ProviderAttemptEventRecord.provider_call_attempt_id == attempt_id,
                ProviderAttemptEventRecord.event_type == event_type,
            )
        ).scalars().all()

    def _assert_fits_columns(self, rows) -> None:
        for row in rows:
            for column in row.__table__.columns:
                length = getattr(column.type, "length", None)
                value = getattr(row, column.key, None)
                if length and isinstance(value, str):
                    self.assertLessEqual(
                        len(value), length, f"{row.__tablename__}.{column.name} = {value!r}",
                    )

    # -- mock mode ---------------------------------------------------------

    def test_mock_mode_runs_end_to_end_with_zero_real_cost(self):
        operation_id, job_id, reservation = self._admit(mode="mock", request=_request())
        self.assertIsNone(reservation.reservation_id)
        self.assertEqual(reservation.quote.amount, Decimal("0"))
        self.assertGreater(reservation.quote.simulated_amount, Decimal("0"))

        result = self._chat(self._client(), job_id=job_id, operation_id=operation_id, mode="mock")
        self.assertEqual(json_schema.validate(result.data, SCHEMA), [])
        self.assertEqual(result.provider, "mock")
        self.assertEqual(result.execution_mode, "mock")
        self.assertFalse(result.replayed)
        self.assertEqual(Decimal(result.cost["provider_amount"]), Decimal("0"))
        self.assertNotIn("E", result.cost["provider_amount"])
        self.assertIn("simulated_amount", result.cost)

        with tenant_session(self.ctx) as s:
            attempt = self._attempts(s, job_id)[0]
            self.assertEqual(attempt.step_kind, "llm_chat")
            self.assertEqual(attempt.chargeability, "free_mock")
            self.assertEqual(attempt.dispatch_state, "succeeded")
            self.assertIsNone(attempt.spend_reservation_id)
            self.assertEqual(len(attempt.request_fingerprint_hash), 64)
            self.assertEqual(len(attempt.redacted_request_hash), 64)
            self.assertNotEqual(attempt.request_fingerprint_hash, attempt.redacted_request_hash)
            projection = s.execute(
                select(ProviderAttemptProjectionRecord).where(
                    ProviderAttemptProjectionRecord.provider_call_attempt_id == attempt.id
                )
            ).scalar_one()
            self.assertGreater(projection.usage_summary_json["input_tokens"], 0)
            self.assertEqual(self._ledger(s, operation_id), [])
            self.assertEqual(
                s.execute(select(SpendReservationRecord).where(
                    SpendReservationRecord.operation_id == operation_id
                )).scalars().all(),
                [],
            )
            audits = s.execute(
                select(AuditEventRecord).where(AuditEventRecord.resource_id == attempt.id)
            ).scalars().all()
            self.assertEqual([a.event_type for a in audits], ["llm_chat.call_succeeded"])

    def test_mock_mode_example_semantic_and_replay(self):
        operation_id, job_id, _ = self._admit(mode="mock")
        request = LlmChatRequest.build(
            semantic=EXAMPLE_SUMMARY_SEMANTIC, system="", user="Summer sale\nmore",
            json_schema=EXAMPLE_SUMMARY_SCHEMA, max_output_tokens=200,
        )
        client = self._client()
        first = self._chat(client, job_id=job_id, operation_id=operation_id, mode="mock", request=request)
        self.assertEqual(first.data["title"], "[mock] Summer sale")
        again = self._chat(client, job_id=job_id, operation_id=operation_id, mode="mock", request=request)
        self.assertTrue(again.replayed)
        self.assertEqual(again.data, first.data)
        self.assertEqual(again.attempt_id, first.attempt_id)
        with tenant_session(self.ctx) as s:
            self.assertEqual(len(self._attempts(s, job_id)), 1)

    def test_mock_fixture_that_breaks_the_schema_is_a_structured_failure(self):
        register_llm_mock_fixture("qc", {"unexpected": True})
        try:
            operation_id, job_id, _ = self._admit(mode="mock")
            with self.assertRaises(LlmChatError) as raised:
                self._chat(self._client(), job_id=job_id, operation_id=operation_id, mode="mock",
                           request=_request(semantic="qc"))
            self.assertEqual(raised.exception.code, "mock_fixture_invalid")
            with tenant_session(self.ctx) as s:
                attempt = self._attempts(s, job_id)[0]
                self.assertEqual(attempt.dispatch_state, "failed")
                self.assertEqual(attempt.error_code, "mock_fixture_invalid")
        finally:
            unregister_llm_mock_fixture("qc")

    # -- byok mode ---------------------------------------------------------

    def test_byok_reserves_at_admission_and_settles_on_reported_usage(self):
        version_id = self._bind_openai_key()
        request = _request(max_output_tokens=800)
        operation_id, job_id, reservation = self._admit(mode="byok", request=request)
        expected_max = call_max_cost(
            model_spec("openai", None),
            input_tokens=estimate_request_input_tokens(request, execution_mode="byok"),
            max_output_tokens=800,
        )
        self.assertEqual(reservation.quote.amount, expected_max)
        self.assertEqual(reservation.call_key, llm_call_key(job_id, "extract"))

        with tenant_session(self.ctx) as s:
            reserved = s.get(SpendReservationRecord, reservation.reservation_id)
            self.assertEqual(reserved.state, "reserved")
            self.assertEqual(Decimal(str(reserved.maximum_provider_amount)), expected_max)
            self.assertEqual(sorted(e.entry_type for e in self._ledger(s, operation_id)), ["quote", "reservation"])

        script = _Script(_ok())
        result = self._chat(self._client(script), job_id=job_id, operation_id=operation_id,
                            mode="byok", request=request)
        self.assertEqual(result.data, ANSWER)
        self.assertEqual(result.model, "gpt-5-mini")
        self.assertEqual(result.model_version, "gpt-5-mini-2025-08-07")
        self.assertEqual(result.provider_request_id, "req_llm_1")
        self.assertEqual(result.usage["output_tokens"], 300)
        self.assertEqual(Decimal(result.cost["provider_amount"]), ANSWER_COST)
        self.assertEqual(script.requests[0].headers["Authorization"], "Bearer sk-ws-own-key")

        with tenant_session(self.ctx) as s:
            attempt = self._attempts(s, job_id)[0]
            self.assertEqual(attempt.step_kind, "llm_chat")
            self.assertEqual(attempt.chargeability, "charged")
            self.assertEqual(attempt.execution_mode, "byok")
            self.assertEqual(attempt.dispatch_state, "succeeded")
            self.assertEqual(attempt.credential_version_id, version_id)
            self.assertEqual(attempt.spend_reservation_id, reservation.reservation_id)
            self.assertEqual(attempt.model, "gpt-5-mini")
            self.assertEqual(attempt.provider_request_id, "req_llm_1")
            self.assertTrue(attempt.provider_evidence_ref.startswith("sha256:"))
            usage_event = self._events(s, attempt.id, "usage_reported")[0]
            self.assertEqual(usage_event.payload_json["usage"]["input_tokens"], 1200)
            self.assertEqual(usage_event.payload_json["evidence"]["model_version"], "gpt-5-mini-2025-08-07")

            settled = s.get(SpendReservationRecord, reservation.reservation_id)
            self.assertEqual(settled.state, "settled")
            self.assertEqual(Decimal(str(settled.outstanding_budget_amount)), Decimal("0"))
            ledger = {e.entry_type: e for e in self._ledger(s, operation_id)}
            self.assertEqual(sorted(ledger), ["quote", "release", "reported_cost", "reservation"])
            self.assertEqual(Decimal(str(ledger["reported_cost"].provider_amount)), ANSWER_COST)
            self.assertEqual(ledger["reported_cost"].direction, "increase")
            self.assertEqual(ledger["reported_cost"].provider_call_attempt_id, attempt.id)
            self.assertEqual(ledger["reported_cost"].credential_version_id, version_id)
            self.assertEqual(ledger["release"].direction, "decrease")
            self.assertEqual(
                Decimal(str(ledger["release"].budget_amount)) + Decimal(str(ledger["reported_cost"].budget_amount)),
                Decimal(str(ledger["reservation"].budget_amount)),
            )

            # PR #150: every key written by admission and settlement fits its column.
            rows = [
                *self._ledger(s, operation_id),
                s.get(SpendReservationRecord, reservation.reservation_id),
                attempt,
                *s.execute(select(ProviderAttemptEventRecord).where(
                    ProviderAttemptEventRecord.provider_call_attempt_id == attempt.id
                )).scalars(),
            ]
            self._assert_fits_columns(rows)
            self.assertEqual(len({e.semantic_key_hash for e in self._ledger(s, operation_id)}), 4)

            # The key never lands in evidence, events or audit.
            evidence_blob = json.dumps([
                e.payload_json for e in s.execute(select(ProviderAttemptEventRecord).where(
                    ProviderAttemptEventRecord.provider_call_attempt_id == attempt.id
                )).scalars()
            ]) + json.dumps([
                a.details_json for a in s.execute(select(AuditEventRecord).where(
                    AuditEventRecord.resource_id == attempt.id
                )).scalars()
            ], default=str)
            self.assertNotIn("sk-ws-own-key", evidence_blob)

    def test_byok_replay_does_not_pay_twice(self):
        self._bind_openai_key()
        operation_id, job_id, _ = self._admit(mode="byok", request=_request())
        script = _Script(_ok())
        client = self._client(script)
        first = self._chat(client, job_id=job_id, operation_id=operation_id, mode="byok")
        again = self._chat(client, job_id=job_id, operation_id=operation_id, mode="byok")
        self.assertEqual(len(script.requests), 1)
        self.assertTrue(again.replayed)
        self.assertEqual(again.data, first.data)
        self.assertEqual(again.cost, first.cost)

    def test_two_calls_in_one_job_are_separate_attempts_and_reservations(self):
        self._bind_openai_key()
        operation_id, job_id, first = self._admit(mode="byok", request=_request(), call_name="extract")
        with tenant_session(self.ctx) as s:
            second = reserve_llm_chat(
                s, self.ctx, operation_id=operation_id, job_id=job_id, call_name="audiences",
                execution_mode="byok", request=_request(),
            )
        self.assertNotEqual(first.call_key, second.call_key)
        script = _Script(_ok(), _ok())
        client = self._client(script)
        self._chat(client, job_id=job_id, operation_id=operation_id, mode="byok", call_name="extract")
        self._chat(client, job_id=job_id, operation_id=operation_id, mode="byok", call_name="audiences")
        with tenant_session(self.ctx) as s:
            attempts = self._attempts(s, job_id)
            self.assertEqual(len(attempts), 2)
            self.assertEqual({a.spend_reservation_id for a in attempts},
                             {first.reservation_id, second.reservation_id})
            self.assertEqual(
                sorted(e.entry_type for e in self._ledger(s, operation_id)),
                ["quote", "quote", "release", "release", "reported_cost", "reported_cost",
                 "reservation", "reservation"],
            )

    def test_rate_limit_fails_the_attempt_and_releases_the_reservation(self):
        self._bind_openai_key()
        operation_id, job_id, reservation = self._admit(mode="byok", request=_request())
        script = _Script(httpx.Response(429, json={"error": {"message": "slow down"}}))
        with self.assertRaises(LlmChatError) as raised:
            self._chat(self._client(script), job_id=job_id, operation_id=operation_id, mode="byok")
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.dispatch_certainty, "not_dispatched")
        with tenant_session(self.ctx) as s:
            attempt = self._attempts(s, job_id)[0]
            self.assertEqual(attempt.dispatch_state, "failed")
            self.assertEqual(attempt.error_code, "rate_limited")
            classified = self._events(s, attempt.id, "failure_classified")[0].payload_json
            self.assertEqual(classified, {"classification": "terminal", "error_code": "rate_limited",
                                          "retryable": True})
            self.assertEqual(s.get(SpendReservationRecord, reservation.reservation_id).state, "released")
            self.assertEqual(sorted(e.entry_type for e in self._ledger(s, operation_id)),
                             ["quote", "reservation"])
            audits = s.execute(select(AuditEventRecord).where(
                AuditEventRecord.event_type == "llm_chat.call_failed",
                AuditEventRecord.resource_id == attempt.id,
            )).scalars().all()
            self.assertEqual(len(audits), 1)

    def test_exhausted_quota_fails_the_attempt_without_retry_and_releases_the_reservation(self):
        self._bind_openai_key()
        operation_id, job_id, reservation = self._admit(mode="byok", request=_request())
        script = _Script(httpx.Response(429, json={"error": {
            "message": "You exceeded your current quota", "type": "insufficient_quota", "code": "insufficient_quota"}}))
        with self.assertRaises(LlmChatError) as raised:
            self._chat(self._client(script), job_id=job_id, operation_id=operation_id, mode="byok")
        self.assertEqual(raised.exception.code, "quota_exhausted")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.dispatch_certainty, "not_dispatched")
        self.assertEqual(len(script.requests), 1)
        with tenant_session(self.ctx) as s:
            attempt = self._attempts(s, job_id)[0]
            self.assertEqual((attempt.dispatch_state, attempt.error_code), ("failed", "quota_exhausted"))
            classified = self._events(s, attempt.id, "failure_classified")[0].payload_json
            self.assertEqual(classified, {"classification": "terminal", "error_code": "quota_exhausted",
                                          "retryable": False})
            self.assertEqual(s.get(SpendReservationRecord, reservation.reservation_id).state, "released")

    def test_rejected_key_is_classified_not_retryable(self):
        self._bind_openai_key()
        operation_id, job_id, _ = self._admit(mode="byok", request=_request())
        script = _Script(httpx.Response(401, json={"error": {"message": "invalid key"}}))
        with self.assertRaises(LlmChatError) as raised:
            self._chat(self._client(script), job_id=job_id, operation_id=operation_id, mode="byok")
        self.assertEqual(raised.exception.code, "credential_rejected")
        self.assertFalse(raised.exception.retryable)

    def test_timeout_leaves_the_call_uncertain_and_never_resends(self):
        self._bind_openai_key()
        operation_id, job_id, reservation = self._admit(mode="byok", request=_request())

        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        adapter = OpenAIChatAdapter(base_url="https://openai.test/v1", transport=httpx.MockTransport(timeout))
        client = LlmChatClient(coordinator=self.coordinator, session_factory=tenant_session,
                               protector=self.PROTECTOR, adapters={"openai": adapter})
        with self.assertRaises(LlmChatError) as raised:
            self._chat(client, job_id=job_id, operation_id=operation_id, mode="byok")
        self.assertEqual(raised.exception.code, "provider_timeout")
        self.assertEqual(raised.exception.dispatch_certainty, "uncertain")
        with tenant_session(self.ctx) as s:
            self.assertEqual(self._attempts(s, job_id)[0].dispatch_state, "uncertain")
            self.assertEqual(s.get(SpendReservationRecord, reservation.reservation_id).state, "reserved")

        resend = _Script(_ok())
        with self.assertRaises(LlmChatError) as again:
            self._chat(self._client(resend), job_id=job_id, operation_id=operation_id, mode="byok")
        self.assertTrue(again.exception.replayed)
        self.assertEqual(again.exception.dispatch_certainty, "uncertain")
        self.assertEqual(resend.requests, [])

    def test_unusable_answers_settle_what_they_billed(self):
        self._bind_openai_key()
        operation_id, job_id, reservation = self._admit(mode="byok", request=_request())
        script = _Script(_ok("{oops"), _ok(json.dumps({"name": "X"})))
        with self.assertRaises(LlmChatError) as raised:
            self._chat(self._client(script), job_id=job_id, operation_id=operation_id, mode="byok")
        self.assertEqual(raised.exception.code, "schema_validation_failed")
        self.assertEqual(raised.exception.dispatch_certainty, "dispatched")
        with tenant_session(self.ctx) as s:
            attempt = self._attempts(s, job_id)[0]
            self.assertEqual(attempt.dispatch_state, "failed")
            self.assertEqual(s.get(SpendReservationRecord, reservation.reservation_id).state, "settled")
            ledger = {e.entry_type: e for e in self._ledger(s, operation_id)}
            self.assertEqual(Decimal(str(ledger["reported_cost"].provider_amount)), ANSWER_COST * 2)
            classified = self._events(s, attempt.id, "failure_classified")[0].payload_json
            self.assertTrue(classified["billed"])
            self.assertEqual(self._events(s, attempt.id, "usage_reported")[0].payload_json["usage"]["input_tokens"], 2400)

    # -- refusals before any call -----------------------------------------

    def test_no_bound_key_is_credential_unavailable_not_a_crash(self):
        with tenant_session(self.ctx) as s:
            self.assertEqual(resolve_llm_execution_mode(s, self.ctx), "mock")
        with self.assertRaises(LlmChatError) as raised:
            self._admit(mode="byok", request=_request())
        self.assertEqual(raised.exception.code, "credential_unavailable")

        # A key bound, reserved, then revoked before the Worker runs.
        self._bind_openai_key()
        operation_id, job_id, _ = self._admit(mode="byok", request=_request())
        with tenant_session(self.ctx) as s:
            CredentialAdmin(self.PROTECTOR).revoke(
                s, self.ctx, credential_key="OPENAI_API_KEY", reason="test",
            )
        script = _Script(_ok())
        with self.assertRaises(LlmChatError) as raised:
            self._chat(self._client(script), job_id=job_id, operation_id=operation_id, mode="byok")
        self.assertEqual(raised.exception.code, "credential_unavailable")
        self.assertEqual(script.requests, [])
        with tenant_session(self.ctx) as s:
            self.assertEqual(self._attempts(s, job_id), [])

    def test_a_call_needs_its_reservation_and_must_fit_it(self):
        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            operation = create_operation(s, command_type="llm_chat.test", context=self.ctx)
            job = JobRecord(
                id=new_id("job"), workspace_id=self.ctx.workspace_id, initiator_role="owner",
                type="llm_chat.test", payload_json={}, idempotency_key=new_id("ik"),
                state="queued", operation_id=operation.id,
            )
            s.add(job)
            operation_id, job_id = operation.id, job.id
        script = _Script(_ok())
        with self.assertRaises(LlmChatError) as raised:
            self._chat(self._client(script), job_id=job_id, operation_id=operation_id, mode="byok")
        self.assertEqual(raised.exception.code, "reservation_missing")

        operation_id, job_id, _ = self._admit(mode="byok", input_token_budget=50, max_output_tokens=800)
        with self.assertRaises(LlmChatError) as raised:
            self._chat(self._client(script), job_id=job_id, operation_id=operation_id, mode="byok",
                       request=_request(user="x" * 5000))
        self.assertEqual(raised.exception.code, "reservation_insufficient")
        self.assertEqual(script.requests, [])

        with tenant_session(self.ctx) as s:
            released = release_llm_chat_reservation(s, self.ctx, job_id=job_id, call_name="extract")
        self.assertTrue(released)

    def test_guardrail_breach_refuses_admission(self):
        from app.generation.spend import ensure_spend_guardrail

        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            ensure_spend_guardrail(s, self.ctx).per_operation_limit = Decimal("0.000001")
        with self.assertRaises(LlmChatError) as raised:
            self._admit(mode="byok", request=_request())
        self.assertEqual(raised.exception.code, "guardrail_exceeded")
        self.assertIn("per_operation_limit", raised.exception.detail)

    def test_invalid_schema_is_refused_before_any_attempt(self):
        operation_id, job_id, _ = self._admit(mode="mock")
        with self.assertRaises(LlmChatError) as raised:
            self._chat(self._client(), job_id=job_id, operation_id=operation_id, mode="mock",
                       request=_request(json_schema={"type": "array"}))
        self.assertEqual(raised.exception.code, "invalid_schema")
        with tenant_session(self.ctx) as s:
            self.assertEqual(self._attempts(s, job_id), [])

    def test_a_validator_requires_a_named_policy_before_any_attempt(self):
        operation_id, job_id, _ = self._admit(mode="mock")
        validate = lambda _data: None
        for fields in (
            {"output_validator": validate},
            {"output_validation_policy": "policy/v1"},
            {"output_validator": validate, "output_validation_policy": 42},
            {"output_validator": validate, "output_validation_policy": " "},
            {"output_validator": validate, "output_validation_policy": "x" * 129},
        ):
            with self.subTest(fields=list(fields)):
                with self.assertRaises(LlmChatError) as raised:
                    self._chat(self._client(), job_id=job_id, operation_id=operation_id,
                               mode="mock", request=_request(**fields))
                self.assertEqual(raised.exception.code, "invalid_request")
        with tenant_session(self.ctx) as session:
            self.assertEqual(self._attempts(session, job_id), [])

    # -- images ------------------------------------------------------------

    def test_blob_images_are_read_from_this_workspace_and_only_digested(self):
        key = f"committed/{self.ctx.workspace_id}/reference/2026/10/10/img.png"
        store = _BlobStore({key: PNG})
        ref = CommittedBlobRef(committed_key=key, storage_version_id="v1", size_bytes=len(PNG),
                               checksum_sha256_b64="", content_type_verified="image/png")
        self._bind_openai_key()
        request = _request(images=(LlmImage.from_blob(ref),))
        operation_id, job_id, _ = self._admit(mode="byok", request=request)
        script = _Script(_ok())
        self._chat(self._client(script, blob_store=store), job_id=job_id, operation_id=operation_id,
                   mode="byok", request=request)
        self.assertEqual(store.reads, [key])
        sent = json.loads(script.requests[0].content)
        self.assertTrue(sent["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        with tenant_session(self.ctx) as s:
            attempt = self._attempts(s, job_id)[0]
            payload = self._events(s, attempt.id, "usage_reported")[0].payload_json
            self.assertEqual(payload["evidence"]["image_sha256"], [hashlib.sha256(PNG).hexdigest()])
            self.assertNotIn(base64.b64encode(PNG).decode(), json.dumps(payload))

    # -- capabilities --------------------------------------------------------

    def test_capabilities_advertise_llm_models_and_byok_readiness(self):
        from app.generation.capabilities import get_capabilities

        with tenant_session(self.ctx) as s:
            before = get_capabilities(s, self.ctx)["llm"]
        self.assertFalse(before["byok_ready"])
        self.assertEqual(before["modes"], ["mock"])
        self.assertEqual(before["providers"]["openai"]["default_model"], "gpt-5-mini")
        self.assertIn("gpt-5-mini", before["providers"]["openai"]["models"])
        self.assertTrue(before["providers"]["openai"]["models"]["gpt-5-mini"]["vision"])
        self.assertEqual(before["price_rule_version"], LLM_PRICE_RULE_VERSION)

        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            after = llm_capability(s, self.ctx)
            self.assertEqual(resolve_llm_execution_mode(s, self.ctx), "byok")
        self.assertTrue(after["byok_ready"])
        self.assertEqual(after["modes"], ["mock", "byok"])

    # -- tenant isolation ----------------------------------------------------

    def test_another_workspace_cannot_use_this_workspaces_key_job_or_images(self):
        version_id = self._bind_openai_key()
        operation_id, job_id, _ = self._admit(mode="byok", request=_request())

        other = self.owner_ctx(self._make_context(role="owner", actor="owner_b"))
        self._entitle(other)
        other_worker = other.for_system_execution()

        # The other Workspace sees no key: no byok readiness, no reservation.
        with tenant_session(other) as s:
            self.assertFalse(llm_capability(s, other)["byok_ready"])
            self.assertEqual(resolve_llm_execution_mode(s, other), "mock")
        with self.assertRaises(LlmChatError) as raised:
            self._admit(mode="byok", request=_request(), ctx=other)
        self.assertEqual(raised.exception.code, "credential_unavailable")

        # ... cannot decrypt this Workspace's credential version by id ...
        from app.generation.credential_binding import open_bound_secret

        with self.assertRaises(NoResultFound):
            with tenant_session(other_worker) as s:
                open_bound_secret(s, other_worker, version_id, self.PROTECTOR)

        # ... cannot run (or replay) this Workspace's Job ...
        script = _Script(_ok())
        with self.assertRaises(LlmChatError) as raised:
            self._chat(self._client(script), job_id=job_id, operation_id=operation_id, mode="mock",
                       ctx=other_worker)
        self.assertEqual(raised.exception.code, "job_not_found")
        self._bind_openai_key(other, plaintext=b"sk-other-key")
        with self.assertRaises(LlmChatError) as raised:
            self._chat(self._client(script), job_id=job_id, operation_id=operation_id, mode="byok",
                       ctx=other_worker)
        self.assertEqual(raised.exception.code, "reservation_missing")
        self.assertEqual(script.requests, [])

        # ... and cannot feed this Workspace's committed images to its model.
        key = f"committed/{self.ctx.workspace_id}/reference/2026/10/10/img.png"
        store = _BlobStore({key: PNG})
        ref = CommittedBlobRef(committed_key=key, storage_version_id="v1", size_bytes=len(PNG),
                               checksum_sha256_b64="", content_type_verified="image/png")
        other_operation, other_job, _ = self._admit(mode="mock", ctx=other)
        with self.assertRaises(LlmChatError) as raised:
            self._chat(self._client(blob_store=store), job_id=other_job, operation_id=other_operation,
                       mode="mock", request=_request(images=(LlmImage.from_blob(ref),)), ctx=other_worker)
        self.assertEqual(raised.exception.code, "image_not_in_workspace")
        self.assertEqual(store.reads, [])

        # The owning Workspace's call is untouched by all of the above.
        with tenant_session(self.ctx) as s:
            self.assertEqual(self._attempts(s, job_id), [])


if __name__ == "__main__":
    unittest.main()
