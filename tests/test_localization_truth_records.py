"""Truth resolution ORM records for Localization (#24, spec Section 1.3)."""
from __future__ import annotations

import unittest

from app.adapters.persistence.models import (
    TruthPackageRecord,
    TruthPackageMarketRecord,
    TruthSourceAttemptRecord,
    TruthFactRecord,
    TruthDerivedFactRecord,
    TruthDerivedFactInputRecord,
    FactLibraryDependencyRecord,
    FactMarketPolicyDependencyRecord,
)


class TestTruthRecordTableNames(unittest.TestCase):
    def test_table_names(self):
        expected = {
            TruthPackageRecord: "truth_packages",
            TruthPackageMarketRecord: "truth_package_markets",
            TruthSourceAttemptRecord: "truth_source_attempts",
            TruthFactRecord: "truth_facts",
            TruthDerivedFactRecord: "truth_derived_facts",
            TruthDerivedFactInputRecord: "truth_derived_fact_inputs",
            FactLibraryDependencyRecord: "fact_library_dependencies",
            FactMarketPolicyDependencyRecord: "fact_market_policy_dependencies",
        }
        for cls, name in expected.items():
            self.assertEqual(cls.__tablename__, name, f"{cls.__name__}")
