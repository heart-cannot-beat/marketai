"""The creative library does not hand out a draft of ads-first ideas (#219; ADR 0018 §2).

The library's two general paths -- a download intent for one image, and an export package -- serve every
image of a workspace. An ad of ads-first ideas leaves through them only when it is exportable, by the same
judgement the ads-first download uses (``app.marketing.ideas_export.statuses``); it is still shown on every
page that lists it. Any other image is handed out exactly as before.

The ads come from a real first batch (fake page, fake model, fake image provider), the plan-first image from
a real confirmed plan, and packages are built by the real Worker. No paid call is made. The product is the
fictional Acme Charge Home (see ``tests.test_marketing_ideas_export``).
"""
from unittest.mock import patch

from sqlalchemy import func, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord, DownloadIntentRecord, JobRecord, OperationRecord
from app.generation.models import ExportPackageRecord
from app.generation.response_models import AssetGalleryPage, DownloadIntentResponse, ExportPackageDetail
from app.marketing import ideas_export
from app.marketing.models import MarketingProjectRecord
from tests.test_marketing_ideas_export import ADS_OF, IdeasExportFixtures
from tests.test_marketing_workflow import MarketingWorkflowFixtures

EVERY_AD = [combination for combinations in ADS_OF.values() for combination in combinations]


class LibraryGateFixtures(IdeasExportFixtures, MarketingWorkflowFixtures):
    def ids(self, project, combination_ids=EVERY_AD):
        ads = self.ads(project)
        return [ads[combination_id]["asset_version_id"] for combination_id in combination_ids]

    def gallery(self):
        """The library's first page, by image id."""
        response = self.client.get("/api/v1/generation/assets", params={"limit": 100})
        self.assertEqual(response.status_code, 200, response.text)
        AssetGalleryPage.model_validate(response.json())
        return {item["id"]: item for item in response.json()["items"]}

    def link(self, asset_version_id, purpose=None, expect=201, key=None):
        """Ask for a signed link to one image: to save it (the default) or to show it."""
        response = self.client.post(f"/api/v1/generation/asset-versions/{asset_version_id}/download-intent",
                                    params={"purpose": purpose} if purpose else None, headers=self.headers(key=key))
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 201:
            self.assertTrue(DownloadIntentResponse.model_validate(response.json()).download_grant.url)
        return response.json()

    def package(self, selector, expect=202, key=None):
        response = self.client.post("/api/v1/generation/exports", json={"selector": selector}, headers=self.headers(key=key))
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 202:
            # The BFF answers 502 for a Location the contract policy does not declare.
            self.assertEqual(response.headers["Location"], f"/api/v1/operations/{response.json()['operation_id']}")
        return response.json()

    def built(self, selector):
        """A package of ``selector``, built by the Worker."""
        package = self.package(selector)
        self.drain(job_types=("generation.export_package",))
        return self.detail(package["package_id"])

    def detail(self, package_id, expect=200):
        response = self.client.get(f"/api/v1/generation/exports/{package_id}")
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            ExportPackageDetail.model_validate(response.json())
        return response.json()

    def packaged(self, package_id):
        response = self.client.get(f"/api/v1/generation/exports/{package_id}/manifest")
        self.assertEqual(response.status_code, 200, response.text)
        return [entry["asset_version_id"] for entry in response.json()["entries"]]

    def package_link(self, package_id, expect=201, key=None):
        response = self.client.post(f"/api/v1/generation/exports/{package_id}/download-intent", headers=self.headers(key=key))
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 201:
            self.assertTrue(DownloadIntentResponse.model_validate(response.json()).download_grant.url)
        return response.json()

    def approve(self, asset_version_id):
        response = self.client.post(f"/api/v1/generation/asset-versions/{asset_version_id}/reviews",
                                    json={"review_value": "approved"}, headers=self.headers())
        self.assertEqual(response.status_code, 201, response.text)

    def refused_ads(self, refusal):
        self.assertEqual(refusal["code"], "ads_not_exportable")
        self.assertEqual(refusal["params"]["count"], len(refusal["params"]["ads"]))
        return {item["asset_version_id"]: item for item in refusal["params"]["ads"]}

    def plan_first_images(self):
        """The images the plan-first flow made from a confirmed plan, one per combination."""
        before = set(self.images_of_workspace())
        self.generate(self.confirmed())
        made = sorted(set(self.images_of_workspace()) - before)
        self.assertTrue(made)
        return made

    def images_of_workspace(self):
        with tenant_session(self.context) as session:
            return session.scalars(select(AssetVersionRecord.id).where(
                AssetVersionRecord.workspace_id == self.context.workspace_id)).all()


