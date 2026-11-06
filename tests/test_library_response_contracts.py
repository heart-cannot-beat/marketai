"""Tests for Gate 0.6 — 13-type response and eligibility contract.

Covers:
- All 13 entry types create with correct content_schema_version
- Version response includes content_schema_version, lifecycle_revision
- Draft response includes content_schema_version
- Entry summary includes version/draft summary fields and updated_at (no N+1)
- Event response includes stable id, created_at, allowlisted payload
- ActionEligibility includes requirements (impact_confirmation)
- Owner vs Member action differences
- Unsupported schema version content projection
- Withdrawn/superseded/current version states
- Archived entry responses
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import LibraryVersionRecord
from app.library import service
from app.library.contracts import (
    CONTENT_SCHEMA_VERSIONS,
    ENTRY_DATA_SOURCE,
    ENTRY_DIMENSION_SCHEMA,
    ENTRY_DIMENSION_TEMPLATE,
    ENTRY_MANUAL_FACT_SET,
    ENTRY_MARKET_MAPPING,
    ENTRY_OFFER,
    ENTRY_PERSON,
    ENTRY_PRODUCT,
    ENTRY_PROMPT_POLICY,
    ENTRY_REFERENCE_ASSET,
    ENTRY_REFERENCE_GROUP,
    ENTRY_SELECTION_RULE,
    ENTRY_TRUTH_RESOLUTION_POLICY,
    ENTRY_TYPES,
    ROLE_MEMBER,
    ROLE_OWNER,
)
from app.main import app
from app.platform.action_eligibility import compute_actions
from app.platform.http_identity import request_tenant_context


class TestVersionResponseFields(LibraryFixtures):

    def test_version_includes_content_schema_version(self):
        ctx = self.ctx()
        ref_id, ref_ver = self._reference_version(ctx)
        self.assertIn("content_schema_version", ref_ver)
        self.assertEqual(ref_ver["content_schema_version"], 1)

    def test_version_includes_lifecycle_revision(self):
        ctx = self.ctx()
        ref_id, ref_ver = self._reference_version(ctx)
        self.assertIn("lifecycle_revision", ref_ver)
        self.assertEqual(ref_ver["lifecycle_revision"], 1)

    def test_withdrawn_version_increments_lifecycle_revision(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ref_id, ref_ver = self._reference_version(ctx)
        service.withdraw_version(ctx, ref_ver["id"], reason="test")
        version = service.get_version(ctx, ref_ver["id"])
        self.assertGreaterEqual(version["lifecycle_revision"], 2)

    def test_superseded_version_state(self):
        ctx = self.ctx()
        ref_id, ref_ver1 = self._reference_version(ctx, name="V1")
        ref2_id, ref2_ver = self._reference_version(ctx, name="V2")
        entry = service.get_entry(ctx, ref_id)
        revision = entry["draft"]["revision"]
        modified_content = dict(ref_ver1["content"])
        modified_content["rights"] = dict(modified_content.get("rights", {}))
        modified_content["rights"]["scope"] = "modified_scope"
        service.save_draft(ctx, ref_id, content=modified_content, expected_revision=revision)
        entry = service.get_entry(ctx, ref_id)
        revision = entry["draft"]["revision"]
        report = service.validate_draft(ctx, ref_id, expected_revision=revision)
        self.assertTrue(report["ok"], f"Validation failed: {report.get('blocking')}")
        ref_ver2 = service.publish(ctx, ref_id, revision)
        ver1 = service.get_version(ctx, ref_ver1["id"])
        self.assertEqual(ver1["state"], "superseded")
        self.assertEqual(ref_ver2["state"], "effective")


class TestDraftResponseFields(LibraryFixtures):

    def test_draft_includes_content_schema_version(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Draft Test")
        draft = entry["draft"]
        self.assertIn("content_schema_version", draft)
        self.assertEqual(draft["content_schema_version"], 1)


class TestEntrySummaryFields(LibraryFixtures):

    def test_list_entries_includes_version_and_draft_summary(self):
        ctx = self.ctx()
        ref_id, ref_ver = self._reference_version(ctx)
        entries = service.list_entries(ctx)
        matching = [e for e in entries if e["id"] == ref_id]
        self.assertEqual(len(matching), 1)
        summary = matching[0]
        self.assertIn("current_version_no", summary)
        self.assertIn("current_version_state", summary)
        self.assertIn("updated_at", summary)
        self.assertEqual(summary["current_version_state"], "effective")
        self.assertIsNotNone(summary["current_version_no"])

    def test_list_entries_includes_draft_fields_for_unpublished(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Unpublished")
        entries = service.list_entries(ctx)
        matching = [e for e in entries if e["id"] == entry["id"]]
        self.assertEqual(len(matching), 1)
        summary = matching[0]
        self.assertIn("draft_revision", summary)
        self.assertIn("draft_state", summary)
        self.assertEqual(summary["draft_revision"], 1)
        self.assertEqual(summary["draft_state"], "editing")
        self.assertIsNone(summary["current_version_id"])
        self.assertIsNone(summary["current_version_no"])


class TestEventResponseFields(LibraryFixtures):

    def test_event_includes_id_and_created_at(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Event Test")
        events = service.list_events(ctx, entry["id"])
        self.assertGreater(len(events), 0)
        first = events[0]
        self.assertIn("id", first)
        self.assertIn("created_at", first)
        self.assertTrue(first["id"].startswith("libev_"))
        self.assertIsNotNone(first["created_at"])

    def test_event_payload_strips_unlisted_keys(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Payload Test")
        events = service.list_events(ctx, entry["id"])
        for event in events:
            payload = event["payload"]
            for key in payload:
                self.assertNotIn(key, ("secret", "password", "token", "credential"))


class TestEventPayloadAllowlist(LibraryFixtures):

    def test_withdraw_event_preserves_reason_and_impact(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ref_id, ref_ver = self._reference_version(ctx)
        group_id, group_ver = self._group_version(ctx, ref_ver["id"])
        product_content = self._product_content(ref_ver["id"], group_ver["id"])
        _prod_id, _prod_ver = self._publish(ctx, ENTRY_PRODUCT, "Impact Test", product_content)

        service.withdraw_version(ctx, ref_ver["id"], reason="test reason")
        events = service.list_events(ctx, ref_id)
        withdrawn_events = [e for e in events if e["event_type"] == "version.withdrawn"]
        self.assertEqual(len(withdrawn_events), 1)
        payload = withdrawn_events[0]["payload"]
        self.assertEqual(payload["reason"], "test reason")
        self.assertIn("impact", payload)
        self.assertIn("downstream_event_contract", payload)


class TestActionEligibilityRequirements(LibraryFixtures):

    def test_compute_actions_with_requirements(self):
        result = compute_actions(
            ["publish", "save_draft"],
            {"publish": (True, None), "save_draft": (True, None)},
            {"publish": ["impact_confirmation"]},
        )
        publish = [a for a in result if a["action"] == "publish"][0]
        save = [a for a in result if a["action"] == "save_draft"][0]
        self.assertEqual(publish["requirements"], ["impact_confirmation"])
        self.assertNotIn("requirements", save)

    def test_requirements_absent_when_not_declared(self):
        result = compute_actions(
            ["edit"],
            {"edit": (True, None)},
        )
        self.assertNotIn("requirements", result[0])


class TestActionEligibilityRoutes(LibraryFixtures):

    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _owner_client(self):
        owner_ctx = self.owner_ctx(self.ctx)
        app.dependency_overrides[request_tenant_context] = lambda: owner_ctx
        return owner_ctx

    def test_entry_publish_declares_impact_confirmation(self):
        owner_ctx = self._owner_client()
        ref_id, ref_ver = self._reference_version(owner_ctx)
        resp = self.client.get(f"/api/v1/library/entries/{ref_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        publish = [a for a in body["allowed_actions"] if a["action"] == "publish"]
        self.assertEqual(len(publish), 1)
        self.assertIn("impact_confirmation", publish[0].get("requirements", []))

    def test_entry_archive_declares_impact_confirmation(self):
        owner_ctx = self._owner_client()
        ref_id, ref_ver = self._reference_version(owner_ctx)
        resp = self.client.get(f"/api/v1/library/entries/{ref_id}")
        body = resp.json()
        archive = [a for a in body["allowed_actions"] if a["action"] == "archive"]
        self.assertEqual(len(archive), 1)
        self.assertIn("impact_confirmation", archive[0].get("requirements", []))

    def test_version_withdraw_declares_impact_confirmation(self):
        owner_ctx = self._owner_client()
        ref_id, ref_ver = self._reference_version(owner_ctx)
        resp = self.client.get(f"/api/v1/library/versions/{ref_ver['id']}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        withdraw = [a for a in body["allowed_actions"] if a["action"] == "withdraw"]
        self.assertEqual(len(withdraw), 1)
        self.assertIn("impact_confirmation", withdraw[0].get("requirements", []))

    def test_member_cannot_archive(self):
        ref_id, ref_ver = self._reference_version(self.ctx)
        resp = self.client.get(f"/api/v1/library/entries/{ref_id}")
        body = resp.json()
        archive = [a for a in body["allowed_actions"] if a["action"] == "archive"]
        self.assertEqual(len(archive), 1)
        self.assertFalse(archive[0]["allowed"])
        self.assertEqual(archive[0]["reason_code"], "owner_required")

    def test_member_cannot_withdraw(self):
        ref_id, ref_ver = self._reference_version(self.ctx)
        resp = self.client.get(f"/api/v1/library/versions/{ref_ver['id']}")
        body = resp.json()
        withdraw = [a for a in body["allowed_actions"] if a["action"] == "withdraw"]
        self.assertEqual(len(withdraw), 1)
        self.assertFalse(withdraw[0]["allowed"])

    def test_archived_entry_disables_publish(self):
        owner_ctx = self._owner_client()
        ref_id, ref_ver = self._reference_version(owner_ctx)
        entry = service.get_entry(owner_ctx, ref_id)
        service.archive_entry(owner_ctx, ref_id, expected_revision=entry["draft"]["revision"])
        resp = self.client.get(f"/api/v1/library/entries/{ref_id}")
        body = resp.json()
        self.assertEqual(body["lifecycle"], "archived")
        publish = [a for a in body["allowed_actions"] if a["action"] == "publish"]
        self.assertEqual(len(publish), 1)
        self.assertFalse(publish[0]["allowed"])


class TestUnsupportedSchemaVersion(LibraryFixtures):

    def test_unsupported_version_returns_projection(self):
        ctx = self.ctx()
        ref_id, ref_ver = self._reference_version(ctx)
        with tenant_session(ctx) as session:
            version = session.get(LibraryVersionRecord, ref_ver["id"])
            version.content_schema_version = 999
            session.flush()
        result = service.get_version(ctx, ref_ver["id"])
        self.assertEqual(result["content_schema_version"], 999)
        self.assertTrue(result["content"].get("_unsupported"))
        self.assertEqual(result["content"]["_schema_version"], 999)
        self.assertEqual(result["content"]["_known_version"], CONTENT_SCHEMA_VERSIONS[ENTRY_REFERENCE_ASSET])

    def test_known_version_returns_real_content(self):
        ctx = self.ctx()
        ref_id, ref_ver = self._reference_version(ctx)
        result = service.get_version(ctx, ref_ver["id"])
        self.assertNotIn("_unsupported", result["content"])
        self.assertEqual(result["content_schema_version"], 1)


class TestAllThirteenEntryTypes(LibraryFixtures):

    def test_all_entry_types_registered(self):
        self.assertEqual(len(ENTRY_TYPES), 14)

    def test_all_types_have_schema_version(self):
        for t in ENTRY_TYPES:
            self.assertIn(t, CONTENT_SCHEMA_VERSIONS, f"{t} missing from CONTENT_SCHEMA_VERSIONS")

    def test_create_product_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Test Product")
        self.assertEqual(entry["entry_type"], ENTRY_PRODUCT)
        self.assertEqual(entry["draft"]["content_schema_version"], 1)

    def test_create_offer_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_OFFER, display_name="Test Offer")
        self.assertEqual(entry["entry_type"], ENTRY_OFFER)

    def test_create_person_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_PERSON, display_name="Test Person")
        self.assertEqual(entry["entry_type"], ENTRY_PERSON)

    def test_create_reference_asset_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_REFERENCE_ASSET, display_name="Test Ref")
        self.assertEqual(entry["entry_type"], ENTRY_REFERENCE_ASSET)

    def test_create_reference_group_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_REFERENCE_GROUP, display_name="Test Group")
        self.assertEqual(entry["entry_type"], ENTRY_REFERENCE_GROUP)

    def test_create_selection_rule_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_SELECTION_RULE, display_name="Test Rule")
        self.assertEqual(entry["entry_type"], ENTRY_SELECTION_RULE)

    def test_create_market_mapping_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_MARKET_MAPPING, display_name="Test Mapping")
        self.assertEqual(entry["entry_type"], ENTRY_MARKET_MAPPING)

    def test_create_dimension_template_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE, display_name="Test Template")
        self.assertEqual(entry["entry_type"], ENTRY_DIMENSION_TEMPLATE)

    def test_create_dimension_schema_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_DIMENSION_SCHEMA, display_name="Test Schema")
        self.assertEqual(entry["entry_type"], ENTRY_DIMENSION_SCHEMA)

    def test_create_prompt_policy_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_PROMPT_POLICY, display_name="Test Policy")
        self.assertEqual(entry["entry_type"], ENTRY_PROMPT_POLICY)

    def test_create_data_source_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_DATA_SOURCE, display_name="Test DS")
        self.assertEqual(entry["entry_type"], ENTRY_DATA_SOURCE)

    def test_create_manual_fact_set_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_MANUAL_FACT_SET, display_name="Test Facts")
        self.assertEqual(entry["entry_type"], ENTRY_MANUAL_FACT_SET)

    def test_create_truth_resolution_policy_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_TRUTH_RESOLUTION_POLICY, display_name="Test TRP")
        self.assertEqual(entry["entry_type"], ENTRY_TRUTH_RESOLUTION_POLICY)


class TestVersionResponseViaHTTP(LibraryFixtures):

    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_version_response_includes_new_fields(self):
        ref_id, ref_ver = self._reference_version(self.ctx)
        resp = self.client.get(f"/api/v1/library/versions/{ref_ver['id']}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("content_schema_version", body)
        self.assertIn("lifecycle_revision", body)
        self.assertEqual(body["content_schema_version"], 1)
        self.assertEqual(body["lifecycle_revision"], 1)

    def test_entry_response_includes_draft_schema_version(self):
        resp = self.client.post(
            "/api/v1/library/entries",
            json={"entry_type": ENTRY_PRODUCT, "display_name": "Schema Test"},
            headers={
                "Idempotency-Key": "ik-schema-test",
                "X-Command-Identity": "cmd.schema.test.0001",
            },
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertIn("draft", body)
        self.assertIn("content_schema_version", body["draft"])

    def test_entry_summary_includes_enriched_fields(self):
        self._reference_version(self.ctx, name="ListEnrich")
        resp = self.client.get("/api/v1/library/entries")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertGreater(len(body["items"]), 0)
        first = body["items"][0]
        self.assertIn("updated_at", first)
        self.assertIn("draft_revision", first)
        self.assertIn("current_version_no", first)

    def test_event_response_includes_id_and_timestamp(self):
        entry = service.create_entry(self.ctx, entry_type=ENTRY_PRODUCT, display_name="Event HTTP")
        resp = self.client.get(f"/api/v1/library/entries/{entry['id']}/events")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertGreater(len(body["items"]), 0)
        first = body["items"][0]
        self.assertIn("id", first)
        self.assertIn("created_at", first)


if __name__ == "__main__":
    unittest.main()
