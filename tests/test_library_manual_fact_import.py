"""Tests for Gate 0.7 — Manual fact import validation and HTTP route.

Covers:
- Fact validation: stable fact_id, ISO country_code, valid_from/valid_to, fact_type
- Duplicate stable fact ID rejection
- Malformed/oversized import
- process_manual_fact_import creates Manual Fact Set entry with draft
- POST /manual-fact-imports returns 202 Operation
- Purpose must be library_manual_fact_import
- Non-data-source entry rejected
- Archived entry rejected
"""
from __future__ import annotations

import asyncio
import unittest

from fastapi.testclient import TestClient

from tests.support.library_fixtures import LibraryFixtures

from app.library import service
from app.library.contracts import (
    ENTRY_DATA_SOURCE,
    ENTRY_MANUAL_FACT_SET,
    ENTRY_PRODUCT,
    ROLE_MEMBER,
    ROLE_OWNER,
)
from app.library.manual_fact_import import (
    ManualFactImportError,
    _validate_facts,
    parse_manual_fact_payload,
    process_manual_fact_import,
    read_manual_fact_payload,
)
from app.main import app
from app.platform.blob_store import CommittedBlobRef
from app.platform.http_identity import request_tenant_context


def _valid_fact(fact_id="f1", fact_type="price", country="US"):
    return {
        "fact_id": fact_id,
        "fact_type": fact_type,
        "country_code": country,
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "amount": "9.99",
        "currency": "USD",
    }


class TestFactValidation(unittest.TestCase):

    def test_valid_facts_pass(self):
        errors = _validate_facts([_valid_fact()])
        self.assertEqual(errors, [])

    def test_empty_facts_rejected(self):
        errors = _validate_facts([])
        self.assertGreater(len(errors), 0)
        self.assertIn("至少需要一条事实", errors[0])

    def test_missing_fact_id_rejected(self):
        fact = _valid_fact()
        del fact["fact_id"]
        errors = _validate_facts([fact])
        self.assertTrue(any("fact_id" in e for e in errors))

    def test_duplicate_fact_id_rejected(self):
        errors = _validate_facts([_valid_fact("dup"), _valid_fact("dup")])
        self.assertTrue(any("重复" in e for e in errors))

    def test_invalid_fact_type_rejected(self):
        fact = _valid_fact()
        fact["fact_type"] = "invalid_type"
        errors = _validate_facts([fact])
        self.assertTrue(any("fact_type" in e for e in errors))

    def test_invalid_country_code_rejected(self):
        fact = _valid_fact()
        fact["country_code"] = "usa"
        errors = _validate_facts([fact])
        self.assertTrue(any("country_code" in e for e in errors))

    def test_two_letter_lowercase_country_rejected(self):
        fact = _valid_fact()
        fact["country_code"] = "us"
        errors = _validate_facts([fact])
        self.assertTrue(any("country_code" in e for e in errors))

    def test_missing_valid_from_rejected(self):
        fact = _valid_fact()
        del fact["valid_from"]
        errors = _validate_facts([fact])
        self.assertTrue(any("valid_from" in e for e in errors))

    def test_missing_valid_to_rejected(self):
        fact = _valid_fact()
        del fact["valid_to"]
        errors = _validate_facts([fact])
        self.assertTrue(any("valid_to" in e for e in errors))

    def test_non_dict_fact_rejected(self):
        errors = _validate_facts(["not_a_dict"])
        self.assertTrue(any("对象" in e for e in errors))

    def test_multiple_valid_facts(self):
        facts = [
            _valid_fact("f1", "price", "US"),
            _valid_fact("f2", "tax", "DE"),
            _valid_fact("f3", "discount", "JP"),
        ]
        errors = _validate_facts(facts)
        self.assertEqual(errors, [])


class TestUploadedFactPayload(unittest.TestCase):
    def test_parses_json_payload(self):
        payload = parse_manual_fact_payload(
            b'{"facts":[{"fact_id":"f1"}]}',
            content_type="application/json",
        )
        self.assertEqual(payload["facts"][0]["fact_id"], "f1")

    def test_parses_csv_payload(self):
        payload = parse_manual_fact_payload(
            b"fact_id,fact_type,country_code,valid_from,valid_to\n"
            b"f1,price,US,2026-01-01,2026-12-31\n",
            content_type="text/csv",
        )
        self.assertEqual(payload["facts"][0]["country_code"], "US")

    def test_rejects_invalid_json(self):
        with self.assertRaises(ManualFactImportError):
            parse_manual_fact_payload(b"{", content_type="application/json")

    def test_reads_committed_blob_before_parsing(self):
        class FakeBlobStore:
            async def open_committed(self, _ref):
                yield b'{"facts":['
                yield b'{"fact_id":"f1"}]}'

        ref = CommittedBlobRef(
            committed_key="committed/ws/purpose/2026/09/23/facts.json",
            storage_version_id="v1",
            size_bytes=28,
            checksum_sha256_b64="",
            content_type_verified="application/json",
        )
        payload = asyncio.run(read_manual_fact_payload(FakeBlobStore(), ref))  # type: ignore[arg-type]
        self.assertEqual(payload["facts"][0]["fact_id"], "f1")


