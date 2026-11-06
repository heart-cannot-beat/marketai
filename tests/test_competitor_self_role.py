"""Issue #328: the product's own page is ``role=self`` however its ads were found.

The acceptance run's set-up over the Controlled Mock P0 recordings: Halcyon Motors and
Brightvolt as competitors, Acme Charge (``100000000000010``) as the product's own page,
both keywords. Acme Charge's page fetch stops at three pages, so the keyword
searches find more of its ads -- among them "Charging Without Limits"
(``100000000011043``), which only ``keyword:`` origins reach. An observation
whose advertiser page is the confirmed page of a ``role=self`` target is
``self`` (the clusters' way of telling the own page), so:

* the board's ``role=self`` group lists every own-page ad, the main list none;
* a snapshot frozen now marks them ``self`` (``ads``, ``self_ad_ids``, counts,
  cluster advertisers);
* ``competitor_copy_reuse`` lets the user reuse that title while a
  competitor's title still blocks;
* a snapshot frozen before the fix is never rewritten: the summary says a
  refreeze would change it, the refreeze is a new snapshot, and a redraft
  cites it;
* another Workspace gets 404.
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from tests.test_competitor_scans_api import HALCYON
from tests.test_competitors_api import BRIGHTVOLT, ACME
from tests.test_marketing_competitor_review import CompetitorPlanFixtures

from app.competitors import snapshots

OWN_KEYWORD_ARCHIVE_ID = "100000000011043"
OWN_KEYWORD_HEADLINE = "Charging Without Limits"  # Acme Charge's own ad, reached only by both keywords
OWN_PAGE_HEADLINE = "Unlock Halcyon Supercharging for Non-Halcyon EVs"  # Acme Charge's own ad, fetched from its page
COMPETITOR_HEADLINE = "Enjoy Charging Without Limits."  # Brightvolt's ad, a competitor here


def _page_only_roles(record, origins, pages=None):
    """The projection before #328: ``page:`` origins only (a snapshot frozen before the fix)."""
    by_competitor = {target["competitor_id"]: target.get("role")
                     for target in (record.evidence_json or {}).get("targets") or [] if target.get("competitor_id")}
    roles = {}
    for ad_id, rows in origins.items():
        competitor_ids = [origin.split(":", 1)[1] for origin in rows if origin.startswith("page:")]
        roles[ad_id] = (None if not competitor_ids
                        else "self" if any(by_competitor.get(cid) == "self" for cid in competitor_ids)
                        else "competitor")
    return roles


class OwnPageFixtures(CompetitorPlanFixtures):
    def _own_page_scan(self) -> str:
        """Halcyon Motors and Brightvolt as competitors, Acme Charge as the product's own page, both keywords."""
        ids = [self._confirmed(HALCYON), self._confirmed(BRIGHTVOLT), self._confirmed(ACME, role="self")]
        accepted = self._start(self._body(ids))
        self._drain()
        self.assertEqual(self._scan(accepted["scan_id"])["status"], "completed")
        return accepted["scan_id"]

    @staticmethod
    def _keyword_only(ad: dict) -> bool:
        return all(origin["kind"] == "keyword" for origin in ad["origins"])


