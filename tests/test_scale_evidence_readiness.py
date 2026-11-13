"""``GET /api/v1/scale/{meta_mode}/enrollments/{id}/evidence`` answers the
Episode's readiness and blocking reasons (#114, #101 S8), on SQLite.

The fixture is #110's (``tests/test_scale_live_evaluation.py``): a
``customer_sandbox`` connection, its Scale contract set and sandbox ads, and
an active Enrollment. Every row the endpoint reads is written by production
code -- only the Worker runs (tick, batch, ``scale_collect_sources``,
``scale_evaluate``) -- and the endpoint is the real FastAPI app with the
verified identity overridden, as in ``tests/test_scale_router.py``.

* nothing collected yet: 200 with ``readiness`` null and every Evaluation
  field null (it was a 404 ``evidence_not_available``);
* a blocked first day: readiness ``insufficient``/``blocked`` with the
  manifest's typed issues and recovery action, still no Evaluation;
* a collected day whose evaluation has not run: readiness ``warming`` with
  its last complete day, still no Evaluation;
* evaluated: the Evaluation and the readiness of the manifest it was sealed
  from, not stale;
* a blocked or a collected day after an evaluated one: the Evaluation stays,
  ``evaluation_stale`` says it is older than the latest manifest;
* a Member reads it (contract §21); another Workspace, another mode, and a
  delegated user asking for ``internal_validation`` read nothing.

Fictional brand only: Acme Charge.
"""
from __future__ import annotations

import gzip
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters.persistence.database import platform_session
from app.meta.models import MetaEvidencePageRecord
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from app.scale import collector
from app.scale.models import ScaleEvidenceReadinessProjectionRecord
from tests.test_scale_live_evaluation import EVALUATE, LiveEvaluationTestCase
from tests.test_scale_source_collection import MOCK, OWNER_ID, SANDBOX, _local_today

#: Every field of the accepted Evaluation in the response, null before one.
EVALUATION_FIELDS = (
    "evaluation_id", "snapshot_id", "policy_version_id", "target_version_id", "algorithm_version",
    "metric_semantic_contract_set_hash", "maturity", "data_quality_state", "health", "health_reason",
    "healthy_evaluations", "streak_counters", "safe_watermark", "last_complete_local_day", "last_sync_at",
    "freshness_reference_at", "complete_local_days_in_episode", "active_delivery_days_in_episode",
    "current_window", "baseline_window", "rolling_window", "quality_issues", "signals",
    "cursor_as_of_local_day", "cursor_source_revision", "cursor_semantic_event_sequence",
    "cursor_lineage_generation", "revision", "updated_at",
)


class EvidenceReadinessTestCase(LiveEvaluationTestCase):
    def setUp(self) -> None:
        super().setUp()
        from app.main import app

        self.app = app
        self.reader = TenantContext(workspace_id=self.workspace_id, role="owner", initiator_id=OWNER_ID)
        app.dependency_overrides[request_tenant_context] = lambda: self.reader
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _evidence(self, enrollment_id: str, meta_mode: str = SANDBOX, *, expect: int = 200) -> dict:
        resp = self.client.get(f"/api/v1/scale/{meta_mode}/enrollments/{enrollment_id}/evidence")
        self.assertEqual(resp.status_code, expect, resp.text)
        return resp.json()

    def _block(self, established, day) -> None:
        """Run ``day`` with its configuration page tampered before the
        collection: the collector blocks the manifest (#109)."""
        self._steps(day, exclude_types=(collector.COLLECT_JOB_TYPE,))
        run = self._runs(established.generation_ids["delivery_configuration"], day)["configuration"]
        with platform_session() as session:  # the stored page no longer matches its checksum
            page = session.scalar(select(MetaEvidencePageRecord).where(MetaEvidencePageRecord.sync_run_id == run.id))
            page.payload_compressed = gzip.compress(b'{"data":[]}', mtime=0)
        self._steps(day)

    def _assert_no_evaluation(self, body: dict) -> None:
        self.assertEqual({field: body[field] for field in EVALUATION_FIELDS},
                         dict.fromkeys(EVALUATION_FIELDS), "nothing evaluated, nothing invented")
        self.assertFalse(body["evaluation_stale"])

    def _assert_readiness_is_the_projection(self, body: dict, episode_id: str) -> None:
        [projection] = self._rows(ScaleEvidenceReadinessProjectionRecord, episode_id=episode_id)
        readiness = body["readiness"]
        self.assertEqual(
            (readiness["manifest_id"], readiness["maturity"], readiness["quality_state"],
             readiness["blocking_issue_count"], readiness["warning_issue_count"], readiness["recovery_action"],
             readiness["revision"]),
            (projection.latest_manifest_id, projection.maturity, projection.quality_state,
             projection.blocking_issue_count, projection.warning_issue_count, projection.recovery_action,
             projection.revision),
        )
        manifest = next(m for m in self._manifests(episode_id) if m.id == projection.latest_manifest_id)
        self.assertEqual(
            (readiness["manifest_state"], readiness["source_revision"], readiness["target_local_day"]),
            (manifest.state, manifest.source_revision, manifest.target_local_day.isoformat()),
        )
        self.assertEqual(
            sorted((i["code"], i["is_blocking"], i["contract_type"], i["window_role"], i["recovery_action"])
                   for i in readiness["issues"]),
            sorted((code, blocking, contract, role, action)
                   for code, blocking, contract, role, action in self._issues(manifest.id)),
        )


