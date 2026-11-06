"""Unit tests for ``app.generation.planner`` (Issue #157).

Pure functions, no database: strategy normalisation (canonical and legacy
shapes), deterministic coverage sampling, template-constraint filtering and
exclusion-rule normalisation/write-back material.
"""
from __future__ import annotations

import unittest

from app.generation import planner
from app.generation.planner import (
    PlannerError,
    apply_template_constraints,
    coverage_sample,
    enumerate_full,
    normalize_exclusions,
    normalize_strategy,
    plan_combinations,
)


def _template_dimensions(entry_id: str = "lib_t") -> dict:
    """A 2x3x2x2 template materialised the way ``preview`` freezes it."""
    spec = {
        "a": ["a1", "a2"],
        "b": ["b1", "b2", "b3"],
        "c": ["c1", "c2"],
        "d": ["d1", "d2"],
    }
    return {
        f"{entry_id}:{dim}": [
            {
                "value": value,
                "concept_key_id": f"dck_{dim}_{value}",
                "template_entry_id": entry_id,
                "template_version_id": "libver_1",
                "dimension_id": dim,
                "value_id": value,
            }
            for value in values
        ]
        for dim, values in spec.items()
    }


class NormalizeStrategyTests(unittest.TestCase):
    def test_canonical_full_and_coverage(self):
        full = normalize_strategy({"kind": "full", "planner_version": "v1"})
        self.assertEqual((full.kind, full.cap, full.deprecated_shape), ("full", None, False))
        self.assertEqual(full.as_recipe(), {"kind": "full"})

        coverage = normalize_strategy({"kind": "coverage", "cap": 12})
        self.assertEqual((coverage.kind, coverage.cap), ("coverage", 12))
        self.assertFalse(coverage.deprecated_shape)
        self.assertEqual(coverage.as_recipe(), {"kind": "coverage", "cap": 12})

    def test_empty_strategy_defaults_to_full(self):
        self.assertEqual(normalize_strategy({}).kind, "full")
        self.assertEqual(normalize_strategy(None).kind, "full")

    def test_foreign_keys_are_carried_but_ignored(self):
        # Other modules stash markers here (regeneration batches, Scale
        # advice conversion); they must neither change nor break the plan.
        decision = normalize_strategy({
            "kind": "full",
            "origin": "scale_advice_refresh",
            "requested_variant_count": 2,
            "batch_draft_ids": ["gd_1"],
        })
        self.assertEqual(decision.as_recipe(), {"kind": "full"})
        self.assertEqual(decision.requested["origin"], "scale_advice_refresh")

    def test_legacy_shape_is_read_and_flagged(self):
        sampled = normalize_strategy({"mode": "sampled", "sample_cap": 7})
        self.assertEqual((sampled.kind, sampled.cap), ("coverage", 7))
        self.assertTrue(sampled.deprecated_shape)
        # The legacy shape never reaches the recipe.
        self.assertEqual(sampled.as_recipe(), {"kind": "coverage", "cap": 7})

        full = normalize_strategy({"mode": "full", "sample_cap": None})
        self.assertEqual((full.kind, full.cap), ("full", None))
        self.assertTrue(full.deprecated_shape)

    def test_unknown_kind_is_rejected_instead_of_collapsing(self):
        for raw in ({"kind": "sampled"}, {"kind": "random"}, {"mode": "weird"}):
            with self.subTest(raw=raw), self.assertRaises(PlannerError) as caught:
                normalize_strategy(raw)
            self.assertEqual(caught.exception.code, "combination_strategy_invalid")

    def test_coverage_requires_positive_integer_cap(self):
        for raw in (
            {"kind": "coverage"},
            {"kind": "coverage", "cap": 0},
            {"kind": "coverage", "cap": "12"},
            {"kind": "coverage", "cap": True},
            {"mode": "sampled", "sample_cap": None},
        ):
            with self.subTest(raw=raw), self.assertRaises(PlannerError) as caught:
                normalize_strategy(raw)
            self.assertEqual(caught.exception.code, "combination_strategy_invalid")


class EnumerateTests(unittest.TestCase):
    def test_full_product_is_sorted_and_fingerprinted(self):
        combos = enumerate_full({"tone": ["warm", "cool"], "angle": ["a"]})
        self.assertEqual(len(combos), 2)
        self.assertEqual(combos[0]["fingerprint"], "angle=a:tone=warm")
        self.assertEqual(combos[1]["fingerprint"], "angle=a:tone=cool")

    def test_no_dimensions_yields_default_combination(self):
        combos = enumerate_full({})
        self.assertEqual(len(combos), 1)
        self.assertEqual(combos[0]["fingerprint"], "default")

    def test_limit_is_enforced_before_expansion(self):
        huge = {f"d{i}": list(range(10)) for i in range(5)}  # 100_000 > 10_000
        with self.assertRaises(PlannerError) as caught:
            enumerate_full(huge)
        self.assertEqual(caught.exception.code, "combination_limit_exceeded")


