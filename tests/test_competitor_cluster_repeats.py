"""Issue #345 -- a clustering answer that names one observation in more than one cluster.

The real ``gpt-5-mini`` (C-50, #277) put an ad into two buying claims. The
validator refused the whole answer (``competitor_clusters_items_mismatch``),
so the scan's cluster step failed, and a paid recluster failed the same way.
Repeats are now dropped the way ``claim_clusters`` always assembled them: an
item stays in the first cluster that names it, its later repeats are
skipped, and a cluster left empty is dropped. The revision's evidence lists
what was dropped (item ID and the answer's cluster position, no ad content).
IDs the request did not send are still refused, and logged.

Covered: the validator and the assembly (pure); the Worker's scan
(``trigger=scan``) and paid recluster (``trigger=recluster``) answered by
the Controlled Mock clustering fixture with repeats added, under the Mock and
under a fake OpenAI answering the same way (byok): the step completes, the
revision has clusters, one request is sent and the call is settled once.
"""
from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from tests.test_competitor_snapshots import SnapshotFixtures

from app.adapters.persistence.database import tenant_session
from app.competitors import clusters
from app.entitlements.cost_models import ProviderCostLedgerEntryRecord
from app.providers.llm_chat import llm_call_key, llm_call_evidence
from app.providers.llm_types import LlmOutputValidationError

REPEATS_ONLY = "Repeats only (Controlled Mock)"

#: Three items: o1 (two variants), o2, o3.
GROUPS = [{"ad_ids": ["ad1", "ad1v"]}, {"ad_ids": ["ad2"]}, {"ad_ids": ["ad3"]}]
ADS = {
    ad_id: SimpleNamespace(id=ad_id, advertiser_page_id=page, advertiser_name=name, days_running=days)
    for ad_id, page, name, days in (
        ("ad1", "p1", "Acme Charge", 30), ("ad1v", "p1", "Acme Charge", 12), ("ad2", "p2", "Northpeak", 90),
        ("ad3", "p3", "Brightvolt", None),
    )
}


def _shape(found: list[dict]) -> list[tuple]:
    return [(cluster["cluster_id"], cluster["claim"], cluster["ad_ids"]) for cluster in found]


# ---------------------------------------------------------------------------
# Pure: validation and assembly
# ---------------------------------------------------------------------------


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validate = clusters._validator(["o1", "o2", "o3"])

    def test_repeated_ids_are_accepted(self):
        self.validate({"clusters": [{"claim": "A", "item_ids": ["o1", "o2"]},
                                    {"claim": "B", "item_ids": ["o2", "o3", "o3"]}], "conclusion": ""})
        self.validate({"clusters": [], "conclusion": ""})

    def test_ids_the_request_did_not_send_are_refused_and_logged(self):
        with patch.object(clusters, "logger") as log, self.assertRaises(LlmOutputValidationError) as raised:
            self.validate({"clusters": [{"claim": "A", "item_ids": ["o1", "o9", "x"]},
                                        {"claim": "B", "item_ids": ["o9"]}], "conclusion": ""})
        self.assertEqual(raised.exception.code, "competitor_clusters_items_mismatch")
        self.assertIn("(2): o9, x", raised.exception.detail)
        log.warning.assert_called_once()
        args, kwargs = log.warning.call_args
        self.assertTrue(args[0].startswith("competitor_clusters_unknown_items"))
        self.assertIn("o9, x", args[0] % args[1:])  # visible without a structured formatter
        self.assertEqual((kwargs["extra"]["unknown_item_ids"], kwargs["extra"]["unknown_count"],
                          kwargs["extra"]["expected_count"]), (["o9", "x"], 2, 3))

    def test_an_unknown_id_is_cut_and_at_most_twenty_are_named(self):
        with patch.object(clusters, "logger") as log, self.assertRaises(LlmOutputValidationError) as raised:
            self.validate({"clusters": [{"claim": "A", "item_ids": ["z" * 500] + [f"u{i:02d}" for i in range(30)]}],
                           "conclusion": ""})
        extra = log.warning.call_args.kwargs["extra"]
        self.assertEqual(extra["unknown_count"], 31)
        self.assertEqual(len(extra["unknown_item_ids"]), 20)
        self.assertTrue(all(len(item) <= 40 for item in extra["unknown_item_ids"]))
        self.assertLess(len(raised.exception.detail), 1_000)

    def test_the_policy_is_named_after_the_rule(self):
        self.assertEqual(clusters.VALIDATION_POLICY, "competitor_clusters_known_items")


