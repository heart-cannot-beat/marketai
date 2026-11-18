"""Renewing a checked photo whose rights ran out (#233; ADR 0018 §3, creative-library contract §5.1, §7.2).

Checking a photo declares, for one year, that it shows the product and may be used in ads. When that runs out
the photo can no longer be used and the ads made with it need redoing. Renewing is the user declaring it again:
checking the same photo publishes the same creative-library reference again with the new declaration. The
newest decision on the same bytes counts (#199), so every ad made with them is as it was before the check ran
out -- no new image, no new check. A photo withdrawn or archived in the library is a person's decision a check
never undoes. Nothing renews on its own. Fake model, fake image provider; no paid call.
"""
import dataclasses
from datetime import date, timedelta

from sqlalchemy import func, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import GenerationRunRecord, LibraryVersionRecord
from app.library import service as library
from app.marketing import product_references
from app.marketing.models import MarketingIdeasExportRecord
from tests.test_marketing_ideas_added_photos import HOME
from tests.test_marketing_ideas_logo import LogoFixtures

YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
NEXT_YEAR = (date.today() + timedelta(days=365)).isoformat()
PHOTO_CODES = {"photo_unverified", "photo_unavailable"}


class RenewalFixtures(LogoFixtures):
    def expire(self, version_id):
        """The user's declaration ran out: a check made a year and a day ago (the version's rights as stored)."""
        with tenant_session(self.context) as session:
            version = session.get(LibraryVersionRecord, version_id)
            content = dict(version.content_json)
            content["rights"] = {**content["rights"], "valid_to": YESTERDAY}
            version.content_json = content

    def version(self, version_id):
        with tenant_session(self.context) as session:
            version = session.get(LibraryVersionRecord, version_id)
            session.expunge(version)
            return version

    def photo_reasons(self, project):
        return {ad["asset_version_id"]: sorted(set(self.reasons(ad)) & PHOTO_CODES) for ad in self.read_ideas(project)["ads"]
                if ad["used_product_photo"]}

    def made_with_photo(self):
        """Ads with the user's own photo: one made before it was checked (frozen as the gated photo), one after
        (frozen as the product's verified photo, a creative-library reference)."""
        project = self.checked_ads()
        photo = self.own_photo(project)
        self.make(self.feedback(project, "c1_1")["draft_id"])
        checked = self.decide(project, photo_ids=[photo["photo_id"]])["photos"][0]
        self.make(self.feedback(project, "c2_1")["draft_id"])
        return project, checked


