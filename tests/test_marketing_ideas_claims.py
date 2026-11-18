"""Change and delete a claim, confirm the audience, claims without a source (#224; ADR 0018, design doc §5.2, §5.7).

The ads come from a real first batch on the fictional Acme Charge page (fake page, fake model, fake image
provider; ``IdeasExportFixtures``): claim a1 cites f1, a2 cites f2, a3 cites f3 only through the guess i1,
and no ad cites f4. New images go through the real preview, submission and Worker. No paid call is made.
"""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import func, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord
from app.marketing import ideas, planner
from app.marketing.claims import literal_claims, unsourced_uses
from app.marketing.contracts import Understanding
from app.marketing.ideas_contracts import Ideas, IdeasProjectDetail, IdeasUnderstanding
from app.marketing.models import MarketingProjectRecord, MarketingRevisionRecord, PlanningCallRecord
from tests.test_ads_first_drafts_stay_out import RoundGateFixtures
from tests.test_experiment_lifecycle import _draft_context
from tests.test_marketing_ideas_checks import IMAGE_JOBS
from tests.test_marketing_ideas_export import FACTS, claims, understood
from tests.test_marketing_ideas_photos import PhotoFixtures
from tests.test_marketing_ideas_revisions import RevisionFixtures

UNSOURCED = [{"id": "u1", "text": "Charges 3x faster than a wall socket"},
             {"id": "u2", "text": "The quietest home charger on the market"}]
REDO = {"ideas_changed", "facts_changed", "facts_removed", "photo_unavailable", "image_blocked"}


def with_unsourced(kind, inputs):
    """The fixture understanding, also listing two claims the page makes without a source."""
    result = understood(kind, inputs)
    if kind == "understanding" and inputs.get("flow") == "ads_first":
        result["unsupported_claims"] = deepcopy(UNSOURCED)
    return result


class ClaimCommands:
    """The claim commands and one new image of an ad, for any fixture with the first batch's ads."""

    def angle(self, project, angle_id):
        return next(a for a in self.read_ideas(project)["ideas"]["angles"] if a["id"] == angle_id)

    def edit_claim(self, project, angle_id, *, proposition=None, rationale=None, fact_ids=None, expect=200, key=None,
                   revision=None):
        current = self.read_ideas(project)
        angle = next((a for a in current["ideas"]["angles"] if a["id"] == angle_id), {})
        body = {"proposition": angle.get("proposition", "A claim") if proposition is None else proposition,
                "rationale": angle.get("rationale", "A reason") if rationale is None else rationale,
                "fact_ids": angle.get("fact_ids", []) if fact_ids is None else fact_ids}
        response = self.client.patch(f"/api/v1/marketing/ideas/{project['id']}/claims/{angle_id}", json=body,
                                     headers=self.headers(revision or current["revision"], key))
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            IdeasProjectDetail.model_validate(response.json())
        return response.json()

    def delete_claim(self, project, angle_id, expect=200, key=None, revision=None):
        current = self.read_ideas(project)
        response = self.client.delete(f"/api/v1/marketing/ideas/{project['id']}/claims/{angle_id}",
                                      headers=self.headers(revision or current["revision"], key))
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            IdeasProjectDetail.model_validate(response.json())
        return response.json()

    def remake(self, project, combination_id):
        """One new image of an ad as the ideas describe it now (``prepare_marketing_ideas_generation``)."""
        current = self.read_ideas(project)
        before = {ad["asset_version_id"] for ad in current["ads"]}
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/generation-drafts",
                                    headers=self.headers(current["revision"]), json={"provider": "mock", "model": "mock-v1"})
        self.assertEqual(response.status_code, 201, response.text)
        draft_id = next(d["draft_id"] for d in response.json()["drafts"] if d["combination_id"] == combination_id)
        draft = self.client.get(f"/api/v1/generation/drafts/{draft_id}").json()
        preview = self.client.post(f"/api/v1/generation/drafts/{draft_id}/previews", headers=self.headers(draft["revision"]))
        self.assertEqual(preview.status_code, 201, preview.text)
        run = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]),
                               json={"preview_id": preview.json()["id"], "preview_hash": preview.json()["preview_hash"]})
        self.assertEqual(run.status_code, 202, run.text)
        self.drain(job_types=IMAGE_JOBS)
        return next(ad for ad in self.read_ideas(project)["ads"] if ad["asset_version_id"] not in before)

    def intent_of(self, project, asset_version_id):
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            return next(ideas.intent_of(recipe) for version, recipe in ideas.project_ads(session, self.context, row)
                        if version.id == asset_version_id)

    def ad_by_id(self, project, asset_version_id):
        return next(ad for ad in self.read_ideas(project)["ads"] if ad["asset_version_id"] == asset_version_id)

    def removed_citations(self, project):
        """Per current ad of the ideas, the ids its claim names that are no fact of the understanding."""
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            facts = {fact["id"] for fact in row.understanding_json["facts"]}
            return {c["id"]: sorted(ideas.citations(row, c["id"]) - facts) for c in row.ideas_json["combinations"]
                    if ideas.citations(row, c["id"]) - facts}

    def remove_fact(self, fact_id):
        def change(understanding):
            understanding["facts"] = [f for f in understanding["facts"] if f["id"] != fact_id]
            for inference in understanding["inferences"]:
                inference["fact_ids"] = [fid for fid in inference["fact_ids"] if fid != fact_id]
        return change

    def set_understanding(self, project, **values):
        """Write the stored understanding directly, as a drafting call would have left it."""
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            row.understanding_json = {**row.understanding_json, **values}

    def events(self, project, event_type):
        with tenant_session(self.context) as session:
            return session.scalars(select(AuditEventRecord.details_json).where(
                AuditEventRecord.workspace_id == self.context.workspace_id, AuditEventRecord.resource_id == project["id"],
                AuditEventRecord.event_type == event_type).order_by(AuditEventRecord.created_at)).all()

    def revisions(self, project, reason):
        with tenant_session(self.context) as session:
            return session.scalar(select(func.count()).select_from(MarketingRevisionRecord).where(
                MarketingRevisionRecord.workspace_id == self.context.workspace_id,
                MarketingRevisionRecord.project_id == project["id"], MarketingRevisionRecord.reason == reason))

    def export_everything(self, project, asset_version_id, fact_ids):
        """Check one new image, its facts, its look and its findings, as the check before download does."""
        self.start_checks(project)
        self.drain(job_types=("marketing.ai_qc",))
        self.verify(project, fact_ids=fact_ids, ads=[])
        ad = self.ad_by_id(project, asset_version_id)
        body = {"appearance_confirmed": [asset_version_id],
                "accepted_findings": [{"asset_version_id": asset_version_id, "check_id": ad["check"]["id"],
                                       "finding_ids": [f["check_id"] for f in ad["check"]["findings"]]}]}
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification", json=body,
                                    headers=self.headers(self.read_ideas(project)["revision"]))
        self.assertEqual(response.status_code, 200, response.text)
        return next(item for item in response.json()["ads"] if item["asset_version_id"] == asset_version_id)