class AssemblyTests(unittest.TestCase):
    def test_a_repeated_item_stays_in_the_first_cluster_that_names_it(self):
        data = {"clusters": [{"claim": "Covers every charger", "item_ids": ["o1", "o2"]},
                             {"claim": "Will not overheat", "item_ids": ["o2", "o3"]}], "conclusion": "x"}
        found = clusters.claim_clusters(data, GROUPS, ADS, {})
        self.assertEqual(_shape(found), [("c1", "Covers every charger", ["ad1", "ad1v", "ad2"]),
                                         ("c2", "Will not overheat", ["ad3"])])
        self.assertEqual(found[0]["advertiser_count"], 2)
        self.assertEqual(found[0]["max_days_running"], 90)
        self.assertEqual(clusters.repeated_items(data, GROUPS), [{"item_id": "o2", "answer_cluster": 2}])

    def test_a_cluster_left_with_repeats_only_is_dropped(self):
        data = {"clusters": [{"claim": "A", "item_ids": ["o1", "o1"]},  # repeated within its own cluster too
                             {"claim": "B", "item_ids": ["o1"]},
                             {"claim": "C", "item_ids": ["o3"]}], "conclusion": ""}
        self.assertEqual(_shape(clusters.claim_clusters(data, GROUPS, ADS, {})),
                         [("c1", "A", ["ad1", "ad1v"]), ("c2", "C", ["ad3"])])
        self.assertEqual(clusters.repeated_items(data, GROUPS),
                         [{"item_id": "o1", "answer_cluster": 1}, {"item_id": "o1", "answer_cluster": 2}])

    def test_a_cluster_without_a_claim_holds_no_item(self):
        data = {"clusters": [{"claim": "  ", "item_ids": ["o1"]}, {"claim": "B", "item_ids": ["o1", "o2"]}],
                "conclusion": ""}
        self.assertEqual(_shape(clusters.claim_clusters(data, GROUPS, ADS, {})),
                         [("c1", "B", ["ad1", "ad1v", "ad2"])])
        self.assertEqual(clusters.repeated_items(data, GROUPS), [])

    def test_an_answer_without_repeats_drops_nothing(self):
        data = {"clusters": [{"claim": "A", "item_ids": ["o3", "o1"]}, {"claim": "B", "item_ids": ["o2"]}],
                "conclusion": ""}
        self.assertEqual(_shape(clusters.claim_clusters(data, GROUPS, ADS, {})),
                         [("c1", "A", ["ad1", "ad1v", "ad3"]), ("c2", "B", ["ad2"])])
        self.assertEqual(clusters.repeated_items(data, GROUPS), [])
        self.assertEqual(clusters.repeated_items({}, GROUPS), [])


# ---------------------------------------------------------------------------
# The Worker: scan and paid recluster
# ---------------------------------------------------------------------------


