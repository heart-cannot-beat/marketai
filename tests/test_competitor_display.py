"""Issue #332 -- what the competitor list shows: the page name a link leaves
out, when each competitor was last scanned, and candidate avatars.

Real HTTP routes and the real Worker with the Controlled Mock (no real key,
no real request).

* ``Competitor.observed_page_name``: the advertiser name of the confirmed
  page's newest observation in the product's scans; the confirmation
  revision a link appended is never rewritten and no call is made.
* ``Competitor.last_scanned_at``: the newest finished scan that included
  the competitor, however many scans the product has (the list the
  interface used to read stops at 50); queued scans do not count, an
  archived competitor keeps its time.
* ``CompetitorScanTarget.observed_page_name``: the same projection per target.
* A page no scan has seen (Issue #355: a wrong page ID): the scan completes,
  the target fetched one empty page, and nothing names the page. The
  interface's "the page ID may be wrong" hint reads exactly these fields.
* Candidate avatars: the resolve downloads each one (free, no reservation,
  no attempt), archives it by SHA-256 and serves a signed link to the
  archived copy, never the provider's link; a failed download is a
  placeholder (``avatar = null``) and the resolve still completes.
* Another Workspace sees none of it (404); Members and the Owner read it.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import select, update

from tests.test_competitor_scans_api import HALCYON, ScanFixtures
from tests.test_competitors_api import BRIGHTVOLT, AD_LIBRARY_LINK, ACME, CompetitorFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import utcnow
from app.competitors import scan_service
from app.competitors.models import CompetitorIdentityConfirmationRecord, CompetitorResolutionRecord
from app.competitors.scan_media import MediaFetch, MockMediaFetcher
from app.competitors.sources.mock import load_fixtures
from app.library.contracts import ROLE_MEMBER

#: The advertiser names the P0 recordings carry for the three pages.
NAMES = {ACME: "Acme Charge", BRIGHTVOLT: "Brightvolt", HALCYON: "Halcyon Motors"}


class DisplayScanFixtures(ScanFixtures):
    def _list(self, **params) -> dict[str, dict]:
        response = self.client.get("/api/v1/competitors", params={"product_id": self.product_id, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return {item["competitor_id"]: item for item in response.json()["items"]}

    def _scanned(self, competitor_ids: list[str]) -> dict:
        accepted = self._start(self._body(competitor_ids, keywords=[]))
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual(scan["status"], "completed", scan.get("failure"))
        return scan

    def _revisions(self, competitor_id: str) -> list[tuple[int, str | None]]:
        with tenant_session(self.ctx) as session:
            return [(row.revision, row.page_name) for row in session.scalars(
                select(CompetitorIdentityConfirmationRecord)
                .where(CompetitorIdentityConfirmationRecord.competitor_id == competitor_id)
                .order_by(CompetitorIdentityConfirmationRecord.revision)
            )]


class ObservedPageNameTests(DisplayScanFixtures):
    def test_a_link_confirmed_page_is_named_by_its_newest_observation(self):
        acme = self._confirmed(ACME)
        before = self._get(acme)
        self.assertEqual((before["confirmation"]["source"], before["confirmation"]["page_name"]), ("link", None))
        self.assertIsNone(before["observed_page_name"])  # no scan has seen the page yet

        queued = self._start(self._body([acme], keywords=[]))
        self.assertIsNone(self._get(acme)["observed_page_name"])
        self._drain()
        scan = self._scan(queued["scan_id"])
        self.assertEqual(scan["status"], "completed")

        after = self._get(acme)
        self.assertEqual(after["observed_page_name"], NAMES[ACME])
        self.assertEqual(self._list()[acme]["observed_page_name"], NAMES[ACME])
        # A projection: the link's revision keeps its empty name, nothing was appended.
        self.assertEqual(self._revisions(acme), [(1, None)])
        self.assertIsNone(after["confirmation"]["page_name"])
        [target] = scan["targets"]
        self.assertEqual((target["page_id"], target["page_name"], target["observed_page_name"]),
                         (ACME, None, NAMES[ACME]))

    def test_the_name_follows_the_confirmed_page_and_stays_in_its_product(self):
        acme, brightvolt = self._confirmed(ACME), self._confirmed(BRIGHTVOLT)
        self._scanned([acme, brightvolt])
        # Re-confirmed to Brightvolt's page by link: the name is that page's.
        moved = self._confirm(acme, {"page_url": f"https://www.facebook.com/profile.php?id={BRIGHTVOLT}"})
        self.assertEqual((moved["confirmation"]["revision"], moved["confirmation"]["page_id"]), (2, BRIGHTVOLT))
        self.assertEqual(moved["observed_page_name"], NAMES[BRIGHTVOLT])

        # Another product of the same Workspace has not scanned Acme Charge's page.
        other_product = self._product()
        elsewhere = self._confirmed(ACME, product_id=other_product)
        self.assertIsNone(self._get(elsewhere)["observed_page_name"])


class LastScannedAtTests(DisplayScanFixtures):
    def test_queued_scans_do_not_count_and_the_newest_finished_scan_does(self):
        acme, brightvolt, halcyon = self._three()
        self.assertTrue(all(item["last_scanned_at"] is None for item in self._list().values()))

        self._start(self._body([acme, brightvolt], keywords=[]))  # admitted, not run yet
        self.assertIsNone(self._get(acme)["last_scanned_at"])
        self._drain()
        first = self._scan(self._list_scans()[0]["scan_id"])
        self.assertEqual(first["status"], "completed")
        listed = self._list()
        self.assertEqual(listed[acme]["last_scanned_at"], first["finished_at"])
        self.assertEqual(listed[brightvolt]["last_scanned_at"], first["finished_at"])
        self.assertIsNone(listed[halcyon]["last_scanned_at"])

        queued = self._start(self._body([acme, halcyon], keywords=[]))
        self.assertEqual(self._get(acme)["last_scanned_at"], first["finished_at"])
        self.assertIsNone(self._get(halcyon)["last_scanned_at"])
        self._drain()
        second = self._scan(queued["scan_id"])
        listed = self._list()
        self.assertGreater(second["finished_at"], first["finished_at"])
        self.assertEqual(listed[acme]["last_scanned_at"], second["finished_at"])
        self.assertEqual(listed[brightvolt]["last_scanned_at"], first["finished_at"])
        self.assertEqual(listed[halcyon]["last_scanned_at"], second["finished_at"])

    def test_independent_of_how_many_scans_the_scan_list_returns(self):
        acme, brightvolt = self._confirmed(ACME), self._confirmed(BRIGHTVOLT)
        old = self._scanned([acme])
        newer = self._scanned([brightvolt])
        with patch.object(scan_service, "LIST_LIMIT", 1):
            self.assertEqual([item["scan_id"] for item in self._list_scans()], [newer["scan_id"]])
            self.assertEqual(self._get(acme)["last_scanned_at"], old["finished_at"])
            self.assertEqual(self._list()[acme]["last_scanned_at"], old["finished_at"])

    def test_a_failed_scan_counts_as_finished(self):
        acme = self._confirmed(ACME)
        accepted = self._start(self._body([acme], keywords=[]))
        with tenant_session(self.ctx) as session:  # the Worker recorded a failure and stopped
            session.execute(update(scan_service.CompetitorScanRecord)
                            .where(scan_service.CompetitorScanRecord.id == accepted["scan_id"])
                            .values(status="failed", finished_at=utcnow()))
        failed = self._scan(accepted["scan_id"])
        self.assertEqual(self._get(acme)["last_scanned_at"], failed["finished_at"])

    def test_an_archived_competitor_keeps_its_projections(self):
        acme = self._confirmed(ACME)
        scan = self._scanned([acme])
        self.current = self.owner_ctx(self.ctx)
        response = self.client.post(f"/api/v1/competitors/{acme}/archive", headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        archived = response.json()
        self.assertEqual(archived["status"], "archived")
        self.assertEqual((archived["last_scanned_at"], archived["observed_page_name"]),
                         (scan["finished_at"], NAMES[ACME]))
        self.current = self.ctx
        listed = self._list(status="archived")[acme]
        self.assertEqual((listed["last_scanned_at"], listed["observed_page_name"]),
                         (scan["finished_at"], NAMES[ACME]))
        self.assertNotIn(acme, self._list())

    def _list_scans(self) -> list[dict]:
        response = self.client.get("/api/v1/competitor-scans", params={"product_id": self.product_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]


class UnseenPageTests(DisplayScanFixtures):
    """Issue #355: what the interface's hint for a wrong page ID depends on."""

    #: No P0 recording has this page: the Mock answers it the provider's empty page.
    UNSEEN = "100000000000001"

    def test_an_unseen_page_completes_with_one_empty_page_and_no_name(self):
        wrong = self._confirmed(self.UNSEEN)
        confirmed = self._get(wrong)
        self.assertEqual((confirmed["confirmation"]["source"], confirmed["confirmation"]["page_name"]),
                         ("link", None))
        scan = self._scanned([wrong])
        self.assertEqual(scan["status"], "completed")
        self.assertFalse(scan["can_resume"])
        [target] = scan["targets"]
        self.assertEqual((target["kind"], target["status"], target["page_id"]), ("page", "completed", self.UNSEEN))
        self.assertEqual((target["pages_fetched"], target["ads_seen"], target["exhausted"]), (1, 0, True))
        self.assertIsNone(target["page_name"])
        self.assertIsNone(target["observed_page_name"])

        listed = self._list()[wrong]
        self.assertIsNone(listed["observed_page_name"])
        self.assertEqual(listed["last_scanned_at"], scan["finished_at"])
        # The list row's hint compares the two: the scan ran after the confirmation.
        self.assertGreater(datetime.fromisoformat(listed["last_scanned_at"]),
                           datetime.fromisoformat(listed["confirmation"]["confirmed_at"]))


