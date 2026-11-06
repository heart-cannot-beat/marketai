import unittest
from app.scale.models import (
    ScaleEnrollmentRecord,
    ScaleEnrollmentEventRecord,
    ScaleEpisodeRecord,
    ScaleEpisodeEventRecord,
    ScalePolicyVersionRecord,
    ScaleTargetVersionRecord,
    ScaleTargetLifecycleEventRecord,
    ScaleMetricSemanticContractSetRecord,
    ScaleMetricSemanticContractSetMemberRecord,
)


class TestScaleEnrollmentRecords(unittest.TestCase):
    def test_enrollment_table_name(self):
        self.assertEqual(ScaleEnrollmentRecord.__tablename__, "scale_enrollments")

    def test_enrollment_event_table_name(self):
        self.assertEqual(ScaleEnrollmentEventRecord.__tablename__, "scale_enrollment_events")

    def test_episode_table_name(self):
        self.assertEqual(ScaleEpisodeRecord.__tablename__, "scale_episodes")

    def test_episode_event_table_name(self):
        self.assertEqual(ScaleEpisodeEventRecord.__tablename__, "scale_episode_events")

    def test_policy_version_table_name(self):
        self.assertEqual(ScalePolicyVersionRecord.__tablename__, "scale_policy_versions")

    def test_target_version_table_name(self):
        self.assertEqual(ScaleTargetVersionRecord.__tablename__, "scale_target_versions")

    def test_enrollment_id_prefix(self):
        from app.adapters.persistence.models import new_id
        eid = new_id("senr")
        self.assertTrue(eid.startswith("senr_"))

    def test_episode_id_prefix(self):
        from app.adapters.persistence.models import new_id
        eid = new_id("sep")
        self.assertTrue(eid.startswith("sep_"))

    def test_episode_event_id_prefix(self):
        from app.adapters.persistence.models import new_id
        eid = new_id("sevt")
        self.assertTrue(eid.startswith("sevt_"))