class RenewalTests(RenewalFixtures):
    def test_a_checked_photo_says_until_when(self):
        project = self.checked_ads()
        photo = self.own_photo(project)
        self.assertEqual((photo["valid_to"], photo["unavailable_reason"]), (None, None))
        checked = self.decide(project, photo_ids=[photo["photo_id"]])["photos"][0]
        self.assertEqual((checked["valid_to"], checked["unavailable_reason"]), (NEXT_YEAR, None))

    def test_renewing_an_expired_photo_restores_its_ads_with_no_new_image(self):
        project, checked = self.made_with_photo()
        before = self.statuses(project)
        self.assertEqual(set(map(tuple, self.photo_reasons(project).values())), {()})
        runs = self.count(GenerationRunRecord)
        self.expire(checked["reference_version_id"])
        expired = self.read_ideas(project)["photos"][0]
        self.assertEqual((expired["status"], expired["unavailable_reason"], expired["valid_to"]),
                         ("unavailable", "expired", YESTERDAY))
        self.assertEqual(set(map(tuple, self.photo_reasons(project).values())), {("photo_unavailable",)})
        # Nothing renews on its own.
        self.assertEqual(self.read_ideas(project)["photos"][0]["status"], "unavailable")

        renewed = self.decide(project, photo_ids=[checked["photo_id"]])["photos"][0]
        self.assertEqual((renewed["status"], renewed["unavailable_reason"], renewed["valid_to"]), ("verified", None, NEXT_YEAR))
        self.assertNotEqual(renewed["reference_version_id"], checked["reference_version_id"])
        old, new = self.version(checked["reference_version_id"]), self.version(renewed["reference_version_id"])
        # The same reference published again with the new declaration; the expired one is superseded.
        self.assertEqual((new.entry_id, new.content_json["object_id"]), (old.entry_id, old.content_json["object_id"]))
        self.assertEqual((old.state, new.state, new.origin_command), ("superseded", "effective", "ideas_photo_renewal"))
        self.assertEqual(new.content_json["rights"]["valid_to"], NEXT_YEAR)
        self.assertEqual({k: v for k, v in new.content_json.items() if k != "rights"},
                         {k: v for k, v in old.content_json.items() if k != "rights"})
        # Both ads are as they were before the check ran out, with no new image and no new check.
        self.assertEqual(self.statuses(project), before)
        self.assertEqual(set(map(tuple, self.photo_reasons(project).values())), {()})
        self.assertEqual(self.count(GenerationRunRecord), runs)
        # The decision says what it renewed; the product's version holds the renewed reference.
        line = self.records(project, "verification")[-1].content_json["photos"][0]
        self.assertEqual(line["renewed_from"], checked["reference_version_id"])
        version, _ = self.published_version(project)
        self.assertEqual([b["reference_version_id"] for b in version.content_json["reference_bindings"]],
                         [renewed["reference_version_id"]])
        # A new image is made with the renewed reference.
        later = self.draft_references(self.feedback(project, "c3_1")["draft_id"])
        self.assertEqual([ref["reference_version_id"] for ref in later], [renewed["reference_version_id"]])

    def test_checking_a_photo_whose_rights_still_run_changes_nothing(self):
        project, checked = self.made_with_photo()
        again = self.decide(project, photo_ids=[checked["photo_id"]])["photos"][0]
        self.assertEqual(again["reference_version_id"], checked["reference_version_id"])
        self.assertNotIn("renewed_from", self.records(project, "verification")[-1].content_json["photos"][0])

    def test_a_photo_withdrawn_in_the_library_cannot_be_renewed(self):
        project, checked = self.made_with_photo()
        owner, self.context = self.context, dataclasses.replace(self.context, role="owner", initiator_role="owner")
        library.withdraw_version(self.context, checked["reference_version_id"], reason="Not our product after all")
        self.context = owner
        photo = self.read_ideas(project)["photos"][0]
        self.assertEqual((photo["status"], photo["unavailable_reason"]), ("unavailable", "withdrawn"))
        records = len(self.records(project, "verification"))
        current = self.read_ideas(project)
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification",
                                    headers=self.headers(current["revision"]),
                                    json={"photo_ids": [checked["photo_id"]]})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "photo_withdrawn"), response.text)
        self.assertEqual(response.json()["params"]["photo_ids"], [checked["photo_id"]])
        self.assertEqual(len(self.records(project, "verification")), records)
        self.assertEqual(set(map(tuple, self.photo_reasons(project).values())), {("photo_unavailable",)})

    def test_an_expired_logo_is_renewed_and_offered_to_the_brand_baseline_again(self):
        project = self.checked_ads()
        logo = self.own_logo(project)
        checked = self.decide(project, photo_ids=[logo["photo_id"]])["logos"][0]
        self.expire(checked["reference_version_id"])
        expired = self.read_ideas(project)["logos"][0]
        self.assertEqual((expired["status"], expired["unavailable_reason"]), ("unavailable", "expired"))
        renewed = self.decide(project, photo_ids=[logo["photo_id"]])["logos"][0]
        self.assertEqual((renewed["status"], renewed["valid_to"]), ("verified", NEXT_YEAR))
        items = self.references(project["product_id"])
        self.assertEqual([(item["role"], item["available"], item["reference_version_id"]) for item in items],
                         [("logo", True, renewed["reference_version_id"])])

    def test_the_same_bytes_renewed_for_one_product_are_renewed_by_checking_them_for_another(self):
        project, checked = self.made_with_photo()
        other = self.understood_ideas()
        object_id, _ = self.uploaded(HOME)
        self.add_photo(other, object_id)
        self.decide(other, photo_ids=[checked["photo_id"]])
        self.assertEqual(self.read_ideas(other)["photos"][0]["reference_version_id"], checked["reference_version_id"])
        self.expire(checked["reference_version_id"])
        renewed = self.decide(project, photo_ids=[checked["photo_id"]])["photos"][0]
        # The other product's check named the expired version: it says so and offers renewing ...
        there = self.read_ideas(other)["photos"][0]
        self.assertEqual((there["status"], there["unavailable_reason"]), ("unavailable", "expired"))
        # ... and checking it there takes the renewed reference, with no third version.
        again = self.decide(other, photo_ids=[checked["photo_id"]])["photos"][0]
        self.assertEqual((again["status"], again["reference_version_id"]), ("verified", renewed["reference_version_id"]))
        with tenant_session(self.context) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(LibraryVersionRecord).where(
                LibraryVersionRecord.workspace_id == self.context.workspace_id,
                LibraryVersionRecord.origin_command == "ideas_photo_renewal")), 1)

    def test_a_plan_first_image_on_the_expired_reference_stays_strict(self):
        project, checked = self.made_with_photo()
        after = next(ad for ad in self.read_ideas(project)["ads"] if ad["combination_id"] == "c2_1" and ad["used_product_photo"])
        references = self.recipe_references(after["asset_version_id"])
        self.expire(checked["reference_version_id"])
        self.decide(project, photo_ids=[checked["photo_id"]])
        with tenant_session(self.context) as session:
            # Rounds and reuse read the frozen reference itself; a renewal elsewhere does not bring it back.
            self.assertFalse(product_references.lineage_current(
                session, self.context, {"policy": "marketing-product-photos-v1", "references": references}))

    def test_another_workspace_cannot_renew_these_photos(self):
        project, checked = self.made_with_photo()
        self.expire(checked["reference_version_id"])
        revision = self.read_ideas(project)["revision"]
        owner, self.context = self.context, self._make_context()
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification", headers=self.headers(revision),
                                    json={"photo_ids": [checked["photo_id"]]})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"))
        self.context = owner
        self.assertEqual(self.read_ideas(project)["photos"][0]["unavailable_reason"], "expired")
        with tenant_session(self.context) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(LibraryVersionRecord).where(
                LibraryVersionRecord.workspace_id == self.context.workspace_id,
                LibraryVersionRecord.origin_command == "ideas_photo_renewal")), 0)
            self.assertEqual(session.scalar(select(func.count()).select_from(MarketingIdeasExportRecord).where(
                MarketingIdeasExportRecord.project_id == project["id"],
                MarketingIdeasExportRecord.kind == "verification")), 1)
