"""Change one ad's headline and body copy in the user's own words (#228; ADR 0018, design doc §5.3).

The ads come from a real first batch on the fictional Acme Charge page (fake page, fake model, fake image
provider; ``IdeasExportFixtures``): claim a1 cites f1, a2 cites f2, a3 cites f3 only through the guess i1,
and no ad cites f4. Each claim has two ways of telling, one ad each (c1_1 ... c3_2), in en-US. New images go
through the real generation preview, submission and Worker. No paid call is made.
"""
from copy import deepcopy
from unittest.mock import patch

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.library.contracts import ROLE_OWNER
from app.main import app
from app.marketing.ideas_contracts import IdeasProjectDetail
from app.marketing.models import MarketingProjectRecord, MarketingRevisionRecord
from app.platform.http_identity import request_tenant_context
from tests.test_marketing_ideas_claims import UNSOURCED, ClaimFixtures

HEADLINE = "Charge on your schedule"
BODY = "Set overnight charging from the Acme Charge app and wake up ready to drive."
#: Copy any claim may carry: it states no number, price or certification.
PLAIN = ("Charging made simple", "Plug in tonight and let the charger do the rest.")


class CopyCommands:
    def edit_copy(self, project, combination_id, headline=HEADLINE, body_copy=BODY, *, expect=200, key=None,
                  revision=None):
        current = self.read_ideas(project)
        response = self.client.patch(f"/api/v1/marketing/ideas/{project['id']}/combinations/{combination_id}",
                                     json={"headline": headline, "body_copy": body_copy},
                                     headers=self.headers(revision or current["revision"], key))
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            IdeasProjectDetail.model_validate(response.json())
            self.assertEqual(response.headers["etag"], f'"{response.json()["revision"]}"')
            self.assertNotIn("location", response.headers)
        return response.json()

    def combination(self, project, combination_id):
        return next(c for c in self.read_ideas(project)["ideas"]["combinations"] if c["id"] == combination_id)

    def by_version(self, project):
        return {ad["asset_version_id"]: ad for ad in self.read_ideas(project)["ads"]}

    def keep(self, project, asset_version_id):
        response = self.client.put(f"/api/v1/marketing/ideas/{project['id']}/ads/{asset_version_id}/kept",
                                   json={"kept": True}, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)

    def prepared_draft(self, project, combination_id):
        """The generation draft of one ad at the current revision, as "make one with the new copy" prepares it."""
        current = self.read_ideas(project)
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/generation-drafts",
                                    headers=self.headers(current["revision"]), json={"provider": "mock", "model": "mock-v1"})
        self.assertEqual(response.status_code, 201, response.text)
        draft_id = next(d["draft_id"] for d in response.json()["drafts"] if d["combination_id"] == combination_id)
        return self.client.get(f"/api/v1/generation/drafts/{draft_id}").json()


class CopyFixtures(CopyCommands, ClaimFixtures):
    pass