class ClaimFixtures(ClaimCommands, RevisionFixtures):
    pass


# -- change a claim ------------------------------------------------------------------------------------


class ChangeClaimTests(ClaimFixtures):
    def test_changing_a_claim_that_cites_a_removed_fact_lets_its_ad_be_made_again(self):
        """The dead end #221 refused (``claim_cites_removed_fact``) is undone by changing the claim (#224)."""
        project = self.finished()
        old = self.ads(project)["c1_1"]
        self.edit_facts(project, self.remove_fact("f1"))
        self.assertEqual(self.removed_citations(project), {"c1_1": ["f1"], "c1_2": ["f1"]})
        refused = self.feedback(project, "c1_1", expect=409)
        self.assertEqual(refused["code"], "claim_cites_removed_fact")

        changed = self.edit_claim(project, "a1", proposition="Plan charging around your evening",
                                  rationale="The charger is a wall charger drivers set up once.", fact_ids=["f4"])
        angle = next(a for a in changed["ideas"]["angles"] if a["id"] == "a1")
        self.assertEqual((angle["proposition"], angle["fact_ids"]), ("Plan charging around your evening", ["f4"]))
        # Its ways of telling stood on f1 only: they keep no fact and say so in their evidence gap.
        self.assertEqual({(tuple(e["evidence_basis"]["fact_ids"]), bool(e["evidence_basis"]["gap"]))
                          for e in angle["expressions"]}, {((), True)})
        self.assertEqual(self.removed_citations(project), {})
        # The images made before are not the claim's ads any more; no other ad changed.
        before = {cid: ad for cid, ad in self.ads(project).items()}
        self.assertEqual(before["c1_1"]["status"], "needs_redo")
        self.assertIn("ideas_changed", self.reasons(before["c1_1"]))
        self.assertEqual({cid: ad["status"] for cid, ad in before.items() if not cid.startswith("c1_")},
                         {cid: "draft" for cid in ("c2_1", "c2_2", "c3_1", "c3_2")})
        # A new image is made on the claim as it is now, and can be checked, verified and exported.
        new = self.remake(project, "c1_1")
        self.assertEqual(new["status"], "draft")
        self.assertFalse(REDO & set(self.reasons(new)), new["reasons"])
        intent = self.recipe_intent(project, new["asset_version_id"])
        self.assertEqual((intent["proposition"], intent["cited_fact_ids"]), ("Plan charging around your evening", ["f4"]))
        self.assertEqual(self.export_everything(project, new["asset_version_id"], ["f4"])["status"], "exportable")
        # The old image is still listed, still needs redoing: nothing was rewritten.
        self.assertEqual(self.ad_by_id(project, old["asset_version_id"])["status"], "needs_redo")
        (event,) = self.events(project, "marketing.ideas_claim_edited")
        self.assertEqual((event["angle_id"], event["fact_ids_before"], event["fact_ids_after"]), ("a1", ["f1"], ["f4"]))
        self.assertEqual(set(event["changed"]), {"proposition", "rationale", "fact_ids"})
        self.assertEqual(set(event["asset_version_ids"]), {before["c1_1"]["asset_version_id"], before["c1_2"]["asset_version_id"]})

    def test_a_claim_that_no_longer_names_a_fact_makes_its_earlier_images_need_redoing(self):
        """An image stands on the facts its claim names now, no fewer: same words, one fact less, needs redoing."""
        project = self.finished()
        statuses = self.statuses(project)
        self.edit_claim(project, "a2", fact_ids=["f2", "f4"])
        added = self.ads(project)["c2_1"]
        self.assertEqual((added["status"], self.reasons(added)[0]), ("needs_redo", "facts_changed"))
        new = self.remake(project, "c2_1")
        self.assertEqual(self.recipe_intent(project, new["asset_version_id"])["cited_fact_ids"], ["f2", "f4"])
        self.assertFalse(REDO & set(self.reasons(new)))
        self.edit_claim(project, "a2", fact_ids=["f2"])
        dropped = self.ad_by_id(project, new["asset_version_id"])
        self.assertEqual(dropped["status"], "needs_redo")
        self.assertIn("ideas_changed", self.reasons(dropped))
        # The ways of telling still cite f2; the other claims' ads are as they were.
        self.assertEqual({tuple(e["evidence_basis"]["fact_ids"]) for e in self.angle(project, "a2")["expressions"]}, {("f2",)})
        now = self.statuses(project)
        self.assertEqual({cid: now[cid] for cid in now if cid[:3] in ("c1_", "c3_")},
                         {cid: statuses[cid] for cid in statuses if cid[:3] in ("c1_", "c3_")})

    def test_a_changed_claim_names_only_facts_with_a_source_and_says_nothing_they_do_not(self):
        project = self.finished()
        self.edit_facts(project, self.remove_fact("f1"))
        self.set_understanding(project, unsupported_claims=deepcopy(UNSOURCED))
        revision = self.read_ideas(project)["revision"]
        # A removed fact, an unknown id, a claim without a source and an AI guess are not facts.
        for fact_ids, unknown in ((["f1", "f2"], ["f1"]), (["f9"], ["f9"]), (["u1"], ["u1"]), (["i1"], ["i1"])):
            refused = self.edit_claim(project, "a2", fact_ids=fact_ids, expect=422)
            self.assertEqual((refused["code"], refused["params"]["fact_ids"]), ("claim_fact_not_found", unknown))
        # A measurement none of the named facts states.
        refused = self.edit_claim(project, "a2", proposition="Charges at 22 kW without tripping", expect=422)
        self.assertEqual((refused["code"], refused["params"]["claims"]), ("claim_unsupported", ["22 kW"]))
        # A claim the understanding lists as having no source, in its own words or by its number.
        for words in ("Charges 3x faster than a wall socket", "Up to 3X quicker every night"):
            refused = self.edit_claim(project, "a2", rationale=words, expect=422)
            self.assertEqual((refused["code"], refused["params"]["claims"]), ("claim_unsupported", [UNSOURCED[0]["text"]]))
        self.assertEqual(self.read_ideas(project)["revision"], revision)

    def test_nothing_changed_spends_no_revision_and_the_same_key_changes_it_once(self):
        project = self.finished()
        revision = self.read_ideas(project)["revision"]
        self.assertEqual(self.edit_claim(project, "a1")["revision"], revision)
        self.assertEqual(self.revisions(project, "claim_edited"), 0)
        first = self.edit_claim(project, "a1", proposition="Wake up to a charged car", key="claim-key", revision=revision)
        again = self.edit_claim(project, "a1", proposition="Wake up to a charged car", key="claim-key", revision=revision)
        self.assertEqual((first["revision"], again["revision"]), (revision + 1, revision + 1))
        self.assertEqual(self.revisions(project, "claim_edited"), 1)

    def test_a_stale_revision_and_an_unknown_claim_are_refused(self):
        project = self.finished()
        revision = self.read_ideas(project)["revision"]
        self.edit_claim(project, "a1", proposition="Wake up to a charged car")
        stale = self.edit_claim(project, "a1", proposition="Another way to say it", revision=revision, expect=409)
        self.assertEqual(stale["code"], "stale_marketing_revision")
        self.assertEqual(self.edit_claim(project, "a9", expect=404)["code"], "angle_not_found")
        self.assertEqual(self.delete_claim(project, "a9", expect=404)["code"], "angle_not_found")

    def test_nothing_is_changed_while_the_first_batch_is_still_making_ads(self):
        project = self.made()
        self.assertEqual(self.edit_claim(project, "a1", proposition="Wake up to a charged car", expect=409)["code"],
                         "first_batch_running")
        self.assertEqual(self.delete_claim(project, "a1", expect=409)["code"], "first_batch_running")