class BeforeTheFirstEvaluation(EvidenceReadinessTestCase):
    def test_nothing_collected_yet_is_a_state_not_a_missing_resource(self):
        day = _local_today() - timedelta(days=3)
        _connection, _set, _ads, enrollment_id, episode_id = self._sandbox(started=day)

        body = self._evidence(enrollment_id)

        self.assertEqual((body["enrollment_id"], body["episode_id"], body["readiness"]),
                         (enrollment_id, episode_id, None))
        self._assert_no_evaluation(body)

    def test_a_blocked_first_day_shows_its_reasons_and_recovery_action(self):
        day = _local_today() - timedelta(days=3)
        _connection, established, _ads, enrollment_id, episode_id = self._sandbox(started=day)
        self._block(established, day)

        body = self._evidence(enrollment_id)

        readiness = body["readiness"]
        self.assertEqual(
            (readiness["manifest_state"], readiness["target_local_day"], readiness["maturity"],
             readiness["quality_state"], readiness["last_complete_local_day"],
             readiness["freshness_reference_at"]),
            ("blocked", day.isoformat(), "insufficient", "blocked", None, None),
        )
        self.assertGreaterEqual(readiness["blocking_issue_count"], 1)
        blocking = [issue for issue in readiness["issues"] if issue["is_blocking"]]
        self.assertTrue(blocking)
        self.assertEqual(readiness["issues"][: len(blocking)], blocking, "blocking first")
        self.assertIn(readiness["recovery_action"], {issue["recovery_action"] for issue in blocking})
        self.assertEqual({issue["code"] for issue in blocking} - set(collector.RECOVERY_ACTIONS), set(),
                         "typed codes, each with its recovery action")
        self._assert_readiness_is_the_projection(body, episode_id)
        self._assert_no_evaluation(body)

    def test_a_collected_day_waits_for_its_evaluation(self):
        day = _local_today() - timedelta(days=3)
        _connection, _set, _ads, enrollment_id, episode_id = self._sandbox(started=day)
        self._steps(day, exclude_types=(EVALUATE,))  # the evaluation Job stays queued

        body = self._evidence(enrollment_id)

        readiness = body["readiness"]
        self.assertEqual(
            (readiness["manifest_state"], readiness["maturity"], readiness["quality_state"],
             readiness["last_complete_local_day"], readiness["recovery_action"], readiness["issues"]),
            ("complete", "warming", "pass", day.isoformat(), None, []),
        )
        self.assertIsNotNone(readiness["freshness_reference_at"])
        self._assert_readiness_is_the_projection(body, episode_id)
        self._assert_no_evaluation(body)


