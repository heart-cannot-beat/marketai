"""A planner failure the provider answered is a plain failure; only an unknown outcome needs reconciling.

The provider's own refusal (a rejected key, a rate limit, a request it will
not serve) and a connection that never opened leave nothing billed and
nothing to reconcile: the call fails with its reason and its reservation is
released. A timeout, a connection lost mid-request or a 5xx stays
``uncertain``. Every failure is logged with its code and HTTP status, never
the key.
"""
import asyncio
import json
import logging
from functools import partial
from unittest.mock import patch

import httpx
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import OperationRecord, OperationStateEventRecord
from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendReservationRecord
from app.entitlements.usage_models import UsageEventRecord
from app.marketing.models import PlanningCallRecord
from app.marketing.planner import PlannerProviderRefused, system_prompt, text_completion
from tests.test_marketing_planning import MarketingPlanningFixtures

SECRET = b"sk-planner-test-secret"
PAID_ENV = {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test",
            "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}


#: Gemini's answer to an invalid key: a 400 whose ErrorInfo names the reason.
GEMINI_KEY_INVALID = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
    "status": "INVALID_ARGUMENT", "details": [
        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}}
#: OpenAI's answer when the key's account has no balance or quota left.
OPENAI_QUOTA_EXHAUSTED = {"error": {"message": "You exceeded your current quota, please check your plan.",
    "type": "insufficient_quota", "param": None, "code": "insufficient_quota"}}


def complete_with(provider, handler, model="text-test"):
    return asyncio.run(text_completion(provider, model, SECRET, system_prompt("understanding"), "{}",
                                       transport=httpx.MockTransport(handler)))


def status(code, body=None):
    return lambda request: httpx.Response(code, json=body or {"error": {"message": "refused"}})