# -- delete a claim ------------------------------------------------------------------------------------


class DeleteClaimTests(ClaimFixtures):
    def test_deleting_a_claim_leaves_its_images_listed_and_never_exportable(self):
        project = self.finished()
        statuses = self.statuses(project)
        kept = self.ads(project)["c2_1"]
        self.client.put(f"/api/v1/marketing/ideas/{project['id']}/ads/{kept['asset_version_id']}/kept", json={"kept": True},
                        headers=self.headers())
        self.edit_facts(project, self.remove_fact("f2"))
        self.assertEqual(set(self.removed_citations(project)), {"c2_1", "c2_2"})
        deleted = self.delete_claim(project, "a2")
        self.assertEqual([a["id"] for a in deleted["ideas"]["angles"]], ["a1", "a3"])
        self.assertNotIn("a2", {c["angle_id"] for c in deleted["ideas"]["combinations"]})
        self.assertEqual(self.removed_citations(project), {})
        # Its images stay ads of the project, as the user left them, and need redoing: their claim is gone.
        gone = self.ads(project)
        self.assertEqual((gone["c2_1"]["status"], gone["c2_1"]["kept"]), ("needs_redo", True))
        self.assertIn("ideas_changed", self.reasons(gone["c2_1"]))
        self.assertEqual(self.download(project, ["c2_1"], expect=409)["code"], "ads_not_exportable")
        # The other claims' ads are untouched.
        self.assertEqual({cid: gone[cid]["status"] for cid in gone if cid[:3] in ("c1_", "c3_")},
                         {cid: statuses[cid] for cid in statuses if cid[:3] in ("c1_", "c3_")})
        (event,) = self.events(project, "marketing.ideas_claim_deleted")
        self.assertEqual((event["angle_id"], sorted(event["combination_ids"])), ("a2", ["c2_1", "c2_2"]))
        self.assertEqual(self.revisions(project, "claim_deleted"), 1)

    def test_two_claims_always_stay(self):
        project = self.finished()
        self.delete_claim(project, "a1")
        refused = self.delete_claim(project, "a3", expect=409)
        self.assertEqual((refused["code"], refused["params"]), ("claims_too_few", {"minimum": 2}))
        self.assertEqual([a["id"] for a in self.read_ideas(project)["ideas"]["angles"]], ["a2", "a3"])


