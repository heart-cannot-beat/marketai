"""Batch, claim, and copy ORM records for Localization (#24, spec §1.4-§1.6)."""
from __future__ import annotations

import unittest

from app.adapters.persistence.models import (
    SourceClaimInventoryRecord,
    SourceClaimConfirmationRecord,
    LocalizationClaimDecisionSetRecord,
    LocalizationClaimDecisionRecord,
    LocalizationBatchRecord,
    LocalizationBatchSourceRecord,
    LocalizationBatchCommandRecord,
    LocalizationBatchMarketTargetRecord,
    LocalizedCopyVersionRecord,
    LocalizedCopyApprovalRecord,
    LocalizationItemRecord,
    LocalizationAttemptRecord,
    LocalizationAttemptOutputRecord,
    LocalizationOutputPublicationRecord,
    LocalizationQcRecordRecord,
    LocalizationItemProviderCallRecord,
    LocalizationPreparationProviderCallRecord,
    LocalizationSemanticClaimRecord,
    MarketAssetBindingRecord,
    MarketAssetBindingEventRecord,
    LocalizedAssetLineageRecord,
)


class TestDomainRecordTableNames(unittest.TestCase):
    def test_table_names(self):
        expected = {
            SourceClaimInventoryRecord: "source_claim_inventories",
            SourceClaimConfirmationRecord: "source_claim_confirmations",
            LocalizationClaimDecisionSetRecord: "localization_claim_decision_sets",
            LocalizationClaimDecisionRecord: "localization_claim_decisions",
            LocalizationBatchRecord: "localization_batches",
            LocalizationBatchSourceRecord: "localization_batch_sources",
            LocalizationBatchCommandRecord: "localization_batch_commands",
            LocalizationBatchMarketTargetRecord: "localization_batch_market_targets",
            LocalizedCopyVersionRecord: "localized_copy_versions",
            LocalizedCopyApprovalRecord: "localized_copy_approvals",
        }
        for cls, name in expected.items():
            self.assertEqual(cls.__tablename__, name, f"{cls.__name__}")


class TestItemAttemptBindingRecordTableNames(unittest.TestCase):
    def test_table_names(self):
        expected = {
            LocalizationItemRecord: "localization_items",
            LocalizationAttemptRecord: "localization_attempts",
            LocalizationAttemptOutputRecord: "localization_attempt_outputs",
            LocalizationOutputPublicationRecord: "localization_output_publications",
            LocalizationQcRecordRecord: "localization_qc_records",
            LocalizationItemProviderCallRecord: "localization_item_provider_calls",
            LocalizationPreparationProviderCallRecord:
                "localization_preparation_provider_calls",
            LocalizationSemanticClaimRecord: "localization_semantic_claims",
            MarketAssetBindingRecord: "market_asset_bindings",
            MarketAssetBindingEventRecord: "market_asset_binding_events",
            LocalizedAssetLineageRecord: "localized_asset_lineages",
        }
        for cls, name in expected.items():
            self.assertEqual(cls.__tablename__, name, f"{cls.__name__}")


def _check_constraint(model, logical_name):
    """Find a CHECK constraint by its logical name (see
    tests/test_localization_table_evolution.py for the naming-convention
    rationale: an explicit CheckConstraint name is decorated as
    ``ck_<tablename>_<given name>`` by the shared NAMING_CONVENTION).
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
    for c in table_args:
        if hasattr(c, "name") and c.name == name:
            return c
    return None


class TestLocalizationItemRecordConstraints(unittest.TestCase):
    def test_state_check_has_nine_values(self):
        c = _check_constraint(LocalizationItemRecord, "ck_li_state")
        self.assertIsNotNone(c)
        text = str(c.sqltext)
        for value in (
            "queued", "running", "retry_wait", "paused", "succeeded",
            "failed", "skipped", "cancelled", "uncertain",
        ):
            self.assertIn(value, text)

    def test_six_unique_constraints_present(self):
        uniques = [
            c for c in LocalizationItemRecord.__table_args__
            if c.__class__.__name__ == "UniqueConstraint"
        ]
        self.assertEqual(len(uniques), 6)

    def test_lineage_target_unique_has_thirteen_columns(self):
        c = _named_constraint(
            LocalizationItemRecord.__table_args__, "uq_li_lineage_target"
        )
        self.assertIsNotNone(c)
        self.assertEqual(len(list(c.columns)), 13)


class TestLocalizationBatchCommandDeferredFk(unittest.TestCase):
    def test_item_fk_now_present(self):
        found = False
        for c in LocalizationBatchCommandRecord.__table_args__:
            if c.__class__.__name__ == "ForeignKeyConstraint":
                cols = [col.name for col in c.columns]
                if "item_id" in cols:
                    self.assertEqual(
                        c.elements[cols.index("item_id")].target_fullname,
                        "localization_items.id",
                    )
                    found = True
        self.assertTrue(found, "deferred item_id FK to localization_items not found")


class TestMarketAssetBindingPartialUniques(unittest.TestCase):
    def test_active_binding_partial_unique_index_exists(self):
        found = False
        for c in MarketAssetBindingRecord.__table__.indexes:
            cols = {col.name for col in c.columns}
            if c.unique and cols == {
                "workspace_id", "source_asset_version_id", "target_market",
                "offer_version_id",
            }:
                found = True
        self.assertTrue(found, "partial unique active-binding index not found")

    def test_publication_partial_unique_index_exists(self):
        found = False
        for c in MarketAssetBindingRecord.__table__.indexes:
            cols = {col.name for col in c.columns}
            if c.unique and cols == {"workspace_id", "publication_id"}:
                found = True
        self.assertTrue(found, "partial unique publication index not found")


class TestLineageCompositeFkChain(unittest.TestCase):
    def test_full_chain_fk_target_tables(self):
        expected_targets = {
            "localization_batches",
            "localization_batch_sources",
            "localization_items",
            "localization_attempts",
            "localization_attempt_outputs",
            "localization_output_publications",
            "truth_package_markets",
            "localization_claim_decision_sets",
            "localized_copy_versions",
            "asset_versions",
            "experiment_decisions",
            "truth_packages",
            "source_claim_inventories",
            "library_versions",
        }
        seen = set()
        for c in LocalizedAssetLineageRecord.__table_args__:
            if c.__class__.__name__ == "ForeignKeyConstraint":
                seen.add(c.elements[0].target_fullname.split(".")[0])
        self.assertEqual(seen, expected_targets)