class BoardAndSnapshotTests(OwnPageFixtures):
    def test_own_page_ads_found_only_by_keyword_are_self_on_the_board_and_in_the_snapshot(self):
        scan_id = self._own_page_scan()
        ads = self._all_ads(scan_id)
        own = {ad["ad_id"]: ad for ad in ads if ad["advertiser_page_id"] == ACME}
        keyword_only = {ad_id for ad_id, ad in own.items() if self._keyword_only(ad)}
        # The acceptance run's shape: 50 ads from three page fetches, 8 more only a keyword reached.
        self.assertEqual((len(own), len(keyword_only)), (58, 8))
        repro = next(ad for ad in own.values() if ad["ad_archive_id"] == OWN_KEYWORD_ARCHIVE_ID)
        self.assertIn(repro["ad_id"], keyword_only)
        self.assertEqual({origin["origin"] for origin in repro["origins"]},
                         {"keyword:NACS adapter", "keyword:CCS1 adapter"})

        # Every own-page ad is self; which page fetched it is still what competitor_ids say.
        self.assertEqual({ad["role"] for ad in own.values()}, {"self"})
        self.assertTrue(all(own[ad_id]["competitor_ids"] == [] for ad_id in keyword_only))
        self.assertNotIn("self", {ad["role"] for ad in ads if ad["advertiser_page_id"] != ACME})

        # The board: the self group is the whole own page (its total too); the other filters leave it out.
        self_group = self._all_ads(scan_id, role="self")
        self.assertEqual({ad["ad_id"] for ad in self_group}, set(own))
        first = self.client.get(f"/api/v1/competitor-scans/{scan_id}/ads", params={"role": "self", "limit": 5})
        self.assertEqual(first.json()["total"], len(own))
        self.assertFalse(set(own) & {ad["ad_id"] for ad in self._all_ads(scan_id, role="competitor")})
        keyword_self = self._all_ads(scan_id, role="self", origin_kind="keyword")
        self.assertTrue(keyword_only <= {ad["ad_id"] for ad in keyword_self})

        # The snapshot frozen by the scan: own-page entries are self, listed apart and never crowding.
        scan = self._scan(scan_id)
        frozen = self._content(scan["snapshot_id"])
        entries = {ad_id: entry for ad_id, entry in frozen["ads"].items() if entry["advertiser_page_id"] == ACME}
        self.assertTrue(keyword_only & set(entries))
        self.assertEqual({entry["role"] for entry in entries.values()}, {"self"})
        judged_own = {ad_id for ad_id, ad in own.items() if ad["relevance"]["relevance"] in ("relevant", "adjacent")}
        self.assertTrue(keyword_only & judged_own)
        self.assertEqual(set(frozen["self_ad_ids"]), judged_own)
        self.assertEqual(frozen["counts"]["self"], len(judged_own))
        summary = self._summary(scan_id)
        self.assertEqual({ad["ad_id"] for ad in summary["self_observations"]}, judged_own)
        self.assertTrue(all(ad["role"] == "self" for ad in summary["self_observations"]))
        for cluster in frozen["claim_clusters"]:
            acme = [item for item in cluster["advertisers"] if item["advertiser_page_id"] == ACME]
            self.assertTrue(all(item["role"] == "self" for item in acme))
            self.assertEqual(cluster["advertiser_count"], len(cluster["advertisers"]) - len(acme))
            self.assertEqual(cluster["self_included"], bool(acme))

        # Another Workspace reads none of it.
        stranger = self._make_context(f"ws_self_other_{self.ctx.workspace_id[-6:]}", role="member",
                                      actor="member_b")
        for current in (stranger, self.owner_ctx(stranger)):
            self.current = current
            for response in (
                self.client.get(f"/api/v1/competitor-scans/{scan_id}/ads", params={"role": "self"}),
                self.client.get(f"/api/v1/competitor-scans/{scan_id}/summary"),
                self.client.get(f"/api/v1/competitor-scans/{scan_id}/snapshots/{scan['snapshot_id']}"),
            ):
                self._problem(response, 404, "competitor_scan_not_found")
        self.current = self.ctx
        self.assertEqual(len(self._all_ads(scan_id, role="self")), len(own))


class CopyReuseTests(OwnPageFixtures):
    def test_own_keyword_only_title_is_reusable_and_a_competitor_title_still_blocks(self):
        project = self._understood()
        snapshot_id = self._snapshot_id(self._own_page_scan())
        frozen = self._content(snapshot_id)
        drafted = self._drafted(project)
        self.assertEqual(drafted["plan"]["competitor_snapshot_id"], snapshot_id)

        plan = deepcopy(drafted["plan"])
        plan["combinations"][0]["headline"] = OWN_KEYWORD_HEADLINE
        plan["combinations"][1]["headline"] = OWN_PAGE_HEADLINE
        plan["combinations"][2]["headline"] = COMPETITOR_HEADLINE
        saved = self._saved(drafted, plan)
        [reused] = self._findings(saved, "competitor_copy_reuse")
        self.assertEqual((reused["severity"], reused["source"], reused["target_ids"]),
                         ("blocking", "system", [plan["combinations"][2]["id"]]))
        self.assertTrue(reused["observations"])
        for observation in reused["observations"]:
            entry = frozen["ads"][observation["ad_id"]]
            self.assertEqual((entry["advertiser_name"], entry["role"]), ("Brightvolt", "competitor"))
        blocked = self._edit(saved, "confirm_plan", plan={**saved["plan"], "executions_per_combination": 1})
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(blocked.json()["code"], "plan_review_unresolved")

        # The competitor's title rewritten, both own titles stay: the plan confirms.
        fixed = deepcopy(saved["plan"])
        fixed["combinations"][2]["headline"] = drafted["plan"]["combinations"][2]["headline"]
        confirmed = self._saved(saved, {**fixed, "executions_per_combination": 1}, "confirm_plan")
        self.assertEqual(self._findings(confirmed, "competitor_copy_reuse"), [])
        self.assertEqual(confirmed["state"], "plan_confirmed")
        self.assertEqual([combo["headline"] for combo in confirmed["plan"]["combinations"][:2]],
                         [OWN_KEYWORD_HEADLINE, OWN_PAGE_HEADLINE])