# -- the audience --------------------------------------------------------------------------------------


class AudienceTests(ClaimFixtures):
    def test_a_confirmed_audience_is_published_until_its_words_change(self):
        project = self.checked_ads()
        market = project["market"]
        self.verify(project, fact_ids=["f1"])
        self.assertFalse(self.read_ideas(project)["audience_confirmed"])
        self.assertEqual(self.product_versions(project)[0][-1].content_json["audience"], market)
        audience = self.read_ideas(project)["understanding"]["audience"]

        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification", json={"audience_confirmed": True},
                                    headers=self.headers(self.read_ideas(project)["revision"]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["audience_confirmed"])
        self.assertEqual(self.product_versions(project)[0][-1].content_json["audience"], audience)
        self.assertEqual(self.records(project, "verification")[-1].content_json["audience"]["text"], audience)
        # Checking a fact later keeps the confirmed audience in the version.
        self.verify(project, fact_ids=["f2"])
        self.assertEqual(self.product_versions(project)[0][-1].content_json["audience"], audience)

        def change(understanding):
            understanding["audience"] = "Drivers who park in a shared driveway"
        changed = self.edit_facts(project, change)
        self.assertFalse(changed["audience_confirmed"])
        self.assertEqual(self.product_versions(project)[0][-1].content_json["audience"], market)
        (event,) = [e for e in self.events(project, "marketing.ideas_facts_edited") if e["audience_changed"]]
        self.assertIsNotNone(event["library_version_id"])
        # The new words count once the user confirms them.
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification", json={"audience_confirmed": True},
                                    headers=self.headers(self.read_ideas(project)["revision"]))
        self.assertTrue(response.json()["audience_confirmed"])
        self.assertEqual(self.product_versions(project)[0][-1].content_json["audience"], "Drivers who park in a shared driveway")

    def test_confirming_only_the_audience_is_a_verification_that_publishes_nothing_yet(self):
        project = self.checked_ads()
        empty = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification", json={},
                                 headers=self.headers(project["revision"]))
        self.assertEqual((empty.status_code, empty.json()["code"]), (422, "verification_empty"))
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification", json={"audience_confirmed": True},
                                    headers=self.headers(self.read_ideas(project)["revision"]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["audience_confirmed"])
        # Nothing else is checked: no product version is published for an audience alone.
        self.assertEqual(self.product_versions(project)[0], [])
        self.assertIsNone(self.records(project, "verification")[-1].library_version_id)
        # The first checked fact publishes the confirmed audience with it.
        self.verify(project, fact_ids=["f1"])
        self.assertEqual(self.product_versions(project)[0][-1].content_json["audience"],
                         self.read_ideas(project)["understanding"]["audience"])


