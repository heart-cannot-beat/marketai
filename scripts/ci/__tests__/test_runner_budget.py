"""Financial admission boundaries and executable workflow wiring."""
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("runner_budget", REPO / "scripts/ci/runner_budget.py")
budget = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = budget
spec.loader.exec_module(budget)
NOW = datetime(2026, 11, 4, 2, 0, tzinfo=timezone.utc)


class RunnerBudgetTests(unittest.TestCase):
    def test_current_run_is_reserved_before_selection(self):
        self.assertEqual(budget.decide(Decimal(2189), 0).provider, "blacksmith")
        for used in (2190, 2399, 2400, 3000, 10000):
            with self.subTest(used=used):
                self.assertEqual(budget.decide(Decimal(used), 0).provider, "github")

    def test_simultaneous_runs_cannot_all_spend_the_same_headroom(self):
        self.assertEqual(budget.decide(Decimal(1695), 2).provider, "blacksmith")
        self.assertEqual(budget.decide(Decimal(1695), 3).provider, "github")

    def test_cli_vcpu_billing_minutes_are_not_runtime_or_free_minutes(self):
        # 1,219 minutes at 2 vCPU and 238 at 4 vCPU -> 3,390 vCPU minutes.
        self.assertEqual(budget.parse_usage({"billing_minutes": 3390}), Decimal(1695))
        self.assertEqual(budget.parse_usage({"summary": {"billing_minutes": "0"}}), 0)
        for payload in ({}, {"runtime_minutes": 1457}, {"billable_minutes": 3390},
                        {"rows": [{"billing_minutes": 3390}]}, [],
                        {"billing_minutes": 2, "summary": {"billing_minutes": 2}}):
            with self.subTest(payload=payload):
                self.assertRaises(budget.UnverifiedUsage, budget.parse_usage, payload)

    def test_invalid_numbers_never_authorize_compute(self):
        for value in (True, None, [], {}, "NaN", "Infinity", "-1", "oops"):
            with self.subTest(value=value):
                self.assertRaises(budget.UnverifiedUsage, budget.parse_usage, {"billing_minutes": value})
        self.assertRaises(budget.UnverifiedUsage, budget.decide, Decimal(0), -1)

    def env(self):
        return {"BLACKSMITH_USAGE_TOKEN": "unit-test-value", "GH_TOKEN": "unit-test-value",
                "GITHUB_REPOSITORY": "heart-cannot-beat/marketai", "GITHUB_RUN_ID": "123"}

    def test_missing_secret_and_manual_override_make_no_network_calls(self):
        usage = Mock(side_effect=AssertionError("network must not be used"))
        runs = Mock(side_effect=AssertionError("network must not be used"))
        for env in ({}, {**self.env(), "BLACKSMITH_FORCE_GITHUB": "true"},
                    {**self.env(), "BLACKSMITH_FORCE_GITHUB": "1"}):
            self.assertEqual(budget.select(env, NOW, usage, runs).provider, "github")
        usage.assert_not_called()
        runs.assert_not_called()

    def test_authentication_api_or_schema_errors_fall_back_without_leaking(self):
        for failure in (ValueError("token=private"), TimeoutError("private URL")):
            decision = budget.select(self.env(), NOW, Mock(side_effect=failure), Mock())
            self.assertEqual(decision.provider, "github")
            self.assertNotIn("private", decision.reason)
        decision = budget.select(self.env(), NOW, Mock(return_value=Decimal(0)),
                                 Mock(side_effect=ValueError("partial listing")))
        self.assertEqual(decision.provider, "github")

    def test_known_low_usage_selects_both_blacksmith_sizes(self):
        decision = budget.select(self.env(), NOW, Mock(return_value=Decimal(1695)),
                                 Mock(return_value=0), clock=lambda: NOW)
        self.assertEqual(decision.outputs()["linux_2"], budget.BLACKSMITH_2)
        self.assertEqual(decision.outputs()["linux_4"], budget.BLACKSMITH_4)

    def test_month_rollover_never_uses_a_previous_months_read(self):
        before = datetime(2026, 11, 30, 23, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 12, 1, tzinfo=timezone.utc)
        decision = budget.select(self.env(), before, Mock(return_value=Decimal(0)),
                                 Mock(return_value=0), clock=lambda: after)
        self.assertEqual(decision.provider, "github")
        self.assertEqual(budget.month_start(after), after)

    def test_active_runs_are_deduplicated_and_current_run_is_not_counted_twice(self):
        fetch = Mock(return_value={"total_count": 3, "workflow_runs": [
            {"id": 123, "run_attempt": 1}, {"id": 4, "run_attempt": 1}, {"id": 5, "run_attempt": 1}]})
        self.assertEqual(budget.concurrent_runs("owner/repo", 123, NOW, "test", fetch), 2)

    def test_recent_reruns_reserve_each_attempt_including_current_prior_attempts(self):
        fetch = Mock(return_value={"total_count": 2, "workflow_runs": [
            {"id": 123, "run_attempt": 3}, {"id": 4, "run_attempt": 2}]})
        self.assertEqual(budget.concurrent_runs("owner/repo", 123, NOW, "test", fetch,
                                               current_attempt=3), 4)

    def test_truncated_or_invalid_run_lists_fall_back(self):
        for reply in ({"total_count": 101, "workflow_runs": [{"id": 4}]},
                      {"workflow_runs": []}, {"total_count": True, "workflow_runs": [{}]}):
            self.assertRaises(budget.UnverifiedUsage, budget.concurrent_runs,
                              "owner/repo", 123, NOW, "test", Mock(return_value=reply))

    def test_fallback_outputs_and_summary_are_safe_and_reviewable(self):
        with tempfile.TemporaryDirectory() as root:
            output, summary = Path(root) / "outputs", Path(root) / "summary"
            budget.publish(budget.Decision("github", "safe fallback"),
                           {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}, NOW)
            self.assertIn("linux_4=ubuntu-24.04", output.read_text())
            self.assertIn("80%", summary.read_text())

    def test_every_ci_job_consumes_the_budget_decision(self):
        text = (REPO / ".github/workflows/ci.yml").read_text()
        # Parse job blocks without introducing a PyYAML dependency into preflight.
        import re
        jobs = dict(re.findall(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)",
                               text.split("\njobs:\n", 1)[1], re.M | re.S))
        self.assertEqual(len(jobs), 8)
        self.assertIn("runs-on: ubuntu-24.04", jobs.pop("runner-budget"))
        for name, job in jobs.items():
            with self.subTest(job=name):
                self.assertIn("runner-budget", re.search(r"^    needs: (.+)$", job, re.M).group(1))
                self.assertIn("needs.runner-budget.outputs.linux_", job)
                self.assertIn("|| 'ubuntu-24.04'", job)
                self.assertNotIn("continue-on-error", job)
        self.assertIn("needs: [runner-budget, verify, web]", jobs["compose-integration"])
        self.assertIn("needs: [runner-budget, verify, web]", jobs["golden-path-e2e"])

    def test_reservation_covers_all_current_blacksmith_timeouts(self):
        import re
        text = (REPO / ".github/workflows/ci.yml").read_text()
        blocks = re.findall(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)",
                            text.split("\njobs:\n", 1)[1], re.M | re.S)
        maximum = 0
        for name, job in blocks:
            if name == "runner-budget":
                continue
            weight = 2 if "outputs.linux_4" in job else 1
            timeout = re.search(r"^    timeout-minutes: (.+)$", job, re.M).group(1)
            minutes = int(re.search(r"&& (\d+)", timeout).group(1)) if "&&" in timeout else int(timeout)
            maximum += weight * (minutes + 5)
        self.assertGreaterEqual(budget.RUN_RESERVATION, maximum)


if __name__ == "__main__":
    unittest.main()
