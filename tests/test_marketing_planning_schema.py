"""Provider-bound schema and lossless dynamic element transport."""
import asyncio
import json
from unittest import TestCase
from unittest.mock import patch

import httpx

from app.marketing.contracts import TestPlan as MarketingTestPlan
from app.marketing.planner import text_completion, system_prompt, example_result, completion_messages
from app.marketing.planning_schema import domain_document, strict_schema


class TestPlanningSchema(TestCase):
    def test_explicit_repair_is_a_correction_turn_after_the_rejected_draft(self):
        inputs = {"source_text": "two candidates", "rejected_plan_feedback": {
            "policy": "marketing-explicit-plan-repair-v1", "rejected_plan": {"groups": [{"id": "g1"}]},
            "validation_errors": [{"msg": "比较至少需要两个测试组"}]}}
        messages = completion_messages("system", json.dumps(inputs))
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(json.loads(messages[1]["content"]), {"source_text": "two candidates"})
        self.assertEqual(json.loads(messages[2]["content"]), inputs["rejected_plan_feedback"]["rejected_plan"])
        self.assertIn("比较至少需要两个测试组", messages[-1]["content"])
        self.assertIn("limited_expression_coverage=true", messages[-1]["content"])

    def test_actual_openai_request_constrains_numeric_fields_and_keeps_receipt(self):
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"x-request-id": "req-planning-test"}, json={
                "id": "response-test", "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                "choices": [{"finish_reason": "stop", "message": {"content": '{"example":true}'}}]})

        client = httpx.AsyncClient
        with patch("app.marketing.planner.httpx.AsyncClient",
            side_effect=lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs)):
            result = asyncio.run(text_completion("openai", "gpt-4.1-mini-2025-04-14", b"test-secret",
                system_prompt("plan"), "{}"))
        self.assertEqual(len(requests), 1)
        fmt = requests[0]["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["json_schema"]["strict"])
        schema = fmt["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["media_budget"]["type"], "number")
        self.assertEqual(schema["properties"]["small_round_budget"]["type"], "number")
        self.assertEqual(schema["$defs"]["DecisionRules"]["properties"]["threshold"]["type"], "number")
        self.assertEqual(result.provider_receipt["request_id"], "req-planning-test")
        self.assertNotIn("test-secret", json.dumps(result.provider_receipt))
        self.assertEqual(json.loads(result), {"example": True})

    def test_every_wire_object_is_closed_and_all_fields_required(self):
        schema = strict_schema(MarketingTestPlan.model_json_schema())

        def visit(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node["additionalProperties"], False)
                    self.assertEqual(set(node["required"]), set(node.get("properties", {})))
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)
        visit(schema)

    def test_dynamic_element_names_round_trip_into_valid_domain_plan(self):
        data = example_result("plan", {"understanding": {"facts": [], "audience": "travel users"}})
        data["combinations"][0]["elements"] = [
            {"key": "互锁细节光线", "value": "侧光"}, {"key": "Compatibility caveat", "value": "核对车辆授权"}]
        value = MarketingTestPlan.model_validate(domain_document(data))
        self.assertEqual(value.combinations[0].elements,
            {"互锁细节光线": "侧光", "Compatibility caveat": "核对车辆授权"})

    def test_duplicate_element_names_cannot_silently_overwrite(self):
        with self.assertRaisesRegex(ValueError, "duplicate_element_names"):
            domain_document({"combinations": [{"elements": [
                {"key": "场景", "value": "one"}, {"key": " 场景 ", "value": "two"}]}]})
