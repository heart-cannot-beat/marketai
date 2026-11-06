"""#253: every refusal the interface can receive carries a stable ``code`` and
the ``params`` its copy interpolates; ``detail`` is a diagnostic only.

The web renders ``error-<code>`` in the interface language
(``web/lib/i18n/domain-errors.ts``) and its ``error-codes.spec.ts`` checks
every code raised here has en and zh-CN copy. These tests pin the server
half: the codes and params on the wire, their idempotent replay, and that
Workspace and role boundaries answer with the same stable codes.
"""
from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests.support.library_fixtures import LibraryFixtures, png_bytes

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app import config
from app.experiments.service import ExperimentConflict, ExperimentError, ExperimentNotFound
from app.generation.run_queries import error_code_of
from app.launch.service import LaunchConflict, LaunchError, LaunchNotFound
from app.library import api as library_api
from app.library import service
from app.library.contracts import ENTRY_PRODUCT, ROLE_MEMBER
from app.main import app
from app.platform.http_identity import request_tenant_context

ROOT = Path(__file__).resolve().parent.parent


class _ShortText(BaseModel):
    text: str = Field(max_length=5)


class ProblemContractTests(unittest.TestCase):
    """The shared Problem Details shape and the app-wide handlers."""

    def _probe(self) -> TestClient:
        probe = FastAPI()
        library_api.register_problem_handlers(probe)

        @probe.get("/plain/{status}")
        def plain(status: int):
            raise HTTPException(status, "数据库未就绪")

        @probe.get("/coded")
        def coded():
            raise HTTPException(422, {
                "code": "text_too_long", "message": "介绍最多 20000 个字符", "params": {"limit": 20000},
            })

        @probe.post("/validated")
        def validated(body: _ShortText):
            return {}

        return TestClient(probe, raise_server_exceptions=False)

    def test_openapi_problem_details_declares_optional_params(self):
        spec = json.loads((ROOT / "contracts/openapi/marketai-domain-v1.json").read_text())
        problem = spec["components"]["schemas"]["ProblemDetails"]
        self.assertIn("code", problem["required"])
        self.assertNotIn("params", problem["required"])
        self.assertEqual(problem["properties"]["params"]["type"], "object")
        self.assertIn("params", problem["properties"]["field_errors"]["items"]["properties"])

    def test_a_bare_http_exception_still_answers_with_a_stable_code(self):
        client = self._probe()
        for status, code in (
            (400, "request_invalid"), (403, "forbidden"), (404, "not_found"),
            (409, "conflict"), (503, "service_unavailable"), (418, "error"),
        ):
            response = client.get(f"/plain/{status}")
            self.assertEqual(response.status_code, status)
            body = response.json()
            self.assertEqual(body["code"], code)
            # The Chinese diagnostic stays a diagnostic.
            self.assertEqual(body["detail"], "数据库未就绪")
            self.assertNotIn("params", body)

    def test_params_travel_with_a_coded_refusal(self):
        body = self._probe().get("/coded").json()
        self.assertEqual(body["code"], "text_too_long")
        self.assertEqual(body["params"], {"limit": 20000})

    def test_a_failed_field_carries_its_limit_but_never_the_input(self):
        response = self._probe().post("/validated", json={"text": "far too long"})
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "validation_error")
        field = body["field_errors"][0]
        self.assertEqual((field["field"], field["code"]), ("body.text", "string_too_long"))
        self.assertEqual(field["params"], {"max_length": 5})
        self.assertNotIn("far too long", json.dumps(body["field_errors"]))


class CodedDomainErrorTests(unittest.TestCase):
    """Errors whose message is a code carry it; any other message is a diagnostic."""

    def test_experiment_errors(self):
        self.assertEqual(ExperimentConflict("no_material_verdicts").code, "no_material_verdicts")
        self.assertEqual(ExperimentConflict("concept_unresolved:k1,k2").code, "concept_unresolved")
        self.assertEqual(ExperimentConflict("实验被并发修改").code, "experiment_conflict")
        self.assertEqual(ExperimentError("alpha 必须在 0 和 1 之间").code, "experiment_invalid")
        self.assertEqual(ExperimentNotFound("实验不存在").code, "experiment_not_found")
        explicit = ExperimentError("x", code="experiment_threshold_invalid", params={"metric": "ctr"})
        self.assertEqual((explicit.code, explicit.params), ("experiment_threshold_invalid", {"metric": "ctr"}))

    def test_launch_errors(self):
        self.assertEqual(LaunchConflict("confirmation_token_expired").code, "confirmation_token_expired")
        self.assertEqual(LaunchConflict("Preview hash mismatch").code, "launch_conflict")
        self.assertEqual(LaunchNotFound("Launch Preview 不存在").code, "launch_not_found")
        self.assertEqual(LaunchError("currency 必须是 3 位", code="launch_currency_invalid").code,
                         "launch_currency_invalid")

    def test_a_run_item_summary_leads_with_its_code(self):
        self.assertEqual(error_code_of("qc_blocking_failure: 文字不清晰"), "qc_blocking_failure")
        self.assertEqual(error_code_of("provider_outcome_unknown"), "provider_outcome_unknown")
        self.assertIsNone(error_code_of("RuntimeError: boom"))
        self.assertIsNone(error_code_of(""))
        self.assertIsNone(error_code_of(None))