class ProjectionAccessTests(DisplayScanFixtures):
    def test_members_and_the_owner_read_them_another_workspace_gets_404(self):
        acme = self._confirmed(ACME)
        scan = self._scanned([acme])
        expected = (NAMES[ACME], scan["finished_at"])
        for reader in (self.ctx, self._make_context(self.ctx.workspace_id, role=ROLE_MEMBER, actor="member_c"),
                       self.owner_ctx(self.ctx)):
            self.current = reader
            viewed = self._get(acme)
            self.assertEqual((viewed["observed_page_name"], viewed["last_scanned_at"]), expected, reader.role)
            listed = self._list()[acme]
            self.assertEqual((listed["observed_page_name"], listed["last_scanned_at"]), expected, reader.role)

        other = self._make_context(role=ROLE_MEMBER, actor="member_b")
        self.current = other
        self._problem(self.client.get(f"/api/v1/competitors/{acme}"), 404, "competitor_not_found")
        self._problem(self.client.get(f"/api/v1/competitor-scans/{scan['scan_id']}"), 404,
                      "competitor_scan_not_found")
        # The same page, confirmed in the other Workspace, learns nothing from this one's scans.
        product = self._product(other)
        theirs = self._create({"product_id": product, "input": AD_LIBRARY_LINK})
        self.assertEqual(theirs["confirmation"]["page_id"], ACME)
        self.assertEqual((theirs["observed_page_name"], theirs["last_scanned_at"]), (None, None))
        listed = self.client.get("/api/v1/competitors").json()["items"]
        self.assertEqual([(item["competitor_id"], item["observed_page_name"]) for item in listed],
                         [(theirs["competitor_id"], None)])