class RepeatedItemsWorkerFixtures(SnapshotFixtures):
    """Every clustering answer is the Mock's, with its first item repeated in
    each of its clusters and in one more cluster of repeats only."""

    def setUp(self) -> None:
        super().setUp()
        self.answers: list[dict] = []
        original = clusters.mock_answer

        def with_repeats(user_text: str) -> dict:
            answer = original(user_text)
            first = answer["clusters"][0]["item_ids"][0]
            for cluster in answer["clusters"]:
                cluster["item_ids"].append(first)
            answer["clusters"].append({"claim": REPEATS_ONLY, "item_ids": [first]})
            self.answers.append(copy.deepcopy(answer))
            return answer

        # Both the Mock fixture and the fake OpenAI build their answer here.
        patcher = patch.object(clusters, "mock_answer", with_repeats)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _assert_deduplicated(self, scan_id: str, revision: int, *, trigger: str, groups: list[dict]) -> None:
        row = self._cluster_rows(scan_id)[revision - 1]
        self.assertEqual((row.revision, row.model_evidence_json["trigger"]), (revision, trigger))
        answer = self.answers[-1]
        self.assertGreaterEqual(len(answer["clusters"]), 3)  # two of the Mock's, at least, and the repeats
        first = answer["clusters"][0]["item_ids"][0]
        evidence = row.model_evidence_json
        self.assertEqual(evidence["validation_policy"], clusters.VALIDATION_POLICY)
        self.assertEqual(evidence["deduplicated_items"],
                         [{"item_id": first, "answer_cluster": position}
                          for position in range(1, len(answer["clusters"]) + 1)])
        found = row.payload_json["claim_clusters"]
        self.assertEqual(len(found), len(answer["clusters"]) - 1)
        self.assertNotIn(REPEATS_ONLY, {cluster["claim"] for cluster in found})
        repeated = set(groups[int(first[1:]) - 1]["ad_ids"])
        self.assertTrue(repeated <= set(found[0]["ad_ids"]))
        listed = [ad_id for cluster in found for ad_id in cluster["ad_ids"]]
        self.assertEqual(len(listed), len(set(listed)))  # every observation in at most one cluster

    def _cluster_requests(self) -> int:
        return len([body for body in self.openai_requests
                    if body["response_format"]["json_schema"]["name"] == clusters.SCHEMA_NAME])

    def _ledger(self, job_id: str) -> list[str]:
        reservation = self._scan_reservations(job_id)[llm_call_key(job_id, clusters.CALL_NAME)]
        self.assertEqual(reservation.state, "settled")
        with tenant_session(self.ctx) as session:
            return list(session.scalars(select(ProviderCostLedgerEntryRecord.entry_type).where(
                ProviderCostLedgerEntryRecord.workspace_id == self.ctx.workspace_id,
                ProviderCostLedgerEntryRecord.spend_reservation_id == reservation.id)).all())

    def _cluster_attempt(self, job_id: str):
        [attempt] = [row for row in self._llm_attempts(job_id)
                     if row.call_semantic_key_hash == llm_call_key(job_id, clusters.CALL_NAME)]
        return attempt

    def _scan_groups(self, scan_id: str) -> list[dict]:
        return self._record(scan_id).evidence_json["cluster"]["assignments"][clusters.CALL_NAME]["groups"]

    def _recluster_groups(self, scan_id: str, job_id: str) -> list[dict]:
        return next(entry["groups"] for entry in self._record(scan_id).evidence_json["reclusters"]
                    if entry["job_id"] == job_id)


class ByokRepeatedItemsTests(RepeatedItemsWorkerFixtures):
    def setUp(self) -> None:
        super().setUp()
        self._bind_both()

    def test_a_scan_keeps_an_answer_with_repeats_and_settles_its_call_once(self):
        accepted = self._acme_and_self(keywords=[])
        with patch("app.competitors.scan_worker.logger") as log:
            self._drain()
        scan_id, job_id = accepted["scan_id"], accepted["job_id"]
        scan = self._scan(scan_id)
        self.assertEqual(scan["status"], "completed")
        self.assertEqual(self._steps(scan_id)["cluster"], ("completed", None))
        self._assert_deduplicated(scan_id, 1, trigger="scan", groups=self._scan_groups(scan_id))
        self.assertTrue(self._summary(scan_id)["clusters"])
        self.assertEqual(self._record(scan_id).evidence_json["cluster"]["answered"], [clusters.CALL_NAME])

        # One request (no schema retry), one attempt, one settlement.
        self.assertEqual(self._cluster_requests(), 1)
        attempt = self._cluster_attempt(job_id)
        self.assertEqual(attempt.dispatch_state, "succeeded")
        with tenant_session(self.ctx) as session:
            evidence = llm_call_evidence(session, self.ctx, attempt.id)
        self.assertEqual((evidence["http_calls"], evidence["output_validation_policy"]),
                         (1, clusters.VALIDATION_POLICY))
        self.assertNotIn("invalid_answers", evidence)
        self.assertEqual(self._ledger(job_id).count("reported_cost"), 1)

        # A structured record with the counts.
        [(args, kwargs)] = [(call.args, call.kwargs) for call in log.warning.call_args_list
                            if call.args[0].startswith("competitor_clusters_deduplicated")]
        repeated = len(self.answers[-1]["clusters"])
        self.assertEqual({key: kwargs["extra"][key] for key in
                          ("scan_id", "job_id", "trigger", "repeated_items", "answer_clusters", "clusters")},
                         {"scan_id": scan_id, "job_id": job_id, "trigger": "scan", "repeated_items": repeated,
                          "answer_clusters": repeated, "clusters": repeated - 1})
        self.assertIn(f"repeated_items={repeated}", args[0] % args[1:])

    def test_a_paid_recluster_keeps_an_answer_with_repeats_and_settles_its_call_once(self):
        accepted = self._acme_and_self(keywords=[])
        self._drain()
        scan_id = accepted["scan_id"]
        first = self._scan(scan_id)
        sent = self._cluster_requests()
        [entry] = self._recluster(scan_id, key="recluster-repeats")["reclusters"]
        job_id = entry["job_id"]
        self._drain()

        scan = self._scan(scan_id)
        [entry] = scan["reclusters"]
        self.assertEqual((entry["status"], entry["failure"], entry["cluster_revision"]), ("completed", None, 2))
        self._assert_deduplicated(scan_id, 2, trigger="recluster", groups=self._recluster_groups(scan_id, job_id))
        self.assertEqual(scan["snapshot_id"], entry["snapshot_id"])
        self.assertTrue(self._summary(scan_id)["clusters"])

        self.assertEqual(self._cluster_requests() - sent, 1)
        self.assertEqual(self._attempt_names(job_id), {clusters.CALL_NAME: "succeeded"})
        self.assertEqual(self._ledger(job_id).count("reported_cost"), 1)
        self.assertEqual(scan["cost"]["llm"]["calls"], first["cost"]["llm"]["calls"] + 1)
        self.assertEqual(self._open_reservations(job_id), [])


