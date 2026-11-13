"""A planner failure the provider answered is a plain failure; only an unknown outcome needs reconciling.

The provider's own refusal (a rejected key, a rate limit, a request it will
not serve) and a connection that never opened leave nothing billed and
nothing to reconcile: the call fails with its reason and its reservation is
released. A timeout, a connection lost mid-request or a 5xx stays
``uncertain``. Every failure is logged with its code and HTTP status, never
the key.
"""
import asyncio
import logging
from functools import partial
from unittest.mock import patch

import httpx
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendReservationRecord
from app.entitlements.usage_models import UsageEventRecord
from app.marketing.models import PlanningCallRecord
from app.marketing.planner import PlannerProviderRefused, system_prompt, text_completion
from tests.test_marketing_planning import MarketingPlanningFixtures

SECRET = b"sk-planner-test-secret"
PAID_ENV = {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test",
            "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}


def complete_with(provider, handler):
    return asyncio.run(text_completion(provider, "text-test", SECRET, system_prompt("understanding"), "{}",
                                       transport=httpx.MockTransport(handler)))


def status(code, body=None):
    return lambda request: httpx.Response(code, json=body or {"error": {"message": "refused"}})


def gemini_answer(payload):
    return lambda request: httpx.Response(200, json=payload)


class TestPlannerCompletionClassification(MarketingPlanningFixtures):
    def test_provider_refusals_are_known_failures_with_their_reason(self):
        cases = [("openai", 401, "planner_credential_rejected"), ("openai", 403, "planner_credential_rejected"),
                 ("openai", 429, "planner_rate_limited"), ("openai", 400, "planner_request_rejected"),
                 ("openai", 404, "planner_request_rejected"), ("gemini", 400, "planner_request_rejected"),
                 ("gemini", 403, "planner_credential_rejected")]
        for provider, code, expected in cases:
            with self.subTest(provider=provider, status=code):
                with self.assertRaises(PlannerProviderRefused) as caught:
                    complete_with(provider, status(code))
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.http_status, code)
                self.assertNotIn(SECRET.decode(), str(caught.exception))

    def test_connection_that_never_opened_is_a_known_failure(self):
        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)
        with self.assertRaises(PlannerProviderRefused) as caught:
            complete_with("openai", refuse)
        self.assertEqual(caught.exception.code, "planner_provider_unreachable")
        self.assertIsNone(caught.exception.http_status)

    def test_server_errors_and_timeouts_stay_unknown(self):
        def time_out(request):
            raise httpx.ReadTimeout("no answer", request=request)
        for handler, expected in ((status(500), httpx.HTTPStatusError), (status(503), httpx.HTTPStatusError),
                                  (time_out, httpx.ReadTimeout)):
            with self.subTest(expected=expected.__name__):
                with self.assertRaises(expected):
                    complete_with("openai", handler)

    def test_answer_without_text_is_an_empty_billed_answer(self):
        cases = {
            "gemini output limit": ("gemini", gemini_answer(
                {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"role": "model"}}]}), "MAX_TOKENS"),
            "gemini blocked prompt": ("gemini", gemini_answer({"promptFeedback": {"blockReason": "SAFETY"}}), None),
            "openai refusal": ("openai", gemini_answer(
                {"id": "resp-1", "choices": [{"finish_reason": "stop", "message": {"content": None}}]}), "stop"),
        }
        for name, (provider, handler, finish) in cases.items():
            with self.subTest(name):
                text = complete_with(provider, handler)
                self.assertEqual(text, "")
                self.assertEqual(text.provider_receipt["finish_reason"], finish)