# -- claims without a source -----------------------------------------------------------------------------


class UnsourcedClaimTests(ClaimFixtures):
    def test_an_ads_first_understanding_lists_the_claims_read_without_a_source(self):
        with patch("app.marketing.planner.example_result", side_effect=with_unsourced), \
                patch("tests.test_marketing_first_batch.example_result", side_effect=with_unsourced):
            project = self.finished()
        understanding = self.read_ideas(project)["understanding"]
        self.assertEqual(understanding["unsupported_claims"], UNSOURCED)
        # The drafting call was asked for them, with the ads-first schema.
        with tenant_session(self.context) as session:
            call = session.scalar(select(PlanningCallRecord).where(PlanningCallRecord.workspace_id == self.context.workspace_id,
                PlanningCallRecord.project_id == project["id"], PlanningCallRecord.kind == "understanding"))
            inputs = call.input_json
        self.assertEqual(inputs["flow"], "ads_first")
        prompt = planner.system_prompt("understanding", inputs)
        self.assertIn("unsupported_claims", prompt)
        self.assertIn("unsupported_claims", prompt.rpartition("Schema:\n")[2])
        # No claim, way of telling or ad says them.
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            doc = Ideas.model_validate(row.ideas_json)
            value = IdeasUnderstanding.model_validate(row.understanding_json)
        parts = [*(p for a in doc.angles for p in ideas._said_by_angle(a, a.id)),
                 *(p for a in doc.angles for e in a.expressions for p in ideas._said_by_expression(e, e.id)),
                 *(p for c in doc.combinations for p in ideas._said_by_combination(c, c.id))]
        self.assertEqual(ideas.unsourced_fields(value, parts), [])
        self.assertNotIn("u1", {fid for c in doc.combinations for fid in ideas.citations(row, c.id)})

    def test_drafted_ideas_that_use_a_claim_without_a_source_are_refused(self):
        understanding = IdeasUnderstanding.model_validate({**understood("understanding", {"source_text": "\n".join(
            f["source_quote"] for f in FACTS), "strategy_language": "en-US"}), "unsupported_claims": UNSOURCED})
        row = SimpleNamespace(understanding_json=understanding.model_dump(mode="json"), market="United States")
        inputs = {"understanding": row.understanding_json, "market": "United States", "ad_language": "en-US",
                  "strategy_language": "en-US"}
        clean = Ideas.model_validate(claims(inputs))
        self.assertEqual(ideas.checked_ideas(row, clean, inputs).combinations, clean.combinations)
        for field, words in (("headline", "Charges 3x faster tonight"), ("body_copy", "The quietest home charger on the market.")):
            drafted = claims(inputs)
            drafted["combinations"][1][field] = words
            with self.assertRaises(ideas.service.MarketingError) as refused:
                ideas.checked_ideas(row, Ideas.model_validate(drafted), inputs)
            self.assertEqual(refused.exception.code, "unsourced_claim_used")
            self.assertEqual([f["field"] for f in refused.exception.field_errors], [f"body.ideas.combinations.1.{field}"])
        # Saying why evidence is missing names it without using it.
        drafted = claims(inputs)
        drafted["angles"][0]["rationale"] = "The page says it charges 3x faster than a wall socket, with no source."
        ideas.checked_ideas(row, Ideas.model_validate(drafted), inputs)

    def test_an_addition_that_uses_a_claim_without_a_source_is_refused(self):
        project = self.finished()
        self.set_understanding(project, unsupported_claims=deepcopy(UNSOURCED))

        original = ideas.example_addition

        def addition(inputs):
            added = original(inputs)
            added["combination"]["headline"] = "3x faster than a wall socket"
            return added
        with patch("app.marketing.ideas.example_addition", side_effect=addition):
            self.add(project, {"kind": "expression", "angle_id": "a1"})
            self.drain(job_types=("marketing.plan",))
        call = self.read_ideas(project)["latest_call"]
        self.assertEqual((call["state"], call["error_code"]), ("failed", "planner_output_invalid"))
        with tenant_session(self.context) as session:
            errors = session.get(PlanningCallRecord, call["id"]).output_json["validation_errors"]
        self.assertEqual([(e["type"], e["loc"]) for e in errors], [("unsourced_claim_used", ["combination", "headline"])])

    def test_adding_a_source_makes_an_unchecked_fact_and_dont_need_it_takes_it_off(self):
        project = self.checked_ads()
        self.set_understanding(project, unsupported_claims=deepcopy(UNSOURCED))
        quote = "In our lab test it charged 3x faster than a standard wall socket."

        def sourced(understanding):
            understanding["unsupported_claims"] = [c for c in understanding["unsupported_claims"] if c["id"] != "u1"]
            understanding["facts"].append({"id": "fact-lab", "statement": "It charged 3x faster than a wall socket in a lab test.",
                                           "source_quote": quote, "confirmation": "confirmed"})
        after = self.edit_facts(project, sourced, source_addition=quote)["understanding"]
        self.assertEqual(after["unsupported_claims"], [UNSOURCED[1]])
        self.assertEqual(after["facts"][-1]["confirmation"], "unconfirmed")
        self.assertEqual(self.events(project, "marketing.ideas_facts_edited")[-1]["unsupported_removed"], ["u1"])

        def dont_need(understanding):
            understanding["unsupported_claims"] = []
        self.assertEqual(self.edit_facts(project, dont_need)["understanding"]["unsupported_claims"], [])

    def test_the_list_only_loses_items_the_ai_wrote(self):
        project = self.checked_ads()
        self.set_understanding(project, unsupported_claims=deepcopy(UNSOURCED))

        def rewrite(understanding):
            understanding["unsupported_claims"] = [{"id": "u2", "text": "Changed words"}, {"id": "u9", "text": "Added by a client"}]
        self.assertEqual(self.edit_facts(project, rewrite)["understanding"]["unsupported_claims"], [UNSOURCED[1]])

    def test_an_understanding_drafted_before_the_list_reads_and_edits_as_before(self):
        project = self.checked_ads()
        # As stored before #224: no list at all.
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            stored = dict(row.understanding_json)
            stored.pop("unsupported_claims", None)
            row.understanding_json = stored
        current = self.read_ideas(project)
        self.assertEqual(current["understanding"]["unsupported_claims"], [])
        # Sending it back unchanged changes nothing and spends no revision.
        self.assertEqual(self.edit_facts(project, lambda understanding: None)["revision"], current["revision"])

    def test_the_literal_guard(self):
        self.assertEqual(literal_claims("Charges 3x faster, 11 kW, $499, UL 2594"), ["$499", "11 kW", "3x", "UL 2594"])
        listed = [("u1", UNSOURCED[0]["text"]), ("u2", UNSOURCED[1]["text"])]
        self.assertEqual(unsourced_uses("Now 3 x faster!", listed, ""), ["u1"])
        self.assertEqual(unsourced_uses("now 3x faster", listed, "Tested: 3x faster than a socket"), [])
        self.assertEqual(unsourced_uses("The quietest home charger on the market, period.", listed, ""), ["u2"])
        self.assertEqual(unsourced_uses("A quiet charger", listed, ""), [])