class ChangeCopyTests(CopyFixtures):
    def test_changing_the_copy_makes_its_images_need_redoing_and_touches_no_other(self):
        project = self.finished()
        before = self.ads(project)
        self.keep(project, before["c1_1"]["asset_version_id"])
        old = self.combination(project, "c1_1")
        revision = self.read_ideas(project)["revision"]

        changed = self.edit_copy(project, "c1_1")
        self.assertEqual(changed["revision"], revision + 1)
        new = next(c for c in changed["ideas"]["combinations"] if c["id"] == "c1_1")
        self.assertEqual((new["headline"], new["body_copy"]), (HEADLINE, BODY))
        # Only the copy changed: the claim, the way of telling, the facts and the English image brief stay.
        self.assertEqual({k: v for k, v in new.items() if k not in ("headline", "body_copy")},
                         {k: v for k, v in old.items() if k not in ("headline", "body_copy")})
        self.assertEqual(changed["ideas"]["angles"], project["ideas"]["angles"])
        self.assertEqual(changed["understanding"], project["understanding"])

        # The image made with the old copy is not this ad any more; every other ad is as it was.
        after = self.ads(project)
        self.assertEqual(after["c1_1"]["status"], "needs_redo")
        self.assertIn("ideas_changed", self.reasons(after["c1_1"]))
        self.assertTrue(after["c1_1"]["kept"])
        self.assertEqual({cid: (ad["status"], ad["reasons"]) for cid, ad in after.items() if cid != "c1_1"},
                         {cid: (ad["status"], ad["reasons"]) for cid, ad in before.items() if cid != "c1_1"})
        self.assertEqual(self.download(project, ["c1_1"], expect=409)["code"], "ads_not_exportable")

        # Recorded: a new version of the ideas, the old copy still in the one before, and an audit event.
        with tenant_session(self.context) as session:
            snapshots = {r.revision: r for r in session.scalars(select(MarketingRevisionRecord).where(
                MarketingRevisionRecord.workspace_id == self.context.workspace_id,
                MarketingRevisionRecord.project_id == project["id"])).all()}
            self.assertEqual(snapshots[revision + 1].reason, "copy_edited")
            self.assertEqual(next(c["headline"] for c in snapshots[revision + 1].content_json["ideas"]["combinations"]
                                  if c["id"] == "c1_1"), HEADLINE)
            self.assertEqual(next(c["headline"] for c in snapshots[revision].content_json["ideas"]["combinations"]
                                  if c["id"] == "c1_1"), old["headline"])
        (event,) = self.events(project, "marketing.ideas_copy_edited")
        self.assertEqual((event["combination_id"], event["angle_id"], event["expression_id"], event["revision"]),
                         ("c1_1", "a1", old["expression_id"], revision + 1))
        self.assertEqual((sorted(event["changed"]), event["asset_version_ids"]),
                         (["body_copy", "headline"], [before["c1_1"]["asset_version_id"]]))

    def test_a_new_image_carries_the_new_copy_and_is_downloaded_only_once_checked(self):
        project = self.finished()
        old = self.ads(project)["c1_1"]
        self.edit_copy(project, "c1_1")
        draft = self.prepared_draft(project, "c1_1")
        prompt = draft["selectors"]["prompt"]
        self.assertIn(f'Render the headline text exactly as: "{HEADLINE}"', prompt)
        self.assertIn(f'Render the body text exactly as: "{BODY}"', prompt)
        before = set(self.by_version(project))
        self.make(draft["id"])
        (made,) = [ad for vid, ad in self.by_version(project).items() if vid not in before]
        self.assertEqual(made["combination_id"], "c1_1")
        intent = self.recipe_intent(project, made["asset_version_id"])
        self.assertEqual((intent["combination"]["headline"], intent["combination"]["body_copy"]), (HEADLINE, BODY))
        # A new image is a draft: it is checked like any other and cannot leave before the user's checking.
        self.assertEqual(made["status"], "draft")
        self.assertNotIn("ideas_changed", self.reasons(made))
        refused = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/download-intent", headers=self.headers(),
                                   json={"asset_version_ids": [made["asset_version_id"]]})
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "ads_not_exportable"))
        self.assertEqual(self.export_everything(project, made["asset_version_id"], ["f1"])["status"], "exportable")
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/download-intent", headers=self.headers(),
                                    json={"asset_version_ids": [made["asset_version_id"]]})
        self.assertEqual(response.status_code, 201, response.text)
        # The image made with the old copy still needs making again: checking never rescues it.
        self.assertEqual(self.ad_by_id(project, old["asset_version_id"])["status"], "needs_redo")

    def test_nothing_changed_spends_no_revision_and_the_same_key_changes_it_once(self):
        project = self.finished()
        revision = self.read_ideas(project)["revision"]
        current = self.combination(project, "c2_1")
        self.assertEqual(self.edit_copy(project, "c2_1", current["headline"], current["body_copy"])["revision"], revision)
        self.assertEqual(self.revisions(project, "copy_edited"), 0)
        first = self.edit_copy(project, "c2_1", *PLAIN, key="copy-key", revision=revision)
        again = self.edit_copy(project, "c2_1", *PLAIN, key="copy-key", revision=revision)
        self.assertEqual((first["revision"], again["revision"]), (revision + 1, revision + 1))
        self.assertEqual(self.revisions(project, "copy_edited"), 1)
        self.assertEqual(len(self.events(project, "marketing.ideas_copy_edited")), 1)

    def test_a_stale_revision_and_an_unknown_ad_are_refused(self):
        project = self.finished()
        revision = self.read_ideas(project)["revision"]
        self.edit_copy(project, "c1_1")
        stale = self.edit_copy(project, "c1_1", *PLAIN, revision=revision, expect=409)
        self.assertEqual(stale["code"], "stale_marketing_revision")
        self.assertEqual(self.edit_copy(project, "c9_9", expect=404)["code"], "combination_not_found")
        self.assertEqual(self.combination(project, "c1_1")["headline"], HEADLINE)

    def test_nothing_is_changed_while_the_first_batch_is_still_making_ads(self):
        project = self.made()
        self.assertEqual(self.edit_copy(project, "c1_1", expect=409)["code"], "first_batch_running")