class GalleryTests(LibraryGateFixtures):
    def test_the_library_lists_ads_first_images_and_marks_the_ones_that_may_not_leave(self):
        project = self.checked_ads()
        ads = {ad["asset_version_id"]: ad for ad in self.read_ideas(project)["ads"]}
        listed = self.gallery()
        # Every ad is in the library like any other image, and each draft says why it may not leave.
        self.assertEqual(set(ads), set(listed))
        for asset_id, ad in ads.items():
            self.assertEqual(listed[asset_id]["export_block"], {"status": "draft", "reasons": ad["reasons"]})
            self.assertEqual(listed[asset_id]["origin_kind"], "generation")

        self.verify_everything(project)
        self.assertEqual({item["export_block"] for item in self.gallery().values()}, {None})

        self.edit_facts(project, self.set_fact("f1", statement="Charging can be scheduled from the app or the wall unit."))
        listed, ads = self.gallery(), {ad["asset_version_id"]: ad for ad in self.read_ideas(project)["ads"]}
        for asset_id in self.ids(project, ADS_OF["f1"]):
            self.assertEqual(listed[asset_id]["export_block"], {"status": "needs_redo", "reasons": ads[asset_id]["reasons"]})
            self.assertEqual([(r["code"], r["fact_ids"]) for r in ads[asset_id]["reasons"]][:1], [("facts_changed", ["f1"])])
        for asset_id in self.ids(project, ADS_OF["f2"] + ADS_OF["f3"]):
            self.assertIsNone(listed[asset_id]["export_block"])


class DownloadIntentTests(LibraryGateFixtures):
    def test_a_page_may_show_a_draft_but_nobody_may_save_it(self):
        project = self.checked_ads()
        ads = self.ads(project)
        draft = ads["c1_1"]["asset_version_id"]
        for purpose in (None, "download"):
            refused = self.refused_ads(self.link(draft, purpose, expect=409))
            self.assertEqual(refused[draft], {"asset_version_id": draft, "status": "draft", "reasons": ads["c1_1"]["reasons"]})
        # Nothing was handed out for saving.
        self.assertEqual(self.count(DownloadIntentRecord), 0)
        self.link(draft, "display")
        self.assertEqual(self.count(DownloadIntentRecord, owner_id=draft), 1)

    def test_a_checked_ad_is_saved_like_any_image(self):
        project = self.checked_ads()
        self.verify_everything(project)
        for asset_id in self.ids(project, ["c1_1", "c3_2"]):
            self.link(asset_id)
            self.link(asset_id, "display")

    def test_an_ad_that_needs_redoing_is_shown_but_not_saved(self):
        project = self.checked_ads()
        self.verify_everything(project)
        self.edit_facts(project, lambda understanding: understanding["facts"].pop(1))  # f2 is removed
        stale, untouched = self.ids(project, ["c2_1", "c1_1"])
        refused = self.refused_ads(self.link(stale, expect=409))
        self.assertEqual(refused[stale]["status"], "needs_redo")
        self.assertEqual([(r["code"], r["fact_ids"]) for r in refused[stale]["reasons"]][:1], [("facts_removed", ["f2"])])
        self.link(stale, "display")
        self.link(untouched)

    def test_a_repeated_request_is_refused_once_the_ad_stopped_being_exportable(self):
        project = self.checked_ads()
        self.verify_everything(project)
        asset_id = self.ids(project, ["c1_1"])[0]
        first = self.link(asset_id, key="save-c1-1")
        self.assertEqual(self.link(asset_id, key="save-c1-1")["intent_id"], first["intent_id"])
        self.edit_facts(project, self.set_fact("f1", statement="Charging can be scheduled from the app or the wall unit."))
        self.refused_ads(self.link(asset_id, key="save-c1-1", expect=409))
        self.assertEqual(self.count(DownloadIntentRecord, owner_id=asset_id), 1)

    def test_a_request_without_a_purpose_is_the_download_it_always_was(self):
        project = self.checked_ads()
        self.verify_everything(project)
        asset_id = self.ids(project, ["c1_1"])[0]
        first = self.link(asset_id, key="unsaid")
        # Same key, same request: the purpose only says out loud what was meant before.
        self.assertEqual(self.link(asset_id, "download", key="unsaid")["intent_id"], first["intent_id"])
        self.assertEqual(self.count(DownloadIntentRecord, owner_id=asset_id), 1)

    def test_one_key_serves_one_purpose(self):
        project = self.checked_ads()
        self.verify_everything(project)
        asset_id = self.ids(project, ["c1_1"])[0]
        self.link(asset_id, "display", key="one-key")
        self.assertEqual(self.link(asset_id, "download", key="one-key", expect=409)["code"], "idempotency_key_reused")

    def test_an_unknown_purpose_is_refused(self):
        project = self.made()
        response = self.client.post(f"/api/v1/generation/asset-versions/{self.ids(project, ['c1_1'])[0]}/download-intent",
                                    params={"purpose": "export"}, headers=self.headers())
        self.assertEqual(response.status_code, 422, response.text)


