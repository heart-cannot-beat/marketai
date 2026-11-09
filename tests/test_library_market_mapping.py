"""MarketSourceMapping publish checks the DataSource version it names (#61).

A mapping's ``execution_mode`` must match the referenced DataSourceVersion
(creative library contract §1.9 / §18: Mock and real never mix, and the
source decides the mode). The reference must be a DataSource version of the
same Workspace, and publish records it as a dependency edge so withdrawing
that version lists the mapping in its impact preview.

Everything here goes through ``service.publish``; nothing inserts a
LibraryVersion row directly.
"""
from __future__ import annotations

import unittest

from sqlalchemy import select

# library_fixtures sets the test database env before any app.* import.
from tests.support.library_fixtures import LibraryFixtures
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import LibraryDependencyRecord
from app.library import command_impacts, service
from app.library.contracts import (
    ENTRY_DATA_SOURCE,
    ENTRY_MARKET_MAPPING,
    ENTRY_OFFER,
    ENTRY_PRODUCT,
)
from app.platform.tenancy import TenantContext

MOCK_SOURCE = {"adapter_kind": "mock", "execution_mode": "mock"}
REAL_SOURCE = {"adapter_kind": "manual", "execution_mode": "real"}


class MarketMappingDataSourceTests(LibraryFixtures):
    def setUp(self) -> None:
        self.member = self.ctx()
        self.owner = self.owner_ctx(self.member)
        self.offer_version_id = self._offer_version_id(self.member, self.owner)

    # -- helpers ---------------------------------------------------------

    def _offer_version_id(self, member: TenantContext, owner: TenantContext) -> str:
        _ref_entry, ref_version = self._reference_version(member)
        _group_entry, group_version = self._group_version(member, ref_version["id"])
        _product_entry, product_version = self._publish(
            member, ENTRY_PRODUCT, "Acme Charge Mini",
            self._product_content(ref_version["id"], group_version["id"]),
        )
        _offer_entry, offer_version = self._publish(
            owner, ENTRY_OFFER, "Acme Charge Mini single", self._offer_content(product_version["id"]),
        )
        return offer_version["id"]

    def _data_source_version_id(self, owner: TenantContext, content: dict) -> str:
        _entry_id, version = self._publish(owner, ENTRY_DATA_SOURCE, "Acme Charge source", content)
        return version["id"]

    def _mapping_content(self, data_source_version_id: str, execution_mode: str) -> dict:
        return {
            "offer_version_id": self.offer_version_id,
            "data_source_version_id": data_source_version_id,
            "external_object": {"kind": "variant", "id": "variant-acme-mini-us"},
            "market": "US",
            "execution_mode": execution_mode,
        }

    def _create_mapping(self, content: dict) -> dict:
        return service.create_entry(
            self.member, entry_type=ENTRY_MARKET_MAPPING, display_name="Acme Charge Mini US", content=content,
        )

    def _publish_rejected(self, content: dict) -> list[tuple[str, str]]:
        """Publish must be refused; returns the blocking ``(code, path)`` pairs."""
        entry = self._create_mapping(content)
        with self.assertRaises(service.LibraryValidationError) as caught:
            service.publish(self.member, entry["id"], entry["draft"]["revision"])
        after = service.get_entry(self.member, entry["id"])
        self.assertEqual(after["versions"], [])
        self.assertIsNone(after["current_version_id"])
        return [(item["code"], item["path"]) for item in caught.exception.report["blocking"]]

    def _edges(self, ctx: TenantContext, version_id: str) -> set[tuple[str, str, str]]:
        with tenant_session(ctx) as session:
            rows = session.scalars(
                select(LibraryDependencyRecord).where(LibraryDependencyRecord.from_version_id == version_id)
            ).all()
            return {(row.to_version_id, row.relation, row.path) for row in rows}

    # -- execution mode --------------------------------------------------

    def test_real_mapping_naming_a_mock_data_source_version_is_rejected(self) -> None:
        mock_version_id = self._data_source_version_id(self.owner, MOCK_SOURCE)
        blocking = self._publish_rejected(self._mapping_content(mock_version_id, "real"))
        self.assertEqual(blocking, [("mapping_execution_mode_mismatch", "execution_mode")])

    def test_mock_mapping_naming_a_real_data_source_version_is_rejected(self) -> None:
        real_version_id = self._data_source_version_id(self.owner, REAL_SOURCE)
        blocking = self._publish_rejected(self._mapping_content(real_version_id, "mock"))
        self.assertEqual(blocking, [("mapping_execution_mode_mismatch", "execution_mode")])

    def test_publish_impact_preview_reports_the_mode_mismatch(self) -> None:
        mock_version_id = self._data_source_version_id(self.owner, MOCK_SOURCE)
        entry = self._create_mapping(self._mapping_content(mock_version_id, "real"))
        preview = command_impacts.preview_publish_impact(self.member, entry["id"])
        self.assertFalse(preview["projection"]["validation_ok"])
        self.assertEqual(
            [(item["code"], item["path"]) for item in preview["projection"]["blocking"]],
            [("mapping_execution_mode_mismatch", "execution_mode")],
        )

    # -- the reference itself --------------------------------------------

    def test_data_source_version_of_another_workspace_is_rejected(self) -> None:
        other_owner = self.owner_ctx(self.ctx())
        foreign_version_id = self._data_source_version_id(other_owner, REAL_SOURCE)
        blocking = self._publish_rejected(self._mapping_content(foreign_version_id, "real"))
        self.assertEqual(blocking, [("dependency_not_found", "data_source_version_id")])

    def test_data_source_version_that_does_not_exist_is_rejected(self) -> None:
        blocking = self._publish_rejected(self._mapping_content("libver_missing", "real"))
        self.assertEqual(blocking, [("dependency_not_found", "data_source_version_id")])

    def test_version_of_another_entry_type_is_rejected(self) -> None:
        blocking = self._publish_rejected(self._mapping_content(self.offer_version_id, "real"))
        self.assertEqual(blocking, [("dependency_type_mismatch", "data_source_version_id")])

    def test_withdrawn_data_source_version_is_rejected(self) -> None:
        real_version_id = self._data_source_version_id(self.owner, REAL_SOURCE)
        service.withdraw_version(self.owner, real_version_id, reason="Source retired")
        blocking = self._publish_rejected(self._mapping_content(real_version_id, "real"))
        self.assertEqual(blocking, [("dependency_withdrawn", "data_source_version_id")])

    # -- matching mode publishes with the edge ----------------------------

    def test_matching_mode_publishes_with_a_data_source_edge(self) -> None:
        for source, mode in ((REAL_SOURCE, "real"), (MOCK_SOURCE, "mock")):
            with self.subTest(execution_mode=mode):
                source_version_id = self._data_source_version_id(self.owner, source)
                entry = self._create_mapping(self._mapping_content(source_version_id, mode))
                version = service.publish(self.member, entry["id"], entry["draft"]["revision"])
                self.assertEqual(version["state"], "effective")
                self.assertEqual(version["content"]["data_source_version_id"], source_version_id)
                self.assertEqual(
                    self._edges(self.member, version["id"]),
                    {
                        (self.offer_version_id, "mapping_offer", "offer_version_id"),
                        (source_version_id, "mapping_data_source", "data_source_version_id"),
                    },
                )

    def test_withdrawing_the_data_source_version_lists_the_mapping(self) -> None:
        source_version_id = self._data_source_version_id(self.owner, REAL_SOURCE)
        entry = self._create_mapping(self._mapping_content(source_version_id, "real"))
        mapping = service.publish(self.member, entry["id"], entry["draft"]["revision"])

        preview = command_impacts.preview_withdraw_impact(self.owner, source_version_id)
        dependents = {item["version_id"]: item for item in preview["projection"]["dependents"]}
        self.assertIn(mapping["id"], dependents)
        self.assertEqual(dependents[mapping["id"]]["entry_type"], ENTRY_MARKET_MAPPING)
        self.assertTrue(dependents[mapping["id"]]["will_block"])
        self.assertEqual(
            [item["version_id"] for item in service.impact_analysis(self.owner, source_version_id)["dependents"]],
            [mapping["id"]],
        )

        service.withdraw_version(self.owner, source_version_id, reason="Source retired")
        after = service.get_entry(self.member, entry["id"])
        self.assertEqual(after["versions"][0]["eligibility"], "blocked")
        # The published mapping itself is never rewritten.
        self.assertEqual(after["versions"][0]["content"]["data_source_version_id"], source_version_id)
        self.assertEqual(after["versions"][0]["content"]["execution_mode"], "real")


if __name__ == "__main__":
    unittest.main()
