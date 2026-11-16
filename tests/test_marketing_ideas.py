"""Ads-first ideas (ADR 0018, #189): the container, its read endpoints and the keep mark."""
import unittest
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (AssetRecord, AssetVersionRecord, AuditEventRecord, GenerationRunRecord, LibraryEntryRecord,
                                            ProductRecord, new_id, utcnow)
from app.generation.models import GenerationRecipeSnapshotRecord, RunItemRecord
from app.library import service as library
from app.library.contracts import ENTRY_PRODUCT
from app.marketing import ideas, service
from app.marketing.ideas_contracts import Ideas, IdeasProjectDetail
from app.marketing.models import MarketingAdKeepRecord, MarketingProjectRecord
from tests.test_marketing_planning import MarketingPlanningFixtures


def ideas_example():
    """Two buying claims for Acme Charge, two ways of telling each, one ad per way."""
    def expression(eid, mechanism):
        return {"id": eid, "name": f"Telling {eid}", "description": "Show the charger at work.",
                "fit_reason": "Travellers carry one charger.", "how_to_tell": "Follow one trip from desk to gate.",
                "visual_mechanism": mechanism, "evidence_basis": {"fact_ids": ["f1"]}}

    def combination(cid, angle_id, expression_id):
        return {"id": cid, "angle_id": angle_id, "expression_id": expression_id,
                "headline": "One charger for the whole trip", "body_copy": "Acme Charge powers laptop and phone together.",
                "visual_brief": "A traveller plugs one charger in at the gate.", "checks": ["Logo is readable"]}

    return {
        "market": "United States", "ad_language": "en-US", "strategy_language": "en-US",
        "angles": [
            {"id": "a1", "proposition": "One charger replaces three", "rationale": "Travellers pack light.",
             "fact_ids": ["f1"], "expressions": [expression("e1", "scenario_story"), expression("e2", "route_map")]},
            {"id": "a2", "proposition": "Charges a laptop at full speed", "rationale": "65W covers laptops.",
             "fact_ids": ["f1"], "expressions": [expression("e3", "live_connection_demo"), expression("e4", "qa_objection")]},
        ],
        "combinations": [combination("c1", "a1", "e1"), combination("c2", "a1", "e2"),
                         combination("c3", "a2", "e3"), combination("c4", "a2", "e4")],
    }


class IdeasDocumentTests(unittest.TestCase):
    def test_two_claims_with_two_ways_each_are_valid(self):
        doc = Ideas.model_validate(ideas_example())
        self.assertEqual([c.id for c in doc.combinations], ["c1", "c2", "c3", "c4"])

    def test_combination_must_point_at_an_expression_of_its_angle(self):
        doc = ideas_example()
        doc["combinations"][0]["expression_id"] = "missing"
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_combination_cannot_borrow_another_angles_expression(self):
        doc = ideas_example()
        doc["combinations"][0]["expression_id"] = "e3"
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_every_expression_needs_an_ad(self):
        doc = ideas_example()
        doc["combinations"] = [c for c in doc["combinations"] if c["id"] != "c4"]
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_ids_are_unique(self):
        doc = ideas_example()
        doc["combinations"][1]["id"] = "c1"
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_one_angle_is_not_enough(self):
        doc = ideas_example()
        doc["angles"] = doc["angles"][:1]
        doc["combinations"] = [c for c in doc["combinations"] if c["angle_id"] == doc["angles"][0]["id"]]
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_ad_language_uses_the_plan_pattern(self):
        doc = ideas_example()
        doc["ad_language"] = "English"
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_detail_states_are_only_the_three_ads_first_states(self):
        detail = {"id": "mp_1", "revision": 1, "state": "input", "product_id": "prod_1", "market": "United States",
                  "ad_language": "en-US", "understanding": None, "ideas": None, "latest_call": None, "ads": [], "pending": []}
        IdeasProjectDetail.model_validate(detail)
        with self.assertRaises(ValidationError):
            IdeasProjectDetail.model_validate({**detail, "state": "plan_confirmed"})