class CopyRulesTests(CopyFixtures):
    def test_empty_too_long_and_other_language_copy_is_refused(self):
        project = self.finished()
        revision = self.read_ideas(project)["revision"]
        for headline, body, field, code in (("", BODY, "headline", "string_too_short"),
                                            (HEADLINE, "   ", "body_copy", "string_too_short"),
                                            ("x" * 2001, BODY, "headline", "string_too_long")):
            refused = self.edit_copy(project, "c1_1", headline, body, expect=422)
            self.assertEqual(refused["code"], "validation_error")
            self.assertEqual([(e["field"], e["code"]) for e in refused["field_errors"]], [(f"body.{field}", code)])
        # Not the ads' language (en-US): only the text in another script is named.
        refused = self.edit_copy(project, "c1_1", "晚上插上，不用再管", BODY, expect=422)
        self.assertEqual((refused["code"], [e["field"] for e in refused["field_errors"]]),
                         ("copy_language_mismatch", ["body.headline"]))
        # Under the 2,000 characters of each text, but too long for the image prompt to hold.
        refused = self.edit_copy(project, "c1_1", "Charge " * 280, "Ready by morning " * 110, expect=422)
        self.assertEqual(refused["code"], "ad_copy_too_long")
        self.assertGreater(refused["params"]["over"], 0)
        self.assertEqual(self.read_ideas(project)["revision"], revision)

    def test_copy_may_state_only_what_the_facts_of_its_ad_state(self):
        project = self.finished()
        # f1 -- the fact behind claim 1 -- states a power; f2 behind claim 2 does not.
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            understanding = deepcopy(row.understanding_json)
            understanding["facts"][0]["statement"] = "Charging at up to 7 kW can be scheduled from the Acme Charge app."
            understanding["unsupported_claims"] = deepcopy(UNSOURCED)
            row.understanding_json = understanding
        revision = self.read_ideas(project)["revision"]
        for headline, body, claims in (
                ("Charges at 22 kW", BODY, ["22 kW"]),
                (HEADLINE, "Certified to UL 2594, from $499.", ["$499", "UL 2594"]),
                # A claim the product material makes without a source, in its own words or by its number.
                (HEADLINE, "Charges 3x faster than a wall socket", [UNSOURCED[0]["text"]]),
                ("Up to 3X quicker every night", BODY, [UNSOURCED[0]["text"]]),
                # Stated by a fact, but not by one this ad cites.
                ("Up to 7 kW, overnight", BODY, ["7 kW"])):
            refused = self.edit_copy(project, "c2_1", headline, body, expect=422)
            self.assertEqual((refused["code"], refused["params"]["claims"]), ("copy_unsupported", claims), headline)
        self.assertEqual(self.read_ideas(project)["revision"], revision)
        # The ad of claim 1 cites f1, which states it.
        changed = self.edit_copy(project, "c1_1", "Up to 7 kW, overnight", BODY)
        self.assertEqual(next(c["headline"] for c in changed["ideas"]["combinations"] if c["id"] == "c1_1"),
                         "Up to 7 kW, overnight")


