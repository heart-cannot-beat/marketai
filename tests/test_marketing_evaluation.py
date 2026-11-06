"""Decisions preserve scope, allocation, maturity and business thresholds."""
from copy import deepcopy
from unittest import TestCase
from app.marketing.planner import example_result
from app.marketing.workflow_contracts import EvaluationProtocol
from app.marketing.evaluation import evaluate_snapshot, combination_key


def fixture(mode="byok"):
    understanding = example_result("understanding", {"source_text": "Alpha 65W"})
    plan = example_result("plan", {"understanding": understanding})
    entries, bindings, rows = [], [], []
    for i, combo in enumerate(plan["combinations"]):
        entry = {"entry_id": f"e{i}", "asset_version_id": f"v{i}", "ad_name": f"Ad {i}", "combination_id": combo["id"], "content_sha256": f"image{i}"}
        entries.append(entry)
        bindings.append({"entry_id": f"e{i}", "ad_id": str(i), "conditions_match": True, "deviations": []})
        for day in range(7):
            rows.append({"date": f"2025-01-{day + 1:02d}", "ad_id": str(i), "entry_id": f"e{i}", "spend": str(10 + day),
                "impressions": 10000, "clicks": 100, "purchases": str(8 + day % 3), "purchase_value": str(100 + day)})
    return {"snapshot": {"plan": plan, "protocol": EvaluationProtocol().model_dump(mode="json")}, "entries": entries,
        "bindings": bindings, "rows": rows, "start_date": "2025-01-01", "end_date": "2025-01-07", "timezone": "UTC",
        "mature": True, "execution_mode": mode, "prior_confirmations": []}


class TestMarketingEvaluation(TestCase):
    def test_optional_missing_purchase_counts_remain_unknown_in_ctr_projection(self):
        inputs = fixture();inputs["snapshot"]["plan"]["rules"].update(business_metric="ctr", threshold="0.01")
        for record in inputs["rows"]:
            record["purchases"] = None
        result = evaluate_snapshot(inputs)
        self.assertTrue(all(r["purchases_reported"] is False for r in result["results"]))
        self.assertEqual(result["qualified_asset_version_ids"], [])

    def test_no_delivery_data_is_not_labelled_as_a_customer_report(self):
        inputs = fixture();inputs["rows"] = [];inputs["bindings"] = []
        result = evaluate_snapshot(inputs)
        self.assertEqual(result["evidence_status"], "not_supplied")
        self.assertEqual(result["qualified_asset_version_ids"], [])
        self.assertTrue(all(r["business_status"] == "insufficient" for r in result["results"]))
        self.assertFalse(result["all_assets_failed"])

    def test_unequal_counts_do_not_pick_total_order_winner_and_both_pass(self):
        inputs = fixture()
        result = evaluate_snapshot(inputs)
        self.assertEqual(result, evaluate_snapshot(deepcopy(inputs)))
        self.assertEqual([r["business_status"] for r in result["results"][:2]], ["passes", "passes"])
        self.assertEqual(result["relative_status"], "no_clear_difference")
        self.assertEqual(len(result["qualified_asset_version_ids"]), 5)
        self.assertEqual(result["confirmed_combinations"], [])
        self.assertTrue(all(r["conclusion_scope"] != "angle" for r in result["results"]))

    def test_relative_best_but_all_below_business_threshold(self):
        inputs = fixture()
        inputs["snapshot"]["plan"]["rules"]["threshold"] = "0.01"
        result = evaluate_snapshot(inputs)
        self.assertEqual(result["qualified_asset_version_ids"], [])
        self.assertTrue(result["all_assets_failed"])

    def test_missing_days_not_zero_maturity_and_unequal_allocation(self):
        inputs = fixture()
        inputs["rows"].pop()
        result = evaluate_snapshot(inputs)
        self.assertIn("missing_ad_day_data", result["results"][1]["reason_codes"])
        inputs = fixture()
        inputs["mature"] = False
        result = evaluate_snapshot(inputs)
        self.assertEqual(result["qualified_asset_version_ids"], [])
        self.assertEqual(result["relative_status"], "insufficient")
        inputs = fixture()
        inputs["snapshot"]["protocol"]["allocation"] = "observational"
        result = evaluate_snapshot(inputs)
        self.assertEqual(result["relative_status"], "insufficient")
        self.assertEqual(len(result["qualified_asset_version_ids"]), 5)

    def test_mock_ctr_and_changed_conditions_never_upgrade(self):
        inputs = fixture("mock")
        self.assertEqual(evaluate_snapshot(inputs)["qualified_asset_version_ids"], [])
        inputs = fixture()
        inputs["snapshot"]["plan"]["rules"].update(business_metric="ctr", threshold="0.001")
        result = evaluate_snapshot(inputs)
        self.assertEqual(result["qualified_asset_version_ids"], [])
        self.assertFalse(result["all_assets_failed"])
        inputs = fixture()
        inputs["bindings"][0]["conditions_match"] = False
        result = evaluate_snapshot(inputs)
        self.assertNotIn("v0", result["qualified_asset_version_ids"])
        self.assertEqual(result["factor_conclusion"], "not_established")

    def test_new_image_and_new_round_needed_for_reusable_combination(self):
        inputs = fixture()
        inputs["snapshot"]["plan"]["stage"] = "confirm_combination"
        entry = inputs["entries"][0]
        proof = {"combination_key": combination_key(inputs["snapshot"], entry), "content_sha256": entry["content_sha256"],
                 "evaluation_id": "prior-evaluation", "timezone": "UTC", "protocol": inputs["snapshot"]["protocol"]}
        inputs["prior_confirmations"] = [proof]
        self.assertEqual(evaluate_snapshot(inputs)["confirmed_combinations"], [])
        proof["content_sha256"] = "new-execution-image"
        result = evaluate_snapshot(inputs)
        self.assertEqual(len(result["confirmed_combinations"]), 1)
        proof["timezone"] = "Asia/Hong_Kong"
        self.assertEqual(evaluate_snapshot(inputs)["confirmed_combinations"], [])