class AfterAnEvaluation(EvidenceReadinessTestCase):
    def test_the_evaluation_and_the_readiness_of_its_manifest(self):
        day = _local_today() - timedelta(days=3)
        _connection, _set, _ads, enrollment_id, episode_id = self._sandbox(started=day)
        self._steps(day)
        run = self._run_for(episode_id, day)

        body = self._evidence(enrollment_id)

        self.assertEqual((body["evaluation_id"], body["cursor_as_of_local_day"], body["evaluation_stale"]),
                         (run.evaluation_id, day.isoformat(), False))
        self.assertEqual((body["readiness"]["manifest_id"], body["readiness"]["quality_state"],
                          body["readiness"]["maturity"]),
                         (run.source_manifest_id, "pass", body["maturity"]))
        self._assert_readiness_is_the_projection(body, episode_id)

    def test_a_newer_blocked_day_leaves_the_evaluation_stale(self):
        first = _local_today() - timedelta(days=4)
        second = first + timedelta(days=1)
        _connection, established, _ads, enrollment_id, episode_id = self._sandbox(started=first)
        self._steps(first)
        evaluated = self._run_for(episode_id, first)
        self._block(established, second)
        self.assertEqual(self._manifest_for(episode_id, second).state, "blocked")

        body = self._evidence(enrollment_id)

        # The older Evaluation stays readable, and says it is stale.
        self.assertEqual((body["evaluation_id"], body["cursor_as_of_local_day"], body["evaluation_stale"]),
                         (evaluated.evaluation_id, first.isoformat(), True))
        readiness = body["readiness"]
        self.assertEqual(
            (readiness["manifest_state"], readiness["target_local_day"], readiness["maturity"],
             readiness["quality_state"], readiness["last_complete_local_day"]),
            ("blocked", second.isoformat(), "insufficient", "blocked", first.isoformat()),
        )
        self.assertGreater(readiness["source_revision"], body["cursor_source_revision"])
        self.assertTrue(readiness["issues"] and readiness["recovery_action"])
        self._assert_readiness_is_the_projection(body, episode_id)

    def test_a_newer_collected_day_leaves_the_evaluation_stale_until_it_is_evaluated(self):
        first = _local_today() - timedelta(days=4)
        second = first + timedelta(days=1)
        _connection, _set, _ads, enrollment_id, episode_id = self._sandbox(started=first)
        self._steps(first)
        self._steps(second, exclude_types=(EVALUATE,))

        pending = self._evidence(enrollment_id)

        self.assertEqual((pending["cursor_as_of_local_day"], pending["evaluation_stale"],
                          pending["readiness"]["quality_state"], pending["readiness"]["last_complete_local_day"]),
                         (first.isoformat(), True, "pass", second.isoformat()))

        self._steps(second)
        current = self._evidence(enrollment_id)

        self.assertEqual((current["cursor_as_of_local_day"], current["evaluation_stale"]),
                         (second.isoformat(), False))


class WhoReadsIt(EvidenceReadinessTestCase):
    def _blocked_enrollment(self) -> str:
        day = _local_today() - timedelta(days=3)
        _connection, established, _ads, enrollment_id, _episode_id = self._sandbox(started=day)
        self._block(established, day)
        return enrollment_id

    def test_a_member_reads_the_same_evidence(self):
        enrollment_id = self._blocked_enrollment()
        owner = self._evidence(enrollment_id)

        self.reader = TenantContext(workspace_id=self.workspace_id, role="member", initiator_id="usr_acme_member")

        self.assertEqual(self._evidence(enrollment_id), owner)

    def test_another_workspace_reads_nothing(self):
        enrollment_id = self._blocked_enrollment()
        other = self._provision("ws_scale_readiness_b")
        self.reader = TenantContext(workspace_id=other, role="owner", initiator_id="usr_northpeak_owner")

        body = self._evidence(enrollment_id, expect=404)

        self.assertEqual(body["code"], "enrollment_not_found")
        self.assertNotIn("readiness", body)

    def test_another_mode_reads_nothing(self):
        enrollment_id = self._blocked_enrollment()

        body = self._evidence(enrollment_id, MOCK, expect=404)
        self.assertEqual(body["code"], "enrollment_not_found")
        self.assertNotIn("readiness", body)
        # A delegated user cannot select internal_validation at all (ADR 0009).
        self.assertEqual(self._evidence(enrollment_id, "internal_validation", expect=403)["code"],
                         "meta_mode_not_permitted")