class ExportPackageTests(LibraryGateFixtures):
    def nothing_started(self):
        self.assertEqual(self.count(ExportPackageRecord), 0)
        self.assertEqual(self.count(JobRecord, type="generation.export_package"), 0)
        self.assertEqual(self.count(OperationRecord, command_type="generation.export"), 0)

    def test_a_selection_of_drafts_is_refused_and_names_each_ad(self):
        project = self.checked_ads()
        ads = self.ads(project)
        chosen = self.ids(project, ["c1_1", "c2_1"])
        refused = self.refused_ads(self.package({"kind": "explicit", "version_ids": chosen}, expect=409))
        self.assertEqual(list(refused), chosen)
        for combination_id in ("c1_1", "c2_1"):
            ad = ads[combination_id]
            self.assertEqual(refused[ad["asset_version_id"]],
                             {"asset_version_id": ad["asset_version_id"], "status": "draft", "reasons": ad["reasons"]})
        self.assertEqual(self.reasons(refused[chosen[0]]), ["facts_unverified", "appearance_unconfirmed", "findings_open"])
        self.nothing_started()

    def test_one_draft_among_checked_ads_refuses_the_whole_selection(self):
        project = self.checked_ads()
        # Only the first claim's ads are checked: f1, their look and their findings.
        self.verify(project, fact_ids=["f1"], ads=ADS_OF["f1"])
        self.assertEqual(self.statuses(project)["c1_1"], "exportable")
        checked, draft = self.ids(project, ["c1_1", "c2_1"])
        refused = self.refused_ads(self.package({"kind": "explicit", "version_ids": [checked, draft]}, expect=409))
        self.assertEqual(list(refused), [draft])
        self.assertEqual(refused[draft], {"asset_version_id": draft, "status": "draft", "reasons": self.ads(project)["c2_1"]["reasons"]})
        self.assertEqual([(r["code"], r["fact_ids"]) for r in refused[draft]["reasons"]][:1], [("facts_unverified", ["f2"])])
        self.nothing_started()

    def test_a_draft_among_old_flow_images_refuses_the_whole_selection(self):
        project = self.checked_ads()
        images = self.plan_first_images()
        draft = self.ids(project, ["c3_1"])[0]
        refused = self.refused_ads(self.package({"kind": "explicit", "version_ids": [*images, draft]}, expect=409))
        self.assertEqual(list(refused), [draft])
        self.nothing_started()

    def test_an_ad_that_needs_redoing_is_refused_too(self):
        project = self.checked_ads()
        self.verify_everything(project)
        self.edit_facts(project, self.set_fact("f1", statement="Charging can be scheduled from the app or the wall unit."))
        stale = self.ids(project, ADS_OF["f1"])
        refused = self.refused_ads(self.package({"kind": "explicit", "version_ids": self.ids(project)}, expect=409))
        self.assertEqual(list(refused), stale)
        self.assertEqual({item["status"] for item in refused.values()}, {"needs_redo"})
        self.nothing_started()

    def test_checked_ads_are_packaged_and_handed_out(self):
        project = self.checked_ads()
        self.verify_everything(project)
        chosen = self.ids(project, ["c1_1", "c2_2", "c3_1"])
        package = self.built({"kind": "explicit", "version_ids": chosen})
        self.assertEqual((package["state"], package["left_out_drafts"]), ("ready", 0))
        self.assertEqual(self.packaged(package["package_id"]), chosen)
        self.package_link(package["package_id"])

    def test_a_package_is_not_handed_out_once_an_ad_in_it_stopped_being_exportable(self):
        project = self.checked_ads()
        self.verify_everything(project)
        chosen = self.ids(project, ["c1_1", "c2_1"])
        package = self.built({"kind": "explicit", "version_ids": chosen})
        first = self.package_link(package["package_id"], key="zip")
        self.edit_facts(project, self.set_fact("f1", statement="Charging can be scheduled from the app or the wall unit."))
        # A new link and a repeated request are both refused; the package stays as history.
        for key in ("zip", None):
            refused = self.refused_ads(self.package_link(package["package_id"], expect=409, key=key))
            self.assertEqual(list(refused), chosen[:1])
            self.assertEqual(refused[chosen[0]]["status"], "needs_redo")
        self.assertEqual(self.detail(package["package_id"])["state"], "ready")
        self.assertEqual(self.count(DownloadIntentRecord, owner_id=package["package_id"]), 1)
        self.assertTrue(first["intent_id"])
        # The same selection can no longer be packaged; the ad that still stands can.
        self.package({"kind": "explicit", "version_ids": chosen}, expect=409)
        again = self.built({"kind": "explicit", "version_ids": chosen[1:]})
        self.package_link(again["package_id"])

    def test_a_preset_leaves_drafts_out_and_says_how_many(self):
        project = self.checked_ads()
        self.verify(project, fact_ids=["f1"], ads=ADS_OF["f1"])
        others = self.plan_first_images()
        package = self.built({"kind": "all_successful"})
        self.assertEqual((package["state"], package["left_out_drafts"]), ("ready", 4))
        self.assertEqual(sorted(self.packaged(package["package_id"])), sorted([*self.ids(project, ADS_OF["f1"]), *others]))
        self.package_link(package["package_id"])

    def test_a_repeated_request_gets_its_package_back_after_an_ad_stopped_being_exportable(self):
        project = self.checked_ads()
        self.verify_everything(project)
        selector = {"kind": "explicit", "version_ids": self.ids(project, ["c1_1", "c2_1"])}
        accepted = self.package(selector, key="pkg")
        self.edit_facts(project, self.set_fact("f1", statement="Charging can be scheduled from the app or the wall unit."))
        # A client retrying after a lost response learns its package, which is no longer handed out.
        self.assertEqual(self.package(selector, key="pkg")["package_id"], accepted["package_id"])
        self.assertEqual(self.count(ExportPackageRecord), 1)
        self.drain(job_types=("generation.export_package",))
        self.refused_ads(self.package_link(accepted["package_id"], expect=409))
        # A new request for the same selection is refused, and a reused key still cannot name another one.
        self.refused_ads(self.package(selector, expect=409))
        reused = self.package({"kind": "explicit", "version_ids": selector["version_ids"][1:]}, key="pkg", expect=409)
        self.assertEqual(reused["code"], "idempotency_key_reused")

    def test_approved_ads_that_need_redoing_are_left_out_of_an_approved_only_package(self):
        project = self.checked_ads()
        self.verify_everything(project)
        stale, kept = self.ids(project, ["c1_1", "c2_1"])
        for asset_id in (stale, kept):
            self.approve(asset_id)
        self.edit_facts(project, self.set_fact("f1", statement="Charging can be scheduled from the app or the wall unit."))
        package = self.built({"kind": "approved_only"})
        self.assertEqual((package["state"], package["left_out_drafts"]), ("ready", 1))
        self.assertEqual(self.packaged(package["package_id"]), [kept])
        self.package_link(package["package_id"])

    def test_the_worker_that_builds_a_package_never_judges_an_ad(self):
        # The Worker's database role cannot read the verification records an ad's status needs.
        project = self.checked_ads()
        self.verify_everything(project)
        package = self.package({"kind": "explicit", "version_ids": self.ids(project, ["c1_1", "c2_1"])})
        with patch("app.marketing.ideas_export.statuses", side_effect=AssertionError("judged in the Worker")):
            self.drain(job_types=("generation.export_package",))
        self.assertEqual(self.detail(package["package_id"])["state"], "ready")

    def test_a_large_selection_is_looked_up_in_parts_with_the_same_answer(self):
        project = self.checked_ads()
        self.verify(project, fact_ids=["f1"], ads=ADS_OF["f1"])
        asked = [*self.plan_first_images(), *self.ids(project), "av_unknown"]
        with tenant_session(self.context) as session:
            whole = ideas_export.library_refusals(session, self.context, asked)
            with patch.object(ideas_export, "_LOOKUP_CHUNK", 2):
                parts = ideas_export.library_refusals(session, self.context, asked)
        self.assertEqual(parts, whole)
        self.assertEqual([item["asset_version_id"] for item in whole], self.ids(project, ADS_OF["f2"] + ADS_OF["f3"]))

    def test_a_preset_that_matches_only_drafts_is_refused(self):
        project = self.checked_ads()
        refused = self.refused_ads(self.package({"kind": "all_successful"}, expect=409))
        self.assertEqual(set(refused), set(self.ids(project)))
        self.nothing_started()

    def test_a_long_refusal_names_the_first_ads_and_counts_them_all(self):
        refused = [{"asset_version_id": f"av_{index}", "status": "draft", "reasons": []} for index in range(120)]
        params = ideas_export.refusal_params(refused)
        self.assertEqual((len(params["ads"]), params["count"]), (ideas_export.LIBRARY_REFUSALS_LISTED, 120))
        self.assertEqual(params["ads"], refused[:ideas_export.LIBRARY_REFUSALS_LISTED])


