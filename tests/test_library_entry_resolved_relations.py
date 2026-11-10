"""#88: impact previews read an entry-resolved dependency by entry.

Some content names a target *entry* that publish resolves to the entry's
current version: a TruthResolutionPolicy's ``source_priority`` (#70), a
ManualFactSet's ``data_source_entry_id`` (#64) and its rows'
``offer_entry_id`` (#54), a DimensionSchema's ``product_entry_id``, a
WorkspacePromptPolicy's ``allowed_template_entry_ids`` (#100). The
stored edge points at the version current at publish and is never rewritten,
so after the target is republished a Withdraw preview of the *new* version
used to list nothing. ``contracts.ENTRY_RESOLVED_RELATIONS`` registers these
relations. When the shared reverse walk (``app.library.dependents``) reaches
an entry's current effective version, an edge of one of them to any version
of the entry counts, since every such dependent resolves to that version. A
superseded version is matched by its exact edges only: withdrawing it changes
nothing the entry resolves to. Every other relation follows the exact version
it pins.

DataSources, Offers, policies, fact sets and mappings publish through
``service.publish``. Only the registry probe inserts LibraryVersion rows
directly: it exercises the publish-time resolver on bare targets of every
relation's type.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

# library_fixtures sets the test database env before any app.* import.
from tests.support.library_fixtures import LibraryFixtures
from tests.support.offers import publish_offer, publish_product, republish_offer
from tests.support.truth_sources import (
    publish_data_source,
    publish_truth_policy,
    republish_data_source,
    truth_policy_content,
)

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    LibraryDependencyRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    new_id,
)
from app.library import command_impacts, contracts, service
from app.library.contracts import (
    ENTRY_MANUAL_FACT_SET,
    ENTRY_MARKET_MAPPING,
    ENTRY_RESOLVED_RELATIONS,
    ENTRY_TRUTH_RESOLUTION_POLICY,
    REL_FACT_SET_DATA_SOURCE,
    REL_FACT_SET_OFFER,
    REL_POLICY_TEMPLATE,
    REL_SCHEMA_PRODUCT,
    REL_TRUTH_POLICY_DATA_SOURCE,
    RELATION_TARGET_TYPES,
    DependencyEdge,
)
from app.platform.tenancy import TenantContext

MANUAL_SOURCE = {"adapter_kind": "manual", "execution_mode": "real", "config": {}}


def _republish(owner: TenantContext, entry_id: str, **changes: object) -> str:
    """Publish the entry's draft with ``changes``; the effective version is superseded."""
    entry = service.get_entry(owner, entry_id)
    draft = service.save_draft(
        owner, entry_id, {**entry["draft"]["content"], **changes},
        expected_revision=entry["draft"]["revision"],
    )
    return service.publish(owner, entry_id, draft["revision"])["id"]


