"""#100: a WorkspacePromptPolicy names its allowed templates by entry.

``allowed_template_entry_ids`` lists DimensionTemplate *entry* ids. Publish
resolves each one to the entry's current version, so the stored
``policy_template`` edge stays version-to-version, the way a
TruthResolutionPolicy names its DataSources (#70). A template *version* id --
what a draft written to get past the old failure may hold -- maps back to its
entry and resolves the same way. The relation is registered in
``ENTRY_RESOLVED_RELATIONS``, so impact previews read its edges by entry (#88).

Before #100 publish looked each entry id up as a version id: every policy that
named a template was blocked with ``dependency_not_found``.

Templates, DataSources and policies publish through ``service.publish``; no
LibraryVersion row is inserted directly.
"""
from __future__ import annotations

import copy

from sqlalchemy import select

# library_fixtures sets the test database env before any app.* import.
from tests.support.library_fixtures import LibraryFixtures
from tests.support.truth_sources import publish_data_source

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import LibraryDependencyRecord
from app.library import command_impacts, service
from app.library.contracts import (
    ENTRY_DIMENSION_TEMPLATE,
    ENTRY_PROMPT_POLICY,
    ENTRY_RESOLVED_RELATIONS,
    REL_POLICY_TEMPLATE,
)
from app.platform.tenancy import TenantContext

TEMPLATE_CONTENT = {
    "dimensions": [{
        "id": "dim-color", "name": "Color",
        "values": [
            {"id": "val-red", "name": "Red", "positive_fragments": ["vibrant red"], "negative_fragments": []},
            {"id": "val-blue", "name": "Blue", "positive_fragments": ["deep blue"], "negative_fragments": []},
        ],
    }],
    "constraints": [],
    "concept_key_mappings": [],
    "combination_cardinality": "one_per_dimension",
}


def policy_content(*template_ids: str) -> dict:
    return {
        "registered_tokens": [{"name": "product_name", "description": "Product name", "required": True}],
        "locked_phrases": [],
        "brand_tone": {"style": "professional"},
        "allowed_template_entry_ids": list(template_ids),
        "optional_clauses": [],
    }


def path(index: int) -> str:
    return f"allowed_template_entry_ids[{index}]"


class PolicyTemplateFixtures(LibraryFixtures):
    def setUp(self) -> None:
        self.member = self.ctx()
        self.owner = self.owner_ctx(self.member)

    def publish_template(self, ctx: TenantContext, name: str = "Acme Charge colors") -> tuple[str, str]:
        entry_id, version = self._publish(ctx, ENTRY_DIMENSION_TEMPLATE, name, copy.deepcopy(TEMPLATE_CONTENT))
        return entry_id, version["id"]

    def republish_template(self, ctx: TenantContext, entry_id: str, value_name: str) -> str:
        """Rename the first colour and publish; the old version is superseded."""
        content = copy.deepcopy(TEMPLATE_CONTENT)
        content["dimensions"][0]["values"][0]["name"] = value_name
        return self._republish(ctx, entry_id, content)

    def publish_policy(self, ctx: TenantContext, *template_ids: str, name: str = "Acme Charge tone") -> tuple[str, str]:
        """Validate, then publish; ``_publish`` fails the test on any blocking finding."""
        entry_id, version = self._publish(ctx, ENTRY_PROMPT_POLICY, name, policy_content(*template_ids))
        return entry_id, version["id"]

    def republish_policy(self, ctx: TenantContext, entry_id: str, *template_ids: str) -> str:
        content = {**policy_content(*template_ids), "brand_tone": {"style": "playful"}}
        return self._republish(ctx, entry_id, content)

    def publish_blocking(self, ctx: TenantContext, *template_ids: str) -> list[tuple[str, str]]:
        entry = service.create_entry(
            ctx, entry_type=ENTRY_PROMPT_POLICY, display_name="Rejected tone", content=policy_content(*template_ids),
        )
        with self.assertRaises(service.LibraryValidationError) as raised:
            service.publish(ctx, entry["id"], entry["draft"]["revision"])
        return [(f["code"], f["path"]) for f in raised.exception.report["blocking"]]

    @staticmethod
    def _republish(ctx: TenantContext, entry_id: str, content: dict) -> str:
        revision = service.get_entry(ctx, entry_id)["draft"]["revision"]
        draft = service.save_draft(ctx, entry_id, content, expected_revision=revision)
        return service.publish(ctx, entry_id, draft["revision"])["id"]

    def edges_from(self, ctx: TenantContext, version_id: str) -> set[tuple[str, str, str]]:
        with tenant_session(ctx) as session:
            rows = session.scalars(
                select(LibraryDependencyRecord).where(LibraryDependencyRecord.from_version_id == version_id)
            ).all()
            return {(row.to_version_id, row.relation, row.path) for row in rows}

    @staticmethod
    def listed(payload: dict) -> set[str]:
        body = payload.get("projection", payload)
        return {item["version_id"] for item in body["dependents"]}

    def eligibility(self, version_id: str) -> str:
        return service.get_version(self.owner, version_id)["eligibility"]