class MarketingIdeasSchemaTests(MarketingPlanningFixtures):
    def test_existing_projects_are_plan_first(self):
        old = self.create()
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, old["id"])
            self.assertEqual(row.flow, "plan_first")
            self.assertIsNone(row.ideas_json)
            self.assertIsNone(row.market)
            self.assertIsNone(row.ad_language)


class MarketingIdeasFixtures(MarketingPlanningFixtures):
    def product(self, ctx=None):
        """A product card in the workspace (an Acme Charge charger)."""
        return self._product_id(ctx or self.context)

    def plant_ad(self, project_id, combination_id="c1", *, flow="ads_first", ctx=None):
        """An image generated for ``project_id``: the recipe's marketing intent names the project and its flow."""
        ctx = ctx or self.context
        with tenant_session(ctx) as s:
            product_id = s.get(MarketingProjectRecord, project_id).product_id
            intent = {"schema_version": 2, "project_id": project_id,
                      "combination": {"id": combination_id, "angle_id": "a1", "expression_id": "e1"}}
            if flow:
                intent["flow"] = flow
            recipe = GenerationRecipeSnapshotRecord(id=new_id("rs"), workspace_id=ctx.workspace_id, canonicalization_version="v1",
                recipe_hash=new_id("h"), canonical_json={"selection": {"marketing_intent": intent}}, execution_mode="byok",
                created_at=utcnow())
            s.add(recipe)
            s.flush()
            run = GenerationRunRecord(id=new_id("gr"), workspace_id=ctx.workspace_id, product_id=product_id,
                plan_id=self._plan_id(ctx, s), provider="openai", model="gpt-image-1", aspect_ratio="4:5", image_size="1K",
                variants=1, use_ref=False, status="succeeded", total=1, done=1, failed=0, currency="USD", revision=1,
                recipe_snapshot_id=recipe.id, execution_mode="byok")
            s.add(run)
            s.flush()
            item = RunItemRecord(id=new_id("ri"), workspace_id=ctx.workspace_id, generation_run_id=run.id, ordinal=0,
                combination_fingerprint="fp", semantic_fingerprint="sf", execution_mode="byok", state="succeeded",
                revision=1, created_at=utcnow(), updated_at=utcnow())
            asset = AssetRecord(id=new_id("asset"), workspace_id=ctx.workspace_id, product_id=product_id, run_id=run.id,
                combo_id="fp", combo_code="fp", tags_json={}, filename="ad.png", provider="openai", model="gpt-image-1",
                prompt="Acme Charge ad", fingerprint=new_id("h"), status="succeeded")
            s.add_all([item, asset])
            s.flush()
            version = AssetVersionRecord(id=new_id("av"), workspace_id=ctx.workspace_id, asset_id=asset.id, kind="image",
                aspect_ratio="4:5", object_key=f"ads/{asset.id}.png", content_sha256=new_id("sha"), byte_size=10,
                mime_type="image/png", producing_run_item_id=item.id, origin_kind="generation", execution_mode="byok")
            s.add(version)
            s.flush()
            return version.id