class LibraryCodeTests(LibraryFixtures):
    """Library refusals over HTTP: specific codes, params, replay and boundaries."""

    def setUp(self):
        self.context = self._make_context(role=ROLE_MEMBER)
        app.dependency_overrides[request_tenant_context] = lambda: self.context
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _upload(self, raw: bytes, key: str):
        # The streaming upload route, with the app-wide problem handlers.
        probe = FastAPI()
        probe.include_router(library_api.router)
        library_api.register_problem_handlers(probe)
        probe.dependency_overrides[request_tenant_context] = lambda: self.context
        with TestClient(probe, raise_server_exceptions=False) as client:
            return client.post(
                "/api/library/reference-objects",
                files={"file": ("big.png", raw, "image/png")},
                headers={"Idempotency-Key": key},
            )

    def test_an_oversized_upload_answers_file_too_large_with_its_limit_and_replays_it(self):
        key = f"ik-{uuid.uuid4().hex}"
        with patch.object(config, "LIBRARY_MAX_REFERENCE_BYTES", 3 * 1024 * 1024):
            first = self._upload(b"x" * (3 * 1024 * 1024 + 1), key)
            replay = self._upload(b"x" * (3 * 1024 * 1024 + 1), key)
        for response in (first, replay):
            self.assertEqual(response.status_code, 413, response.text)
            body = response.json()
            self.assertEqual(body["code"], "file_too_large")
            self.assertEqual(body["params"], {"limit_mb": 3})
        # The replay is the recorded refusal, not a second evaluation.
        self.assertEqual(first.json()["detail"], replay.json()["detail"])

    def test_a_library_validation_refusal_names_its_reason(self):
        response = self.client.post(
            "/api/v1/library/entries",
            json={"entry_type": ENTRY_PRODUCT, "display_name": "  "},
            headers={"Idempotency-Key": f"ik-{uuid.uuid4().hex}"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["code"], "library_display_name_required")

    def test_a_replayed_refusal_keeps_its_exact_code(self):
        refusal = service.LibraryConflict("条目已归档，不能继续编辑", code="entry_archived")
        recorded = service._terminal_response(refusal)
        self.assertEqual((recorded["_terminal"], recorded["_code"]), ("conflict", "entry_archived"))
        too_large = service._terminal_response(service.UploadTooLarge("big", limit=20 * 1024 * 1024))
        self.assertEqual(too_large["_params"], {"limit_mb": 20})
        # Records written before #253 replay the class's default code.
        legacy = service.LibraryConflict("old", {})
        self.assertEqual(legacy.code, "library_conflict")

    def test_a_member_is_refused_an_owner_only_archive_with_a_stable_code(self):
        owner = self.owner_ctx(self.context)
        entry = service.create_entry(owner, entry_type=ENTRY_PRODUCT, display_name="Charger")
        response = self.client.post(
            f"/api/v1/library/entries/{entry['id']}/archive",
            headers={
                "Idempotency-Key": f"ik-{uuid.uuid4().hex}",
                "If-Match": f'"{entry["draft"]["revision"]}"',
            },
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], "library_forbidden")
        self.assertEqual(service.get_entry(owner, entry["id"])["lifecycle"], "active")

    def test_another_workspaces_object_reads_exactly_like_an_unknown_one(self):
        other = self._make_context(role=ROLE_MEMBER)
        foreign = service.upload_reference_object(
            other, raw=png_bytes(), declared_mime="image/png", filename="x.png",
            idempotency_key=f"ik-{uuid.uuid4().hex}", request_fingerprint="fp",
        )
        foreign_id = foreign.get("object_id") or foreign.get("id")
        unknown = self.client.get("/api/v1/library/reference-objects/refobj_does_not_exist")
        crossed = self.client.get(f"/api/v1/library/reference-objects/{foreign_id}")
        self.assertEqual((unknown.status_code, crossed.status_code), (404, 404))
        self.assertEqual(unknown.json()["code"], "reference_object_not_found")
        self.assertEqual(crossed.json()["code"], unknown.json()["code"])
        self.assertEqual(crossed.json()["detail"], unknown.json()["detail"])


class MarketingSourceLimitTests(LibraryFixtures):
    """A product import over the limit reads by code and limit (the page's "too long")."""

    def setUp(self):
        self.context = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.context
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_product_material_over_the_limit_names_the_field_and_its_limit(self):
        response = self.client.post(
            "/api/v1/marketing/projects",
            json={"source_text": "长" * 16001},
            headers={"Idempotency-Key": f"key_{uuid.uuid4().hex}"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        body = response.json()
        self.assertEqual(body["code"], "validation_error")
        field = body["field_errors"][0]
        self.assertEqual((field["field"], field["code"]), ("body.source_text", "string_too_long"))
        self.assertEqual(field["params"], {"max_length": 16000})

    def test_the_service_limit_is_sent_as_a_param(self):
        from app.adapters.persistence.database import tenant_session
        from app.marketing import service as marketing

        with tenant_session(self.context) as session:
            with self.assertRaises(marketing.MarketingError) as raised:
                marketing.create_project(session, self.context, "  ", None)
        self.assertEqual(raised.exception.code, "source_invalid")
        self.assertEqual(raised.exception.params, {"limit": 16000})


if __name__ == "__main__":
    unittest.main()
