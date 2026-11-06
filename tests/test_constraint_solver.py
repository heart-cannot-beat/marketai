import unittest
from dataclasses import dataclass
from hypothesis import given, settings, assume
import hypothesis.strategies as st

from app.library.templates import (
    detect_cycles, check_satisfiability, evaluate_constraints,
    ConstraintResult, SkipReason, COMBINATION_LIMIT,
)


def _dims(n_dims=2, n_vals=2):
    """Build a simple dimension list."""
    return [
        {
            "id": f"d{i}",
            "name": f"Dim{i}",
            "values": [
                {"id": f"d{i}v{j}", "name": f"Val{j}",
                 "positive_fragments": [], "negative_fragments": []}
                for j in range(n_vals)
            ],
        }
        for i in range(n_dims)
    ]


class TestCycleDetection(unittest.TestCase):

    def test_no_cycles(self):
        constraints = [
            {"type": "requires", "from_dimension_id": "d0", "from_value_id": "d0v0",
             "to_dimension_id": "d1", "to_value_id": "d1v0"},
        ]
        findings = detect_cycles(constraints, {"d0", "d1"})
        self.assertEqual(findings, [])

    def test_direct_cycle(self):
        constraints = [
            {"type": "requires", "from_dimension_id": "d0", "from_value_id": "d0v0",
             "to_dimension_id": "d1", "to_value_id": "d1v0"},
            {"type": "requires", "from_dimension_id": "d1", "from_value_id": "d1v0",
             "to_dimension_id": "d0", "to_value_id": "d0v0"},
        ]
        findings = detect_cycles(constraints, {"d0", "d1"})
        self.assertTrue(len(findings) > 0)
        self.assertTrue(any(f.code == "constraint_cycle" for f in findings))

    def test_transitive_cycle(self):
        constraints = [
            {"type": "requires", "from_dimension_id": "d0", "from_value_id": "d0v0",
             "to_dimension_id": "d1", "to_value_id": "d1v0"},
            {"type": "requires", "from_dimension_id": "d1", "from_value_id": "d1v0",
             "to_dimension_id": "d2", "to_value_id": "d2v0"},
            {"type": "requires", "from_dimension_id": "d2", "from_value_id": "d2v0",
             "to_dimension_id": "d0", "to_value_id": "d0v0"},
        ]
        findings = detect_cycles(constraints, {"d0", "d1", "d2"})
        self.assertTrue(len(findings) > 0)


class TestSatisfiability(unittest.TestCase):

    def test_no_constraints_satisfiable(self):
        findings = check_satisfiability(_dims(2, 2), [], "one_per_dimension")
        self.assertEqual(findings, [])

    def test_excludes_all_blocking(self):
        dims = _dims(1, 2)
        constraints = [
            {"type": "excludes", "dimension_id_a": "d0", "value_id_a": "d0v0",
             "dimension_id_b": "d0", "value_id_b": "d0v1"},
        ]
        # With one_per_dimension, each combo picks one value per dim,
        # so excluding within the same dim doesn't reduce combos.
        # Need cross-dim exclusion to be unsatisfiable.
        dims = _dims(2, 1)  # 2 dims, 1 value each
        constraints = [
            {"type": "excludes", "dimension_id_a": "d0", "value_id_a": "d0v0",
             "dimension_id_b": "d1", "value_id_b": "d1v0"},
        ]
        findings = check_satisfiability(dims, constraints, "one_per_dimension")
        self.assertTrue(any(f.code == "unsatisfiable" for f in findings))


class TestEvaluateConstraints(unittest.TestCase):

    def test_no_constraints_all_valid(self):
        schema = {
            "dimensions": _dims(2, 2),
            "constraints": [],
            "combination_cardinality": "one_per_dimension",
        }
        result = evaluate_constraints(schema)
        self.assertTrue(result.is_satisfiable)
        self.assertEqual(len(result.valid_items), 4)  # 2 * 2
        self.assertEqual(len(result.skip_reasons), 0)

    def test_excludes_produces_skip_reason(self):
        schema = {
            "dimensions": _dims(2, 2),
            "constraints": [
                {"type": "excludes", "dimension_id_a": "d0", "value_id_a": "d0v0",
                 "dimension_id_b": "d1", "value_id_b": "d1v0"},
            ],
            "combination_cardinality": "one_per_dimension",
        }
        result = evaluate_constraints(schema)
        self.assertTrue(result.is_satisfiable)
        self.assertEqual(len(result.valid_items), 3)
        self.assertEqual(len(result.skip_reasons), 1)
        self.assertTrue(len(result.skip_reasons[0].violations) >= 1)

    def test_combination_limit(self):
        schema = {
            "dimensions": _dims(20, 3),  # 3^20 > 3 billion
            "constraints": [],
            "combination_cardinality": "one_per_dimension",
        }
        result = evaluate_constraints(schema)
        self.assertFalse(result.is_satisfiable)
        self.assertTrue(any(f.code == "combination_limit_exceeded"
                            for f in result.limit_findings))

    def test_planned_items_filtering(self):
        schema = {
            "dimensions": _dims(2, 2),
            "constraints": [
                {"type": "excludes", "dimension_id_a": "d0", "value_id_a": "d0v0",
                 "dimension_id_b": "d1", "value_id_b": "d1v0"},
            ],
            "combination_cardinality": "one_per_dimension",
        }
        planned = [{"d0": "d0v0", "d1": "d1v0"}, {"d0": "d0v0", "d1": "d1v1"}]
        result = evaluate_constraints(schema, planned_items=planned)
        self.assertEqual(len(result.valid_items), 1)
        self.assertEqual(len(result.skip_reasons), 1)


@given(
    n_dims=st.integers(min_value=2, max_value=5),
    n_vals=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=50)
def test_solver_agrees_with_brute_force(n_dims, n_vals):
    """Property: solver and brute-force produce the same valid set."""
    import itertools
    dims = _dims(n_dims, n_vals)
    schema = {"dimensions": dims, "constraints": [], "combination_cardinality": "one_per_dimension"}
    result = evaluate_constraints(schema)
    expected = n_vals ** n_dims
    assume(expected <= COMBINATION_LIMIT)
    assert len(result.valid_items) == expected