class _Fetcher:
    """The free avatar download, answered per page: ``ok`` (the Mock placeholder),
    ``expired`` (403), ``garbage`` (bytes that are no image) or ``boom`` (raises)."""

    def __init__(self, failures: dict[str, str]) -> None:
        self.failures = failures
        self.urls: list[str] = []
        self._mock = MockMediaFetcher()

    async def fetch(self, url: str) -> MediaFetch:
        self.urls.append(url)
        failure = next((how for ref, how in self.failures.items() if ref in url), "ok")
        if failure == "expired":
            return MediaFetch(reason="expired", http_status=403)
        if failure == "garbage":
            return MediaFetch(content=b"not an image", http_status=200)
        if failure == "boom":
            raise RuntimeError("connection reset")
        return await self._mock.fetch(url)


class CandidateAvatarTests(CompetitorFixtures):
    def _resolved(self, *, body: dict | None = None) -> tuple[str, dict]:
        competitor = self._competitor("Acme Charge", website="acme-charge.example")
        accepted = self._resolve(competitor["competitor_id"], body)
        self._drain()
        resolution = self._resolution(competitor["competitor_id"], accepted["resolve_job_id"])
        self.assertEqual(resolution["status"], "completed", resolution)
        return competitor["competitor_id"], resolution

    def _evidence(self, job_id: str) -> dict:
        with tenant_session(self.ctx) as session:
            return session.scalars(select(CompetitorResolutionRecord.evidence_json).where(
                CompetitorResolutionRecord.job_id == job_id)).one()

    def test_each_avatar_is_archived_once_and_served_as_a_signed_link_to_the_copy(self):
        competitor_id, resolution = self._resolved()
        self.assertTrue(all(c["avatar"] and c["avatar"]["url"].startswith("http://blob.test/")
                            for c in resolution["candidates"]))
        avatars = self._evidence(resolution["resolve_job_id"])["avatars"]
        self.assertEqual(set(avatars), {c["page_id"] for c in resolution["candidates"]})
        self.assertEqual({outcome["status"] for outcome in avatars.values()}, {"available"})
        # Every Mock page has the same placeholder: one object (and one derivative) by SHA-256.
        self.assertEqual(len({outcome["object_key"] for outcome in avatars.values()}), 1)
        self.assertEqual(len({outcome["thumbnail"]["object_key"] for outcome in avatars.values()}), 1)
        self.assertIn("/competitor_media/", next(iter(avatars.values()))["object_key"])
        # The browser never gets the provider's (here the Mock's) link.
        text = self.client.get(f"/api/v1/competitors/{competitor_id}/resolutions/"
                               f"{resolution['resolve_job_id']}").text
        self.assertNotIn("media.mock.invalid", text)
        self.assertNotIn("avatar_url", text)
        # Free: the search and three probes, nothing for the downloads.
        self.assertEqual(sorted(a.model for a in self._attempts(resolution["resolve_job_id"])),
                         ["page_search", "probe", "probe", "probe"])
        self.assertEqual(resolution["cost"]["credits_charged"], 4)
        # The confirmation keeps the card without the avatar.
        competitor = self._get(competitor_id)
        self.assertIsNone(competitor["confirmation"]["candidate"]["avatar"])
        with tenant_session(self.ctx) as session:
            evidence = session.scalars(select(CompetitorIdentityConfirmationRecord.evidence_json).where(
                CompetitorIdentityConfirmationRecord.competitor_id == competitor_id)).one()
        self.assertNotIn("avatar", str(evidence))

    def test_a_failed_download_leaves_the_placeholder_and_the_resolve_completes(self):
        pages = [item["page_id"] for item in load_fixtures().lookup(
            "/v1/facebook/adLibrary/search/companies", {"query": "Acme Charge"}).body["searchResults"]]

        def ref(page_id: str) -> str:  # the Mock's placeholder link names the page by this
            return hashlib.sha256(page_id.encode("utf-8")).hexdigest()[:16]

        failing = {pages[1]: "expired", pages[2]: "garbage", pages[3]: "boom"}
        self.media_fetcher = _Fetcher({ref(page_id): how for page_id, how in failing.items()})
        competitor_id, resolution = self._resolved()
        by_page = {c["page_id"]: c for c in resolution["candidates"]}
        for page_id in failing:
            self.assertIsNone(by_page[page_id]["avatar"], page_id)
        self.assertTrue(all(by_page[page_id]["avatar"] for page_id in by_page if page_id not in failing))
        avatars = self._evidence(resolution["resolve_job_id"])["avatars"]
        self.assertEqual({page_id: avatars[page_id]["reason"] for page_id in failing},
                         {pages[1]: "expired", pages[2]: "unsupported_type", pages[3]: "fetch_failed"})
        self.assertEqual(len(self.media_fetcher.urls), len(pages))  # each avatar fetched once
        self.assertTrue(resolution["auto_confirmed"])

    def test_a_real_resolve_only_downloads_from_metas_cdn(self):
        self._bind_market_data()
        competitor_id, resolution = self._resolved(body={"execution_mode": "byok"})
        # The mock stands in for ScrapeCreators but its links are not on *.fbcdn.net:
        # the real downloader refuses them before any request, and the cards keep placeholders.
        self.assertEqual(resolution["execution_mode"], "byok")
        self.assertTrue(all(c["avatar"] is None for c in resolution["candidates"]))
        avatars = self._evidence(resolution["resolve_job_id"])["avatars"]
        self.assertEqual({outcome["reason"] for outcome in avatars.values()}, {"host_not_allowed"})
        self.assertEqual(resolution["cost"]["credits_charged"], 4)

    def test_avatars_are_served_only_while_the_resolve_can_be_confirmed(self):
        competitor_id, resolution = self._resolved()
        with tenant_session(self.ctx) as session:
            session.execute(update(CompetitorResolutionRecord)
                            .where(CompetitorResolutionRecord.job_id == resolution["resolve_job_id"])
                            .values(expires_at=utcnow() - timedelta(seconds=1)))
        expired = self._resolution(competitor_id, resolution["resolve_job_id"])
        self.assertFalse(expired["reusable"])
        self.assertTrue(all(c["avatar"] is None for c in expired["candidates"]))

    def test_members_and_the_owner_see_avatars_another_workspace_gets_404(self):
        competitor_id, resolution = self._resolved()
        path = f"/api/v1/competitors/{competitor_id}/resolutions/{resolution['resolve_job_id']}"
        for reader in (self._make_context(self.ctx.workspace_id, role=ROLE_MEMBER, actor="member_c"),
                       self.owner_ctx(self.ctx)):
            self.current = reader
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(all(c["avatar"] for c in response.json()["candidates"]), reader.role)
        self.current = self._make_context(role=ROLE_MEMBER, actor="member_b")
        self._problem(self.client.get(path), 404, "competitor_not_found")
