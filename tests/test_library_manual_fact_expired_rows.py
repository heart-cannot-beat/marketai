"""#60: publishing a Manual Fact Set whose rows already ended warns, and does not block.

Truth judges each row's validity on its own and leaves out the rows not in
force (`tests/test_localization_manual_fact_row_validity.py`), so an expired
row is not a reason to refuse the version. It is still worth a look before
publishing: the publish validation gives it as a `fact_expired` *warning* on
the row (`facts[i].valid_to`), which the Owner acknowledges like any other
warning. A row that has not started yet is a normal scheduled fact and gives
nothing.
"""
from __future__ import annotations

import unittest
import uuid

from tests.test_library_manual_fact_row_report import FactRowReportFixtures, _fact

from app.library import service
from app.library.contracts import (
    DRAFT_READY,
    ENTRY_MANUAL_FACT_SET,
    ROLE_MEMBER,
    ROLE_OWNER,
    validate_content,
)

IN_FORCE = {"valid_from": "2026-01-01", "valid_to": "2099-12-31"}
EXPIRED = {"valid_from": "2020-01-01", "valid_to": "2020-12-31"}
NOT_YET = {"valid_from": "2099-01-01", "valid_to": "2099-12-31"}


def _content(*facts: dict) -> dict:
    return {"data_source_entry_id": "lib_ds_acme", "facts": list(facts)}


class TestTheWarningIsPartOfTheRowReport(unittest.TestCase):
    def test_an_expired_row_is_a_warning_on_its_row(self):
        report = validate_content(ENTRY_MANUAL_FACT_SET, _content(
            _fact("price-now", amount="19.99", currency="USD", **IN_FORCE),
            _fact("price-old", amount="24.99", currency="USD", **EXPIRED),
        ))
        self.assertTrue(report.ok)
        self.assertEqual(report.blocking, [])
        self.assertEqual(
            [(f.code, f.path, f.severity) for f in report.warnings],
            [("fact_expired", "facts[1].valid_to", "warning")],
        )

    def test_a_row_not_yet_in_force_gives_nothing(self):
        report = validate_content(ENTRY_MANUAL_FACT_SET, _content(
            _fact("price-next", amount="17.99", currency="USD", **NOT_YET),
        ))
        self.assertEqual(report.findings, [])

    def test_a_reversed_window_is_blocking_and_not_also_a_warning(self):
        report = validate_content(ENTRY_MANUAL_FACT_SET, _content(
            _fact("reversed", amount="19.99", currency="USD", valid_from="2020-12-31", valid_to="2020-01-01"),
        ))
        self.assertEqual(
            [(f.code, f.path, f.severity) for f in report.findings],
            [("fact_valid_range", "facts[0].valid_to", "blocking")],
        )


class TestPublishWithAnExpiredRow(FactRowReportFixtures):
    def fact_set(self, ctx=None, rows=None):
        ctx = ctx or self.ctx
        entry = service.create_entry(
            ctx, entry_type=ENTRY_MANUAL_FACT_SET, display_name=f"Acme Charge facts {uuid.uuid4().hex[:6]}",
            content={
                "data_source_entry_id": self.published_data_source(),
                "facts": rows or [
                    _fact("price-now", amount="19.99", currency="USD", **IN_FORCE),
                    _fact("price-old", amount="24.99", currency="USD", **EXPIRED),
                ],
            },
        )
        return entry["id"], entry["draft"]["revision"]

    def warnings(self, report):
        return [(f["code"], f["path"]) for f in report["warnings"]]

    def test_the_owner_sees_the_warning_and_publishes_after_acknowledging_it(self):
        fact_set_id, revision = self.fact_set()

        validated = self.command(fact_set_id, "validate", revision)
        self.assertEqual(validated.status_code, 200, validated.text)
        self.assertEqual(validated.json()["blocking"], [])
        self.assertEqual(self.warnings(validated.json()), [("fact_expired", "facts[1].valid_to")])

        unacknowledged = self.command(fact_set_id, "publish", revision)
        self.assertEqual(unacknowledged.status_code, 422, unacknowledged.text)
        self.assertEqual(unacknowledged.json()["code"], "library_warnings_unacknowledged")
        self.assertEqual(
            self.warnings(unacknowledged.json()["report"]), [("fact_expired", "facts[1].valid_to")],
        )
        entry = service.get_entry(self.ctx, fact_set_id)
        self.assertIsNone(entry["current_version_id"])
        self.assertEqual(entry["draft"]["state"], DRAFT_READY, "a warning never fails the draft")

        published = self.command(fact_set_id, "publish", revision, acknowledged_warnings=["fact_expired"])
        self.assertEqual(published.status_code, 201, published.text)
        self.assertEqual(len(published.json()["content"]["facts"]), 2, "the expired row is published as written")

    def test_rows_all_in_force_or_scheduled_publish_without_a_warning(self):
        fact_set_id, revision = self.fact_set(rows=[
            _fact("price-now", amount="19.99", currency="USD", **IN_FORCE),
            _fact("price-next", amount="17.99", currency="USD", **NOT_YET),
        ])
        published = self.command(fact_set_id, "publish", revision)
        self.assertEqual(published.status_code, 201, published.text)

    def test_a_member_cannot_publish_even_acknowledging_the_warning(self):
        fact_set_id, revision = self.fact_set()
        member = self._make_context(role=ROLE_MEMBER, workspace_id=self.ctx.workspace_id)
        self.act_as(member)
        denied = self.command(fact_set_id, "publish", revision, acknowledged_warnings=["fact_expired"])
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertIsNone(service.get_entry(self.ctx, fact_set_id)["current_version_id"])

    def test_another_workspace_cannot_see_or_publish_the_fact_set(self):
        fact_set_id, revision = self.fact_set()
        other = self._make_context(role=ROLE_OWNER, actor="user_b")
        self.act_as(other)
        self.assertEqual(self.command(fact_set_id, "validate", revision).status_code, 404)
        self.assertEqual(
            self.command(fact_set_id, "publish", revision, acknowledged_warnings=["fact_expired"]).status_code,
            404,
        )