class EntryResolvedFixtures(LibraryFixtures):
    def setUp(self) -> None:
        self.member = self.ctx()
        self.owner = self.owner_ctx(self.member)

    def world(self, owner: TenantContext) -> SimpleNamespace:
        """A DataSource and an Offer, each referenced both by entry and by a pinned version.

        - the TruthResolutionPolicy names the DataSource entry;
        - the ManualFactSet names the DataSource entry and, in its row, the Offer entry;
        - the MarketSourceMapping pins the Offer version and the DataSource version.
        """
        product_entry, product_v1 = publish_product(owner)
        offer_entry, offer_v1 = publish_offer(owner, product_v1, markets=["US"])
        source_entry, source_v1 = publish_data_source(owner, "Acme Charge price list", MANUAL_SOURCE)
        policy_entry, policy_v1 = publish_truth_policy(
            owner, "Acme Charge truth policy", truth_policy_content([source_entry], {"price": "block"}),
        )
        fact_set_entry, fact_set_v1 = self._publish_entry(owner, ENTRY_MANUAL_FACT_SET, "Acme Charge prices", {
            "data_source_entry_id": source_entry,
            "facts": [{
                "fact_id": "price-us", "fact_type": "price", "amount": "19.99", "currency": "USD",
                "country_code": "US", "valid_from": "2026-01-01", "valid_to": "2099-12-31",
                "evidence_source": "Acme Charge price list 2026", "offer_entry_id": offer_entry,
            }],
        })
        _mapping_entry, mapping_v1 = self._publish_entry(owner, ENTRY_MARKET_MAPPING, "Acme Charge US variant", {
            "offer_version_id": offer_v1,
            "data_source_version_id": source_v1,
            "external_object": {"kind": "variant", "id": "variant-acme-65w-us"},
            "market": "US",
            "execution_mode": "real",
        })
        return SimpleNamespace(
            product_entry=product_entry, product_v1=product_v1,
            offer_entry=offer_entry, offer_v1=offer_v1,
            source_entry=source_entry, source_v1=source_v1,
            policy_entry=policy_entry, policy_v1=policy_v1,
            fact_set_entry=fact_set_entry, fact_set_v1=fact_set_v1,
            mapping_v1=mapping_v1,
        )

    def publish_late_fact_set(self, owner: TenantContext, w: SimpleNamespace) -> str:
        """A fact set naming the same DataSource and Offer entries, published now.

        Its edges point at whatever those entries resolve to at this point.
        """
        _entry, version = self._publish_entry(owner, ENTRY_MANUAL_FACT_SET, "Acme Charge prices, later", {
            "data_source_entry_id": w.source_entry,
            "facts": [{
                "fact_id": "price-us-later", "fact_type": "price", "amount": "18.99", "currency": "USD",
                "country_code": "US", "valid_from": "2026-06-01", "valid_to": "2099-12-31",
                "evidence_source": "Acme Charge price list 2026", "offer_entry_id": w.offer_entry,
            }],
        })
        return version

    @staticmethod
    def _publish_entry(owner: TenantContext, entry_type: str, name: str, content: dict) -> tuple[str, str]:
        entry = service.create_entry(owner, entry_type=entry_type, display_name=name, content=content)
        return entry["id"], service.publish(owner, entry["id"], entry["draft"]["revision"])["id"]

    def withdraw_preview(self, ctx: TenantContext, version_id: str) -> dict:
        return command_impacts.preview_withdraw_impact(ctx, version_id)

    @staticmethod
    def listed(payload: dict) -> set[str]:
        """Dependent version ids of an impact payload or a projection."""
        body = payload.get("projection", payload)
        return {item["version_id"] for item in body["dependents"]}

    def edges_from(self, ctx: TenantContext, version_id: str) -> set[tuple[str, str]]:
        with tenant_session(ctx) as session:
            rows = session.scalars(
                select(LibraryDependencyRecord).where(LibraryDependencyRecord.from_version_id == version_id)
            ).all()
            return {(row.to_version_id, row.relation) for row in rows}

    def eligibility(self, version_id: str) -> str:
        return service.get_version(self.owner, version_id)["eligibility"]


class TestEntryResolvedRelationsRegistry(LibraryFixtures):
    """Guard: a relation publish resolves from an entry id must be registered."""

    def test_every_relation_names_its_target_type(self):
        # The probe below walks RELATION_TARGET_TYPES; a relation missing there
        # would escape it.
        relations = {
            value for name, value in vars(contracts).items()
            if name.startswith("REL_") and isinstance(value, str)
        }
        self.assertEqual(relations - set(RELATION_TARGET_TYPES), set())
        self.assertLessEqual(ENTRY_RESOLVED_RELATIONS, set(RELATION_TARGET_TYPES))

    def test_the_registry_lists_exactly_the_relations_publish_resolves_by_entry(self):
        resolved_by_entry = {
            relation for relation in RELATION_TARGET_TYPES if self._resolves_an_entry_id(relation)
        }
        self.assertEqual(
            resolved_by_entry, set(ENTRY_RESOLVED_RELATIONS),
            "service._validate_dependencies resolves these relations from an entry id to the entry's "
            "current version; register them in contracts.ENTRY_RESOLVED_RELATIONS so impact previews "
            "read their edges by entry (#88)",
        )
        self.assertEqual(ENTRY_RESOLVED_RELATIONS, {
            REL_SCHEMA_PRODUCT, REL_FACT_SET_DATA_SOURCE, REL_FACT_SET_OFFER, REL_TRUTH_POLICY_DATA_SOURCE,
            REL_POLICY_TEMPLATE,
        })

    def _resolves_an_entry_id(self, relation: str) -> bool:
        """Does publish turn an edge naming a target *entry* into that entry's current version?"""
        ctx = self.owner_ctx(self.ctx())
        target_type = RELATION_TARGET_TYPES[relation]
        with tenant_session(ctx) as session:
            source = LibraryEntryRecord(
                id=new_id("lib"), workspace_id=ctx.workspace_id,
                entry_type=ENTRY_TRUTH_RESOLUTION_POLICY, display_name="Probe source",
            )
            target = LibraryEntryRecord(
                id=new_id("lib"), workspace_id=ctx.workspace_id,
                entry_type=target_type, display_name="Probe target",
            )
            session.add_all([source, target])
            session.flush()
            version = LibraryVersionRecord(
                id=new_id("libver"), workspace_id=ctx.workspace_id, entry_id=target.id,
                entry_type=target_type, version_no=1, content_json={}, content_hash="0" * 64,
                published_by="probe",
            )
            session.add(version)
            session.flush()
            target.current_version_id = version.id
            session.flush()
            resolved = service._validate_dependencies(
                session, ctx, source, [DependencyEdge(target.id, relation, "probe")], [],
                today=date.today(),
            )
            return [found.id for _edge, found in resolved] == [version.id]