class OtherImagesTests(LibraryGateFixtures):
    def test_plan_first_images_are_handed_out_as_before_and_never_judged(self):
        images = self.plan_first_images()
        with patch("app.marketing.ideas_export.statuses", wraps=ideas_export.statuses) as judged:
            listed = self.gallery()
            self.assertEqual({listed[image]["export_block"] for image in images}, {None})
            for image in images:
                self.link(image)
                self.link(image, "display")
            package = self.built({"kind": "explicit", "version_ids": images})
            self.assertEqual((package["state"], package["left_out_drafts"]), ("ready", 0))
            self.assertEqual(self.packaged(package["package_id"]), images)
            self.package_link(package["package_id"])
            preset = self.built({"kind": "all_successful"})
            self.assertEqual(sorted(self.packaged(preset["package_id"])), images)
        judged.assert_not_called()
        with tenant_session(self.context) as session:
            stored = session.get(ExportPackageRecord, package["package_id"]).selected_versions_json
        self.assertEqual(stored, {"version_ids": images})

    def test_the_gate_asks_only_about_ads_of_ads_first_ideas(self):
        project = self.checked_ads()
        images = self.plan_first_images()
        with tenant_session(self.context) as session:
            refused = ideas_export.library_refusals(session, self.context, [*images, *self.ids(project), "av_unknown"])
        self.assertEqual([item["asset_version_id"] for item in refused], self.ids(project))

    def test_ideas_that_cannot_be_read_keep_their_images_in(self):
        project = self.checked_ads()
        self.verify_everything(project)
        asset_id = self.ids(project, ["c1_1"])[0]
        with tenant_session(self.context) as session:
            session.get(MarketingProjectRecord, project["id"]).flow = "plan_first"
        refused = self.refused_ads(self.link(asset_id, expect=409))
        self.assertEqual((refused[asset_id]["status"], self.reasons(refused[asset_id])), ("needs_redo", ["ideas_changed"]))


    def test_ideas_that_no_longer_read_refuse_their_own_images_and_nothing_else(self):
        project = self.checked_ads()
        self.verify_everything(project)
        images, ads = self.plan_first_images(), self.ids(project)
        # Stored ideas a later contract no longer reads (its own ideas page fails as it always did).
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            combinations = [dict(c) for c in row.ideas_json["combinations"]]
            combinations[0]["angle_id"] = "a_missing"
            row.ideas_json = {**row.ideas_json, "combinations": combinations}
        listed = self.gallery()
        for asset_id in ads:
            self.assertEqual(listed[asset_id]["export_block"]["status"], "needs_redo")
            self.assertEqual(self.reasons(listed[asset_id]["export_block"]), ["ideas_changed"])
        self.assertEqual({listed[image]["export_block"] for image in images}, {None})
        self.refused_ads(self.link(ads[0], expect=409))
        self.link(ads[0], "display")
        package = self.built({"kind": "all_successful"})
        self.assertEqual((package["left_out_drafts"], sorted(self.packaged(package["package_id"]))), (6, images))


