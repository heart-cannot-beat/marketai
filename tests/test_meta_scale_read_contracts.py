"""Contract tests for Scale's four Meta read contracts (#108, #101 S2).

One test per query instance pins what it reads for a target day D: its
fields, breakdowns, time window, ``time_increment`` and attribution
(Scale contract §6.2, #101 planning comment §3). The contract hashes are
pinned too: changing any frozen part of a contract changes its hash, which
needs a new contract version and a deliberate edit here -- never a silent
change at run time.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from app.meta import scale_read_contracts as contracts
from app.meta.scale_read_contracts import (
    CONTRACTS,
    SCALE_READ_POLICY,
    AccountBinding,
    ScaleReadContractError,
    generation_spec,
    resolve_generation_contract,
    sandbox_account_binding,
)

ROOT = Path(__file__).resolve().parents[1]
D = date(2026, 11, 9)
BINDING = AccountBinding("act_acme_charge_sandbox", "USD", "America/Los_Angeles")
ATTRIBUTION = {
    "action_attribution_windows": ["7d_click", "1d_view"],
    "action_report_time": "impression",
    "purchase_action_type": "purchase",
}
IDENTITY = {"account_id", "account_currency", "ad_id"}
DAILY_METRICS = {"spend", "impressions", "inline_link_clicks", "actions", "action_values"}

#: Changing one of these is a contract change: bump the version and re-pin.
PINNED_HASHES = {
    "daily": "dd008255b042f5c4af05100edb5f32409c14445f70952dddee04701eb27ce901",
    "exact_window": "723924610d9ae1e5bb21c1af5699ff01f2bcbe5df84c6b6130d52d5fd50094f8",
    "breakdown_placement": "6b67c2e8cf321cf498263ac34db2b22ab60bf8db2cd7515235b15423a0d4c32f",
    "breakdown_country": "27a3b97d2bcd1b18be434e627b0e6a3f64d32733a5a7164f4678ca34b9f2ff50",
    "breakdown_age_gender": "eb088e0a835383217dc60da12379234bc3655c67c40431a9cf081e701e5dee49",
    "delivery_configuration": "5a04e913e705437f3fd01020b8eb92cb3732ddf6207470c74d9833a39350965a",
}


class QueryInstanceContracts(unittest.TestCase):
    """What each query instance reads for D = 2026-11-09."""

    def _instance(self, contract_type: str, name: str):
        contract = CONTRACTS[contract_type]
        return contract, contract.instance(name)

    def _assert_insights(self, contract, instance, *, fields, breakdowns, since, until, time_increment, role):
        self.assertEqual(contract.level, "ad")
        self.assertEqual(set(contract.fields), fields)
        self.assertEqual(contract.breakdowns, breakdowns)
        self.assertEqual(contract.attribution(), ATTRIBUTION)
        self.assertEqual(instance.read, contracts.INSIGHTS_READ)
        self.assertEqual(instance.time_range(D), (since, until))
        self.assertEqual(instance.time_increment, time_increment)
        self.assertEqual(instance.window_role, role)
        definition = contract.definition()
        self.assertEqual((definition["api_version"], definition["pagination"], definition["parser_rules"]),
                         (contracts.META_GRAPH_API_VERSION, {"limit": 25}, "scale-read-parse-v1"))

    def test_daily(self):
        contract, instance = self._instance("daily", "daily")
        self.assertEqual([i.name for i in contract.instances], ["daily"])
        self._assert_insights(contract, instance, fields=IDENTITY | DAILY_METRICS, breakdowns=(),
                              since=date(2026, 10, 27), until=D, time_increment="1", role=None)
        self.assertNotIn("reach", contract.fields, "daily reach and frequency are never summed (§6.3)")
        self.assertNotIn("frequency", contract.fields)

    def test_exact_window_current_7d(self):
        contract, instance = self._instance("exact_window", "current_7d")
        self._assert_insights(contract, instance, fields=IDENTITY | DAILY_METRICS | {"reach", "frequency"},
                              breakdowns=(), since=date(2026, 11, 3), until=D, time_increment="all_days",
                              role="current_7d")

    def test_exact_window_baseline_7d(self):
        contract, instance = self._instance("exact_window", "baseline_7d")
        self._assert_insights(contract, instance, fields=IDENTITY | DAILY_METRICS | {"reach", "frequency"},
                              breakdowns=(), since=date(2026, 10, 27), until=date(2026, 11, 2),
                              time_increment="all_days", role="baseline_7d")

    def test_exact_window_rolling_14d(self):
        contract, instance = self._instance("exact_window", "rolling_14d")
        self._assert_insights(contract, instance, fields=IDENTITY | DAILY_METRICS | {"reach", "frequency"},
                              breakdowns=(), since=date(2026, 10, 27), until=D, time_increment="all_days",
                              role="rolling_14d")
        self.assertEqual([i.name for i in contract.instances], ["current_7d", "baseline_7d", "rolling_14d"])

    def test_breakdown_placement(self):
        contract, instance = self._instance("breakdown_placement", "current_7d")
        self._assert_insights(contract, instance, fields=IDENTITY | DAILY_METRICS,
                              breakdowns=("publisher_platform", "platform_position"),
                              since=date(2026, 11, 3), until=D, time_increment="1", role="current_7d")

    def test_breakdown_country(self):
        contract, instance = self._instance("breakdown_country", "current_7d")
        self._assert_insights(contract, instance, fields=IDENTITY | DAILY_METRICS, breakdowns=("country",),
                              since=date(2026, 11, 3), until=D, time_increment="1", role="current_7d")

    def test_breakdown_age_gender(self):
        contract, instance = self._instance("breakdown_age_gender", "current_7d")
        self._assert_insights(contract, instance, fields=IDENTITY | DAILY_METRICS, breakdowns=("age", "gender"),
                              since=date(2026, 11, 3), until=D, time_increment="1", role="current_7d")

    def test_delivery_configuration(self):
        contract, instance = self._instance("delivery_configuration", "configuration")
        self.assertEqual((instance.read, instance.time_increment, instance.time_range(D)),
                         (contracts.CONFIGURATION_READ, None, (D, D)))
        self.assertEqual(contract.attribution(), {}, "a configuration read has no attribution window")
        self.assertEqual(contract.breakdowns, ())
        edges = dict(contract.edges)
        self.assertEqual(list(edges), ["account", "ads", "adsets", "campaigns"], "the page chain's order")
        self.assertEqual(set(edges["account"]), {"account_id", "currency", "timezone_name"})
        self.assertLessEqual({"creative", "status", "effective_status", "adset_id"}, set(edges["ads"]))
        self.assertLessEqual({"daily_budget", "lifetime_budget", "optimization_goal", "bid_strategy",
                              "attribution_spec", "targeting", "learning_stage_info"}, set(edges["adsets"]))
        self.assertLessEqual({"status", "effective_status", "daily_budget", "lifetime_budget", "bid_strategy"},
                             set(edges["campaigns"]))
        self.assertEqual(set(contract.fields), {f"{edge}.{field}" for edge, names in edges.items() for field in names})


class ContractIdentity(unittest.TestCase):
    def test_hashes_are_pinned(self):
        self.assertEqual({kind: contract.contract_hash for kind, contract in CONTRACTS.items()}, PINNED_HASHES)

    def test_every_frozen_part_changes_the_hash(self):
        daily = CONTRACTS["daily"]
        variants = [
            replace(daily, version=2),
            replace(daily, fields=daily.fields + ("reach",)),
            replace(daily, breakdowns=("country",)),
            replace(daily, level="adset"),
            replace(daily, instances=(replace(daily.instances[0], since_offset_days=-6),)),
            replace(daily, instances=(replace(daily.instances[0], time_increment="all_days"),)),
        ]
        hashes = {variant.contract_hash for variant in variants}
        self.assertEqual(len(hashes), len(variants))
        self.assertNotIn(daily.contract_hash, hashes)

    def test_the_first_batch_reads_no_breakdown_family(self):
        self.assertEqual(SCALE_READ_POLICY.breakdown_contract_types, frozenset())
        self.assertEqual(SCALE_READ_POLICY.required_contract_types(),
                         ("daily", "exact_window", "delivery_configuration"))
        later = replace(SCALE_READ_POLICY, breakdown_contract_types=frozenset({"breakdown_country"}))
        self.assertEqual(later.required_contract_types(),
                         ("daily", "exact_window", "breakdown_country", "delivery_configuration"))

    def test_unknown_contracts_and_instances_are_refused(self):
        with self.assertRaises(ScaleReadContractError) as raised:
            contracts.contract_for("lifetime_value")
        self.assertEqual(raised.exception.code, "meta_read_contract_unknown")
        with self.assertRaises(ScaleReadContractError) as raised:
            CONTRACTS["daily"].instance("current_7d")
        self.assertEqual(raised.exception.code, "meta_read_instance_unknown")


class GenerationBinding(unittest.TestCase):
    def _resolve(self, spec):
        return resolve_generation_contract(**spec)

    def test_a_generation_round_trips_to_its_contract(self):
        for kind, contract in CONTRACTS.items():
            with self.subTest(kind=kind):
                spec = generation_spec(contract, BINDING)
                self.assertEqual(spec["local_timezone"], "America/Los_Angeles")
                self.assertEqual((spec["time_window"]["definition_hash"], spec["time_window"]["account_currency"]),
                                 (contract.contract_hash, "USD"))
                resolved, binding = self._resolve(spec)
                self.assertIs(resolved, contract)
                self.assertEqual(binding, BINDING)

    def test_a_generation_that_is_not_a_scale_contract_resolves_to_none(self):
        spec = generation_spec(CONTRACTS["daily"], BINDING)
        spec["time_window"] = {"granularity": "daily", "lookback_days": 14}
        self.assertIsNone(self._resolve(spec))

    def test_any_drift_fails_closed(self):
        base = generation_spec(CONTRACTS["exact_window"], BINDING)
        drifts = {
            "a dropped field": {"fields": [f for f in base["fields"] if f != "reach"]},
            "another attribution": {"attribution_window": {**base["attribution_window"],
                                                           "action_attribution_windows": ["28d_click"]}},
            "a breakdown": {"breakdowns": ["country"]},
            "another time zone": {"local_timezone": "Asia/Shanghai"},
            "another contract type": {"contract_type": "daily"},
            "a stale hash": {"time_window": {**base["time_window"], "definition_hash": "0" * 64}},
            "no currency": {"time_window": {**base["time_window"], "account_currency": ""}},
        }
        for name, change in drifts.items():
            with self.subTest(name), self.assertRaises(ScaleReadContractError) as raised:
                self._resolve({**base, **change})
            self.assertEqual(raised.exception.code, "meta_read_contract_mismatch")
        with self.assertRaises(ScaleReadContractError) as raised:
            self._resolve({**base, "time_window": {**base["time_window"], "version": 9}})
        self.assertEqual(raised.exception.code, "meta_read_contract_unknown")

    def test_a_sandbox_account_comes_from_its_scenario_or_the_defaults(self):
        self.assertEqual(sandbox_account_binding("act_x", None), AccountBinding("act_x", "USD", "UTC"))
        self.assertEqual(
            sandbox_account_binding("act_x", {"account": {"currency": "CAD", "timezone_name": "America/Toronto"}}),
            AccountBinding("act_x", "CAD", "America/Toronto"),
        )


class Purity(unittest.TestCase):
    """Scale consumes these modules; they must not reach an adapter (ADR 0017)."""

    MODULES = ("app/meta/scale_read_contracts.py", "app/meta/scale_read_parsers.py")

    def test_the_contract_and_parser_modules_import_only_the_standard_library(self):
        for relative in self.MODULES:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    self.assertEqual(node.module, "scale_read_contracts", f"{relative} imports .{node.module}")
                elif isinstance(node, ast.ImportFrom):
                    self.assertIn(node.module.split(".")[0],
                                  {"__future__", "hashlib", "json", "dataclasses", "datetime", "decimal", "typing"},
                                  f"{relative} imports {node.module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name, {"hashlib", "json"}, f"{relative} imports {alias.name}")

    def test_importing_them_loads_no_meta_adapter(self):
        probe = (
            "import sys\n"
            "import app.meta.scale_read_contracts, app.meta.scale_read_parsers\n"
            "print(','.join(sorted(m for m in sys.modules if m.startswith(('app.adapters', 'sqlalchemy'))"
            " or m == 'app.meta.credential_resolution')))\n"
        )
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
                                cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