class MockRepeatedItemsTests(RepeatedItemsWorkerFixtures):
    def test_a_mock_scan_and_recluster_keep_answers_with_repeats(self):
        accepted = self._acme_and_self(keywords=[])
        self._drain()
        scan_id = accepted["scan_id"]
        self.assertEqual(self._steps(scan_id)["cluster"], ("completed", None))
        self._assert_deduplicated(scan_id, 1, trigger="scan", groups=self._scan_groups(scan_id))
        self.assertEqual(self._cluster_attempt(accepted["job_id"]).dispatch_state, "succeeded")
        first = self._scan(scan_id)
        self.assertTrue(self._summary(scan_id)["clusters"])

        [entry] = self._recluster(scan_id)["reclusters"]
        self._drain()
        scan = self._scan(scan_id)
        [entry] = scan["reclusters"]
        self.assertEqual((entry["status"], entry["cluster_revision"]), ("completed", 2))
        self._assert_deduplicated(scan_id, 2, trigger="recluster",
                                  groups=self._recluster_groups(scan_id, entry["job_id"]))
        self.assertEqual(self._attempt_names(entry["job_id"]), {clusters.CALL_NAME: "succeeded"})
        self.assertEqual(scan["cost"]["llm"]["calls"], first["cost"]["llm"]["calls"] + 1)
        self.assertEqual(self.openai_requests, [])  # the Mock: nothing left


class UnknownItemWorkerTests(SnapshotFixtures):
    def test_an_answer_naming_an_item_never_sent_still_fails_and_the_log_names_it(self):
        original_answer, original_schema = clusters.mock_answer, clusters.build_schema

        def with_unknown(user_text: str) -> dict:
            answer = original_answer(user_text)
            answer["clusters"][0]["item_ids"].append("o999")
            return answer

        def without_enum(ids):  # the schema's enum would refuse it first
            schema = original_schema(ids)
            schema["properties"]["clusters"]["items"]["properties"]["item_ids"]["items"] = {"type": "string"}
            return schema

        accepted = self._acme_and_self(keywords=[])
        with patch.object(clusters, "mock_answer", with_unknown), \
                patch.object(clusters, "build_schema", without_enum), \
                patch("app.competitors.scan_worker.logger") as log:
            self._drain()
        scan_id = accepted["scan_id"]
        self.assertEqual(self._steps(scan_id)["cluster"], ("failed", "competitor_clusters_items_mismatch"))
        self.assertEqual(self._cluster_rows(scan_id), [])
        [(args, kwargs)] = [(call.args, call.kwargs) for call in log.warning.call_args_list
                            if call.args[0].startswith("competitor_cluster_failed")]
        self.assertIn("ad ids not in the request (1): o999", args[0] % args[1:])
        self.assertEqual((kwargs["extra"]["scan_id"], kwargs["extra"]["trigger"], kwargs["extra"]["error_code"]),
                         (scan_id, "scan", "competitor_clusters_items_mismatch"))
        self.assertIn("o999", kwargs["extra"]["error_detail"])


if __name__ == "__main__":
    unittest.main()
