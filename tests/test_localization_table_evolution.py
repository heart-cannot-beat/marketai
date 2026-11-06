"""Tests for existing-table evolution: asset_versions and provider_call_attempts.

Spec: docs/superpowers/specs/2026-09-06-localization-truth-bounded-qc-design.md
§1.11 Existing table modifications (Issue #24, Task 3).
"""
import unittest

from app.adapters.persistence.models import AssetVersionRecord


def _check_constraint(model, logical_name):
    """Find a CHECK constraint by its logical name, stripping SQLAlchemy's
    naming-convention-applied ``ck_<tablename>_`` prefix (see
    app/adapters/persistence/database.py NAMING_CONVENTION and the existing
    ``_check_constraint_names`` helper in tests/test_cost_usage_models.py).
    """
    prefix = f"ck_{model.__tablename__}_"
    for c in model.__table__.constraints:
        if c.__class__.__name__ != "CheckConstraint" or not c.name:
            continue
        name = c.name
        if name.startswith(prefix):
            name = name[len(prefix):]
        if name == logical_name:
            return c
    return None


def _named_constraint(table_args, name):
    """Find a non-CHECK constraint (Unique/ForeignKey) by its exact given
    name. Those constraint types are not rewritten by the naming convention
    because their templates don't reference %(constraint_name)s.
    """
    for c in table_args:
        if hasattr(c, "name") and c.name == name:
            return c
    return None


class TestAssetVersionEvolution(unittest.TestCase):
    def test_origin_kind_check_includes_localized(self):
        c = _check_constraint(AssetVersionRecord, "ck_asset_version_origin_kind")
        self.assertIsNotNone(c, "ck_asset_version_origin_kind not found")
        self.assertIn("localized", str(c.sqltext))

    def test_localized_requires_truth_parent_check_exists(self):
        c = _check_constraint(
            AssetVersionRecord, "ck_av_localized_requires_truth_parent"
        )
        self.assertIsNotNone(
            c, "ck_av_localized_requires_truth_parent not found"
        )
        text = str(c.sqltext)
        self.assertIn("truth_package_id", text)
        self.assertIn("parent_version_id", text)

    def test_workspace_truth_parent_unique_constraint_exists(self):
        c = _named_constraint(
            AssetVersionRecord.__table_args__, "uq_av_workspace_truth_parent"
        )
        self.assertIsNotNone(c, "uq_av_workspace_truth_parent not found")
        cols = {col.name for col in c.columns}
        self.assertEqual(
            cols,
            {"workspace_id", "id", "truth_package_id", "parent_version_id"},
        )

    def test_truth_package_fk_exists(self):
        found = False
        for c in AssetVersionRecord.__table_args__:
            if hasattr(c, "columns") and hasattr(c, "elements"):
                cols = [col.name for col in c.columns]
                if "truth_package_id" in cols:
                    self.assertEqual(
                        c.elements[cols.index("truth_package_id")].target_fullname,
                        "truth_packages.id",
                    )
                    found = True
        self.assertTrue(found, "no FK referencing truth_packages.id found")

    def test_truth_package_id_column_still_present(self):
        self.assertTrue(hasattr(AssetVersionRecord, "truth_package_id"))


class TestProviderCallAttemptEvolution(unittest.TestCase):
    def test_step_kind_check_includes_localization_values(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord

        c = _check_constraint(
            ProviderCallAttemptRecord, "ck_provider_call_attempt_step_kind"
        )
        self.assertIsNotNone(c, "ck_provider_call_attempt_step_kind not found")
        text = str(c.sqltext)
        for val in (
            "generation",
            "visual_qc",
            "source_analysis",
            "translation",
            "localization_edit",
            "localization_ocr",
            "localization_visual_qc",
        ):
            self.assertIn(val, text)

    def test_step_kind_column_width_40(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord

        col = ProviderCallAttemptRecord.__table__.columns["step_kind"]
        self.assertEqual(col.type.length, 40)

    def test_chargeability_column_exists(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord

        self.assertTrue(hasattr(ProviderCallAttemptRecord, "chargeability"))
        col = ProviderCallAttemptRecord.__table__.columns["chargeability"]
        self.assertFalse(col.nullable)
        self.assertEqual(col.type.length, 24)

    def test_truth_package_id_column_exists(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord

        self.assertTrue(hasattr(ProviderCallAttemptRecord, "truth_package_id"))
        col = ProviderCallAttemptRecord.__table__.columns["truth_package_id"]
        self.assertTrue(col.nullable)

    def test_spend_reservation_id_nullable(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord

        col = ProviderCallAttemptRecord.__table__.columns["spend_reservation_id"]
        self.assertTrue(col.nullable)

    def test_chargeability_enum_check_exists(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord

        c = _check_constraint(ProviderCallAttemptRecord, "chargeability")
        self.assertIsNotNone(c, "chargeability check not found")
        text = str(c.sqltext)
        for val in ("charged", "free_mock", "free_sandbox", "legacy_reserved"):
            self.assertIn(val, text)

    def test_chargeability_consistency_check_exists(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord

        c = _check_constraint(
            ProviderCallAttemptRecord, "chargeability_consistency"
        )
        self.assertIsNotNone(c, "chargeability_consistency check not found")
        text = str(c.sqltext)
        self.assertIn("legacy_reserved", text)
        self.assertIn("byok", text)

    def test_truth_required_check_exists(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord

        c = _check_constraint(ProviderCallAttemptRecord, "truth_required")
        self.assertIsNotNone(c, "truth_required check not found")
        text = str(c.sqltext)
        self.assertIn("truth_package_id", text)
        self.assertIn("source_analysis", text)

    def test_alternate_key_unique_constraint_exists(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord

        c = _named_constraint(
            ProviderCallAttemptRecord.__table_args__,
            "uq_provider_call_attempt_alt_key",
        )
        self.assertIsNotNone(c, "uq_provider_call_attempt_alt_key not found")
        cols = {col.name for col in c.columns}
        self.assertEqual(
            cols,
            {
                "workspace_id",
                "id",
                "operation_id",
                "truth_package_id",
                "step_kind",
                "execution_mode",
            },
        )

    def test_truth_package_fk_exists(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord

        found = False
        for c in ProviderCallAttemptRecord.__table_args__:
            if hasattr(c, "columns") and hasattr(c, "elements"):
                cols = [col.name for col in c.columns]
                if "truth_package_id" in cols:
                    self.assertEqual(
                        c.elements[cols.index("truth_package_id")].target_fullname,
                        "truth_packages.id",
                    )
                    found = True
        self.assertTrue(found, "no FK referencing truth_packages.id found")


if __name__ == "__main__":
    unittest.main()