# -- the plan-first flow and other workspaces --------------------------------------------------------------


class BoundaryTests(ClaimFixtures):
    def test_the_plan_first_understanding_is_unchanged(self):
        self.assertNotIn("unsupported_claims", Understanding.model_json_schema()["properties"])
        prompt = planner.system_prompt("understanding", {"source_text": "Acme Charge Home", "strategy_language": "en-US"})
        self.assertNotIn("unsupported_claims", prompt)
        self.assertEqual(json.loads(prompt.rpartition("Schema:\n")[2]), Understanding.model_json_schema())
        old = self.create()
        base = f"/api/v1/marketing/ideas/{old['id']}/claims/a1"
        for response in (self.client.patch(base, headers=self.headers(old["revision"]),
                                           json={"proposition": "A claim", "rationale": "A reason", "fact_ids": []}),
                         self.client.delete(base, headers=self.headers(old["revision"]))):
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"))

    def test_another_workspace_can_neither_change_a_claim_nor_confirm_the_audience(self):
        project = self.finished()
        before = self.read_ideas(project)
        owner = self.context
        self.context = self._make_context()
        base = f"/api/v1/marketing/ideas/{project['id']}"
        attempts = (
            self.client.patch(f"{base}/claims/a1", headers=self.headers(before["revision"]),
                              json={"proposition": "A claim", "rationale": "A reason", "fact_ids": ["f1"]}),
            self.client.delete(f"{base}/claims/a1", headers=self.headers(before["revision"])),
            self.client.post(f"{base}/verification", headers=self.headers(before["revision"]), json={"audience_confirmed": True}),
            self.client.patch(f"{base}/understanding", headers=self.headers(before["revision"]),
                              json={"understanding": {**before["understanding"], "unsupported_claims": []}}),
        )
        for response in attempts:
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"), response.text)
        self.context = owner
        after = self.read_ideas(project)
        self.assertEqual((after["revision"], after["ideas"], after["audience_confirmed"]),
                         (before["revision"], before["ideas"], False))
        self.assertEqual(self.records(project, "verification"), [])


