import unittest

from app.scale.models import (
    ScaleEvidenceSnapshotRecord,
    ScaleEvidenceSourceLinkRecord,
    ScaleSnapshotQualityIssueRecord,
    ScaleSnapshotBreakdownSegmentRecord,
    ScaleSnapshotBreakdownDailyRecord,
    ScaleEvaluationRunRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationSignalRecord,
    ScaleEvaluationAdviceCandidateRecord,
    ScaleEvaluationStreakCounterRecord,
    ScaleEvaluationStreakResetRecord,
    ScaleEpisodeProjectionRecord,
)
from app.adapters.persistence.database import Base
from app.adapters.persistence.models import new_id


class TestScaleSnapshotAndEvaluationRecords(unittest.TestCase):
    def test_evidence_snapshot_table_name(self):
        self.assertEqual(
            ScaleEvidenceSnapshotRecord.__tablename__, "scale_evidence_snapshots",
        )

    def test_evidence_source_link_table_name(self):
        self.assertEqual(
            ScaleEvidenceSourceLinkRecord.__tablename__, "scale_evidence_source_links",
        )

    def test_snapshot_quality_issue_table_name(self):
        self.assertEqual(
            ScaleSnapshotQualityIssueRecord.__tablename__, "scale_snapshot_quality_issues",
        )

    def test_snapshot_breakdown_segment_table_name(self):
        self.assertEqual(
            ScaleSnapshotBreakdownSegmentRecord.__tablename__,
            "scale_snapshot_breakdown_segments",
        )

    def test_snapshot_breakdown_daily_table_name(self):
        self.assertEqual(
            ScaleSnapshotBreakdownDailyRecord.__tablename__, "scale_snapshot_breakdown_daily",
        )

    def test_evaluation_run_table_name(self):
        self.assertEqual(ScaleEvaluationRunRecord.__tablename__, "scale_evaluation_runs")

    def test_evaluation_table_name(self):
        self.assertEqual(ScaleEvaluationRecord.__tablename__, "scale_evaluations")

    def test_evaluation_signal_table_name(self):
        self.assertEqual(
            ScaleEvaluationSignalRecord.__tablename__, "scale_evaluation_signals",
        )

    def test_evaluation_advice_candidate_table_name(self):
        self.assertEqual(
            ScaleEvaluationAdviceCandidateRecord.__tablename__,
            "scale_evaluation_advice_candidates",
        )

    def test_evaluation_streak_counter_table_name(self):
        self.assertEqual(
            ScaleEvaluationStreakCounterRecord.__tablename__,
            "scale_evaluation_streak_counters",
        )

    def test_evaluation_streak_reset_table_name(self):
        self.assertEqual(
            ScaleEvaluationStreakResetRecord.__tablename__, "scale_evaluation_streak_resets",
        )

    def test_episode_projection_table_name(self):
        self.assertEqual(
            ScaleEpisodeProjectionRecord.__tablename__, "scale_episode_projections",
        )

    def test_evidence_snapshot_id_prefix(self):
        self.assertTrue(new_id("sesn").startswith("sesn_"))

    def test_evidence_source_link_id_prefix(self):
        self.assertTrue(new_id("sesl").startswith("sesl_"))

    def test_snapshot_quality_issue_id_prefix(self):
        self.assertTrue(new_id("snqi").startswith("snqi_"))

    def test_snapshot_breakdown_segment_id_prefix(self):
        self.assertTrue(new_id("ssbs").startswith("ssbs_"))

    def test_snapshot_breakdown_daily_id_prefix(self):
        self.assertTrue(new_id("ssbd").startswith("ssbd_"))

    def test_evaluation_run_id_prefix(self):
        self.assertTrue(new_id("sevr").startswith("sevr_"))

    def test_evaluation_id_prefix(self):
        self.assertTrue(new_id("sev").startswith("sev_"))

    def test_evaluation_signal_id_prefix(self):
        self.assertTrue(new_id("sevs").startswith("sevs_"))

    def test_evaluation_advice_candidate_id_prefix(self):
        self.assertTrue(new_id("seac").startswith("seac_"))

    def test_evaluation_streak_counter_id_prefix(self):
        self.assertTrue(new_id("sesc").startswith("sesc_"))

    def test_evaluation_streak_reset_id_prefix(self):
        self.assertTrue(new_id("sesr").startswith("sesr_"))

    def test_episode_projection_id_prefix(self):
        self.assertTrue(new_id("sepp").startswith("sepp_"))

    def test_all_constraint_names_fit_postgres_63_char_limit(self):
        """Guards the naming-convention gotcha: SQLAlchemy re-runs an
        explicit CheckConstraint `name=` through the metadata naming
        convention (`ck_%(table_name)s_%(constraint_name)s`), so the final
        identifier is longer than the literal string passed in the model.
        Every constraint on every table this task added must still resolve
        under PostgreSQL's 63-byte identifier limit."""
        new_table_names = {
            ScaleEvidenceSnapshotRecord.__tablename__,
            ScaleEvidenceSourceLinkRecord.__tablename__,
            ScaleSnapshotQualityIssueRecord.__tablename__,
            ScaleSnapshotBreakdownSegmentRecord.__tablename__,
            ScaleSnapshotBreakdownDailyRecord.__tablename__,
            ScaleEvaluationRunRecord.__tablename__,
            ScaleEvaluationRecord.__tablename__,
            ScaleEvaluationSignalRecord.__tablename__,
            ScaleEvaluationAdviceCandidateRecord.__tablename__,
            ScaleEvaluationStreakCounterRecord.__tablename__,
            ScaleEvaluationStreakResetRecord.__tablename__,
            ScaleEpisodeProjectionRecord.__tablename__,
        }
        offenders = []
        for table_name, table in Base.metadata.tables.items():
            if table_name not in new_table_names:
                continue
            for constraint in table.constraints:
                name = getattr(constraint, "name", None)
                if name and len(name) > 63:
                    offenders.append((table_name, type(constraint).__name__, name, len(name)))
        self.assertEqual(offenders, [], f"constraint name(s) exceed 63 chars: {offenders}")


if __name__ == "__main__":
    unittest.main()
