"""J-44 (#255): CSV preflight issues are ``code + params``; the browser words them."""
import ast
import unittest
from dataclasses import replace
from pathlib import Path

from app.adapters.persistence.database import tenant_session
from app.marketing import metrics
from app.marketing.workflow_models import MarketingMetricBatchRecord
from tests.test_marketing_workflow import MarketingWorkflowFixtures

METRICS_SOURCE = Path(metrics.__file__).read_text(encoding="utf-8")


def raised_codes():
    """Every code literal passed to ``issue(...)`` or ``preflight_issue(...)`` in metrics.py."""
    found = set()
    for node in ast.walk(ast.parse(METRICS_SOURCE)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("issue", "preflight_issue"):
            code = node.args[2]
            if isinstance(code, ast.Name) and code.id == "code":
                continue  # the local ``issue`` helper forwarding its own argument
            assert isinstance(code, ast.Constant), ast.dump(code)
            found.add(code.value)
    return found


class TestIssueTable(unittest.TestCase):
    def test_every_raised_code_is_declared_and_every_declared_code_is_raised(self):
        self.assertEqual(raised_codes(), set(metrics.ISSUE_PARAMS))

    def test_every_metric_column_is_a_declared_field(self):
        self.assertLessEqual(set(metrics.ALIASES), set(metrics.ISSUE_FIELDS))

    def test_an_issue_must_carry_exactly_its_declared_params(self):
        self.assertEqual(metrics.preflight_issue(3, "date", "outside_round_dates", start="2025-01-01", end="2025-01-07"),
                         {"row": 3, "field": "date", "code": "outside_round_dates",
                          "params": {"start": "2025-01-01", "end": "2025-01-07"}})
        self.assertEqual(metrics.preflight_issue(1, "headers", "invalid_headers"),
                         {"row": 1, "field": "headers", "code": "invalid_headers"})
        with self.assertRaises(ValueError):
            metrics.preflight_issue(3, "date", "outside_round_dates", start="2025-01-01")
        with self.assertRaises(ValueError):
            metrics.preflight_issue(1, "headers", "invalid_headers", detail="不允许的说明")
        with self.assertRaises(ValueError):
            metrics.preflight_issue(1, "headers", "made_up_code")
        with self.assertRaises(ValueError):
            metrics.preflight_issue(1, "made_up_field", "invalid_headers")

    def test_values_from_the_file_are_bounded(self):
        issue = metrics.preflight_issue(2, "currency", "row_currency_mismatch", expected="USD", found="X" * 500)
        self.assertEqual(len(issue["params"]["found"]), 100)


class TestPreflightIssues(MarketingWorkflowFixtures):
    def batches(self, row):
        return self.client.get(f"/api/v1/marketing/rounds/{row['id']}", params={"include_text_language": "true"}).json()["data"]["batches"]

    def test_issues_are_codes_with_params_and_no_sentences(self):
        _, row = self.bound()
        body = self.csv(row, account_timezone="Asia/Shanghai")
        ad = row["bindings"][0]["ad_id"]
        body["csv_text"] = body["csv_text"].replace("purchase_value\n", "purchase_value,Currency\n", 1)
        lines = body["csv_text"].splitlines()
        lines = [lines[0]] + [line + ",USD" for line in lines[1:]]
        lines.append(f"2025-02-01,{ad},1,1000,10,1,30,EUR")
        body["csv_text"] = "\n".join(lines) + "\n"
        legacy = self.command(row, "metrics/preview", body, 201)["batches"][-1]
        self.assertTrue(all("params" not in issue for issue in legacy["issues"]), "older clients keep the pre-#255 shape")
        batch = self.batches(row)[-1]
        self.assertEqual(batch["id"], legacy["id"])
        by_code = {issue["code"]: issue for issue in batch["issues"]}
        self.assertEqual(by_code["reporting_context_mismatch"]["params"],
                         {"timezone": "UTC", "currency": "USD", "attribution_window": "7d_click_1d_view"})
        self.assertEqual(by_code["outside_round_dates"]["params"], {"start": "2025-01-01", "end": "2025-01-07"})
        self.assertEqual(by_code["row_currency_mismatch"]["params"], {"expected": "USD", "found": "EUR"})
        for issue in batch["issues"]:
            self.assertEqual(set(issue.get("params") or {}), set(metrics.ISSUE_PARAMS[issue["code"]]), issue)
            self.assertEqual(set(issue) - {"params"}, {"row", "field", "code"})

    def test_issues_stored_before_params_still_read_back(self):
        _, row = self.bound()
        batch = self.command(row, "metrics/preview", self.csv(row, account_timezone="Asia/Shanghai"), 201)["batches"][-1]
        with tenant_session(self.context) as session:
            record = session.get(MarketingMetricBatchRecord, batch["id"])
            record.issues_json = [{k: v for k, v in issue.items() if k != "params"} for issue in record.issues_json]
        stored = self.batches(row)[-1]
        self.assertEqual(stored["issues"], [{"row": 0, "field": "context", "code": "reporting_context_mismatch"}])

    def test_the_interface_language_does_not_change_the_issues_or_their_fingerprint(self):
        _, row = self.bound()
        body = self.csv(row, account_timezone="Asia/Shanghai")
        row = self.command(row, "metrics/preview", body, 201)
        english = self.batches(row)[-1]
        self.context = replace(self.context, ui_language="zh-CN")
        self.command(row, "metrics/preview", body, 201)
        chinese = self.batches(row)[-1]
        self.assertEqual(chinese["id"], english["id"], "the same file is the same preview in any language")
        self.assertEqual(chinese["issues"], english["issues"])
