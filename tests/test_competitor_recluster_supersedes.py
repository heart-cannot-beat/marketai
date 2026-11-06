"""Issue #353 -- a completed recluster supersedes the scan's own cluster step.

C-50 (#277) found it on S1: the scan's ``cluster:1`` failed (#345), and two
paid reclusters later completed revisions 1 and 2. Every resume still owed
``cluster:1``, so the panel offered a paid resume, and that resume would
have clustered the scan's first, frozen groups again as revision 3 and
replaced the snapshot the member had paid for. Now a completed recluster
counts as the cluster step done (the maintainer's option A): a resume
neither reserves nor calls ``cluster:1``, and the step's record is left as
it was (``failed`` with its reason), read beside the scan's ``reclusters``.
A failed recluster supersedes nothing.

Covered: the rule (pure) and the plan it feeds; S1's shape under the
Controlled Mock (the clustering answer names an item never sent); byok,
with the scan's clustering refused by the AI key: a flipped ad tagged by a
resume that never clusters, a resume admitted by the previous release that
reserved ``cluster:1`` (skipped and released), and a ``partial`` scan whose
resume fetches, filters, tags and discovers without clustering again.
"""
from __future__ import annotations

import contextlib
import unittest
from unittest.mock import patch

from tests.test_competitor_snapshots import REJECTED, SnapshotFixtures

from app.competitors import clusters, scan_plan
from app.providers.llm_chat import llm_call_key

PARAMS = scan_plan.ScanParams(
    competitor_ids=("comp_1",), keywords=(), country="US", max_pages_per_competitor=3, keyword_pages=2,
    max_media_refreshes=10, max_tagged_ads=60, max_discover_advertisers=30, language="en",
)
UNANSWERED = {"cluster": {"status": "failed", "reason": "competitor_clusters_items_mismatch", "answered": []}}


@contextlib.contextmanager
def _answers_name_an_unknown_item():
    """The Mock's clustering answer names an item the request never sent:
    refused as ``competitor_clusters_items_mismatch``, as S1's was."""
    original_answer, original_schema = clusters.mock_answer, clusters.build_schema

    def with_unknown(user_text: str) -> dict:
        answer = original_answer(user_text)
        answer["clusters"][0]["item_ids"].append("o999")
        return answer

    def without_enum(ids):  # the schema's enum would refuse it first
        schema = original_schema(ids)
        schema["properties"]["clusters"]["items"]["properties"]["item_ids"]["items"] = {"type": "string"}
        return schema

    with patch.object(clusters, "mock_answer", with_unknown), patch.object(clusters, "build_schema", without_enum):
        yield


# ---------------------------------------------------------------------------
# Pure: the rule and the resume plan
# ---------------------------------------------------------------------------


class SupersededRuleTests(unittest.TestCase):
    def test_only_a_completed_recluster_supersedes_an_unanswered_cluster_step(self):
        self.assertFalse(clusters.superseded_by_recluster(UNANSWERED))
        for status in ("queued", "running", "failed"):
            self.assertFalse(clusters.superseded_by_recluster({**UNANSWERED, "reclusters": [{"status": status}]}))
        self.assertTrue(clusters.superseded_by_recluster(
            {**UNANSWERED, "reclusters": [{"status": "failed"}, {"status": "completed"}, {"status": "failed"}]}))

    def test_an_answered_cluster_step_is_answered_not_superseded(self):
        self.assertFalse(clusters.superseded_by_recluster(
            {"cluster": {"answered": ["cluster:1"]}, "reclusters": [{"status": "completed"}]}))

    def test_a_resume_owes_cluster_1_until_a_recluster_completes(self):
        backlog = scan_plan.LlmBacklog(relevant=3)
        for pages_left in (False, True):
            owed = scan_plan.llm_backlog_names(PARAMS, UNANSWERED, backlog, pages_left=pages_left)
            self.assertIn(("cluster", "cluster:1"), owed)
            failed = {**UNANSWERED, "reclusters": [{"status": "failed"}]}
            self.assertIn(("cluster", "cluster:1"),
                          scan_plan.llm_backlog_names(PARAMS, failed, backlog, pages_left=pages_left))
            superseded = {**UNANSWERED, "reclusters": [{"status": "completed"}]}
            self.assertEqual(scan_plan.llm_backlog_names(PARAMS, superseded, backlog, pages_left=pages_left),
                             [item for item in owed if item[0] != "cluster"])