class TestPublishResolvesTemplateEntries(PolicyTemplateFixtures):
    def test_the_relation_is_registered_as_entry_resolved(self):
        self.assertIn(REL_POLICY_TEMPLATE, ENTRY_RESOLVED_RELATIONS)

    def test_a_policy_naming_template_entries_publishes_with_edges_to_their_current_versions(self):
        colors_entry, colors_v1 = self.publish_template(self.owner)
        finishes_entry, finishes_v1 = self.publish_template(self.owner, "Acme Charge finishes")

        policy_entry, policy_v1 = self.publish_policy(self.owner, colors_entry, finishes_entry)

        self.assertEqual(self.edges_from(self.owner, policy_v1), {
            (colors_v1, REL_POLICY_TEMPLATE, path(0)),
            (finishes_v1, REL_POLICY_TEMPLATE, path(1)),
        })
        # The content keeps the entry ids it was written with.
        self.assertEqual(
            service.get_version(self.owner, policy_v1)["content"]["allowed_template_entry_ids"],
            [colors_entry, finishes_entry],
        )

        # Once a template is republished, the next policy version resolves the
        # same entry id to the new version; the stored v1 edge is not moved.
        colors_v2 = self.republish_template(self.owner, colors_entry, "Crimson")
        policy_v2 = self.republish_policy(self.owner, policy_entry, colors_entry, finishes_entry)
        self.assertEqual(self.edges_from(self.owner, policy_v2), {
            (colors_v2, REL_POLICY_TEMPLATE, path(0)),
            (finishes_v1, REL_POLICY_TEMPLATE, path(1)),
        })
        self.assertIn((colors_v1, REL_POLICY_TEMPLATE, path(0)), self.edges_from(self.owner, policy_v1))

    def test_a_template_version_id_maps_back_to_its_entry(self):
        colors_entry, colors_v1 = self.publish_template(self.owner)
        # The form a draft may hold if it was written to get past the old failure.
        _legacy_entry, legacy_policy_v1 = self.publish_policy(self.owner, colors_v1, name="Legacy tone")
        self.assertEqual(self.edges_from(self.owner, legacy_policy_v1), {(colors_v1, REL_POLICY_TEMPLATE, path(0))})

        # A superseded version id still names the template: publish maps it to
        # the entry's current version, as it does an entry id.
        colors_v2 = self.republish_template(self.owner, colors_entry, "Crimson")
        _entry, policy_v1 = self.publish_policy(self.owner, colors_v1, name="Superseded id tone")
        self.assertEqual(self.edges_from(self.owner, policy_v1), {(colors_v2, REL_POLICY_TEMPLATE, path(0))})

        # The entry id and one of its version ids name the same template: one stored edge.
        _entry, both_v1 = self.publish_policy(self.owner, colors_entry, colors_v1, name="Both forms tone")
        self.assertEqual(
            {(to, relation) for to, relation, _path in self.edges_from(self.owner, both_v1)},
            {(colors_v2, REL_POLICY_TEMPLATE)},
        )


class TestPublishBlocks(PolicyTemplateFixtures):
    def test_another_workspaces_template_cannot_be_named(self):
        foreign = self.owner_ctx(self.ctx(), actor="owner_b")
        foreign_entry, foreign_v1 = self.publish_template(foreign, "Brightvolt colors")

        for reference in (foreign_entry, foreign_v1):
            with self.subTest(reference=reference):
                self.assertEqual(
                    self.publish_blocking(self.owner, reference), [("dependency_not_found", path(0))],
                )
        # The template's own Workspace publishes the same reference.
        _entry, policy_v1 = self.publish_policy(foreign, foreign_entry, name="Brightvolt tone")
        self.assertEqual(self.edges_from(foreign, policy_v1), {(foreign_v1, REL_POLICY_TEMPLATE, path(0))})

    def test_an_unpublished_wrong_type_withdrawn_or_archived_template_is_blocked(self):
        unpublished = service.create_entry(
            self.owner, entry_type=ENTRY_DIMENSION_TEMPLATE, display_name="Draft colors",
            content=copy.deepcopy(TEMPLATE_CONTENT),
        )["id"]
        data_source_entry, _source_v1 = publish_data_source(
            self.owner, "Acme Charge price list", {"adapter_kind": "manual", "execution_mode": "real", "config": {}},
        )
        withdrawn_entry, withdrawn_v1 = self.publish_template(self.owner, "Recalled colors")
        service.withdraw_version(self.owner, withdrawn_v1, reason="Palette recalled")
        archived_entry, _archived_v1 = self.publish_template(self.owner, "Retired colors")
        service.archive_entry(
            self.owner, archived_entry,
            expected_revision=service.get_entry(self.owner, archived_entry)["draft"]["revision"],
        )

        self.assertEqual(
            self.publish_blocking(self.owner, unpublished, data_source_entry, withdrawn_entry, archived_entry),
            [
                ("dependency_not_found", path(0)),
                ("dependency_type_mismatch", path(1)),
                ("dependency_withdrawn", path(2)),
                ("dependency_archived", path(3)),
            ],
        )
        # The withdrawn version id itself maps to the same entry and is blocked alike.
        self.assertEqual(self.publish_blocking(self.owner, withdrawn_v1), [("dependency_withdrawn", path(0))])

    def test_a_member_sees_the_policy_validate_but_cannot_publish_it(self):
        colors_entry, _colors_v1 = self.publish_template(self.owner)
        entry = service.create_entry(
            self.member, entry_type=ENTRY_PROMPT_POLICY, display_name="Member tone",
            content=policy_content(colors_entry),
        )
        report = service.validate_draft(self.member, entry["id"], expected_revision=entry["draft"]["revision"])
        self.assertTrue(report["ok"], report["blocking"])

        with self.assertRaises(service.LibraryForbidden):
            service.publish(self.member, entry["id"], entry["draft"]["revision"])
        self.assertIsNone(service.get_entry(self.owner, entry["id"])["current_version_id"])


