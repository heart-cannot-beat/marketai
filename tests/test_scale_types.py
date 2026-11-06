import unittest


class TestScaleCanonicalEnums(unittest.TestCase):
    def test_meta_mode_canonical_values(self):
        from app.scale.types import MetaMode
        self.assertEqual(MetaMode.CUSTOMER_SANDBOX.value, "customer_sandbox")
        self.assertEqual(MetaMode.CONTROLLED_MOCK.value, "controlled_mock")
        self.assertEqual(MetaMode.INTERNAL_VALIDATION.value, "internal_validation")
        self.assertEqual(len(MetaMode), 3)

    def test_meta_mode_rejects_aliases(self):
        from app.scale.types import MetaMode
        with self.assertRaises(ValueError):
            MetaMode("sandbox")
        with self.assertRaises(ValueError):
            MetaMode("mock")
        with self.assertRaises(ValueError):
            MetaMode("real_read_only")

    def test_evidence_maturity_canonical_values(self):
        from app.scale.types import EvidenceMaturity
        self.assertEqual(EvidenceMaturity.WARMING.value, "warming")
        self.assertEqual(EvidenceMaturity.MONITORABLE.value, "monitorable")
        self.assertEqual(EvidenceMaturity.ECONOMICS_READY.value, "economics_ready")
        self.assertEqual(EvidenceMaturity.INSUFFICIENT.value, "insufficient")
        self.assertEqual(len(EvidenceMaturity), 4)

    def test_evidence_maturity_rejects_aliases(self):
        from app.scale.types import EvidenceMaturity
        with self.assertRaises(ValueError):
            EvidenceMaturity("insufficient_evidence")

    def test_enrollment_lifecycle_values(self):
        from app.scale.types import EnrollmentLifecycle
        values = {e.value for e in EnrollmentLifecycle}
        self.assertEqual(values, {"draft", "active", "paused", "closed"})

    def test_episode_phase_values(self):
        from app.scale.types import EpisodePhase
        values = {e.value for e in EpisodePhase}
        self.assertEqual(values, {"collecting", "evaluating", "closed"})

    def test_scale_health_values(self):
        from app.scale.types import ScaleHealth
        expected = {"healthy", "fatigue_watch", "creative_fatigue",
                    "economics_decline", "delivery_anomaly", "not_delivering"}
        self.assertEqual({h.value for h in ScaleHealth}, expected)

    def test_advice_type_values(self):
        from app.scale.types import AdviceType
        expected = {"refresh_creative", "review_offer_or_landing_page",
                    "review_delivery_mix", "investigate_delivery_anomaly",
                    "consider_scale_test"}
        self.assertEqual({a.value for a in AdviceType}, expected)

    def test_signal_state_four_values(self):
        from app.scale.types import SignalState
        self.assertEqual(len(SignalState), 4)
        values = {s.value for s in SignalState}
        self.assertEqual(values, {"hit", "miss", "unavailable", "blocked"})

    def test_all_enums_are_str_enum(self):
        from app.scale import types
        import enum
        for name in dir(types):
            obj = getattr(types, name)
            if isinstance(obj, type) and issubclass(obj, enum.Enum) and obj is not enum.Enum:
                self.assertTrue(issubclass(obj, str),
                                f"{name} must be str+Enum for DB/wire compatibility")