class CoverageSampleTests(unittest.TestCase):
    def test_cap_12_on_2x3x2x2_picks_exactly_12_and_covers_every_value(self):
        combos = enumerate_full(_template_dimensions())
        self.assertEqual(len(combos), 24)

        picked = coverage_sample(combos, 12)
        self.assertEqual(len(picked), 12)
        covered = {cell for combo in picked for cell in combo["dimensions"].items()}
        every_value = {cell for combo in combos for cell in combo["dimensions"].items()}
        self.assertEqual(covered, every_value)  # 2 + 3 + 2 + 2 = 9 single values
        self.assertEqual(len({c["fingerprint"] for c in picked}), 12)

    def test_sampling_is_deterministic(self):
        combos_a = enumerate_full(_template_dimensions())
        combos_b = enumerate_full(_template_dimensions())
        first = [c["fingerprint"] for c in coverage_sample(combos_a, 12)]
        second = [c["fingerprint"] for c in coverage_sample(combos_b, 12)]
        self.assertEqual(first, second)

    def test_cap_at_or_above_total_keeps_everything(self):
        combos = enumerate_full({"a": ["1", "2"], "b": ["x", "y"]})
        self.assertEqual(len(coverage_sample(combos, 4)), 4)
        self.assertEqual(len(coverage_sample(combos, 99)), 4)

    def test_small_cap_still_prefers_uncovered_values(self):
        combos = enumerate_full({"a": ["1", "2"], "b": ["x", "y"]})
        picked = coverage_sample(combos, 2)
        covered = {cell for combo in picked for cell in combo["dimensions"].items()}
        # Two picks can cover all four single values: (1,x) then (2,y).
        self.assertEqual(len(covered), 4)


class TemplateConstraintTests(unittest.TestCase):
    def _constraints(self, *constraints: dict) -> list[dict]:
        return [{
            "template_entry_id": "lib_t",
            "template_version_id": "libver_1",
            "combination_cardinality": "one_per_dimension",
            "constraints": list(constraints),
        }]

    def test_excludes_drops_the_pair(self):
        combos = enumerate_full(_template_dimensions())
        dropped, kept = apply_template_constraints(
            combos,
            self._constraints({
                "type": "excludes",
                "dimension_id_a": "a", "value_id_a": "a1",
                "dimension_id_b": "b", "value_id_b": "b1",
            }),
        )
        self.assertEqual(len(dropped), 4)   # a1 x b1 x (2 c) x (2 d)
        self.assertEqual(len(kept), 20)
        self.assertTrue(all(c["exclusion_reason"] == "template_constraint" for c in dropped))
        self.assertTrue(all(c["constraint_violations"] for c in dropped))
        self.assertFalse(any(c["dimensions"]["lib_t:a"] == "a1" and c["dimensions"]["lib_t:b"] == "b1" for c in kept))

    def test_requires_drops_combinations_missing_the_target(self):
        combos = enumerate_full(_template_dimensions())
        dropped, kept = apply_template_constraints(
            combos,
            self._constraints({
                "type": "requires",
                "from_dimension_id": "a", "from_value_id": "a2",
                "to_dimension_id": "c", "to_value_id": "c1",
            }),
        )
        self.assertEqual(len(dropped), 6)   # a2 x (3 b) x c2 x (2 d)
        self.assertEqual(len(kept), 18)

    def test_max_occurrence_is_a_no_op_under_one_per_dimension(self):
        combos = enumerate_full(_template_dimensions())
        dropped, kept = apply_template_constraints(
            combos, self._constraints({"type": "max_occurrence", "dimension_id": "a", "max": 1}),
        )
        self.assertEqual(dropped, [])
        self.assertEqual(len(kept), 24)

    def test_constraints_only_apply_to_their_own_template(self):
        combos = enumerate_full({
            **_template_dimensions("lib_t"),
            "lib_other:a": [{
                "value": "a1", "template_entry_id": "lib_other", "dimension_id": "a", "value_id": "a1",
            }],
        })
        dropped, _kept = apply_template_constraints(
            combos,
            [{
                "template_entry_id": "lib_other",
                "constraints": [{
                    "type": "excludes",
                    "dimension_id_a": "a", "value_id_a": "a1",
                    "dimension_id_b": "b", "value_id_b": "b1",
                }],
            }],
        )
        # lib_other has no dimension "b", so its exclusion can never fire.
        self.assertEqual(dropped, [])

    def test_explicit_dimensions_carry_no_template_cells(self):
        combos = enumerate_full({"angle": ["a", "b"]})
        dropped, kept = apply_template_constraints(combos, self._constraints({"type": "excludes"}))
        self.assertEqual((len(dropped), len(kept)), (0, 2))