class FrozenSnapshotTests(OwnPageFixtures):
    def test_a_snapshot_frozen_before_the_fix_is_kept_and_a_refreeze_and_redraft_pick_it_up(self):
        project = self._understood()
        with patch.object(snapshots, "roles_of", _page_only_roles):
            scan_id = self._own_page_scan()
        old_id = self._snapshot_id(scan_id)
        old = self._content(old_id)
        repro = next(ad_id for ad_id, entry in old["ads"].items()
                     if entry["ad_archive_id"] == OWN_KEYWORD_ARCHIVE_ID)
        self.assertIsNone(old["ads"][repro]["role"])
        drafted = self._drafted(project)
        self.assertEqual(drafted["plan"]["competitor_snapshot_id"], old_id)
        plan = deepcopy(drafted["plan"])
        plan["combinations"][0]["headline"] = OWN_KEYWORD_HEADLINE
        saved = self._saved(drafted, plan)
        # The plan cites the old snapshot, which is never rewritten: the title still blocks there.
        [reused] = self._findings(saved, "competitor_copy_reuse")
        self.assertIn(repro, [observation["ad_id"] for observation in reused["observations"]])
        self.assertEqual(self._content(old_id), old)

        # The fix only changes what a freeze computes now: the summary offers the refreeze.
        summary = self._summary(scan_id)
        self.assertEqual(summary["snapshot_id"], old_id)
        self.assertTrue(summary["pending_changes"])
        self.assertNotIn(repro, {ad["ad_id"] for ad in summary["self_observations"]})
        new_id = self._refreeze(scan_id)["snapshot_id"]
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(self._content(old_id), old)
        new = self._content(new_id)
        self.assertEqual(new["ads"][repro]["role"], "self")
        self.assertIn(repro, new["self_ad_ids"])
        self.assertGreater(new["counts"]["self"], old["counts"]["self"])
        self.assertFalse(self._summary(scan_id)["pending_changes"])

        # The plan keeps its snapshot (now stale); a redraft cites the new one and the own title passes.
        stale = self._read(saved)
        self.assertTrue(stale["competitor_snapshot_stale"])
        self.assertEqual(stale["plan"]["competitor_snapshot_id"], old_id)
        redrafted = self._drafted(stale)
        self.assertEqual(redrafted["plan"]["competitor_snapshot_id"], new_id)
        plan = deepcopy(redrafted["plan"])
        plan["combinations"][0]["headline"] = OWN_KEYWORD_HEADLINE
        confirmed = self._saved(redrafted, {**plan, "executions_per_combination": 1}, "confirm_plan")
        self.assertEqual(self._findings(confirmed, "competitor_copy_reuse"), [])
        self.assertEqual(confirmed["state"], "plan_confirmed")


class RoleProjectionTests(TestCase):
    """:func:`snapshots.roles_of` alone: the scan's frozen targets decide."""

    record = SimpleNamespace(evidence_json={"targets": [
        {"competitor_id": "cmp_own", "role": "self", "page_id": "111"},
        {"competitor_id": "cmp_rival", "role": "competitor", "page_id": "222"},
        {"competitor_id": None, "role": None, "page_id": None, "keyword": "NACS adapter"},
    ]})

    def test_the_own_page_is_self_by_origin_or_by_advertiser_page(self):
        origins = {
            "own_by_page": ["page:cmp_own", "keyword:NACS adapter"],
            "own_by_keyword": ["keyword:NACS adapter"],
            "rival_by_page": ["page:cmp_rival"],
            "rival_by_keyword": ["keyword:NACS adapter"],
            "stranger": ["keyword:NACS adapter"],
            "no_page": ["keyword:NACS adapter"],
        }
        pages = {"own_by_page": "111", "own_by_keyword": "111", "rival_by_page": "222", "rival_by_keyword": "222",
                 "stranger": "333", "no_page": None}
        self.assertEqual(snapshots.roles_of(self.record, origins, pages), {
            "own_by_page": "self", "own_by_keyword": "self", "rival_by_page": "competitor",
            "rival_by_keyword": None, "stranger": None, "no_page": None,
        })

    def test_only_a_page_this_scan_targeted_as_self_counts(self):
        record = SimpleNamespace(evidence_json={"targets": [
            {"competitor_id": "cmp_rival", "role": "competitor", "page_id": "111"},
        ]})
        self.assertEqual(snapshots.roles_of(record, {"ad": ["keyword:NACS adapter"]}, {"ad": "111"}), {"ad": None})