class TestARepublishedDataSource(EntryResolvedFixtures):
    def test_withdrawing_the_new_version_lists_the_policy_and_the_fact_set_that_name_its_entry(self):
        w = self.world(self.owner)
        source_v2 = republish_data_source(self.owner, w.source_entry, {**MANUAL_SOURCE, "config": {"note": "v2"}})

        # The stored edges still point at the version current at publish.
        self.assertIn((w.source_v1, REL_TRUTH_POLICY_DATA_SOURCE), self.edges_from(self.owner, w.policy_v1))
        self.assertIn((w.source_v1, REL_FACT_SET_DATA_SOURCE), self.edges_from(self.owner, w.fact_set_v1))

        preview = self.withdraw_preview(self.owner, source_v2)
        # The mapping pins DataSource v1, so withdrawing v2 does not reach it.
        self.assertEqual(self.listed(preview), {w.policy_v1, w.fact_set_v1})
        self.assertEqual(preview["projection"]["dependent_count"], 2)
        self.assertEqual(preview["projection"]["newly_blocked_count"], 2)
        self.assertEqual(self.listed(service.impact_analysis(self.owner, source_v2)), {w.policy_v1, w.fact_set_v1})

        # v1 is superseded: its preview lists the dependents whose edges name
        # it, the mapping that pins it and, as before #88, the entry-named
        # dependents published while it was current.
        self.assertEqual(
            self.listed(self.withdraw_preview(self.owner, w.source_v1)),
            {w.policy_v1, w.fact_set_v1, w.mapping_v1},
        )

    def test_withdrawing_the_superseded_version_follows_its_exact_edges_only(self):
        w = self.world(self.owner)
        source_v2 = republish_data_source(self.owner, w.source_entry, {**MANUAL_SOURCE, "config": {"note": "v2"}})
        # Published after the republish, these name the entry and resolve to v2.
        policy_v2 = _republish(self.owner, w.policy_entry, **truth_policy_content(
            [w.source_entry], {"price": "block"}, max_staleness_seconds=3600,
        ))
        late_fact_set = self.publish_late_fact_set(self.owner, w)
        self.assertEqual(self.edges_from(self.owner, policy_v2), {(source_v2, REL_TRUTH_POLICY_DATA_SOURCE)})
        self.assertIn((source_v2, REL_FACT_SET_DATA_SOURCE), self.edges_from(self.owner, late_fact_set))

        # The current version stands for the entry and reaches all of them.
        self.assertEqual(
            self.listed(self.withdraw_preview(self.owner, source_v2)),
            {w.policy_v1, policy_v2, w.fact_set_v1, late_fact_set},
        )

        # Withdrawing v1 leaves the entry resolving to v2: only the edges that
        # name v1 count, so the dependents published on v2 are not listed.
        preview = self.withdraw_preview(self.owner, w.source_v1)
        self.assertEqual(self.listed(preview), {w.policy_v1, w.fact_set_v1, w.mapping_v1})
        self.assertEqual(preview["projection"]["dependent_count"], 3)
        self.assertEqual(self.listed(service.impact_analysis(self.owner, w.source_v1)), self.listed(preview))

        result = service.withdraw_version(
            self.owner, w.source_v1, reason="Superseded price list recalled",
            impact_id=preview["impact_id"], impact_hash=preview["impact_hash"],
        )

        self.assertEqual(self.listed(result), self.listed(preview))
        self.assertEqual(self.eligibility(w.policy_v1), "blocked")
        self.assertEqual(self.eligibility(w.fact_set_v1), "blocked")
        self.assertEqual(self.eligibility(w.mapping_v1), "blocked")
        self.assertEqual(self.eligibility(policy_v2), "eligible")
        self.assertEqual(self.eligibility(late_fact_set), "eligible")
        self.assertEqual(service.get_version(self.owner, source_v2)["state"], "effective")

    def test_the_withdraw_blocks_what_its_preview_listed(self):
        w = self.world(self.owner)
        source_v2 = republish_data_source(self.owner, w.source_entry, {**MANUAL_SOURCE, "config": {"note": "v2"}})
        preview = self.withdraw_preview(self.owner, source_v2)

        result = service.withdraw_version(
            self.owner, source_v2, reason="Supplier feed retired",
            impact_id=preview["impact_id"], impact_hash=preview["impact_hash"],
        )

        self.assertEqual(self.listed(result), self.listed(preview))
        self.assertEqual(self.eligibility(w.policy_v1), "blocked")
        self.assertEqual(self.eligibility(w.fact_set_v1), "blocked")
        self.assertEqual(self.eligibility(w.mapping_v1), "eligible")
        # No edge was added or moved.
        self.assertEqual(
            self.edges_from(self.owner, w.policy_v1), {(w.source_v1, REL_TRUTH_POLICY_DATA_SOURCE)},
        )

    def test_the_archive_preview_is_unchanged(self):
        w = self.world(self.owner)
        republish_data_source(self.owner, w.source_entry, {**MANUAL_SOURCE, "config": {"note": "v2"}})

        preview = command_impacts.preview_archive_impact(self.owner, w.source_entry)

        self.assertEqual(self.listed(preview), {w.policy_v1, w.fact_set_v1, w.mapping_v1})


