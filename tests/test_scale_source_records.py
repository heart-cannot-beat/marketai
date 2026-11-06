import unittest

from app.scale.models import (
    ScaleDailyMetricSourceRecord,
    ScaleExactWindowSourceRecord,
    ScaleBreakdownSourceRecord,
    ScaleBreakdownDailyChildRecord,
    ScaleBreakdownPrivacyRecord,
    ScaleDeliveryConfigSourceRecord,
    ScaleEvidenceSourceManifestRecord,
    ScaleManifestItemRecord,
    ScaleSourceQualityIssueRecord,
    ScaleEvidenceReadinessProjectionRecord,
)
from app.adapters.persistence.models import new_id


class TestScaleSourceRecords(unittest.TestCase):
    def test_daily_metric_source_table_name(self):
        self.assertEqual(
            ScaleDailyMetricSourceRecord.__tablename__, "scale_daily_metric_sources",
        )

    def test_exact_window_source_table_name(self):
        self.assertEqual(
            ScaleExactWindowSourceRecord.__tablename__, "scale_exact_window_sources",
        )

    def test_breakdown_source_table_name(self):
        self.assertEqual(
            ScaleBreakdownSourceRecord.__tablename__, "scale_breakdown_sources",
        )

    def test_breakdown_daily_child_table_name(self):
        self.assertEqual(
            ScaleBreakdownDailyChildRecord.__tablename__,
            "scale_breakdown_daily_children",
        )

    def test_breakdown_privacy_table_name(self):
        self.assertEqual(
            ScaleBreakdownPrivacyRecord.__tablename__, "scale_breakdown_privacy_records",
        )

    def test_delivery_config_source_table_name(self):
        self.assertEqual(
            ScaleDeliveryConfigSourceRecord.__tablename__, "scale_delivery_config_sources",
        )

    def test_evidence_source_manifest_table_name(self):
        self.assertEqual(
            ScaleEvidenceSourceManifestRecord.__tablename__,
            "scale_evidence_source_manifests",
        )

    def test_manifest_item_table_name(self):
        self.assertEqual(ScaleManifestItemRecord.__tablename__, "scale_manifest_items")

    def test_source_quality_issue_table_name(self):
        self.assertEqual(
            ScaleSourceQualityIssueRecord.__tablename__, "scale_source_quality_issues",
        )

    def test_evidence_readiness_projection_table_name(self):
        self.assertEqual(
            ScaleEvidenceReadinessProjectionRecord.__tablename__,
            "scale_evidence_readiness_projections",
        )

    def test_daily_metric_source_id_prefix(self):
        self.assertTrue(new_id("sdms").startswith("sdms_"))

    def test_exact_window_source_id_prefix(self):
        self.assertTrue(new_id("sews").startswith("sews_"))

    def test_breakdown_source_id_prefix(self):
        self.assertTrue(new_id("sbds").startswith("sbds_"))

    def test_breakdown_daily_child_id_prefix(self):
        self.assertTrue(new_id("sbdc").startswith("sbdc_"))

    def test_breakdown_privacy_id_prefix(self):
        self.assertTrue(new_id("sbpr").startswith("sbpr_"))

    def test_delivery_config_source_id_prefix(self):
        self.assertTrue(new_id("sdcs").startswith("sdcs_"))

    def test_evidence_source_manifest_id_prefix(self):
        self.assertTrue(new_id("sesm").startswith("sesm_"))

    def test_manifest_item_id_prefix(self):
        self.assertTrue(new_id("smi").startswith("smi_"))

    def test_source_quality_issue_id_prefix(self):
        self.assertTrue(new_id("ssqi").startswith("ssqi_"))

    def test_evidence_readiness_projection_id_prefix(self):
        self.assertTrue(new_id("serp").startswith("serp_"))


if __name__ == "__main__":
    unittest.main()