class TenantTests(LibraryGateFixtures):
    def test_another_workspace_can_neither_see_save_nor_package_these_ads(self):
        project = self.checked_ads()
        self.verify_everything(project)
        chosen = self.ids(project, ["c1_1", "c2_1"])
        package = self.built({"kind": "explicit", "version_ids": chosen})
        owner = self.context
        self.context = self._make_context()

        self.assertEqual(self.gallery(), {})
        for purpose in ("download", "display"):
            self.assertEqual(self.link(chosen[0], purpose, expect=422)["code"], "asset_blob_unavailable")
        missing = self.package({"kind": "explicit", "version_ids": chosen}, expect=404)
        self.assertEqual(missing["code"], "asset_version_not_found")
        self.assertNotIn("params", missing)
        self.assertEqual(self.package({"kind": "all_successful"}, expect=422)["code"], "empty_selection")
        self.assertEqual(self.detail(package["package_id"], expect=404)["code"], "export_package_not_found")
        self.assertEqual(self.package_link(package["package_id"], expect=404)["code"], "export_package_not_found")
        with tenant_session(self.context) as session:
            # It cannot even learn whether these images are ads, let alone their status.
            self.assertEqual(ideas_export.library_refusals(session, self.context, chosen), [])
            for model in (DownloadIntentRecord, ExportPackageRecord):
                self.assertEqual(session.scalar(select(func.count()).select_from(model).where(
                    model.workspace_id == self.context.workspace_id)), 0)

        self.context = owner
        self.assertEqual(self.detail(package["package_id"])["state"], "ready")
        self.assertEqual(self.count(DownloadIntentRecord), 0)

    def test_a_draft_stays_a_draft_to_its_own_workspace_whatever_another_one_checks(self):
        project = self.checked_ads()
        draft = self.ids(project, ["c1_1"])[0]
        owner = self.context
        self.context = self._make_context()
        other = self.checked_ads()
        self.verify_everything(other)
        self.link(self.ids(other, ["c1_1"])[0])
        self.context = owner
        self.refused_ads(self.link(draft, expect=409))
        self.refused_ads(self.package({"kind": "explicit", "version_ids": [draft]}, expect=409))