def raw(code, text):
    return lambda request: httpx.Response(code, text=text, headers={"content-type": "text/html"})


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

    def test_the_providers_structured_reason_refines_the_status(self):
        cases = {
            "gemini invalid key": ("gemini", status(400, GEMINI_KEY_INVALID),
                                   "planner_credential_rejected", "API_KEY_INVALID"),
            "gemini other ErrorInfo": ("gemini", status(400, {"error": {"code": 400, "details": [
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED"}]}}),
                "planner_request_rejected", "SERVICE_DISABLED"),
            "gemini reason outside ErrorInfo": ("gemini", status(400, {"error": {"code": 400, "details": [
                {"@type": "type.googleapis.com/google.rpc.BadRequest", "reason": "API_KEY_INVALID"}]}}),
                "planner_request_rejected", None),
            "gemini message text only": ("gemini", status(400, {"error": {
                "code": 400, "message": "API key not valid. Please pass a valid API key."}}),
                "planner_request_rejected", None),
            "gemini exhausted resource": ("gemini", status(429, {"error": {
                "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota exceeded"}}),
                "planner_rate_limited", None),
            "openai exhausted quota": ("openai", status(429, OPENAI_QUOTA_EXHAUSTED),
                                       "planner_quota_exhausted", "insufficient_quota"),
            "openai exhausted quota by type": ("openai", status(429, {"error": {
                "message": "no quota", "type": "insufficient_quota", "code": None}}),
                "planner_quota_exhausted", "insufficient_quota"),
            "openai rate limit": ("openai", status(429, {"error": {
                "message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"}}),
                "planner_rate_limited", "rate_limit_exceeded"),
            "openai quota in message text only": ("openai", status(429, {"error": {
                "message": "You exceeded your current quota"}}), "planner_rate_limited", None),
            "openai insufficient_quota on a 400": ("openai", status(400, OPENAI_QUOTA_EXHAUSTED),
                                                    "planner_request_rejected", "insufficient_quota"),
            "openai unsupported parameter": ("openai", status(400, {"error": {"message": "Unsupported parameter",
                "type": "invalid_request_error", "param": "max_tokens", "code": "unsupported_parameter"}}),
                "planner_request_rejected", "unsupported_parameter"),
        }
        for name, (provider, handler, expected, reason) in cases.items():
            with self.subTest(name):
                with self.assertRaises(PlannerProviderRefused) as caught:
                    complete_with(provider, handler)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.provider_reason, reason)

    def test_a_body_without_structured_fields_falls_back_to_the_status(self):
        cases = [("openai", raw(400, "<html>bad request</html>"), "planner_request_rejected"),
                 ("openai", raw(429, "<html>insufficient_quota</html>"), "planner_rate_limited"),
                 ("gemini", raw(400, "<html>API_KEY_INVALID</html>"), "planner_request_rejected"),
                 ("gemini", raw(429, "<html>too many requests</html>"), "planner_rate_limited"),
                 ("openai", status(429, {"error": "insufficient_quota"}), "planner_rate_limited"),
                 ("openai", status(429, ["insufficient_quota"]), "planner_rate_limited"),
                 ("gemini", status(400, {"error": {"details": "API_KEY_INVALID"}}), "planner_request_rejected")]
        for provider, handler, expected in cases:
            with self.subTest(provider=provider, expected=expected):
                with self.assertRaises(PlannerProviderRefused) as caught:
                    complete_with(provider, handler)
                self.assertEqual(caught.exception.code, expected)
                self.assertIsNone(caught.exception.provider_reason)

    def test_openai_request_caps_completion_tokens_and_sets_reasoning_effort_from_the_registry(self):
        answer = {"id": "resp-1", "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        for model, effort in (("gpt-5-mini", "minimal"), ("gpt-5.4-mini", "low"),
                              ("gpt-4o-mini", None), ("text-test", None)):
            with self.subTest(model=model):
                sent = []

                def answer_and_keep(request):
                    sent.append(json.loads(request.content))
                    return httpx.Response(200, json=answer)
                complete_with("openai", answer_and_keep, model=model)
                body = sent[0]
                self.assertEqual(body["model"], model)
                self.assertEqual(body["max_completion_tokens"], 8192)
                self.assertNotIn("max_tokens", body)
                self.assertEqual(body.get("reasoning_effort"), effort)
                self.assertEqual("reasoning_effort" in body, effort is not None)

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

    def test_exhausted_quota_fails_with_its_own_reason_and_releases_cost(self):
        project, call = self.paid_call()
        requests = []

        def exhausted(request):
            requests.append(request)
            return httpx.Response(429, json=OPENAI_QUOTA_EXHAUSTED, headers={"x-request-id": "req-429"})
        complete = partial(text_completion, transport=httpx.MockTransport(exhausted))
        with self.assertLogs("app.marketing.planner", level="WARNING") as logged:
            self.execute(call, complete)
        self.execute(call, complete)

        self.assertEqual(len(requests), 1, "a failed call is never resent")
        latest = self.read(project)["latest_call"]
        self.assertEqual((latest["state"], latest["error_code"]), ("failed", "planner_quota_exhausted"))
        row, reservation, attempt, counted = self.records(call)
        self.assertEqual(row.state, "failed")
        self.assertEqual(reservation.state, "released")
        self.assertEqual(reservation.outstanding_budget_amount, 0)
        self.assertEqual((attempt.dispatch_state, attempt.error_code), ("failed", "planner_quota_exhausted"))
        self.assertEqual(attempt.provider_request_id, "req-429")
        self.assertEqual(counted, [], "a refused call reached no billable provider work")
        self.assertEqual(row.output_json, {"provider_receipt": {"http_status": 429, "request_id": "req-429"}})
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(OperationRecord, call["operation_id"]).state, "failed")
            ended = session.scalars(select(OperationStateEventRecord).where(
                OperationStateEventRecord.operation_id == call["operation_id"],
                OperationStateEventRecord.to_state == "failed")).all()
            self.assertEqual([event.reason for event in ended], ["planner_quota_exhausted"])
        output = "\n".join(logged.output)
        for expected in ("planner_quota_exhausted", "429", "req-429", "provider_reason=insufficient_quota", call["id"]):
            self.assertIn(expected, output)
        self.assertNotIn("tenant-planner-secret", output)
        self.assertNotIn("You exceeded your current quota", output, "the response body is never logged")

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