# -- the gates of #225 and #219 ------------------------------------------------------------------------


class ClaimGateTests(ClaimCommands, RoundGateFixtures):
    """A changed or deleted claim makes its images need redoing (#224); every way out that asks the ads' status
    -- reuse into a round, the round's handoff, an experiment, the creative library's download (#219, #225) --
    then refuses them like any other ad that needs redoing."""

    def ideas_and_round(self):
        """The fixture's ideas and round, with the first batch ended so its claims may be changed."""
        ideas, project, row = super().ideas_and_round()
        self.drain(job_types=("marketing.first_batch",))
        self.assertEqual(self.read_ideas(ideas)["first_batch"]["state"], "done")
        return ideas, project, row

    def blocked(self, row, asset_version_id):
        block = self.listed(row)[asset_version_id]["reuse_block"]
        self.assertEqual(block["status"], "needs_redo")
        self.assertIn("ideas_changed", [reason["code"] for reason in block["reasons"]])
        self.refused(self.reuse(row, asset_version_id, expect=409), "ad_not_reusable")
        self.assertEqual(self.link(asset_version_id, expect=409)["code"], "ads_not_exportable")

    def test_an_ad_checked_when_reused_is_not_handed_off_once_its_claim_changed(self):
        ideas, _, row = self.ideas_and_round()
        checked, other = self.ids(ideas, ["c3_1", "c3_2"])
        row = self.approved(self.reuse(row, checked))
        self.edit_claim(ideas, "a3", proposition="Safe to leave in the driveway")
        refused = self.refused(self.handoff(row, expect=409), "ads_not_exportable")
        self.assertEqual((list(refused), refused[checked]["status"]), ([checked], "needs_redo"))
        self.nothing_handed_off(row)
        self.blocked(row, other)

    def test_the_images_of_a_deleted_claim_go_nowhere(self):
        ideas, _, row = self.ideas_and_round()
        checked, other = self.ids(ideas, ["c3_1", "c3_2"])
        row = self.approved(self.reuse(row, checked))
        self.delete_claim(ideas, "a3")
        self.refused(self.handoff(row, expect=409), "ads_not_exportable")
        self.blocked(row, other)
        experiment = self.client.post("/api/v1/experiments", headers=self.headers(), json={
            "product_id": ideas["product_id"], "name": "Acme Charge Home ads", "draft_context": _draft_context()}).json()
        response = self.client.post(f"/api/v1/experiments/{experiment['id']}/entries", headers=self.headers(),
                                    json={"asset_version_id": other, "expected_revision": 1, "marketing_vars": {}})
        self.assertEqual(response.status_code, 409, response.text)
        self.refused(response.json(), "ad_not_reusable")

    def test_an_image_on_a_fact_its_claim_no_longer_names_goes_nowhere(self):
        ideas, _, row = self.ideas_and_round()
        # Claim 3 also names f4: a new image of it stands on f3 (through the guess i1) and f4.
        self.edit_claim(ideas, "a3", fact_ids=["f4"])
        new = self.remake(ideas, "c3_1")
        self.assertEqual(self.intent_of(ideas, new["asset_version_id"])["cited_fact_ids"], ["f3", "f4"])
        self.start_checks(ideas)
        self.drain(job_types=("marketing.ai_qc",))
        current = self.ad_by_id(ideas, new["asset_version_id"])
        self.verify(ideas, fact_ids=["f4"], findings=[])
        response = self.client.post(f"/api/v1/marketing/ideas/{ideas['id']}/verification", headers=self.headers(
            self.read_ideas(ideas)["revision"]), json={"appearance_confirmed": [new["asset_version_id"]], "accepted_findings": [
                {"asset_version_id": new["asset_version_id"], "check_id": current["check"]["id"],
                 "finding_ids": [f["check_id"] for f in current["check"]["findings"]]}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.ad_by_id(ideas, new["asset_version_id"])["status"], "exportable")
        self.assertNotIn("reuse_block", {k for k, v in self.listed(row)[new["asset_version_id"]].items() if v})
        # Same words, f4 taken off: the image stands on a fact the claim no longer names.
        self.edit_claim(ideas, "a3", fact_ids=[])
        self.assertEqual(self.reasons(self.ad_by_id(ideas, new["asset_version_id"])), ["ideas_changed"])
        self.blocked(row, new["asset_version_id"])


# -- the audience beside the photos of #229 -------------------------------------------------------------


class AudienceAndPhotoTests(PhotoFixtures):
    """One verification takes facts, looks, findings, photos and the audience (#199, #224): together or apart,
    the published version holds each of them, and none undoes another."""

    def audience_only(self, project):
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification", json={"audience_confirmed": True},
                                    headers=self.headers(self.read_ideas(project)["revision"]))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_facts_a_photo_and_the_audience_in_one_verification(self):
        project = self.made()
        main = self.photos(project)["main"]["photo_id"]
        audience = self.read_ideas(project)["understanding"]["audience"]
        current = self.read_ideas(project)
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification", headers=self.headers(current["revision"]),
                                    json={"fact_ids": ["f1"], "photo_ids": [main], "audience_confirmed": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["audience_confirmed"])
        self.assertEqual(self.photos(project)["main"]["status"], "verified")
        version, _ = self.published_version(project)
        self.assertEqual((version.content_json["audience"], len(version.content_json["reference_bindings"]),
                          version.content_json["selling_points"]), (audience, 1, [FACTS[0]["statement"]]))
        (record,) = self.records(project, "verification")
        self.assertEqual((record.content_json["audience"]["text"], [p["photo_id"] for p in record.content_json["photos"]]),
                         (audience, [main]))

    def test_the_audience_and_the_photos_checked_apart_keep_each_other(self):
        project = self.made()
        main = self.photos(project)["main"]["photo_id"]
        audience = self.read_ideas(project)["understanding"]["audience"]
        self.audience_only(project)
        self.assertEqual(self.published_version(project)[0], None)
        # A photo alone publishes a version; it names the audience the user confirmed.
        self.decide(project, photo_ids=[main])
        version, _ = self.published_version(project)
        self.assertEqual((version.content_json["audience"], len(version.content_json["reference_bindings"])), (audience, 1))
        # Rejecting the photo takes it out and leaves the audience; changing the audience leaves the photos alone.
        self.decide(project, rejected=[main])
        version, _ = self.published_version(project)
        self.assertEqual((version.content_json["audience"], version.content_json.get("reference_bindings")), (audience, None))
        self.decide(project, photo_ids=[main])

        def renamed(understanding):
            understanding["audience"] = "Drivers who park in a shared driveway"
        self.edit_facts(project, renamed)
        version, _ = self.published_version(project)
        self.assertEqual((version.content_json["audience"], len(version.content_json["reference_bindings"])),
                         ("United States", 1))
        self.assertEqual(self.photos(project)["main"]["status"], "verified")
        self.assertFalse(self.read_ideas(project)["audience_confirmed"])
