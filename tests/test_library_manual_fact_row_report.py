"""#23: a manual fact import reports its rows on the draft it creates.

Only a problem with the file as a whole fails the import. Rows with problems
still become a Manual Fact Set draft, which carries the publish validation's
ValidationReport (``facts[i].<field>`` paths); publishing stays blocked until
the rows are fixed, and only the Owner publishes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from tests.support.offers import publish_offers
from tests.support.queue_hygiene import cancel_workspace_jobs
from tests.test_library_manual_fact_import_worker import ManualFactImportFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import UploadIntentRecord, new_id, utcnow
from app.library import manual_fact_import, service
from app.library.contracts import (
    DRAFT_READY,
    DRAFT_VALIDATION_FAILED,
    ENTRY_DATA_SOURCE,
    ROLE_MEMBER,
    ROLE_OWNER,
)


def _fact(fact_id: str, fact_type: str = "price", **values: object) -> dict:
    row: dict[str, object] = {
        "fact_id": fact_id,
        "fact_type": fact_type,
        "country_code": "US",
        "valid_from": "2026-01-01",
        "valid_to": "2099-12-31",
        "evidence_source": "Acme Charge price list 2026",
    }
    row.update(values)
    return row


# One problem per row: each is a blocking finding of the draft, on its row.
BAD_JSON = {"facts": [
    _fact("no-currency", amount="19.99"),
    _fact("not-decimal", amount="19,99", currency="USD"),
    _fact("unknown-type", "tax", rate="0.19"),
    _fact("reversed", amount="19.99", currency="USD", valid_from="2026-12-31", valid_to="2026-01-01"),
    _fact("no-evidence", amount="19.99", currency="USD", evidence_source=""),
]}
BAD_JSON_FINDINGS = [
    ("fact_value_missing", "facts[0].currency"),
    ("fact_amount_parse", "facts[1].amount"),
    ("fact_type_unknown", "facts[2].fact_type"),
    ("fact_valid_range", "facts[3].valid_to"),
    ("fact_evidence_source_required", "facts[4].evidence_source"),
]

CSV_HEADER = (
    "fact_id,fact_type,country_code,valid_from,valid_to,evidence_source,"
    "amount,currency,available,method,cost,delivery_days_min,delivery_days_max,region\n"
)
BAD_CSV = CSV_HEADER + (
    "no-currency,price,US,2026-01-01,2099-12-31,price list,19.99,,,,,,,\n"
    "not-decimal,price,US,2026-01-01,2099-12-31,price list,19.99 USD,USD,,,,,,\n"
    "unknown-type,tax,US,2026-01-01,2099-12-31,price list,,,,,,,,\n"
    "reversed,price,US,2026-12-31,2026-01-01,price list,19.99,USD,,,,,,\n"
    "no-evidence,price,US,2026-01-01,2099-12-31,,19.99,USD,,,,,,\n"
    "not-a-bool,availability,US,2026-01-01,2099-12-31,stock sheet,,,maybe,,,,,\n"
    "not-days,delivery,US,2026-01-01,2099-12-31,carrier sheet,,,,,,two,5,US\n"
)
BAD_CSV_FINDINGS = [
    ("fact_value_missing", "facts[0].currency"),
    ("fact_amount_parse", "facts[1].amount"),
    ("fact_type_unknown", "facts[2].fact_type"),
    ("fact_valid_range", "facts[3].valid_to"),
    ("fact_evidence_source_required", "facts[4].evidence_source"),
    ("fact_bool_invalid", "facts[5].available"),
    ("fact_days_invalid", "facts[6].delivery_days_min"),
]
# Booleans and day counts arrive as strings in a CSV (#50's gap).
GOOD_CSV = CSV_HEADER + (
    "price-us,price,US,2026-01-01,2099-12-31,price list,19.99,USD,,,,,,\n"
    "stock-us,availability,US,2026-01-01,2099-12-31,stock sheet,,,TRUE,,,,,\n"
    "ship-us,shipping,US,2026-01-01,2099-12-31,carrier sheet,,USD,,standard,4.90,2,5,\n"
    "deliver-us,delivery,US,2026-01-01,2099-12-31,carrier sheet,,,,,, 1 ,3,US\n"
)


class FactRowReportFixtures(ManualFactImportFixtures):
    def offer(self):
        """A published Offer of the Workspace selling in US and DE: its entry id.

        Every row names its Offer to publish (#54); these imports give it to
        the rows as the import's default Offer.
        """
        if getattr(self, "_offer_entry_id", None) is None:
            [(self._offer_entry_id, _version)] = publish_offers(self.ctx, markets=["US", "DE"])
        return self._offer_entry_id

    def published_data_source(self):
        entry = service.create_entry(
            self.ctx, entry_type=ENTRY_DATA_SOURCE, display_name="Acme Charge manual facts",
            content={"adapter_kind": "manual", "execution_mode": "real", "config": {}},
        )
        service.publish(self.ctx, entry["id"], entry["draft"]["revision"])
        return entry["id"]

    def upload_file(self, data_source_id, raw: bytes, extension: str, ctx=None):
        """A finalized fact-import upload of ``raw``; the Worker reads its MIME from ``extension``."""
        ctx = ctx or self.ctx
        committed_key = f"committed/{ctx.workspace_id}/library_manual_fact_import/{uuid.uuid4().hex}.{extension}"
        self.store.objects[committed_key] = raw
        intent_id = new_id("ui")
        now = utcnow()
        with tenant_session(ctx) as session:
            session.add(UploadIntentRecord(
                id=intent_id, workspace_id=ctx.workspace_id, initiator_id=ctx.initiator_id,
                authorization_epoch_at_issue=0, owner_kind="data_source", owner_id=data_source_id,
                staging_key=f"staging/{ctx.workspace_id}/{uuid.uuid4().hex}.dat",
                purpose="library_manual_fact_import", filename_original=f"facts.{extension}",
                content_type="text/csv" if extension == "csv" else "application/json",
                content_length_min=len(raw), content_length_max=len(raw),
                checksum_sha256_b64=base64.b64encode(hashlib.sha256(raw).digest()).decode(),
                state="finalized", committed_blob_ref_key=committed_key, committed_blob_ref_version="v1",
                created_at=now, expires_at=now + timedelta(minutes=5), finalized_at=now,
            ))
        return intent_id

    def import_file(self, data_source_id, raw: bytes, extension: str):
        """Import ``raw`` with the fixture Offer as default and run the Worker; returns the Operation."""
        accepted = self.import_(
            data_source_id, self.upload_file(data_source_id, raw, extension), default_offer=self.offer(),
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.drain()
        return self.operation(accepted.json()["operation_id"])

    def imported_draft(self, operation):
        self.assertEqual(operation["state"], "succeeded", operation)
        fact_set_id = operation["result"]["resource_id"]
        response = self.client.get(f"/api/v1/library/entries/{fact_set_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return fact_set_id, response.json()["draft"]

    def command(self, fact_set_id, action, revision, **body):
        return self.client.post(
            f"/api/v1/library/entries/{fact_set_id}/{action}", json=body,
            headers={"Idempotency-Key": f"ci-only-{action}-{uuid.uuid4().hex}", "If-Match": str(revision)},
        )

    def save(self, fact_set_id, revision, content):
        response = self.client.put(
            f"/api/v1/library/entries/{fact_set_id}/draft", json={"content": content},
            headers={"Idempotency-Key": f"ci-only-save-{uuid.uuid4().hex}", "If-Match": str(revision)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["draft"]

    def findings(self, report):
        return [(f["code"], f["path"]) for f in report["blocking"]]


class TestRowsDoNotFailTheImport(FactRowReportFixtures):
    def test_a_json_file_with_bad_rows_becomes_a_draft_reporting_each_row(self):
        operation = self.import_file(
            self.published_data_source(), json.dumps(BAD_JSON).encode(), "json",
        )
        fact_set_id, draft = self.imported_draft(operation)
        self.assertEqual(draft["state"], DRAFT_VALIDATION_FAILED)
        self.assertEqual(self.findings(draft["validation"]), BAD_JSON_FINDINGS)
        self.assertEqual(
            draft["content"]["facts"],
            [{**fact, "offer_entry_id": self.offer()} for fact in BAD_JSON["facts"]],
            "a JSON import keeps its rows as written, with the default Offer where they name none",
        )

        blocked = self.command(fact_set_id, "publish", draft["revision"])
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(self.findings(blocked.json()["report"]), BAD_JSON_FINDINGS)
        self.assertIsNone(service.get_entry(self.ctx, fact_set_id)["current_version_id"])

    def test_a_csv_file_with_bad_rows_becomes_a_draft_reporting_each_row(self):
        operation = self.import_file(self.published_data_source(), BAD_CSV.encode(), "csv")
        fact_set_id, draft = self.imported_draft(operation)
        self.assertEqual(draft["state"], DRAFT_VALIDATION_FAILED)
        self.assertEqual(self.findings(draft["validation"]), BAD_CSV_FINDINGS)
        facts = draft["content"]["facts"]
        self.assertEqual(facts[5]["available"], "maybe", "a value that does not convert stays as written")
        self.assertEqual(facts[6]["delivery_days_min"], "two")
        self.assertEqual(facts[6]["delivery_days_max"], 5)
        self.assertNotIn("currency", facts[0], "an empty cell is no value")

        blocked = self.command(fact_set_id, "publish", draft["revision"])
        self.assertEqual(blocked.status_code, 422, blocked.text)

    def test_the_report_is_on_the_draft_events_and_audit(self):
        operation = self.import_file(
            self.published_data_source(), json.dumps(BAD_JSON).encode(), "json",
        )
        fact_set_id, _draft = self.imported_draft(operation)
        events = self.client.get(f"/api/v1/library/entries/{fact_set_id}/events")
        self.assertEqual(events.status_code, 200, events.text)
        kinds = [(e["event_type"], e.get("to_state")) for e in events.json()["items"]]
        self.assertIn(("draft_created", DRAFT_VALIDATION_FAILED), kinds)
        self.assertIn(("validated", DRAFT_VALIDATION_FAILED), kinds)


class TestCsvTypes(FactRowReportFixtures):
    def test_correct_availability_shipping_and_delivery_rows_from_a_csv_publish(self):
        operation = self.import_file(self.published_data_source(), GOOD_CSV.encode(), "csv")
        fact_set_id, draft = self.imported_draft(operation)
        self.assertEqual(draft["validation"]["blocking"], [])
        self.assertEqual(draft["state"], DRAFT_READY)
        stock, ship, deliver = draft["content"]["facts"][1:]
        self.assertIs(stock["available"], True)
        self.assertEqual((ship["cost"], ship["delivery_days_min"], ship["delivery_days_max"]), ("4.90", 2, 5))
        self.assertEqual((deliver["delivery_days_min"], deliver["delivery_days_max"]), (1, 3))

        published = self.command(fact_set_id, "publish", draft["revision"])
        self.assertEqual(published.status_code, 201, published.text)


class TestFixAndPublish(FactRowReportFixtures):
    def test_fixed_rows_validate_and_only_the_owner_publishes(self):
        data_source = self.published_data_source()
        member = self._make_context(role=ROLE_MEMBER, workspace_id=self.ctx.workspace_id)
        self.act_as(member)
        operation = self.import_file(
            data_source, json.dumps(BAD_JSON).encode(), "json",
        )
        fact_set_id, draft = self.imported_draft(operation)
        self.assertEqual(self.findings(draft["validation"]), BAD_JSON_FINDINGS, "a Member imports")

        fixed = dict(draft["content"])
        fixed["facts"] = [
            _fact("price-us", amount="19.99", currency="USD", offer_entry_id=self.offer()),
            _fact("price-de", amount="17.99", currency="EUR", country_code="DE", offer_entry_id=self.offer()),
        ]
        saved = self.save(fact_set_id, draft["revision"], fixed)
        self.assertIsNone(saved["validation"], "saving clears the import's report")
        report = self.command(fact_set_id, "validate", saved["revision"])
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()["blocking"], [])

        denied = self.command(fact_set_id, "publish", saved["revision"])
        self.assertEqual(denied.status_code, 403, denied.text)

        self.act_as(self.ctx)
        published = self.command(fact_set_id, "publish", saved["revision"])
        self.assertEqual(published.status_code, 201, published.text)
        self.assertEqual(published.json()["content"]["facts"], fixed["facts"])

    def test_a_fact_set_of_an_unpublished_data_source_cannot_publish(self):
        rows = {"facts": [_fact("p", amount="1", currency="USD")]}
        operation = self.import_file(self.data_source(), json.dumps(rows).encode(), "json")
        _fact_set_id, draft = self.imported_draft(operation)
        self.assertEqual(self.findings(draft["validation"]), [("dependency_not_found", "data_source_entry_id")])


class TestFileLevelProblemsFailTheImport(FactRowReportFixtures):
    def assert_failed(self, operation):
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(self.reasons(operation["operation_id"])[-1], "manual_fact_import_failed")
        self.assertEqual(self.fact_sets(), [])

    def test_a_json_file_that_does_not_parse(self):
        self.assert_failed(self.import_file(self.data_source(), b'{"facts": [', "json"))

    def test_a_json_file_without_a_facts_list(self):
        self.assert_failed(self.import_file(self.data_source(), b'{"rows": []}', "json"))

    def test_a_file_that_is_not_utf8(self):
        self.assert_failed(self.import_file(self.data_source(), "fact_id\né\n".encode("latin-1"), "csv"))

    def test_a_csv_file_without_rows(self):
        self.assert_failed(self.import_file(self.data_source(), CSV_HEADER.encode(), "csv"))

    def test_a_file_over_the_fact_limit(self):
        rows = {"facts": [_fact(f"p{i}", amount="1", currency="USD") for i in range(3)]}
        with patch.object(manual_fact_import, "MAX_FACT_COUNT", 2):
            self.assert_failed(self.import_file(
                self.data_source(), json.dumps(rows).encode(), "json",
            ))


class TestImportBoundaries(FactRowReportFixtures):
    def test_a_fact_set_id_is_not_a_data_source(self):
        operation = self.import_file(
            self.published_data_source(), json.dumps(BAD_JSON).encode(), "json",
        )
        fact_set_id, _draft = self.imported_draft(operation)
        response = self.import_(fact_set_id, self.upload_file(fact_set_id, b'{"facts": []}', "json"))
        self.assert_problem(response, 422, "entry_type_mismatch")

    def test_another_workspaces_fact_set_draft_is_not_found(self):
        operation = self.import_file(
            self.published_data_source(), json.dumps(BAD_JSON).encode(), "json",
        )
        fact_set_id, draft = self.imported_draft(operation)
        other = self._make_context(role=ROLE_OWNER, actor="user_b")
        self.addCleanup(cancel_workspace_jobs, other.workspace_id)
        self.act_as(other)
        self.assertEqual(self.client.get(f"/api/v1/library/entries/{fact_set_id}").status_code, 404)
        self.assertEqual(self.command(fact_set_id, "publish", draft["revision"]).status_code, 404)