class TestPlannerFailureOutcome(MarketingPlanningFixtures):
    def setUp(self):
        super().setUp()
        # The fixtures' in-process Alembic run disables already-imported loggers
        # (see tests.test_asset_thumbnails); production migrates in a subprocess.
        logging.getLogger("app.marketing.planner").disabled = False

    def paid_call(self):
        self.paid_setup()
        with patch.dict("os.environ", PAID_ENV):
            cap = self.client.get("/api/v1/marketing/capabilities").json()["paid"]
            self.assertTrue(cap["available"], cap)
            project = self.create()
            return project, self.plan_call(project, mode="byok", quote_hash=cap["quote_hash"])

    def records(self, call):
        with tenant_session(self.context) as session:
            row = session.get(PlanningCallRecord, call["id"])
            reservation = session.scalar(select(SpendReservationRecord).where(
                SpendReservationRecord.operation_id == call["operation_id"]))
            attempt = session.scalar(select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.operation_id == call["operation_id"]))
            counted = session.scalars(select(UsageEventRecord).where(
                UsageEventRecord.workspace_id == self.context.workspace_id,
                UsageEventRecord.source_resource_id == attempt.id)).all()
            session.expunge_all()
            return row, reservation, attempt, counted

    def test_rejected_key_fails_with_reason_releases_cost_and_is_logged(self):
        project, call = self.paid_call()
        requests = []

        def reject(request):
            requests.append(request)
            return httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}},
                                  headers={"x-request-id": "req-401"})
        complete = partial(text_completion, transport=httpx.MockTransport(reject))
        with self.assertLogs("app.marketing.planner", level="WARNING") as logged:
            self.execute(call, complete)
        self.execute(call, complete)

        self.assertEqual(len(requests), 1, "a failed call is never resent")
        latest = self.read(project)["latest_call"]
        self.assertEqual((latest["state"], latest["error_code"]), ("failed", "planner_credential_rejected"))
        row, reservation, attempt, counted = self.records(call)
        self.assertEqual(reservation.state, "released")
        self.assertEqual(reservation.outstanding_budget_amount, 0)
        self.assertEqual((attempt.dispatch_state, attempt.error_code), ("failed", "planner_credential_rejected"))
        self.assertEqual(counted, [], "a refused call reached no billable provider work")
        self.assertEqual(row.output_json, {"provider_receipt": {"http_status": 401, "request_id": "req-401"}})
        output = "\n".join(logged.output)
        for expected in ("planner_credential_rejected", "401", "req-401", call["id"]):
            self.assertIn(expected, output)
        self.assertNotIn("tenant-planner-secret", output)

    def test_answer_without_text_is_invalid_output_not_uncertain(self):
        project, call = self.paid_call()
        empty = {"id": "resp-1", "choices": [{"finish_reason": "length", "message": {"content": None}}]}
        self.execute(call, partial(text_completion, transport=httpx.MockTransport(gemini_answer(empty))))
        latest = self.read(project)["latest_call"]
        self.assertEqual((latest["state"], latest["error_code"]), ("failed", "planner_output_invalid"))
        row, reservation, attempt, counted = self.records(call)
        self.assertEqual(reservation.state, "settled", "the provider answered, so its cost is settled")
        self.assertEqual(attempt.dispatch_state, "succeeded")
        self.assertEqual(len(counted), 1)
        self.assertEqual(row.output_json["provider_receipt"]["finish_reason"], "length")

    def test_unknown_outcome_stays_uncertain_and_is_logged(self):
        project, call = self.paid_call()

        def time_out(request):
            raise httpx.ReadTimeout("no answer", request=request)
        with self.assertLogs("app.marketing.planner", level="WARNING") as logged:
            self.execute(call, partial(text_completion, transport=httpx.MockTransport(time_out)))
        latest = self.read(project)["latest_call"]
        self.assertEqual((latest["state"], latest["error_code"]), ("uncertain", "planner_outcome_unknown"))
        row, reservation, attempt, counted = self.records(call)
        self.assertEqual(reservation.state, "uncertain")
        self.assertEqual(attempt.dispatch_state, "uncertain")
        output = "\n".join(logged.output)
        self.assertIn("ReadTimeout", output)
        self.assertIn("planner_outcome_unknown", output)
        self.assertNotIn("tenant-planner-secret", output)

    def test_server_error_stays_uncertain(self):
        project, call = self.paid_call()
        self.execute(call, partial(text_completion, transport=httpx.MockTransport(status(502))))
        latest = self.read(project)["latest_call"]
        self.assertEqual((latest["state"], latest["error_code"]), ("uncertain", "planner_outcome_unknown"))
        _, reservation, _, _ = self.records(call)
        self.assertEqual(reservation.state, "uncertain")