class IdeasServiceTests(MarketingIdeasFixtures):
    def make(self, ctx=None, product_id=None):
        ctx = ctx or self.context
        with tenant_session(ctx) as session:
            row = ideas.create_ideas_project(session, ctx, product_id=product_id or self.product(ctx),
                                             source_text="Schedule charging from the Acme Charge app.",
                                             market="United States", ad_language="en-US")
            return row.id

    def test_create_starts_in_input(self):
        project_id = self.make()
        with tenant_session(self.context) as session:
            row = ideas.get_ideas_project(session, self.context, project_id)
            self.assertEqual((row.flow, row.state, row.market, row.ad_language), ("ads_first", "input", "United States", "en-US"))
            self.assertIsNone(row.plan_json)
            self.assertEqual(ideas.ideas_dict(session, self.context, row)["ads"], [])

    def test_old_lookup_does_not_find_an_ads_first_project(self):
        project_id = self.make()
        with tenant_session(self.context) as session:
            with self.assertRaises(service.MarketingError) as caught:
                service.get_project(session, self.context, project_id)
            self.assertEqual(caught.exception.status, 404)

    def test_ideas_lookup_does_not_find_a_plan_first_project(self):
        old = self.create()
        with tenant_session(self.context) as session:
            with self.assertRaises(service.MarketingError) as caught:
                ideas.get_ideas_project(session, self.context, old["id"])
            self.assertEqual(caught.exception.status, 404)

    def test_another_workspace_cannot_find_it(self):
        project_id = self.make()
        other = self._make_context()
        with tenant_session(other) as session:
            with self.assertRaises(service.MarketingError) as caught:
                ideas.get_ideas_project(session, other, project_id)
            self.assertEqual(caught.exception.status, 404)

    def test_keeping_an_ad_that_is_not_in_the_project_is_refused(self):
        project_id = self.make()
        elsewhere = self.plant_ad(self.make())  # an ad of another ideas project of this workspace
        with tenant_session(self.context) as session:
            row = ideas.get_ideas_project(session, self.context, project_id)
            for asset_version_id in ("av_missing", elsewhere):
                with self.assertRaises(service.MarketingError) as caught:
                    ideas.set_kept(session, self.context, row, asset_version_id, True)
                self.assertEqual((caught.exception.code, caught.exception.status), ("ad_not_found", 404))

    def test_keeping_marks_the_ad_and_audits_each_change(self):
        project_id = self.make()
        ad = self.plant_ad(project_id)
        for kept in (True, False, True):
            with tenant_session(self.context) as session:
                ideas.set_kept(session, self.context, ideas.get_ideas_project(session, self.context, project_id), ad, kept)
        with tenant_session(self.context) as session:
            row = ideas.get_ideas_project(session, self.context, project_id)
            self.assertEqual([(a["asset_version_id"], a["combination_id"], a["status"], a["kept"])
                              for a in ideas.ideas_dict(session, self.context, row)["ads"]], [(ad, "c1", "draft", True)])
            summary = ideas.ideas_summary(session, self.context, row)
            self.assertEqual((summary["ad_count"], summary["kept_count"]), (1, 1))
            self.assertEqual(len(session.scalars(select(MarketingAdKeepRecord).where(
                MarketingAdKeepRecord.project_id == project_id)).all()), 1)
            events = session.scalars(select(AuditEventRecord).where(AuditEventRecord.event_type == "marketing.ad_kept",
                AuditEventRecord.resource_id == project_id).order_by(AuditEventRecord.created_at, AuditEventRecord.id)).all()
            self.assertEqual([e.details_json["kept"] for e in events], [True, False, True])
            self.assertEqual({e.details_json["asset_version_id"] for e in events}, {ad})

    def test_only_ads_first_images_of_the_project_are_its_ads(self):
        project_id = self.make()
        mine = self.plant_ad(project_id, "c2")
        self.plant_ad(project_id, "c3", flow=None)  # an intent without the ads_first flow is not an ad of this project
        with tenant_session(self.context) as session:
            row = ideas.get_ideas_project(session, self.context, project_id)
            self.assertEqual([v.id for v, _ in ideas.project_ads(session, self.context, row)], [mine])

    def test_unknown_product_is_refused(self):
        with tenant_session(self.context) as session:
            with self.assertRaises(service.MarketingError) as caught:
                ideas.create_ideas_project(session, self.context, product_id="prod_missing", source_text="x",
                                           market="United States", ad_language="en-US")
            self.assertEqual((caught.exception.code, caught.exception.status), ("product_not_found", 404))

    def test_another_workspaces_product_is_refused(self):
        theirs = self.product(self._make_context())
        with tenant_session(self.context) as session:
            with self.assertRaises(service.MarketingError) as caught:
                ideas.create_ideas_project(session, self.context, product_id=theirs, source_text="x",
                                           market="United States", ad_language="en-US")
            self.assertEqual(caught.exception.code, "product_not_found")

    def test_an_unpublished_product_draft_anchors_ideas_by_either_id(self):
        """D3: the first batch saves the read product as an unpublished draft and builds ideas on it."""
        draft = library.create_entry(self.context, entry_type=ENTRY_PRODUCT, display_name="Acme Charge 65W", content={})
        with tenant_session(self.context) as s:
            bridged = s.scalar(select(ProductRecord.id).where(ProductRecord.product_profile_entry_id == draft["id"]))
        for given in (bridged, draft["id"]):
            with tenant_session(self.context) as session:
                self.assertEqual(session.get(MarketingProjectRecord, self.make(product_id=given)).product_id, bridged)

    def test_an_archived_product_is_refused(self):
        draft = library.create_entry(self.context, entry_type=ENTRY_PRODUCT, display_name="Acme Charge 65W", content={})
        with tenant_session(self.context) as s:
            s.get(LibraryEntryRecord, draft["id"]).lifecycle = "archived"
        with tenant_session(self.context) as session:
            with self.assertRaises(service.MarketingError) as caught:
                ideas.create_ideas_project(session, self.context, product_id=draft["id"], source_text="x",
                                           market="United States", ad_language="en-US")
            self.assertEqual(caught.exception.code, "product_not_found")

    def test_creation_is_recorded_as_a_revision_with_the_ideas_fields(self):
        project_id = self.make()
        with tenant_session(self.context) as session:
            from app.marketing.models import MarketingRevisionRecord
            revision = session.scalar(select(MarketingRevisionRecord).where(MarketingRevisionRecord.project_id == project_id))
            self.assertEqual((revision.revision, revision.content_json["flow"], revision.content_json["market"],
                              revision.content_json["ad_language"], revision.content_json["ideas"]),
                             (1, "ads_first", "United States", "en-US", None))


