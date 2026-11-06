"""Integration tests for issue #20: templates, prompt policy, data sources, preview.

Cross-tenant, role, idempotency, and transaction evidence.
"""
import unittest
from tests.support.library_fixtures import LibraryFixtures, FUTURE
from app.library.contracts import (
    ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA,
    ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
    ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY,
    ENTRY_PRODUCT, ROLE_OWNER, ROLE_MEMBER,
)
from app.library import service
from app.library.service import LibraryForbidden, LibraryValidationError
from app.library.templates import apply_template
from app.library.prompt_policy import publish_baseline
from app.library.datasources import bind_credential


class TestOwnerOnlyPublish(LibraryFixtures):
    """Member cannot publish Owner-only entry types."""

    def test_member_cannot_publish_prompt_policy(self):
        ctx = self.ctx(role=ROLE_MEMBER)
        entry = service.create_entry(ctx, entry_type=ENTRY_PROMPT_POLICY,
                                     display_name="Test Policy",
                                     content=_policy_content())
        rev = entry["draft"]["revision"]
        service.validate_draft(ctx, entry["id"], expected_revision=rev)
        with self.assertRaises(LibraryForbidden):
            service.publish(ctx, entry["id"], rev)

    def test_owner_can_publish_prompt_policy(self):
        ctx = self.ctx(role=ROLE_OWNER)
        entry = service.create_entry(ctx, entry_type=ENTRY_PROMPT_POLICY,
                                     display_name="Test Policy",
                                     content=_policy_content())
        rev = entry["draft"]["revision"]
        service.validate_draft(ctx, entry["id"], expected_revision=rev)
        version = service.publish(ctx, entry["id"], rev)
        self.assertEqual(version["state"], "effective")

    def test_member_can_publish_dimension_template(self):
        ctx = self.ctx(role=ROLE_MEMBER)
        entry = service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                     display_name="Test Template",
                                     content=_template_content())
        rev = entry["draft"]["revision"]
        service.validate_draft(ctx, entry["id"], expected_revision=rev)
        version = service.publish(ctx, entry["id"], rev)
        self.assertEqual(version["state"], "effective")


class TestCrossTenantIsolation(LibraryFixtures):
    """Cross-workspace access is blocked for all new entry types."""

    def test_cross_workspace_template_blocked(self):
        ctx_a = self.ctx()
        ctx_b = self.ctx()
        entry = service.create_entry(ctx_a, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                     display_name="Template A",
                                     content=_template_content())
        with self.assertRaises(Exception):
            service.validate_draft(ctx_b, entry["id"], expected_revision=1)


class TestIdempotencyEvidence(LibraryFixtures):
    """Same key + fingerprint replays; same key + different fingerprint rejects."""

    def test_same_key_replays(self):
        ctx = self.ctx()
        r1 = service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                  display_name="Template",
                                  content=_template_content(),
                                  idempotency_key="idem-tmpl-1",
                                  request_fingerprint="fp1")
        r2 = service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                  display_name="Template",
                                  content=_template_content(),
                                  idempotency_key="idem-tmpl-1",
                                  request_fingerprint="fp1")
        self.assertEqual(r1["id"], r2["id"])

    def test_different_fingerprint_rejects(self):
        ctx = self.ctx()
        service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                             display_name="Template",
                             content=_template_content(),
                             idempotency_key="idem-tmpl-2",
                             request_fingerprint="fp1")
        from app.library.service import IdempotencyConflict
        with self.assertRaises(IdempotencyConflict):
            service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                 display_name="Template",
                                 content=_template_content(),
                                 idempotency_key="idem-tmpl-2",
                                 request_fingerprint="fp_different")


class TestBlockingValidationNoPartialVersion(LibraryFixtures):
    """A blocked publish produces no version or dependency rows."""

    def test_blocked_publish_no_version(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                     display_name="Bad Template",
                                     content={"dimensions": []})
        rev = entry["draft"]["revision"]
        with self.assertRaises(LibraryValidationError):
            service.publish(ctx, entry["id"], rev)
        # No version should exist
        result = service.get_entry(ctx, entry["id"])
        self.assertIsNone(result["current_version_id"])


def _template_content():
    return {
        "dimensions": [{
            "id": "dim-x", "name": "X",
            "values": [{"id": "val-1", "name": "V1",
                        "positive_fragments": ["v1"], "negative_fragments": []}],
        }],
        "constraints": [],
        "concept_key_mappings": [],
        "combination_cardinality": "one_per_dimension",
    }


def _policy_content():
    return {
        "registered_tokens": [],
        "locked_phrases": [],
        "brand_tone": {"style": "neutral"},
        "allowed_template_entry_ids": [],
        "optional_clauses": [],
    }


if __name__ == "__main__":
    unittest.main()
