"""BrandLocalizationPolicy Library entry type (Issue #24 Task 4).

Covers registration of ENTRY_BRAND_LOCALIZATION_POLICY: entry type constant,
ENTRY_TYPES membership, Owner-only publish, content validation, and
dependency extraction dispatch.
"""
from __future__ import annotations

import unittest

from app.library.contracts import (
    CONTENT_SCHEMA_VERSIONS,
    ENTRY_BRAND_LOCALIZATION_POLICY,
    ENTRY_TYPES,
    OWNER_ONLY_PUBLISH,
    extract_dependencies,
    validate_content,
)


class TestBrandLocalizationPolicyType(unittest.TestCase):
    def test_constant_value(self):
        self.assertEqual(ENTRY_BRAND_LOCALIZATION_POLICY, "brand_localization_policy")

    def test_registered_in_entry_types(self):
        self.assertIn(ENTRY_BRAND_LOCALIZATION_POLICY, ENTRY_TYPES)

    def test_owner_only_publish(self):
        self.assertIn(ENTRY_BRAND_LOCALIZATION_POLICY, OWNER_ONLY_PUBLISH)

    def test_has_content_schema_version(self):
        self.assertIn(ENTRY_BRAND_LOCALIZATION_POLICY, CONTENT_SCHEMA_VERSIONS)


class TestBrandLocalizationPolicyValidation(unittest.TestCase):
    def test_missing_required_fields(self):
        report = validate_content(ENTRY_BRAND_LOCALIZATION_POLICY, {})
        codes = {f.code for f in report.blocking}
        self.assertIn("brand_policy.missing_claim_switches", codes)
        self.assertIn("brand_policy.missing_style_guide", codes)

    def test_claim_switches_must_be_object(self):
        report = validate_content(
            ENTRY_BRAND_LOCALIZATION_POLICY,
            {"claim_switches": ["not", "a", "dict"], "style_guide": {}},
        )
        codes = {f.code for f in report.blocking}
        self.assertIn("brand_policy.invalid_claim_switches", codes)

    def test_valid_content_passes(self):
        report = validate_content(
            ENTRY_BRAND_LOCALIZATION_POLICY,
            {
                "claim_switches": {"clinically_proven": {"replacement": "dermatologist tested"}},
                "style_guide": {"tone": "formal"},
            },
        )
        self.assertTrue(report.ok)


class TestBrandLocalizationPolicyDependencies(unittest.TestCase):
    def test_extract_dependencies_dispatched_without_error(self):
        edges = extract_dependencies(
            ENTRY_BRAND_LOCALIZATION_POLICY,
            {
                "claim_switches": {"clinically_proven": {"replacement": "dermatologist tested"}},
                "style_guide": {"tone": "formal"},
            },
        )
        self.assertEqual(edges, [])


if __name__ == "__main__":
    unittest.main()