class TestImpactPreviewReadsTheTemplateByEntry(PolicyTemplateFixtures):
    def test_withdrawing_the_republished_template_lists_the_policy_published_on_the_old_version(self):
        colors_entry, colors_v1 = self.publish_template(self.owner)
        _policy_entry, policy_v1 = self.publish_policy(self.owner, colors_entry)
        colors_v2 = self.republish_template(self.owner, colors_entry, "Crimson")
        # The stored edge still names the version current at publish.
        self.assertEqual(self.edges_from(self.owner, policy_v1), {(colors_v1, REL_POLICY_TEMPLATE, path(0))})

        preview = command_impacts.preview_withdraw_impact(self.owner, colors_v2)
        self.assertEqual(self.listed(preview), {policy_v1})
        self.assertEqual(preview["projection"]["newly_blocked_count"], 1)
        self.assertEqual(self.listed(service.impact_analysis(self.owner, colors_v2)), {policy_v1})

        result = service.withdraw_version(
            self.owner, colors_v2, reason="Palette recalled",
            impact_id=preview["impact_id"], impact_hash=preview["impact_hash"],
        )
        self.assertEqual(self.listed(result), {policy_v1})
        self.assertEqual(self.eligibility(policy_v1), "blocked")
        self.assertEqual(self.edges_from(self.owner, policy_v1), {(colors_v1, REL_POLICY_TEMPLATE, path(0))})

    def test_withdrawing_the_superseded_version_blocks_no_policy_published_on_the_new_one(self):
        colors_entry, colors_v1 = self.publish_template(self.owner)
        _early_entry, early_policy = self.publish_policy(self.owner, colors_entry, name="Early tone")
        colors_v2 = self.republish_template(self.owner, colors_entry, "Crimson")
        _late_entry, late_policy = self.publish_policy(self.owner, colors_entry, name="Late tone")
        self.assertEqual(self.edges_from(self.owner, late_policy), {(colors_v2, REL_POLICY_TEMPLATE, path(0))})

        # The current version stands for the entry and reaches both policies.
        self.assertEqual(
            self.listed(command_impacts.preview_withdraw_impact(self.owner, colors_v2)), {early_policy, late_policy},
        )

        # v1 is superseded: withdrawing it leaves the entry resolving to v2,
        # so only the policy whose edge names v1 is listed.
        preview = command_impacts.preview_withdraw_impact(self.owner, colors_v1)
        self.assertEqual(self.listed(preview), {early_policy})
        service.withdraw_version(
            self.owner, colors_v1, reason="Old palette recalled",
            impact_id=preview["impact_id"], impact_hash=preview["impact_hash"],
        )
        self.assertEqual(self.eligibility(early_policy), "blocked")
        self.assertEqual(self.eligibility(late_policy), "eligible")
        self.assertEqual(service.get_version(self.owner, colors_v2)["state"], "effective")

    def test_another_workspaces_policies_never_appear(self):
        colors_entry, _colors_v1 = self.publish_template(self.owner)
        _entry, policy_v1 = self.publish_policy(self.owner, colors_entry)
        colors_v2 = self.republish_template(self.owner, colors_entry, "Crimson")

        foreign = self.owner_ctx(self.ctx(), actor="owner_b")
        foreign_entry, _foreign_v1 = self.publish_template(foreign, "Brightvolt colors")
        _entry, foreign_policy = self.publish_policy(foreign, foreign_entry, name="Brightvolt tone")
        self.republish_template(foreign, foreign_entry, "Crimson")

        self.assertEqual(self.listed(command_impacts.preview_withdraw_impact(self.owner, colors_v2)), {policy_v1})
        with self.assertRaises(service.LibraryNotFound):
            command_impacts.preview_withdraw_impact(foreign, colors_v2)

        service.withdraw_version(self.owner, colors_v2, reason="Palette recalled")
        self.assertEqual(service.get_version(foreign, foreign_policy)["eligibility"], "eligible")