class TestARepublishedOffer(EntryResolvedFixtures):
    def test_withdrawing_the_new_version_lists_the_fact_set_whose_rows_name_its_entry(self):
        w = self.world(self.owner)
        offer_v2 = republish_offer(self.owner, w.offer_entry, {
            **service.get_version(self.owner, w.offer_v1)["content"],
            "landing_page_url": "https://acme-charge.example/offers/acme-65w-v2",
        })
        self.assertIn((w.offer_v1, REL_FACT_SET_OFFER), self.edges_from(self.owner, w.fact_set_v1))

        # The mapping pins Offer v1 (mapping_offer), so only the fact set is listed.
        self.assertEqual(self.listed(self.withdraw_preview(self.owner, offer_v2)), {w.fact_set_v1})
        self.assertEqual(
            self.listed(self.withdraw_preview(self.owner, w.offer_v1)), {w.fact_set_v1, w.mapping_v1},
        )

    def test_withdrawing_the_superseded_version_does_not_reach_fact_sets_published_on_the_new_one(self):
        w = self.world(self.owner)
        offer_v2 = republish_offer(self.owner, w.offer_entry, {
            **service.get_version(self.owner, w.offer_v1)["content"],
            "landing_page_url": "https://acme-charge.example/offers/acme-65w-v2",
        })
        late_fact_set = self.publish_late_fact_set(self.owner, w)
        self.assertIn((offer_v2, REL_FACT_SET_OFFER), self.edges_from(self.owner, late_fact_set))

        self.assertEqual(
            self.listed(self.withdraw_preview(self.owner, offer_v2)), {w.fact_set_v1, late_fact_set},
        )
        preview = self.withdraw_preview(self.owner, w.offer_v1)
        self.assertEqual(self.listed(preview), {w.fact_set_v1, w.mapping_v1})

        service.withdraw_version(
            self.owner, w.offer_v1, reason="Superseded landing page recalled",
            impact_id=preview["impact_id"], impact_hash=preview["impact_hash"],
        )
        self.assertEqual(self.eligibility(w.fact_set_v1), "blocked")
        self.assertEqual(self.eligibility(late_fact_set), "eligible")


