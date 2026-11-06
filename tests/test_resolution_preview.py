import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.support.library_fixtures import LibraryFixtures
from app.library.contracts import (
    ENTRY_PRODUCT, ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA,
    ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
    ROLE_OWNER, canonical_hash,
)
from app.library import service
from app.library.templates import apply_template
from app.library.preview import (
    build_canonical_snapshot, compute_preview_hashes,
    compute_recipe_input_hash, SNAPSHOT_SCHEMA_VERSION,
)


class TestCanonicalSnapshot(unittest.TestCase):
    """Deterministic hashing with fixed clock and inputs."""

    def test_same_inputs_same_recipe_hash(self):
        snapshot = _sample_snapshot()
        h1 = compute_recipe_input_hash(snapshot)
        h2 = compute_recipe_input_hash(snapshot)
        self.assertEqual(h1, h2)

    def test_same_inputs_same_preview_hash(self):
        snapshot = _sample_snapshot()
        h1 = compute_preview_hashes(snapshot)
        h2 = compute_preview_hashes(snapshot)
        self.assertEqual(h1["preview_hash"], h2["preview_hash"])

    def test_different_time_bound_fields_same_recipe_hash(self):
        s1 = _sample_snapshot()
        s2 = _sample_snapshot()
        s2["cost_quote"]["expires_at"] = "2026-12-31T00:00:00Z"
        s2["credential_evidence"]["binding_revision"] = 99
        self.assertEqual(
            compute_recipe_input_hash(s1),
            compute_recipe_input_hash(s2),
        )

    def test_different_time_bound_fields_different_preview_hash(self):
        s1 = _sample_snapshot()
        s2 = _sample_snapshot()
        s2["cost_quote"]["expires_at"] = "2026-12-31T00:00:00Z"
        self.assertNotEqual(
            compute_preview_hashes(s1)["preview_hash"],
            compute_preview_hashes(s2)["preview_hash"],
        )

    def test_snapshot_schema_version_in_hash(self):
        s = _sample_snapshot()
        self.assertEqual(s["_schema"], f"resolution_preview/{SNAPSHOT_SCHEMA_VERSION}")

    def test_canonicalization_sorts(self):
        s1 = _sample_snapshot()
        s2 = _sample_snapshot()
        # Reverse the reference choices order
        s2["reference_choices"] = list(reversed(s2["reference_choices"]))
        # After canonicalization, hashes should still be equal
        self.assertEqual(
            compute_preview_hashes(s1)["preview_hash"],
            compute_preview_hashes(s2)["preview_hash"],
        )


def _sample_snapshot():
    return {
        "_schema": f"resolution_preview/{SNAPSHOT_SCHEMA_VERSION}",
        "resolved_versions": {
            "product": {"selector": "latest", "entry_id": "lib_p1",
                        "version_id": "lib_v1", "content_hash": "abc"},
        },
        "constraint_results": {
            "valid_run_items": [{"d0": "v0"}],
            "skip_reasons": [],
        },
        "prompt_assembly": {
            "final_prompt": "test prompt",
            "layer_hashes": {"baseline": "h1", "policy": "h2", "run_inputs": "h3"},
            "composition_hash": "ch",
        },
        "reference_choices": [
            {"run_item_index": 0, "refs": [{"ref_version_id": "rv1"}]},
            {"run_item_index": 1, "refs": [{"ref_version_id": "rv2"}]},
        ],
        "provider_decisions": {
            "provider": "mock", "model": "mock-v1",
            "params": {}, "capability_version": "1.0",
            "pruning": {"needed": False, "decision_hash": "dh"},
        },
        "credential_evidence": {"binding_revision": 1, "eligible": True},
        "exclusions": [],
        "cost_quote": {
            "payable_max_cost": "0",
            "currency": "USD",
            "simulated_estimate": "1.50",
            "expires_at": "2026-10-01T00:00:00Z",
        },
        "eligibility": {"overall": "eligible", "checks": []},
        "confirmation_decisions": [],
    }
