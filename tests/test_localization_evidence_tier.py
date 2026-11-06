import unittest
from app.localization.evidence_tier import derive_evidence_tier, EvidenceTier


class TestEvidenceTierDerivation(unittest.TestCase):
    def test_all_production(self):
        tier = derive_evidence_tier(
            source_modes=["byok"], experiment_modes=["production"],
            truth_data_mode="real", provider_mode="byok",
        )
        self.assertEqual(tier, EvidenceTier.PRODUCTION)

    def test_one_sandbox_source(self):
        tier = derive_evidence_tier(
            source_modes=["byok", "sandbox"], experiment_modes=["production", "production"],
            truth_data_mode="real", provider_mode="byok",
        )
        self.assertEqual(tier, EvidenceTier.SANDBOX)

    def test_any_mock_is_mock(self):
        tier = derive_evidence_tier(
            source_modes=["byok"], experiment_modes=["mock"],
            truth_data_mode="real", provider_mode="byok",
        )
        self.assertEqual(tier, EvidenceTier.MOCK)

    def test_mock_truth_is_mock(self):
        tier = derive_evidence_tier(
            source_modes=["byok"], experiment_modes=["production"],
            truth_data_mode="mock", provider_mode="byok",
        )
        self.assertEqual(tier, EvidenceTier.MOCK)

    def test_sandbox_provider_is_sandbox(self):
        tier = derive_evidence_tier(
            source_modes=["byok"], experiment_modes=["production"],
            truth_data_mode="real", provider_mode="sandbox",
        )
        self.assertEqual(tier, EvidenceTier.SANDBOX)
