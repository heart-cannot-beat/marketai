"""Tests for Gate 0.7 — Manual fact import and HTTP route.

Covers:
- File-level problems fail the import: no facts list, no facts, too many facts,
  malformed JSON or CSV
- Row-level problems do not (#23): the draft carries the publish validation's
  report, row by row
- CSV cells become their fact family's value types (#50's gap)
- process_manual_fact_import creates Manual Fact Set entry with draft
- POST /manual-fact-imports returns 202 Operation
- Purpose must be library_manual_fact_import
- Non-data-source entry rejected
- Archived entry rejected
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import LibraryDependencyRecord
from app.library import manual_fact_import, service
from app.library.contracts import (
    DRAFT_READY,
    DRAFT_VALIDATION_FAILED,
    ENTRY_DATA_SOURCE,
    ENTRY_MANUAL_FACT_SET,
    ENTRY_PRODUCT,
    ROLE_MEMBER,
    ROLE_OWNER,
)
from app.library.manual_fact_import import (
    ManualFactImportError,
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
        "evidence_source": "Acme Charge price list",
    }


def _csv(*rows: str) -> dict:
    header = "fact_id,fact_type,country_code,valid_from,valid_to,available,delivery_days_min,delivery_days_max,region"
    return parse_manual_fact_payload("\n".join([header, *rows]).encode(), content_type="text/csv")


class TestCsvCellTypes(unittest.TestCase):
    """A CSV only has strings; the import converts them by fact family (#23)."""

    def test_booleans_and_day_counts_are_converted(self):
        facts = _csv(
            "a,availability,US,2026-01-01,2026-12-31,True,,,",
            "b,availability,US,2026-01-01,2026-12-31,false,,,",
            "c,delivery,US,2026-01-01,2026-12-31,,2, 5 ,US",
        )["facts"]
        self.assertIs(facts[0]["available"], True)
        self.assertIs(facts[1]["available"], False)
        self.assertEqual((facts[2]["delivery_days_min"], facts[2]["delivery_days_max"]), (2, 5))

    def test_a_value_that_does_not_convert_stays_as_written(self):
        facts = _csv(
            "a,availability,US,2026-01-01,2026-12-31,yes,,,",
            "b,delivery,US,2026-01-01,2026-12-31,,-1,2.5,US",
        )["facts"]
        self.assertEqual(facts[0]["available"], "yes")
        self.assertEqual((facts[1]["delivery_days_min"], facts[1]["delivery_days_max"]), ("-1", "2.5"))

    def test_only_the_rows_own_family_is_converted(self):
        # `available` is not a delivery field, and an unknown type has none.
        facts = _csv(
            "a,delivery,US,2026-01-01,2026-12-31,true,1,2,US",
            "b,tax,US,2026-01-01,2026-12-31,true,1,2,US",
        )["facts"]
        self.assertEqual(facts[0]["available"], "true")
        self.assertEqual(facts[1]["delivery_days_min"], "1")

    def test_an_empty_cell_is_no_value(self):
        [fact] = _csv("a,availability,US,2026-01-01,2026-12-31,true,,,")["facts"]
        self.assertNotIn("delivery_days_min", fact)
        self.assertNotIn("region", fact)

    def test_a_json_payload_is_kept_as_written(self):
        payload = parse_manual_fact_payload(
            b'{"facts":[{"fact_type":"availability","available":"true","delivery_days_min":"2"}]}',
            content_type="application/json",
        )
        self.assertEqual(payload["facts"][0], {
            "fact_type": "availability", "available": "true", "delivery_days_min": "2",
        })


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

    def test_rejects_a_csv_without_a_header(self):
        with self.assertRaises(ManualFactImportError):
            parse_manual_fact_payload(b"", content_type="text/csv")

    def test_rejects_a_csv_the_reader_cannot_read(self):
        # A cell over the csv module's field limit is a csv.Error.
        with self.assertRaises(ManualFactImportError):
            parse_manual_fact_payload(
                b"fact_id,fact_type\n" + b"f" * 200_000 + b",price\n", content_type="text/csv",
            )

    def test_rejects_a_file_that_is_not_utf8(self):
        with self.assertRaises(ManualFactImportError):
            parse_manual_fact_payload("fact_id\n\u00e9\n".encode("latin-1"), content_type="text/csv")

    def test_rejects_an_unsupported_mime(self):
        with self.assertRaises(ManualFactImportError):
            parse_manual_fact_payload(b"{}", content_type="text/plain")

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

    def _published_data_source(self, ctx):
        ds = service.create_entry(ctx, entry_type=ENTRY_DATA_SOURCE, display_name="Test DS",
                                  content={"adapter_kind": "manual", "execution_mode": "real"})
        service.publish(ctx, ds["id"], ds["draft"]["revision"])
        return ds

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
        facts = [_valid_fact("f1"), _valid_fact("f2", "availability", "DE")]
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

    def test_rejects_an_empty_facts_list(self):
        ctx = self.ctx()
        ds = self._create_data_source(ctx)
        with self.assertRaises(ManualFactImportError) as cm:
            process_manual_fact_import(
                ctx, data_source_entry_id=ds["id"],
                upload_intent_id="ui_test_empty",
                content_payload={"facts": []},
            )
        self.assertIn("至少需要一条事实", str(cm.exception))

    def test_rejects_more_facts_than_the_limit(self):
        ctx = self.ctx()
        ds = self._create_data_source(ctx)
        with patch.object(manual_fact_import, "MAX_FACT_COUNT", 1), \
                self.assertRaises(ManualFactImportError) as cm:
            process_manual_fact_import(
                ctx, data_source_entry_id=ds["id"],
                upload_intent_id="ui_test_limit",
                content_payload={"facts": [_valid_fact("a"), _valid_fact("b")]},
            )
        self.assertIn("超过限制", str(cm.exception))

    def test_rows_with_problems_still_create_a_draft_that_reports_them(self):
        # Before #23 any of these failed the whole import.
        ctx = self.ctx()
        ds = self._create_data_source(ctx)
        no_id = _valid_fact()
        del no_id["fact_id"]
        rows = [
            _valid_fact("dup"),
            _valid_fact("dup"),
            {**_valid_fact("bad-market"), "country_code": "usa"},
            {**_valid_fact("bad-type"), "fact_type": "invalid_type"},
            {**_valid_fact("no-end"), "valid_to": ""},
            {**_valid_fact("bad-date"), "valid_from": "01/02/2026"},
            no_id,
            "not_a_dict",
        ]
        result = process_manual_fact_import(
            ctx, data_source_entry_id=ds["id"],
            upload_intent_id="ui_test_004",
            content_payload={"facts": rows},
        )
        draft = result["draft"]
        self.assertEqual(draft["content"]["facts"], rows)
        self.assertEqual(draft["state"], DRAFT_VALIDATION_FAILED)
        findings = [(f["code"], f["path"]) for f in draft["validation"]["blocking"]]
        for expected in [
            ("fact_duplicate_id", "facts[1].fact_id"),
            ("fact_country_code", "facts[2].country_code"),
            ("fact_type_unknown", "facts[3].fact_type"),
            ("fact_valid_to", "facts[4].valid_to"),
            ("fact_date_format", "facts[5].valid_from"),
            ("fact_id_required", "facts[6].fact_id"),
            ("fact_shape", "facts[7]"),
        ]:
            self.assertIn(expected, findings)

    def test_a_clean_file_creates_a_draft_ready_to_publish_once_its_source_is(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds = self._published_data_source(ctx)
        result = process_manual_fact_import(
            ctx, data_source_entry_id=ds["id"],
            upload_intent_id="ui_test_clean",
            content_payload={"facts": [_valid_fact()]},
        )
        self.assertEqual(result["draft"]["state"], DRAFT_READY)
        self.assertEqual(result["draft"]["validation"]["blocking"], [])

    def test_the_import_writes_rows_without_updating_any(self):
        # The Worker runs the import under a database role that may insert a
        # draft but not update one (deploy/postgres/domain-tenant-roles.sql).
        # SQLite does not enforce grants, so the statements are checked here.
        ctx = self.ctx(role=ROLE_OWNER)
        ds = self._published_data_source(ctx)
        statements: list[str] = []

        def record(_conn, _cursor, statement, *_args):
            statements.append(statement.lstrip().upper())

        event.listen(Engine, "before_cursor_execute", record)
        try:
            result = process_manual_fact_import(
                ctx, data_source_entry_id=ds["id"],
                upload_intent_id="ui_test_grants",
                content_payload={"facts": [_valid_fact("a"), {**_valid_fact("b"), "currency": "usd"}]},
            )
        finally:
            event.remove(Engine, "before_cursor_execute", record)
        self.assertEqual(result["draft"]["state"], DRAFT_VALIDATION_FAILED)
        self.assertTrue(any(s.startswith("INSERT INTO LIBRARY_DRAFTS") for s in statements))
        self.assertEqual([s for s in statements if s.startswith("UPDATE")], [])

    def test_a_member_imports_but_cannot_publish(self):
        owner = self.ctx(role=ROLE_OWNER)
        ds = self._published_data_source(owner)
        member = self.ctx(role=ROLE_MEMBER, workspace=owner.workspace_id, actor="user_m")
        result = process_manual_fact_import(
            member, data_source_entry_id=ds["id"],
            upload_intent_id="ui_test_member",
            content_payload={"facts": [_valid_fact()]},
        )
        self.assertEqual(result["draft"]["validation"]["blocking"], [])
        with self.assertRaises(service.LibraryForbidden):
            service.publish(member, result["id"], result["draft"]["revision"])
        version = service.publish(owner, result["id"], result["draft"]["revision"])
        self.assertEqual(version["entry_id"], result["id"])

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


class TestFactSetDataSourceDependency(LibraryFixtures):
    """``data_source_entry_id`` names the data source entry the import read (#23).

    Before #23 the publish validation looked it up as a version ID, so no
    imported fact set could publish (``dependency_not_found``).
    """

    def _published_data_source(self, ctx):
        ds = service.create_entry(ctx, entry_type=ENTRY_DATA_SOURCE, display_name="Test DS",
                                  content={"adapter_kind": "manual", "execution_mode": "real"})
        version = service.publish(ctx, ds["id"], ds["draft"]["revision"])
        return ds["id"], version["id"]

    def _fact_set_report(self, ctx, data_source_ref):
        entry = service.create_entry(
            ctx, entry_type=ENTRY_MANUAL_FACT_SET, display_name="Facts",
            content={"data_source_entry_id": data_source_ref, "facts": [_valid_fact()]},
        )
        report = service.validate_draft(ctx, entry["id"], expected_revision=entry["draft"]["revision"])
        return entry, [(f["code"], f["path"]) for f in report["blocking"]]

    def test_an_entry_id_resolves_to_the_data_sources_current_version(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds_entry, ds_version = self._published_data_source(ctx)
        entry, blocking = self._fact_set_report(ctx, ds_entry)
        self.assertEqual(blocking, [])
        version = service.publish(ctx, entry["id"], entry["draft"]["revision"])
        with tenant_session(ctx) as session:
            edges = session.execute(
                select(LibraryDependencyRecord.to_version_id, LibraryDependencyRecord.relation)
                .where(LibraryDependencyRecord.from_version_id == version["id"])
            ).all()
        self.assertEqual([tuple(edge) for edge in edges], [(ds_version, "fact_set_data_source")])

    def test_a_data_source_version_id_still_resolves(self):
        ctx = self.ctx(role=ROLE_OWNER)
        _ds_entry, ds_version = self._published_data_source(ctx)
        self.assertEqual(self._fact_set_report(ctx, ds_version)[1], [])

    def test_an_unpublished_data_source_blocks(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds = service.create_entry(ctx, entry_type=ENTRY_DATA_SOURCE, display_name="Test DS",
                                  content={"adapter_kind": "manual", "execution_mode": "real"})
        self.assertEqual(
            self._fact_set_report(ctx, ds["id"])[1],
            [("dependency_not_found", "data_source_entry_id")],
        )

    def test_another_entry_type_is_a_type_mismatch(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds_entry, _version = self._published_data_source(ctx)
        other_set, _blocking = self._fact_set_report(ctx, ds_entry)
        service.publish(ctx, other_set["id"], other_set["draft"]["revision"])
        self.assertEqual(
            self._fact_set_report(ctx, other_set["id"])[1],
            [("dependency_type_mismatch", "data_source_entry_id")],
        )

    def test_another_workspaces_data_source_is_not_found(self):
        foreign, _version = self._published_data_source(self.ctx(role=ROLE_OWNER))
        self.assertEqual(
            self._fact_set_report(self.ctx(role=ROLE_OWNER), foreign)[1],
            [("dependency_not_found", "data_source_entry_id")],
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

    def test_manual_fact_import_rejects_a_fact_set_id(self):
        fact_set = service.create_entry(self.ctx, entry_type=ENTRY_MANUAL_FACT_SET, display_name="Facts")
        resp = self.client.post(
            f"/api/v1/library/entries/{fact_set['id']}/manual-fact-imports",
            json={"upload_intent_id": "ui_http_005"},
            headers={"Idempotency-Key": "ik-factimport-factset-1"},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "entry_type_mismatch")

    def test_a_member_can_start_an_import(self):
        ds = self._create_data_source()
        member = self._make_context(self.ctx.workspace_id, role=ROLE_MEMBER, actor="user_m")
        app.dependency_overrides[request_tenant_context] = lambda: member
        resp = self.client.post(
            f"/api/v1/library/entries/{ds['id']}/manual-fact-imports",
            json={"upload_intent_id": "ui_http_member"},
            headers={"Idempotency-Key": "ik-factimport-member-1"},
        )
        self.assertEqual(resp.status_code, 202, resp.text)

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