class IdeasApiTests(MarketingIdeasFixtures):
    def ideas_create(self, **overrides):
        body = {"product_id": self.product(), "source_text": "Schedule charging from the Acme Charge app.",
                "market": "United States", "ad_language": "en-US", **overrides}
        return self.client.post("/api/v1/marketing/ideas", json=body, headers={"Idempotency-Key": uuid4().hex})

    def keep(self, project_id, asset_version_id, kept=True):
        return self.client.put(f"/api/v1/marketing/ideas/{project_id}/ads/{asset_version_id}/kept", json={"kept": kept},
                               headers={"Idempotency-Key": uuid4().hex})

    def test_create_then_read(self):
        created = self.ideas_create()
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual((body["state"], body["ads"], body["pending"], body["ideas"], body["understanding"], body["latest_call"]),
                         ("input", [], [], None, None, None))
        self.assertEqual((body["market"], body["ad_language"]), ("United States", "en-US"))
        read = self.client.get(f"/api/v1/marketing/ideas/{body['id']}")
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json(), body)
        self.assertEqual(read.headers["ETag"], '"1"')

    def test_same_idempotency_key_returns_the_same_project(self):
        key = uuid4().hex
        body = {"product_id": self.product(), "source_text": "x", "market": "United States", "ad_language": "en-US"}
        first = self.client.post("/api/v1/marketing/ideas", json=body, headers={"Idempotency-Key": key})
        second = self.client.post("/api/v1/marketing/ideas", json=body, headers={"Idempotency-Key": key})
        self.assertEqual((first.status_code, second.status_code), (201, 201), second.text)
        self.assertEqual(first.json()["id"], second.json()["id"])

    def test_creating_needs_a_product_of_this_workspace(self):
        missing = self.ideas_create(product_id="prod_missing")
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "product_not_found"))
        unset = self.client.post("/api/v1/marketing/ideas", headers={"Idempotency-Key": uuid4().hex},
                                 json={"source_text": "x", "market": "United States", "ad_language": "en-US"})
        self.assertEqual(unset.status_code, 422, unset.text)

    def test_old_endpoints_do_not_show_it(self):
        body = self.ideas_create().json()
        for path in ("/api/v1/marketing/projects", "/api/v1/marketing/projects?limit=100"):
            listed = self.client.get(path).json()["items"]
            self.assertNotIn(body["id"], [item["id"] for item in listed])
        self.assertEqual(self.client.get(f"/api/v1/marketing/projects/{body['id']}").status_code, 404)
        edit = self.client.patch(f"/api/v1/marketing/projects/{body['id']}", headers=self.headers(body["revision"]),
                                 json={"command": "revise_source", "source_text": "Acme Charge"})
        self.assertEqual(edit.status_code, 404, edit.text)

    def test_new_endpoints_do_not_show_a_plan_first_project(self):
        old = self.create()
        self.assertEqual(self.client.get(f"/api/v1/marketing/ideas/{old['id']}").status_code, 404)
        self.assertEqual(self.keep(old["id"], "av_missing").status_code, 404)

    def test_list_shows_only_ads_first_projects_newest_first(self):
        old = self.create()
        first = self.ideas_create().json()
        second = self.ideas_create().json()
        page = self.client.get("/api/v1/marketing/ideas").json()
        ids = [item["id"] for item in page["items"]]
        self.assertEqual(ids[:2], [second["id"], first["id"]])
        self.assertNotIn(old["id"], ids)
        self.assertEqual(page["items"][0]["product_name"], "Test Product")
        self.assertEqual((page["items"][0]["ad_count"], page["items"][0]["kept_count"]), (0, 0))
        self.assertIsNone(page["next_cursor"])

    def test_list_pages_with_a_cursor(self):
        made = [self.ideas_create().json()["id"] for _ in range(3)]
        first = self.client.get("/api/v1/marketing/ideas?limit=2").json()
        self.assertEqual([item["id"] for item in first["items"]], made[::-1][:2])
        rest = self.client.get(f"/api/v1/marketing/ideas?limit=2&cursor={first['next_cursor']}").json()
        self.assertEqual([item["id"] for item in rest["items"]], made[:1])
        broken = self.client.get("/api/v1/marketing/ideas?cursor=not-a-cursor")
        self.assertEqual((broken.status_code, broken.json()["code"]), (400, "invalid_cursor"))

    def test_keep_on_unknown_ad_is_404(self):
        body = self.ideas_create().json()
        response = self.keep(body["id"], "av_missing")
        self.assertEqual((response.status_code, response.json()["code"]), (404, "ad_not_found"))

    def test_keep_marks_the_ad_and_returns_the_ideas(self):
        body = self.ideas_create().json()
        ad = self.plant_ad(body["id"])
        kept = self.keep(body["id"], ad)
        self.assertEqual(kept.status_code, 200, kept.text)
        self.assertEqual([(a["asset_version_id"], a["kept"]) for a in kept.json()["ads"]], [(ad, True)])
        self.assertEqual(self.client.get("/api/v1/marketing/ideas").json()["items"][0]["kept_count"], 1)
        dropped = self.keep(body["id"], ad, kept=False)
        self.assertEqual([a["kept"] for a in dropped.json()["ads"]], [False])

    def test_another_workspace_cannot_read_list_or_keep(self):
        body = self.ideas_create().json()
        ad = self.plant_ad(body["id"])
        mine = self.context
        self.context = self._make_context()
        self.assertEqual(self.client.get(f"/api/v1/marketing/ideas/{body['id']}").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/marketing/ideas").json()["items"], [])
        refused = self.keep(body["id"], ad)
        self.assertEqual((refused.status_code, refused.json()["code"]), (404, "marketing_project_not_found"))
        theirs = self.ideas_create(product_id=self.product(mine))
        self.assertEqual((theirs.status_code, theirs.json()["code"]), (404, "product_not_found"))
        self.context = mine
        with tenant_session(self.context) as session:
            self.assertIsNone(session.scalar(select(MarketingAdKeepRecord).where(MarketingAdKeepRecord.project_id == body["id"])))


class IdeasReplayAndCostTests(MarketingIdeasFixtures):
    ideas_create, keep = IdeasApiTests.ideas_create, IdeasApiTests.keep

    def test_reusing_a_create_key_with_another_body_is_refused(self):
        key = uuid4().hex
        body = {"product_id": self.product(), "source_text": "x", "market": "United States", "ad_language": "en-US"}
        self.assertEqual(self.client.post("/api/v1/marketing/ideas", json=body, headers={"Idempotency-Key": key}).status_code, 201)
        reused = self.client.post("/api/v1/marketing/ideas", json={**body, "market": "Canada"}, headers={"Idempotency-Key": key})
        self.assertEqual((reused.status_code, reused.json()["code"]), (409, "idempotency_key_reused"))
        self.assertEqual(len(self.client.get("/api/v1/marketing/ideas").json()["items"]), 1)

    def test_replaying_an_old_keep_does_not_undo_a_later_one(self):
        body = self.ideas_create().json()
        ad = self.plant_ad(body["id"])
        first = uuid4().hex
        path = f"/api/v1/marketing/ideas/{body['id']}/ads/{ad}/kept"
        self.assertEqual(self.client.put(path, json={"kept": True}, headers={"Idempotency-Key": first}).status_code, 200)
        self.assertEqual(self.keep(body["id"], ad, kept=False).status_code, 200)
        replayed = self.client.put(path, json={"kept": True}, headers={"Idempotency-Key": first})
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual([a["kept"] for a in replayed.json()["ads"]], [False])
        with tenant_session(self.context) as session:
            events = session.scalars(select(AuditEventRecord).where(AuditEventRecord.event_type == "marketing.ad_kept",
                                                                    AuditEventRecord.resource_id == body["id"])).all()
            self.assertEqual(len(events), 2)

    def test_listing_costs_the_same_number_of_queries_for_one_or_many_projects(self):
        from sqlalchemy import event
        from app.adapters.persistence.database import engine

        def statements():
            seen = []
            listener = lambda *args: seen.append(args[2])  # noqa: E731
            event.listen(engine, "before_cursor_execute", listener)
            try:
                self.assertEqual(self.client.get("/api/v1/marketing/ideas").status_code, 200)
            finally:
                event.remove(engine, "before_cursor_execute", listener)
            return len(seen)

        first = self.ideas_create().json()
        self.plant_ad(first["id"])
        one = statements()
        for _ in range(4):
            self.plant_ad(self.ideas_create().json()["id"])
        self.assertEqual(statements(), one)


class AdsFirstMigrationDowngradeTests(unittest.TestCase):
    """Dropping ``flow`` would hand ads-first projects to the plan-first endpoints, so downgrade refuses."""
    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-ads-first-migration-")
        self.addCleanup(self.temp.cleanup)
        self.database = f"{self.temp.name}/migration.db"
        self.environment = {**__import__("os").environ, "MARKETAI_DATABASE_URL": f"sqlite:///{self.database}",
                            "MARKETAI_DATA_DIR": f"{self.temp.name}/data"}
        upgraded = self.alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

    def alembic(self, *arguments):
        import subprocess
        import sys
        return subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=self.ROOT, env=self.environment,
                              capture_output=True, text=True, check=False)

    def columns(self):
        import sqlite3
        with sqlite3.connect(self.database) as connection:
            return {row[1] for row in connection.execute("PRAGMA table_info(marketing_projects)")}

    def test_downgrade_without_ads_first_projects_drops_the_columns_and_table(self):
        downgraded = self.alembic("downgrade", "zzl148a1b2c3")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        self.assertFalse({"flow", "market", "ad_language", "ideas_json"} & self.columns())

    def test_downgrade_refuses_while_an_ads_first_project_exists(self):
        import sqlite3
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO marketing_projects (id, workspace_id, revision, state, source_text, flow, created_at, updated_at)"
                               " VALUES ('mp_1', 'ws_1', 1, 'input', 'Acme Charge', 'ads_first', '2026-11-16', '2026-11-16')")
        downgraded = self.alembic("downgrade", "zzl148a1b2c3")
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("ads_first projects or keep marks exist", downgraded.stderr)
        self.assertIn("flow", self.columns())