class TestPinnedRelationsFollowTheirVersion(EntryResolvedFixtures):
    def test_an_offer_pinned_to_the_old_product_version_is_not_listed_for_the_new_one(self):
        w = self.world(self.owner)
        product_v2 = _republish(self.owner, w.product_entry, description="Compact 65W GaN charger, v2")

        self.assertEqual(self.listed(self.withdraw_preview(self.owner, product_v2)), set())
        # Product v1 reaches the Offer it pins, the mapping pinning that Offer
        # version and the fact set naming the Offer entry.
        self.assertEqual(
            self.listed(self.withdraw_preview(self.owner, w.product_v1)),
            {w.offer_v1, w.mapping_v1, w.fact_set_v1},
        )

    def test_an_entry_named_dependent_further_down_follows_the_current_version(self):
        # The fact set names the Offer entry. After the Offer is republished on
        # Product v2, the fact set resolves to Offer v2, so withdrawing Product
        # v2 reaches it through Offer v2 although its own edge stays on Offer v1.
        w = self.world(self.owner)
        product_v2 = _republish(self.owner, w.product_entry, description="Compact 65W GaN charger, v2")
        offer_v2 = republish_offer(self.owner, w.offer_entry, {
            **service.get_version(self.owner, w.offer_v1)["content"], "product_version_id": product_v2,
        })

        self.assertEqual(self.edges_from(self.owner, w.fact_set_v1), {
            (w.source_v1, REL_FACT_SET_DATA_SOURCE), (w.offer_v1, REL_FACT_SET_OFFER),
        })
        self.assertEqual(self.listed(self.withdraw_preview(self.owner, product_v2)), {offer_v2, w.fact_set_v1})

    def test_a_superseded_version_further_down_follows_its_exact_edges_only(self):
        # Product v1 pins Offer v1, which is superseded once the Offer moves to
        # Product v2. From Offer v1 the walk follows exact edges only: the fact
        # set published on Offer v2 is not reached.
        w = self.world(self.owner)
        product_v2 = _republish(self.owner, w.product_entry, description="Compact 65W GaN charger, v2")
        offer_v2 = republish_offer(self.owner, w.offer_entry, {
            **service.get_version(self.owner, w.offer_v1)["content"], "product_version_id": product_v2,
        })
        late_fact_set = self.publish_late_fact_set(self.owner, w)
        self.assertIn((offer_v2, REL_FACT_SET_OFFER), self.edges_from(self.owner, late_fact_set))

        self.assertEqual(
            self.listed(self.withdraw_preview(self.owner, w.product_v1)),
            {w.offer_v1, w.mapping_v1, w.fact_set_v1},
        )
        self.assertEqual(
            self.listed(self.withdraw_preview(self.owner, product_v2)), {offer_v2, w.fact_set_v1, late_fact_set},
        )


class TestWorkspaceAndRoleBoundaries(EntryResolvedFixtures):
    def test_another_workspace_s_dependents_never_appear(self):
        w = self.world(self.owner)
        other_owner = self.owner_ctx(self.ctx(), actor="owner_b")
        other = self.world(other_owner)
        source_v2 = republish_data_source(self.owner, w.source_entry, {**MANUAL_SOURCE, "config": {"note": "v2"}})
        republish_data_source(other_owner, other.source_entry, {**MANUAL_SOURCE, "config": {"note": "v2"}})
        theirs = {other.policy_v1, other.fact_set_v1, other.mapping_v1}

        self.assertEqual(self.listed(self.withdraw_preview(self.owner, source_v2)), {w.policy_v1, w.fact_set_v1})
        self.assertEqual(self.listed(service.impact_analysis(self.owner, source_v2)) & theirs, set())
        self.assertEqual(
            self.listed(command_impacts.preview_archive_impact(self.owner, w.source_entry)) & theirs, set(),
        )
        with self.assertRaises(service.LibraryNotFound):
            command_impacts.preview_withdraw_impact(other_owner, source_v2)
        with self.assertRaises(service.LibraryNotFound):
            service.impact_analysis(other_owner, source_v2)

        service.withdraw_version(self.owner, source_v2, reason="Supplier feed retired")
        self.assertEqual(service.get_version(other_owner, other.policy_v1)["eligibility"], "eligible")
        self.assertEqual(service.get_version(other_owner, other.fact_set_v1)["eligibility"], "eligible")

    def test_a_member_sees_the_same_preview_but_cannot_withdraw(self):
        w = self.world(self.owner)
        source_v2 = republish_data_source(self.owner, w.source_entry, {**MANUAL_SOURCE, "config": {"note": "v2"}})

        self.assertEqual(self.listed(self.withdraw_preview(self.member, source_v2)), {w.policy_v1, w.fact_set_v1})
        with self.assertRaises(service.LibraryForbidden):
            service.withdraw_version(self.member, source_v2, reason="Supplier feed retired")
        self.assertEqual(service.get_version(self.owner, source_v2)["state"], "effective")
        self.assertEqual(self.eligibility(w.policy_v1), "eligible")
        self.assertEqual(self.eligibility(w.fact_set_v1), "eligible")