class TestProcessManualFactImport(LibraryFixtures):

    def _create_data_source(self, ctx):
        content = {"adapter_kind": "manual", "label": "Test DS"}
        return service.create_entry(ctx, entry_type=ENTRY_DATA_SOURCE,
                                     display_name="Test DS", content=content)

    def test_creates_manual_fact_set_entry(self):
        ctx = self.ctx()
        ds = self._create_data_source(ctx)
        result = process_manual_fact_import(
            ctx,
            data_source_entry_id=ds["id"],
            upload_intent_id="ui_test_001",
            content_payload={"facts": [_valid_fact()]},
        )
        self.assertEqual(result["entry_type"], ENTRY_MANUAL_FACT_SET)
        self.assertIsNotNone(result.get("draft"))

    def test_a_derived_fact_set_name_stays_inside_its_column(self):
        # "Fact Import for <data source>" from a 300-character data source
        # name would overflow library_entries.display_name's varchar(300).
        ctx = self.ctx()
        long_name = "D" * 300
        ds = service.create_entry(ctx, entry_type=ENTRY_DATA_SOURCE, display_name=long_name,
                                  content={"adapter_kind": "manual", "label": "Test DS"})
        result = process_manual_fact_import(
            ctx,
            data_source_entry_id=ds["id"],
            upload_intent_id="ui_test_long_name",
            content_payload={"facts": [_valid_fact()]},
        )
        self.assertEqual(len(result["display_name"]), service.DISPLAY_NAME_MAX_LENGTH)
        self.assertTrue(result["display_name"].startswith("Fact Import for DDD"))
        self.assert_stored_widths(ctx)

    def test_draft_contains_facts(self):
        ctx = self.ctx()
        ds = self._create_data_source(ctx)
        facts = [_valid_fact("f1"), _valid_fact("f2", "tax", "DE")]
        result = process_manual_fact_import(
            ctx,
            data_source_entry_id=ds["id"],
            upload_intent_id="ui_test_002",
            content_payload={"facts": facts},
        )
        draft_content = result["draft"]["content"]
        self.assertEqual(len(draft_content["facts"]), 2)
        self.assertEqual(draft_content["data_source_entry_id"], ds["id"])
        self.assertEqual(draft_content["upload_intent_id"], "ui_test_002")

    def test_rejects_missing_facts_list(self):
        ctx = self.ctx()
        ds = self._create_data_source(ctx)
        with self.assertRaises(ManualFactImportError) as cm:
            process_manual_fact_import(
                ctx, data_source_entry_id=ds["id"],
                upload_intent_id="ui_test_003",
                content_payload={},
            )
        self.assertIn("facts", str(cm.exception))

    def test_rejects_duplicate_fact_ids(self):
        ctx = self.ctx()
        ds = self._create_data_source(ctx)
        with self.assertRaises(ManualFactImportError):
            process_manual_fact_import(
                ctx, data_source_entry_id=ds["id"],
                upload_intent_id="ui_test_004",
                content_payload={"facts": [_valid_fact("dup"), _valid_fact("dup")]},
            )

    def test_rejects_non_data_source_entry(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Not DS")
        with self.assertRaises(ManualFactImportError) as cm:
            process_manual_fact_import(
                ctx, data_source_entry_id=entry["id"],
                upload_intent_id="ui_test_005",
                content_payload={"facts": [_valid_fact()]},
            )
        self.assertIn("数据源", str(cm.exception))

    def test_rejects_archived_data_source(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds = self._create_data_source(ctx)
        entry = service.get_entry(ctx, ds["id"])
        service.archive_entry(ctx, ds["id"], expected_revision=entry["draft"]["revision"])
        with self.assertRaises(ManualFactImportError):
            process_manual_fact_import(
                ctx, data_source_entry_id=ds["id"],
                upload_intent_id="ui_test_006",
                content_payload={"facts": [_valid_fact()]},
            )


class TestManualFactImportHTTP(LibraryFixtures):

    def setUp(self):
        self.ctx = self._make_context(role=ROLE_OWNER)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _create_data_source(self):
        content = {"adapter_kind": "manual", "label": "Test DS"}
        return service.create_entry(self.ctx, entry_type=ENTRY_DATA_SOURCE,
                                     display_name="Test DS", content=content)

    def test_manual_fact_import_returns_202(self):
        ds = self._create_data_source()
        resp = self.client.post(
            f"/api/v1/library/entries/{ds['id']}/manual-fact-imports",
            json={"upload_intent_id": "ui_http_001"},
            headers={"Idempotency-Key": "ik-factimport-1"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("operation_id", body)
        self.assertEqual(body["state"], "queued")
        self.assertEqual(body["command_type"], "manual_fact_import")

    def test_manual_fact_import_rejects_wrong_purpose(self):
        ds = self._create_data_source()
        resp = self.client.post(
            f"/api/v1/library/entries/{ds['id']}/manual-fact-imports",
            json={"upload_intent_id": "ui_http_002", "purpose": "wrong_purpose"},
            headers={"Idempotency-Key": "ik-factimport-wrong-1"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_manual_fact_import_rejects_non_data_source(self):
        entry = service.create_entry(self.ctx, entry_type=ENTRY_PRODUCT, display_name="Not DS")
        resp = self.client.post(
            f"/api/v1/library/entries/{entry['id']}/manual-fact-imports",
            json={"upload_intent_id": "ui_http_003"},
            headers={"Idempotency-Key": "ik-factimport-notds-1"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_manual_fact_import_rejects_archived_entry(self):
        ds = self._create_data_source()
        entry = service.get_entry(self.ctx, ds["id"])
        service.archive_entry(self.ctx, ds["id"], expected_revision=entry["draft"]["revision"])
        resp = self.client.post(
            f"/api/v1/library/entries/{ds['id']}/manual-fact-imports",
            json={"upload_intent_id": "ui_http_004"},
            headers={"Idempotency-Key": "ik-factimport-archived-1"},
        )
        self.assertEqual(resp.status_code, 409)


if __name__ == "__main__":
    unittest.main()