# ---------------------------------------------------------------------------
# Real routes and the real Worker
# ---------------------------------------------------------------------------


class SupersededFixtures(SnapshotFixtures):
    def _cluster_step(self, scan_id: str) -> dict:
        return next(step for step in self._scan(scan_id)["steps"] if step["step"] == clusters.STEP)

    @staticmethod
    def _llm_lines(quote: dict) -> dict[str, int]:
        return {line["step"]: line["calls"] for line in quote["llm"]["lines"]}

    def _cluster_revision_of(self, scan_id: str, snapshot_id: str) -> int | None:
        return next(row.cluster_revision for row in self._snapshot_rows(scan_id) if row.id == snapshot_id)


class MockSupersededTests(SupersededFixtures):
    def test_s1_a_completed_recluster_ends_the_resume_its_failed_cluster_step_offered(self):
        accepted = self._acme_and_self(keywords=[])
        with _answers_name_an_unknown_item():
            self._drain()
        scan_id = accepted["scan_id"]
        failed = self._cluster_step(scan_id)
        self.assertEqual((failed["status"], failed["reason"], failed["attempts"]),
                         ("failed", "competitor_clusters_items_mismatch", 1))
        self.assertEqual(self._cluster_rows(scan_id), [])
        self.assertTrue(self._scan(scan_id)["can_resume"])
        quote = self._resume_quote(scan_id)
        self.assertEqual(quote["market_data"]["max_credits"], 0)
        self.assertEqual(self._llm_lines(quote), {"cluster": 1})  # all a resume would do is cluster

        # A recluster refused the same way supersedes nothing.
        self._recluster(scan_id)
        with _answers_name_an_unknown_item():
            self._drain()
        [refused] = self._scan(scan_id)["reclusters"]
        self.assertEqual((refused["status"], refused["failure"]),
                         ("failed", {"code": "competitor_clusters_items_mismatch"}))
        self.assertTrue(self._scan(scan_id)["can_resume"])
        self.assertEqual(self._llm_lines(self._resume_quote(scan_id)), {"cluster": 1})

        self._recluster(scan_id)
        self._drain()
        scan = self._scan(scan_id)
        [_refused, entry] = scan["reclusters"]
        self.assertEqual((entry["status"], entry["cluster_revision"]), ("completed", 1))
        self.assertFalse(scan["can_resume"])
        self._problem(self.client.get(f"/api/v1/competitor-scans/{scan_id}/resume-quote"),
                      409, "competitor_scan_not_resumable")
        self._problem(self.client.post(f"/api/v1/competitor-scans/{scan_id}/resume", headers=self._headers()),
                      409, "competitor_scan_not_resumable")
        self.assertEqual((scan["status"], scan["failure"]), ("completed", None))
        self.assertEqual(self._cluster_step(scan_id), failed)  # the step's history is not rewritten
        self.assertEqual(scan["snapshot_id"], entry["snapshot_id"])
        self.assertEqual([row.revision for row in self._cluster_rows(scan_id)], [1])
        self.assertEqual(self._summary(scan_id)["snapshot_id"], entry["snapshot_id"])