class UploadedImageTests(CopyFixtures):
    """An uploaded image is the user's own picture with the user's own words: a change of the copy of the place
    it was put under leaves it -- and a crop of it -- as it is; any other change of the place still counts."""

    def test_an_uploaded_image_and_its_crop_keep_their_status_when_the_copy_changes(self):
        project = self.finished()
        creative_id, upload_id = self.uploaded(project)
        self.place(project, creative_id, "c2_2")
        self.crop(project, asset_version_id=upload_id)
        self.drain(job_types=("marketing.image_edit",))
        (cropped,) = [ad["asset_version_id"] for ad in self.by_origin(project, "image_edit")]
        before = self.by_version(project)
        (generated,) = [vid for vid, ad in before.items() if ad["combination_id"] == "c2_2" and ad["origin"] == "generated"]

        changed = self.edit_copy(project, "c2_2", *PLAIN)
        after = self.by_version(project)
        self.assertIn("ideas_changed", self.reasons(after[generated]))
        for version_id in (upload_id, cropped):
            self.assertEqual((after[version_id]["status"], after[version_id]["reasons"]),
                             (before[version_id]["status"], before[version_id]["reasons"]))
            self.assertNotIn("ideas_changed", self.reasons(after[version_id]))
        self.assertEqual({vid: (ad["status"], ad["reasons"]) for vid, ad in after.items() if vid != generated},
                         {vid: (ad["status"], ad["reasons"]) for vid, ad in before.items() if vid != generated})
        (event,) = self.events(project, "marketing.ideas_copy_edited")
        self.assertEqual(event["asset_version_ids"], [generated])
        self.assertEqual(changed["revision"], self.read_ideas(project)["revision"])

        # The upload can still be cropped, and the Worker makes the crop: its place is still its place.
        self.crop(project, asset_version_id=upload_id, crop={"x": 0, "y": 0, "width": 0.5, "height": 0.5}, rotation=0)
        self.drain(job_types=("marketing.image_edit",))
        self.assertEqual([edit["state"] for edit in self.read_ideas(project)["image_edits"]], ["succeeded", "succeeded"])
        crops = [ad for ad in self.by_origin(project, "image_edit") if ad["asset_version_id"] != cropped]
        self.assertEqual(len(crops), 1)
        self.assertNotIn("ideas_changed", self.reasons(crops[0]))

        # A changed claim is another ad, for the upload too (#224, unchanged).
        self.edit_claim(project, "a2", proposition="Charge without tripping the house")
        for version_id in (upload_id, cropped):
            self.assertIn("ideas_changed", self.reasons(self.ad_by_id(project, version_id)))


class BoundaryTests(CopyFixtures):
    def test_an_owner_and_a_member_may_change_the_copy(self):
        project = self.finished()
        member = self.context
        self.assertEqual(member.role, "member")
        self.edit_copy(project, "c1_1")
        self.context = self._make_context(member.workspace_id, role=ROLE_OWNER, actor="owner_copy")
        self.edit_copy(project, "c1_2", *PLAIN)
        self.context = member

    def test_another_workspace_and_a_request_without_identity_change_nothing(self):
        project = self.finished()
        before = self.read_ideas(project)
        url = f"/api/v1/marketing/ideas/{project['id']}/combinations/c1_1"
        body = {"headline": HEADLINE, "body_copy": BODY}
        owner = self.context
        self.context = self._make_context()
        for response in (self.client.patch(url, json=body, headers=self.headers(before["revision"])),
                         self.client.get(f"/api/v1/marketing/ideas/{project['id']}")):
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"))
        self.context = owner
        app.dependency_overrides.pop(request_tenant_context)
        try:
            with patch("app.config.LOCAL_IDENTITY_ENABLED", False):
                response = self.client.patch(url, json=body, headers=self.headers(before["revision"]))
        finally:
            app.dependency_overrides[request_tenant_context] = lambda: self.context
        self.assertEqual((response.status_code, response.json()["code"]), (401, "delegated_identity_required"))
        after = self.read_ideas(project)
        self.assertEqual((after["revision"], after["ideas"]), (before["revision"], before["ideas"]))
        self.assertEqual(self.events(project, "marketing.ideas_copy_edited"), [])

    def test_a_plan_first_project_has_no_copy_command(self):
        old = self.create()
        response = self.client.patch(f"/api/v1/marketing/ideas/{old['id']}/combinations/c1",
                                     json={"headline": HEADLINE, "body_copy": BODY}, headers=self.headers(old["revision"]))
        self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"))
        # The plan-first project itself is untouched.
        self.assertEqual(self.read(old)["revision"], old["revision"])
