"""#70: a TruthResolutionPolicy published through the Creative Library drives Localization.

`rules[].source_priority` lists DataSource *entry* ids. Library publish
resolves each one to the entry's current version (the stored dependency
edge stays version-to-version); Truth resolution reads the entry's current
effective version, picks the adapter from its `adapter_kind`, and records
that version in `truth_source_attempts`. The start-preparation request names
only the policy and, for a `manual` source, the fact set.

Before #70 no policy that passed publish could be resolved: Truth read the
list as adapter kinds, while publish only accepted DataSource version ids.

Every DataSource, policy and fact set here goes through `service.publish`
and preparation goes through the v1 Localization API; no LibraryVersion row
is inserted directly.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    LibraryDependencyRecord,
    LocalizationBatchRecord,
    MarketPolicyVersionRecord,
    TruthSourceAttemptRecord,
)
from app.library import service
from app.library.contracts import ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY
from tests import test_localization_routes as legacy_routes
from tests.support.library_fixtures import LibraryFixtures
from tests.support.truth_sources import (
    publish_data_source,
    publish_truth_policy,
    republish_data_source,
    truth_policy_content,
)

_TAGS = itertools.count(1)
_MANUAL_SOURCE = {"adapter_kind": "manual", "execution_mode": "real"}
_RULES = {"price": "block", "availability": "skip"}


class TestTruthPolicySourcesThroughPublish(LibraryFixtures):
    def setUp(self) -> None:
        self.harness = legacy_routes.TestLocalizationRoutes(methodName="test_create_draft")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.owner = self.harness.ctx
        self.client = self.harness.client
        self.tag = f"trp70-{next(_TAGS)}"

    # ------------------------------------------------------------------
    # fixtures: Library publish and the v1 Localization API only
    # ------------------------------------------------------------------

    def _data_source(self, name: str = "Manual prices") -> tuple[str, str]:
        return publish_data_source(self.owner, f"{name} {self.tag}", _MANUAL_SOURCE)

    def _policy(self, *source_priority: str) -> str:
        _entry_id, version_id = publish_truth_policy(
            self.owner, f"Truth policy {self.tag}", truth_policy_content(source_priority, _RULES)
        )
        return version_id

    def _batch(self) -> dict:
        draft = self.harness._create_draft_via_http(f"{self.tag}-{next(_TAGS)}")
        draft["recipe_hash"] = self.harness._analysis_recipe_hash(draft["batch_id"])
        self.harness._seed_analysis(draft["winner"], draft["recipe_hash"], draft["batch_id"])
        with tenant_session(self.owner) as session:
            draft["country"] = session.get(
                MarketPolicyVersionRecord, draft["market_policy_version_id"]
            ).country
        return draft

    def _fact_set(self, data_source_entry_id: str, country: str) -> str:
        now = datetime.now(timezone.utc)
        window = {
            "country_code": country,
            "valid_from": (now - timedelta(days=1)).isoformat(),
            "valid_to": (now + timedelta(days=365)).isoformat(),
            "evidence_source": "Acme Charge price list",
        }
        entry = service.create_entry(
            self.owner,
            entry_type=ENTRY_MANUAL_FACT_SET,
            display_name=f"Facts {self.tag}",
            content={
                "data_source_entry_id": data_source_entry_id,
                "facts": [
                    {"fact_id": "price-1", "fact_type": "price", "amount": "19.99",
                     "currency": "USD", **window},
                    {"fact_id": "availability-1", "fact_type": "availability",
                     "available": True, **window},
                ],
            },
        )
        return service.publish(self.owner, entry["id"], entry["draft"]["revision"])["id"]

    def _start(self, draft: dict, policy_version_id: str, fact_set_version_id: str):
        batch_id = draft["batch_id"]
        revision = self.client.get(f"/api/v1/localization/batches/{batch_id}").json()["revision"]
        return self.client.post(
            f"/api/v1/localization/batches/{batch_id}/start-preparation",
            json={
                "truth_resolution_policy_version_id": policy_version_id,
                "manual_fact_set_version_id": fact_set_version_id,
                "analysis_recipe_hash": draft["recipe_hash"],
                "analysis_provider": "mock-vision",
                "analysis_model": "mock-vision-1",
                "analysis_adapter_version": "mock-v1",
            },
            headers={"Idempotency-Key": f"{batch_id}-start", "If-Match": str(revision)},
        )

    def _attempted_sources(self, truth_package_id: str) -> set[tuple[str, str]]:
        with tenant_session(self.owner) as session:
            return {
                (attempt.data_source_version_id, attempt.adapter_kind)
                for attempt in session.query(TruthSourceAttemptRecord).filter_by(
                    workspace_id=self.owner.workspace_id, truth_package_id=truth_package_id
                )
            }

    def _frozen_source_versions(self, batch_id: str) -> list[str]:
        with tenant_session(self.owner) as session:
            batch = session.get(LocalizationBatchRecord, batch_id)
            return batch.frozen_recipe_json["truth_data_source_version_ids"]

    def _policy_edges(self, policy_version_id: str) -> list[tuple[str, str, str]]:
        with tenant_session(self.owner) as session:
            return [
                tuple(row)
                for row in session.execute(
                    select(
                        LibraryDependencyRecord.to_version_id,
                        LibraryDependencyRecord.relation,
                        LibraryDependencyRecord.path,
                    ).where(LibraryDependencyRecord.from_version_id == policy_version_id)
                ).all()
            ]

    def _publish_blocking(self, ctx, content: dict) -> list[tuple[str, str]]:
        entry = service.create_entry(
            ctx, entry_type=ENTRY_TRUTH_RESOLUTION_POLICY, display_name=f"Rejected {self.tag}",
            content=content,
        )
        with self.assertRaises(service.LibraryValidationError) as raised:
            service.publish(ctx, entry["id"], entry["draft"]["revision"])
        return [(f["code"], f["path"]) for f in raised.exception.report["blocking"]]

    # ------------------------------------------------------------------
    # the published path works end to end
    # ------------------------------------------------------------------

    def test_a_published_policy_resolves_through_start_preparation(self):
        source_entry_id, source_version_id = self._data_source()
        policy_version_id = self._policy(source_entry_id)
        self.assertEqual(
            self._policy_edges(policy_version_id),
            [(source_version_id, "truth_policy_data_source", "rules[0].source_priority[0]")],
        )
        draft = self._batch()

        response = self._start(
            draft, policy_version_id, self._fact_set(source_entry_id, draft["country"])
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["truth_status"], "full")
        self.assertEqual(
            self._attempted_sources(body["truth_package_id"]), {(source_version_id, "manual")}
        )
        self.assertEqual(self._frozen_source_versions(draft["batch_id"]), [source_version_id])
        self.assert_stored_widths(self.owner)

    def test_a_republished_data_source_is_read_by_the_same_policy_version(self):
        source_entry_id, first_version_id = self._data_source()
        policy_version_id = self._policy(source_entry_id)
        first = self._batch()
        started = self._start(
            first, policy_version_id, self._fact_set(source_entry_id, first["country"])
        )
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(
            self._attempted_sources(started.json()["truth_package_id"]),
            {(first_version_id, "manual")},
        )

        second_version_id = republish_data_source(
            self.owner, source_entry_id, {**_MANUAL_SOURCE, "config": {"note": "re-checked"}}
        )
        self.assertNotEqual(second_version_id, first_version_id)

        # The policy is not republished: it is still the effective version.
        self.assertEqual(service.get_version(self.owner, policy_version_id)["state"], "effective")
        second = self._batch()
        restarted = self._start(
            second, policy_version_id, self._fact_set(source_entry_id, second["country"])
        )

        self.assertEqual(restarted.status_code, 200, restarted.text)
        self.assertEqual(
            self._attempted_sources(restarted.json()["truth_package_id"]),
            {(second_version_id, "manual")},
        )
        self.assertEqual(self._frozen_source_versions(second["batch_id"]), [second_version_id])

    # ------------------------------------------------------------------
    # withdrawn and archived DataSources
    # ------------------------------------------------------------------

    def test_withdrawing_the_data_source_lists_the_policy_and_preparation_answers_422(self):
        source_entry_id, source_version_id = self._data_source()
        policy_version_id = self._policy(source_entry_id)
        draft = self._batch()
        fact_set_version_id = self._fact_set(source_entry_id, draft["country"])

        preview = self.client.post(f"/api/v1/library/versions/{source_version_id}/withdraw-impact")
        self.assertEqual(preview.status_code, 201, preview.text)
        self.assertIn(
            (policy_version_id, ENTRY_TRUTH_RESOLUTION_POLICY),
            {(d["version_id"], d["entry_type"]) for d in preview.json()["projection"]["dependents"]},
        )

        service.withdraw_version(self.owner, source_version_id, reason="Supplier feed retired")

        response = self._start(draft, policy_version_id, fact_set_version_id)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "truth_configuration_invalid")
        self.assertIsNone(self._frozen_truth_package(draft["batch_id"]))

        self.assertEqual(
            self._publish_blocking(self.owner, truth_policy_content([source_entry_id], _RULES)),
            [("dependency_withdrawn", f"rules[{i}].source_priority[0]") for i in range(len(_RULES))],
        )

    def test_an_archived_data_source_blocks_publish_and_preparation_answers_422(self):
        source_entry_id, _version_id = self._data_source()
        policy_version_id = self._policy(source_entry_id)
        draft = self._batch()
        fact_set_version_id = self._fact_set(source_entry_id, draft["country"])

        service.archive_entry(
            self.owner, source_entry_id,
            expected_revision=service.get_entry(self.owner, source_entry_id)["draft"]["revision"],
        )

        response = self._start(draft, policy_version_id, fact_set_version_id)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "truth_configuration_invalid")
        self.assertIsNone(self._frozen_truth_package(draft["batch_id"]))

        self.assertEqual(
            self._publish_blocking(self.owner, truth_policy_content([source_entry_id], _RULES)),
            [("dependency_archived", f"rules[{i}].source_priority[0]") for i in range(len(_RULES))],
        )

    def _frozen_truth_package(self, batch_id: str) -> str | None:
        with tenant_session(self.owner) as session:
            return session.get(LocalizationBatchRecord, batch_id).truth_package_id

    # ------------------------------------------------------------------
    # publish-time negatives
    # ------------------------------------------------------------------

    def test_another_workspaces_data_source_cannot_be_published(self):
        foreign = self.harness._make_context(role="owner")
        foreign_entry_id, foreign_version_id = publish_data_source(
            foreign, f"Foreign source {self.tag}", _MANUAL_SOURCE
        )
        for reference in (foreign_entry_id, foreign_version_id):
            with self.subTest(reference=reference):
                self.assertEqual(
                    self._publish_blocking(
                        self.owner, truth_policy_content([reference], {"price": "block"})
                    ),
                    [("dependency_not_found", "rules[0].source_priority[0]")],
                )

    def test_an_unpublished_or_wrong_type_reference_cannot_be_published(self):
        unpublished = service.create_entry(
            self.owner, entry_type="data_source", display_name=f"Draft source {self.tag}",
            content=_MANUAL_SOURCE,
        )
        policy_entry_id = service.get_version(self.owner, self._policy(self._data_source()[0]))[
            "entry_id"
        ]
        self.assertEqual(
            self._publish_blocking(
                self.owner,
                truth_policy_content([unpublished["id"], policy_entry_id], {"price": "block"}),
            ),
            [
                ("dependency_not_found", "rules[0].source_priority[0]"),
                ("dependency_type_mismatch", "rules[0].source_priority[1]"),
            ],
        )

    def test_a_member_cannot_publish_a_policy(self):
        source_entry_id, _version_id = self._data_source()
        member = self.ctx("member", workspace=self.owner.workspace_id, actor="member_a")
        entry = service.create_entry(
            member, entry_type=ENTRY_TRUTH_RESOLUTION_POLICY, display_name=f"Member {self.tag}",
            content=truth_policy_content([source_entry_id], {"price": "block"}),
        )
        with self.assertRaises(service.LibraryForbidden):
            service.publish(member, entry["id"], entry["draft"]["revision"])

    # ------------------------------------------------------------------
    # legacy forms
    # ------------------------------------------------------------------

    def test_an_adapter_kind_gets_its_own_finding(self):
        self.assertEqual(
            self._publish_blocking(self.owner, truth_policy_content(["mock"], {"price": "block"})),
            [("trp_source_priority_adapter_kind", "rules[0].source_priority[0]")],
        )

    def test_a_legacy_version_id_maps_back_to_its_data_source(self):
        source_entry_id, first_version_id = self._data_source()
        # The pre-#70 form: the only one publish accepted.
        legacy_policy_version_id = self._policy(first_version_id)
        self.assertEqual(
            self._policy_edges(legacy_policy_version_id),
            [(first_version_id, "truth_policy_data_source", "rules[0].source_priority[0]")],
        )

        second_version_id = republish_data_source(
            self.owner, source_entry_id, {**_MANUAL_SOURCE, "config": {"note": "re-checked"}}
        )
        # A superseded version id still names the DataSource: publish maps
        # it to the entry's current version, as Truth resolution does.
        self.assertEqual(
            self._policy_edges(self._policy(first_version_id)),
            [(second_version_id, "truth_policy_data_source", "rules[0].source_priority[0]")],
        )

        draft = self._batch()
        response = self._start(
            draft, legacy_policy_version_id, self._fact_set(source_entry_id, draft["country"])
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self._attempted_sources(response.json()["truth_package_id"]),
            {(second_version_id, "manual")},
        )