class ByokSupersededTests(SupersededFixtures):
    def setUp(self) -> None:
        super().setUp()
        self._bind_both()
        self.cluster_answers = [REJECTED]  # the scan's cluster:1: the AI key is refused

    def _supersede(self, scan_id: str) -> tuple[dict, dict]:
        """The scan's cluster step failed; a paid recluster completes revision 1."""
        failed = self._cluster_step(scan_id)
        self.assertEqual((failed["status"], failed["reason"]), ("failed", "credential_rejected"))
        self.assertIn("cluster", self._llm_lines(self._resume_quote(scan_id)))  # owed until a recluster completes
        self._recluster(scan_id)
        self._drain()
        [entry] = self._scan(scan_id)["reclusters"]
        self.assertEqual((entry["status"], entry["cluster_revision"]), ("completed", 1))
        return failed, entry

    def test_a_resume_after_a_flip_tags_the_flipped_ad_and_never_clusters_again(self):
        accepted = self._acme_and_self(keywords=[])
        self._drain()
        scan_id = accepted["scan_id"]
        failed, entry = self._supersede(scan_id)
        before = self._scan(scan_id)
        self.assertFalse(before["can_resume"])
        flipped = self._all_ads(scan_id, relevance="unrelated")[0]
        self.assertEqual(self._flip(scan_id, flipped["ad_id"], "relevant").status_code, 200)

        quote = self._resume_quote(scan_id)
        self.assertEqual(self._llm_lines(quote), {"tag": 1})  # C-32: the flipped ad is tagged, not clustered
        resumed = self._resume(scan_id)
        job_id = resumed["job_id"]
        [tag_name] = [name for name in (f"tag:{i}" for i in range(1, 61))
                      if llm_call_key(job_id, name) in self._scan_reservations(job_id)]
        self.assertEqual(set(self._scan_reservations(job_id)), {llm_call_key(job_id, tag_name)})
        self._drain()

        scan = self._scan(scan_id)
        self.assertEqual(self._attempt_names(job_id), {tag_name: "succeeded"})
        self.assertEqual([row.revision for row in self._cluster_rows(scan_id)], [1])
        self.assertNotEqual(scan["snapshot_id"], entry["snapshot_id"])  # the flip and its tag changed the content
        self.assertEqual(self._cluster_revision_of(scan_id, scan["snapshot_id"]), 1)
        self.assertEqual(self._cluster_step(scan_id), failed)  # status, reason and attempts unchanged
        self.assertEqual(self._open_reservations(job_id), [])
        self.assertEqual(scan["cost"]["llm"]["calls"], before["cost"]["llm"]["calls"] + 1)
        self.assertIn(flipped["ad_id"], [ad["ad_id"] for ad in self._summary(scan_id)["unclustered"]])
        self.assertFalse(scan["can_resume"])

    def test_a_resume_admitted_by_the_previous_release_skips_cluster_1_and_releases_it(self):
        accepted = self._acme_and_self(keywords=[])
        self._drain()
        scan_id = accepted["scan_id"]
        failed, entry = self._supersede(scan_id)
        # The release before #353 still owed cluster:1 here, so its admission reserved it.
        with patch.object(clusters, "superseded_by_recluster", lambda evidence: False):
            resumed = self._resume(scan_id)
        job_id = resumed["job_id"]
        key = llm_call_key(job_id, clusters.CALL_NAME)
        self.assertEqual(set(self._scan_reservations(job_id)), {key})
        self._drain()

        scan = self._scan(scan_id)
        self.assertEqual(self._attempt_names(job_id), {})
        self.assertEqual(self._scan_reservations(job_id)[key].state, "released")
        self.assertEqual(self._cluster_step(scan_id), failed)
        self.assertEqual([row.revision for row in self._cluster_rows(scan_id)], [1])
        self.assertEqual((scan["status"], scan["snapshot_id"]), ("completed", entry["snapshot_id"]))
        self.assertFalse(scan["can_resume"])

    def test_a_partial_scan_resumes_its_pages_and_judgments_without_clustering_again(self):
        self.mock_credits = 4  # Acme Charge's three pages and Brightvolt's one; Halcyon Motors' first page gets a 402
        accepted = self._start(self._body())
        self._drain()
        scan_id = accepted["scan_id"]
        self.assertEqual(self._scan(scan_id)["status"], "partial")
        failed, entry = self._supersede(scan_id)

        quote = self._resume_quote(scan_id)
        self.assertGreater(quote["market_data"]["max_credits"], 0)
        lines = self._llm_lines(quote)
        self.assertNotIn("cluster", lines)
        self.assertLessEqual({"filter", "tag", "discover"}, set(lines))
        self.mock_credits = 100  # topped up
        resumed = self._resume(scan_id)
        job_id = resumed["job_id"]
        self.assertNotIn(llm_call_key(job_id, clusters.CALL_NAME), self._scan_reservations(job_id))
        self._drain()

        scan = self._scan(scan_id)
        self.assertEqual(scan["status"], "completed")
        names = self._attempt_names(job_id)
        self.assertNotIn(clusters.CALL_NAME, names)
        self.assertTrue(any(name.startswith("filter:") for name in names))
        self.assertEqual([row.revision for row in self._cluster_rows(scan_id)], [1])
        self.assertEqual(self._cluster_revision_of(scan_id, scan["snapshot_id"]), 1)
        self.assertEqual(self._cluster_step(scan_id), failed)
        self.assertEqual(self._open_reservations(job_id), [])
        self.assertFalse(scan["can_resume"])


if __name__ == "__main__":
    unittest.main()
