"""Checking an OpenAI key before it is saved (#192, ads-first S5).

No test reaches OpenAI: every request goes to an ``httpx.MockTransport`` that records what was sent, and the keys
are fixture strings that exist nowhere else.
"""
from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord
from app.entitlements import provider_key_check
from app.entitlements.provider_key_check import CHECK_EVENT, check_key, checks_key
from app.main import app
from app.platform.http_identity import request_tenant_context
from tests.support.library_fixtures import LibraryFixtures

FIXTURE_KEY = b"fixture-openai-key"
KEY_NAME = "openai_api_key"
BASE_URL = "https://api.openai.example/v1"
MODEL_LIST = {"object": "list", "data": [{"id": "fixture-model", "object": "model", "created": 0, "owned_by": "acme"}]}
QUOTA_CODES = ("insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
               "project_spend_limit_exceeded", "organization_usage_limit_exceeded")
# OpenAI's 401 text repeats part of the key; nothing of it may be kept.
ECHOING_401 = {"error": {"message": "Incorrect API key provided: fixture-***-key. You can find your API key at "
                                    "https://platform.openai.example/account/api-keys.",
                         "type": "invalid_request_error", "param": None, "code": "invalid_api_key"}}


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _error(status: int, code: str | None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": "fixture refusal", "type": "fixture", "code": code}})


class _OpenAI:
    """A stand-in for OpenAI's model list."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def patch(self):
        original = httpx.AsyncClient
        return patch.object(provider_key_check.httpx, "AsyncClient",
                            side_effect=lambda **kw: original(**{**kw, "transport": self.transport()}))


class OpenAIKeyCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        base_url = patch.object(config, "OPENAI_BASE_URL", BASE_URL)
        base_url.start()
        self.addCleanup(base_url.stop)

    async def _check(self, openai: _OpenAI, key: bytes = FIXTURE_KEY):
        return await check_key("openai", key, transport=openai.transport())

    def test_only_openai_keys_are_checked(self):
        self.assertTrue(checks_key("openai"))
        for provider in ("gemini", "scrapecreators", "mock", ""):
            self.assertFalse(checks_key(provider))

    async def test_a_model_list_means_the_key_is_valid(self):
        openai = _OpenAI(MODEL_LIST)
        check = await self._check(openai)
        self.assertEqual((check.provider, check.outcome, check.reason, check.http_status), ("openai", "valid", None, 200))
        (request,) = openai.requests
        self.assertEqual((request.method, str(request.url)), ("GET", f"{BASE_URL}/models"))
        self.assertEqual(request.headers["authorization"], f"Bearer {FIXTURE_KEY.decode()}")
        self.assertEqual(request.content, b"")
        self.assertNotIn(FIXTURE_KEY.decode(), str(request.url))
        self.assertNotIn(FIXTURE_KEY.decode(), repr(check))

    async def test_surrounding_whitespace_is_not_sent(self):
        openai = _OpenAI(MODEL_LIST)
        await self._check(openai, b"  fixture-openai-key\n")
        self.assertEqual(openai.requests[0].headers["authorization"], "Bearer fixture-openai-key")

    async def test_401_is_an_invalid_key(self):
        check = await self._check(_OpenAI(_error(401, None)))
        self.assertEqual((check.outcome, check.reason, check.http_status), ("rejected", "key_invalid", 401))

    async def test_403_is_access_denied(self):
        check = await self._check(_OpenAI(_error(403, "unsupported_country_region_territory")))
        self.assertEqual((check.outcome, check.reason, check.http_status), ("rejected", "access_denied", 403))

    async def test_no_credit_or_spend_limit_is_quota_exhausted(self):
        for code in QUOTA_CODES:
            with self.subTest(code=code):
                check = await self._check(_OpenAI(_error(429, code)))
                self.assertEqual((check.outcome, check.reason), ("rejected", "quota_exhausted"))
                self.assertIn(code, check.diagnostic)

    async def test_anything_unreadable_is_unavailable(self):
        cases = {
            "rate limit": _error(429, None),
            "slow down": _error(429, "slow_down"),
            "server error": httpx.Response(500, text="oops"),
            "overloaded": _error(503, "server_is_overloaded"),
            "not found": httpx.Response(404, text="no such path"),
            "not json": httpx.Response(200, text="<html>"),
            "not a list": httpx.Response(200, json={"object": "list"}),
            "wrong object": httpx.Response(200, json={"object": "model", "data": []}),
            "timeout": httpx.ReadTimeout("slow"),
            "network": httpx.ConnectError("down"),
        }
        for name, reply in cases.items():
            with self.subTest(case=name):
                check = await self._check(_OpenAI(reply))
                self.assertEqual((check.outcome, check.reason), ("unavailable", None))

    async def test_a_key_a_header_cannot_carry_is_rejected_without_a_request(self):
        for key in (b"", b"   ", b"fixture key with spaces", b"line\nbreak", "fixture-ключ".encode()):
            with self.subTest(key=key):
                openai = _OpenAI(MODEL_LIST)
                check = await self._check(openai, key)
                self.assertEqual((check.outcome, check.reason, check.http_status), ("rejected", "key_invalid", None))
                self.assertEqual(openai.requests, [])

    async def test_rejection_never_echoes_the_key_or_the_provider_message(self):
        # Migration tests run Alembic in this process, and its `fileConfig`
        # disables every already-imported logger (see `tests.test_runtime_wiring`).
        logger = provider_key_check.logger
        self.addCleanup(setattr, logger, "disabled", logger.disabled)
        logger.disabled = False
        with self.assertLogs(logger, "INFO") as logs:
            check = await self._check(_OpenAI(httpx.Response(401, json=ECHOING_401)))
        self.assertEqual(check.diagnostic, "models answered 401 (invalid_api_key)")
        for kept in (repr(check), *logs.output):
            self.assertNotIn(FIXTURE_KEY.decode(), kept)
            self.assertNotIn("fixture-***-key", kept)
            self.assertNotIn("Incorrect API key", kept)

    async def test_an_unusual_error_code_is_not_copied(self):
        check = await self._check(_OpenAI(_error(401, "Bad Code: fixture-openai-key")))
        self.assertEqual(check.diagnostic, "models answered 401")


class OpenAIKeyVerifyApiTests(LibraryFixtures):
    """``verify`` on the Settings page's credential commands, over HTTP."""

    def setUp(self) -> None:
        self.ctx = self.owner_ctx(self._make_context(role="owner", actor="owner_a"))
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        base_url = patch.object(config, "OPENAI_BASE_URL", BASE_URL)
        base_url.start()
        self.addCleanup(base_url.stop)

    # -- helpers ----------------------------------------------------------

    def _with_openai(self, *responses) -> _OpenAI:
        openai = _OpenAI(*responses)
        patcher = openai.patch()
        patcher.start()
        self.addCleanup(patcher.stop)
        return openai

    def _create(self, plaintext: bytes = FIXTURE_KEY, *, provider: str = "openai", key: str = KEY_NAME, **extra):
        return self.client.post("/api/v1/credentials", json={
            "credential_key": key, "provider": provider, "plaintext_b64": _b64(plaintext),
            "display_label": "Image key", **extra})

    def _rotate(self, plaintext: bytes, key: str = KEY_NAME, **extra):
        return self.client.post(f"/api/v1/credentials/{key}/rotate",
                                json={"new_plaintext_b64": _b64(plaintext), **extra})

    def _bind(self, version_id: str):
        return self.client.put(f"/api/v1/credentials/{version_id}/bindings/generation", json={"provider": "openai"})

    def _listed(self) -> list[dict]:
        response = self.client.get("/api/v1/credentials")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _assert_problem(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        self.assertIn("application/problem+json", response.headers["content-type"])
        body = response.json()
        self.assertEqual(body["code"], code)
        return body

    def _assert_no_secret(self, text: str, *secrets: bytes) -> None:
        for secret in secrets or (FIXTURE_KEY,):
            self.assertNotIn(secret.decode(), text)
            self.assertNotIn(_b64(secret), text)

    def _check_events(self) -> list[AuditEventRecord]:
        with tenant_session(self.ctx) as session:
            return list(session.scalars(
                select(AuditEventRecord)
                .where(AuditEventRecord.workspace_id == self.ctx.workspace_id,
                       AuditEventRecord.event_type == CHECK_EVENT)
                .order_by(AuditEventRecord.created_at)))

    # -- verify -------------------------------------------------------------

    def test_a_verified_openai_key_is_checked_once_then_stored(self):
        openai = self._with_openai(MODEL_LIST)
        created = self._create(verify=True)
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual((body["provider"], body["credential_key"], body["state"]), ("openai", KEY_NAME, "active"))
        self.assertIsNone(body["last_check"])
        self._assert_no_secret(created.text)
        (request,) = openai.requests
        self.assertEqual(request.headers["authorization"], f"Bearer {FIXTURE_KEY.decode()}")
        (listed,) = self._listed()
        self.assertEqual(listed["credential_version_id"], body["credential_version_id"])
        (event,) = self._check_events()
        self.assertEqual((event.resource_type, event.resource_id), ("credential_version", body["credential_version_id"]))
        self.assertEqual((event.details_json["provider"], event.details_json["outcome"], event.details_json["http_status"]),
                         ("openai", "valid", 200))
        self._assert_no_secret(json.dumps(event.details_json))

    def test_a_rejected_key_is_not_stored_and_says_why(self):
        self._with_openai(httpx.Response(401, json=ECHOING_401))
        refused = self._create(verify=True)
        problem = self._assert_problem(refused, 422, "provider_key_rejected")
        self.assertEqual(problem["params"], {"provider": "openai", "reason": "key_invalid"})
        self._assert_no_secret(refused.text)
        self.assertNotIn("Incorrect API key", refused.text)
        self.assertEqual(self._listed(), [])
        (event,) = self._check_events()
        self.assertEqual((event.resource_type, event.resource_id), ("credential_key", KEY_NAME))
        self.assertEqual((event.details_json["outcome"], event.details_json["reason"]), ("rejected", "key_invalid"))
        self._assert_no_secret(json.dumps(event.details_json))
        self.assertNotIn("Incorrect API key", json.dumps(event.details_json))

    def test_each_rejection_reason_reaches_the_caller(self):
        cases = {"access_denied": _error(403, None), "quota_exhausted": _error(429, "insufficient_quota")}
        for reason, reply in cases.items():
            with self.subTest(reason=reason), _OpenAI(reply).patch():
                problem = self._assert_problem(self._create(verify=True), 422, "provider_key_rejected")
                self.assertEqual(problem["params"]["reason"], reason)
        self.assertEqual(self._listed(), [])

    def test_a_check_that_could_not_run_stores_nothing(self):
        self._with_openai(httpx.Response(503, text="maintenance"))
        problem = self._assert_problem(self._create(verify=True), 503, "provider_key_check_unavailable")
        self.assertTrue(problem["retryable"])
        self.assertEqual(problem["params"], {"provider": "openai"})
        self.assertEqual(self._listed(), [])
        (event,) = self._check_events()
        self.assertEqual(event.details_json["outcome"], "unavailable")

    def test_without_verify_nothing_changes(self):
        openai = self._with_openai(MODEL_LIST)
        for extra, key in (({}, "openai_unverified"), ({"verify": False}, "openai_explicitly_unverified")):
            with self.subTest(extra=extra):
                created = self._create(key=key, **extra)
                self.assertEqual(created.status_code, 200, created.text)
                self.assertIsNone(created.json()["last_check"])
                rotated = self._rotate(b"fixture-openai-rotated", key=key, **extra)
                self.assertEqual(rotated.status_code, 200, rotated.text)
                self.assertEqual(rotated.json()["version_no"], 2)
        self.assertEqual(openai.requests, [])
        self.assertEqual(self._check_events(), [])

    def test_replacing_with_a_rejected_key_keeps_the_current_one(self):
        openai = self._with_openai(_error(401, "invalid_api_key"), MODEL_LIST)
        first = self._create().json()["credential_version_id"]
        self.assertEqual(self._bind(first).status_code, 200)

        refused = self._rotate(b"fixture-openai-wrong", verify=True)
        self._assert_problem(refused, 422, "provider_key_rejected")
        self._assert_no_secret(refused.text, b"fixture-openai-wrong")
        (still,) = self._listed()
        self.assertEqual(still["credential_version_id"], first)
        self.assertEqual(still["bindings"], [{"binding_scope": "generation", "provider": "openai"}])

        rotated = self._rotate(b"fixture-openai-right", verify=True)
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertEqual(rotated.json()["version_no"], 2)
        self.assertEqual([r.headers["authorization"] for r in openai.requests],
                         ["Bearer fixture-openai-wrong", "Bearer fixture-openai-right"])
        self.assertEqual([(e.resource_type, e.details_json["outcome"]) for e in self._check_events()],
                         [("credential_key", "rejected"), ("credential_version", "valid")])

    def test_providers_without_a_check_refuse_verify(self):
        openai = self._with_openai(MODEL_LIST)
        problem = self._assert_problem(self._create(provider="gemini", key="gemini_api_key", verify=True),
                                       422, "provider_key_check_unsupported")
        self.assertEqual(problem["params"], {"provider": "gemini"})
        self.assertEqual(self._listed(), [])
        self.assertEqual(self._create(provider="gemini", key="gemini_api_key").status_code, 200)
        self._assert_problem(self._rotate(b"fixture-gemini-rotated", key="gemini_api_key", verify=True),
                             422, "provider_key_check_unsupported")
        (listed,) = self._listed()
        self.assertEqual(listed["version_no"], 1)
        self.assertEqual(openai.requests, [])

    def test_a_duplicate_is_refused_before_openai_is_asked(self):
        openai = self._with_openai(MODEL_LIST)
        self.assertEqual(self._create(verify=True).status_code, 200)
        self._assert_problem(self._create(b"fixture-second-key", verify=True), 409, "credential_key_active")
        self.assertEqual(len(openai.requests), 1)

    def test_verify_is_a_boolean(self):
        openai = self._with_openai(MODEL_LIST)
        self.assertEqual(self._create(verify="sometimes").status_code, 422)
        self.assertEqual(self._listed(), [])
        self.assertEqual(openai.requests, [])

    # -- authorization and isolation ------------------------------------------

    def test_members_cannot_add_or_replace_a_key_and_cause_no_check(self):
        openai = self._with_openai(MODEL_LIST)
        version_id = self._create().json()["credential_version_id"]
        owner = self.ctx
        self.ctx = self._make_context(owner.workspace_id, role="member", actor="member_b")
        self._assert_problem(self._create(b"fixture-member-key", key="member_key", verify=True), 403, "owner_role_required")
        self._assert_problem(self._rotate(b"fixture-member-key", verify=True), 403, "owner_role_required")
        self.assertEqual(openai.requests, [])
        self.ctx = owner
        (listed,) = self._listed()
        self.assertEqual((listed["credential_version_id"], listed["version_no"]), (version_id, 1))

    def test_another_workspace_cannot_replace_the_key(self):
        openai = self._with_openai(MODEL_LIST)
        version_id = self._create().json()["credential_version_id"]
        owner = self.ctx
        self.ctx = self.owner_ctx(self._make_context(role="owner", actor="other_owner"))
        self.assertEqual(self._listed(), [])
        self._assert_problem(self._rotate(b"fixture-foreign-key", verify=True), 404, "credential_not_found")
        self.assertEqual(openai.requests, [])
        self.assertEqual(self._check_events(), [])
        self.ctx = owner
        (listed,) = self._listed()
        self.assertEqual((listed["credential_version_id"], listed["version_no"]), (version_id, 1))


if __name__ == "__main__":
    unittest.main()