class ExclusionNormalisationTests(unittest.TestCase):
    DIMENSIONS = {"lib_t:a": [{"value": "a1"}, {"value": "a2"}], "tone": ["warm", "cool"]}

    def test_valid_rules_are_normalised(self):
        rules, invalid = normalize_exclusions(
            {"rules": [{"dimensions": {"lib_t:a": "a1", "tone": "warm"}, "reason": "clash"},
                       {"dimensions": {"tone": "cool"}}]},
            self.DIMENSIONS,
        )
        self.assertEqual(invalid, [])
        self.assertEqual(rules[0], {"dimensions": {"lib_t:a": "a1", "tone": "warm"}, "reason": "clash"})
        self.assertEqual(rules[1]["reason"], planner.DEFAULT_RULE_REASON)

    def test_invalid_rules_are_reported_with_codes(self):
        rules, invalid = normalize_exclusions(
            {
                "rules": [
                    "just a string",
                    {"reason": "no dimensions"},
                    {"dimensions": {"missing": "x"}},
                    {"dimensions": {"tone": "lukewarm"}},
                    {"dimensions": {"tone": {"nested": True}}},
                ],
                "manual": ["warm+a1"],
            },
            self.DIMENSIONS,
        )
        self.assertEqual(rules, [])
        self.assertEqual(
            [entry["code"] for entry in invalid],
            ["unparseable_rule", "unparseable_rule", "unknown_dimension", "unknown_value",
             "unparseable_rule", "legacy_manual_exclusion"],
        )
        self.assertEqual(invalid[2]["rule"], {"dimensions": {"missing": "x"}})
        self.assertEqual(invalid[-1]["rule"], "warm+a1")

    def test_rules_not_a_list_is_invalid(self):
        rules, invalid = normalize_exclusions({"rules": {"dimensions": {}}}, self.DIMENSIONS)
        self.assertEqual(rules, [])
        self.assertEqual(invalid[0]["code"], "unparseable_rule")


class PlanCombinationsTests(unittest.TestCase):
    def test_order_is_constraints_then_rules_then_sampling(self):
        selectors = {
            "dimensions": _template_dimensions(),
            "dimension_template_constraints": [{
                "template_entry_id": "lib_t",
                "constraints": [{
                    "type": "excludes",
                    "dimension_id_a": "a", "value_id_a": "a1",
                    "dimension_id_b": "b", "value_id_b": "b1",
                }],
            }],
        }
        plan = plan_combinations(
            selectors,
            {"kind": "coverage", "cap": 12},
            {"rules": [{"dimensions": {"lib_t:d": "d2"}, "reason": "no_d2"}]},
        )
        self.assertEqual(plan.full_count, 24)
        self.assertEqual(len(plan.constraint_dropped), 4)
        # 20 survivors, half of them d2 -> 10 rule-excluded, 10 candidates.
        self.assertEqual(len(plan.rule_excluded), 10)
        self.assertEqual(plan.candidate_count, 10)
        # Cap 12 > 10 candidates: sampling keeps all survivors, never the
        # excluded ones.
        self.assertEqual(len(plan.selected), 10)
        self.assertTrue(all(c["dimensions"]["lib_t:d"] == "d1" for c in plan.selected))
        self.assertEqual(
            plan.strategy_applied(),
            {
                "kind": "coverage", "cap": 12, "deprecated_shape": False,
                "full_count": 24, "candidate_count": 10, "selected_count": 10,
            },
        )
        # Planner internals never leak into the frozen combinations.
        self.assertTrue(all("cells" not in c for c in plan.selected + plan.constraint_dropped))

    def test_full_strategy_keeps_every_candidate(self):
        plan = plan_combinations({"dimensions": _template_dimensions()}, {"kind": "full"}, {})
        self.assertEqual(len(plan.selected), 24)
        self.assertEqual(plan.strategy_applied()["selected_count"], 24)


if __name__ == "__main__":
    unittest.main()
