"""Unit tests for recovery preview Pydantic models."""
import unittest
from pydantic import ValidationError
from app.generation.response_models import (
    GenerationRecoveryPreview,
    GenerationPreviewDetail,
    GenerationResolutionPreview,
    MaximumCost,
    RecoveryPreviewInput,
    RecoveryPreviewSelection,
)


class TestRecoveryPreviewModel(unittest.TestCase):
    def test_recovery_preview_serialises_with_purpose(self):
        rp = GenerationRecoveryPreview(
            id="rpv_abc",
            preview_hash="deadbeef",
            command="retry_failed_items",
            targets=["ri_1"],
            estimated_additional_cost=MaximumCost(amount="0", currency="USD"),
            valid_until="2026-09-05T12:00:00Z",
        )
        d = rp.model_dump()
        self.assertEqual(d["purpose"], "run_recovery")
        self.assertEqual(d["estimated_additional_cost"]["amount"], "0")

    def test_initial_preview_serialises_with_purpose(self):
        ip = GenerationPreviewDetail(
            id="rpv_xyz", preview_hash="h", recipe_input_hash="r",
            execution_mode="mock", draft_id="d", draft_revision=1,
        )
        self.assertEqual(ip.model_dump()["purpose"], "initial_generation")

    def test_discriminated_union_round_trip(self):
        from pydantic import TypeAdapter
        ta = TypeAdapter(GenerationResolutionPreview)
        recovery_data = {
            "purpose": "run_recovery", "id": "rpv_1", "preview_hash": "h",
            "command": "retry_failed_items", "targets": [],
            "estimated_additional_cost": {"amount": "0", "currency": "USD"},
            "valid_until": "2026-09-05T12:00:00Z",
        }
        parsed = ta.validate_python(recovery_data)
        self.assertIsInstance(parsed, GenerationRecoveryPreview)

        initial_data = {
            "purpose": "initial_generation", "id": "rpv_2", "preview_hash": "h2",
            "recipe_input_hash": "r", "execution_mode": "mock",
            "draft_id": "d1", "draft_revision": 1,
        }
        parsed2 = ta.validate_python(initial_data)
        self.assertIsInstance(parsed2, GenerationPreviewDetail)

    def test_recovery_input_rejects_empty_explicit_item_ids(self):
        with self.assertRaises(ValidationError):
            RecoveryPreviewInput(
                command="retry_failed_items",
                selection=RecoveryPreviewSelection(kind="item_ids", item_ids=[]),
            )

    def test_recovery_input_accepts_all_eligible_failed(self):
        inp = RecoveryPreviewInput(
            command="retry_failed_items",
            selection=RecoveryPreviewSelection(kind="all_eligible_failed"),
        )
        self.assertEqual(inp.command, "retry_failed_items")
