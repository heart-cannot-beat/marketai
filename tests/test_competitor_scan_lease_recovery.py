"""Issue #327 -- a Worker killed past a call's dispatch fence, recovered by the lease reclaim.

Spec §5.1 and §13 scenario 5. The Worker dies while a page call is
``dispatching`` and stops renewing its lease. Nothing else is forged: only the
lease's expiry moves into the past, and the queue's own recovery decides what
becomes of the Job (``jobs.recover_expired_jobs`` here; the PostgreSQL
dispatcher in ``tests.test_competitor_scans_pg``). Scan and resolve Jobs are
safe to redeliver -- each paid call sits behind its own dispatch fence, so the
redelivered run finds the call ``dispatching``, turns it ``uncertain`` and
never resends it -- and the scan or resolve then ends like an in-process loss.
The reconciliation after it (difference 0, 1, anything else for the Owner) is
``tests.test_competitor_scans_api.UncertainScanTests``, through this same
recovery.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import select, update

from tests.support.queue_hygiene import quarantine_foreign_jobs
from tests.test_competitor_scans_api import HALCYON, Killed, ScanFixtures, expire_lease
from tests.test_competitors_api import BRIGHTVOLT, ACME, CompetitorFixtures
from tests.test_test_plans_api import PROTECTOR

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, utcnow
from app.competitors.sources.base import PriceOperation
from app.market_data.calls import market_data_call_key
from app.worker_runtime import WorkerRuntime


def kill_after_fence(page_id: str):
    """Kill the Worker on ``page_id``'s second page, its attempt already committed ``dispatching``."""

    def wrap(source):
        original = source._send

        async def send(request, *, credential):
            if request.page_id == page_id and request.cursor is not None:
                raise Killed("worker killed after the fence")
            return await original(request, credential=credential)

        source._send = send
        return source

    return wrap


def _job(ctx, job_id: str) -> JobRecord:
    with tenant_session(ctx) as session:
        job = session.get(JobRecord, job_id)
        session.expunge(job)
        return job


def _job_events(ctx, job_id: str) -> list[tuple[str, dict]]:
    with tenant_session(ctx) as session:
        return [(row.event_type, dict(row.details_json or {})) for row in session.scalars(
            select(AuditEventRecord).where(
                AuditEventRecord.resource_type == "job", AuditEventRecord.resource_id == job_id,
            ).order_by(AuditEventRecord.created_at, AuditEventRecord.id))]


class ScanLeaseRecoveryTests(ScanFixtures):
    def setUp(self) -> None:
        super().setUp()
        self._bind_both()

    def _killed(self, competitor_ids: list[str]) -> dict:
        """Admit a scan and kill its Worker with Acme Charge's page 2 past the fence."""
        self.wrap = kill_after_fence(ACME)
        accepted = self._start(self._body(competitor_ids, keywords=[]))
        runtime = self._runtime()
        with self.assertRaises(Killed):
            for _ in range(5):
                asyncio.run(runtime.run_once())
        self.wrap = None
        return accepted

    def _resent(self, since: int) -> list:
        """Acme Charge page requests with a cursor (page 2+) any source sent after ``since`` sources."""
        return [request for source in self.sources[since:] for request in source.requests
                if request.page_id == ACME and request.cursor is not None]

    def test_the_reclaimed_job_is_redelivered_and_the_lost_page_waits_for_reconciliation(self):
        acme = self._confirmed(ACME)
        accepted = self._killed([acme])
        job_id, call_name = accepted["job_id"], f"page:{acme}:2"
        lost_key = market_data_call_key(job_id, call_name)
        [lost] = [attempt for attempt in self._attempts(job_id) if attempt.call_semantic_key_hash == lost_key]
        self.assertEqual(lost.dispatch_state, "dispatching")
        self.assertEqual(self._scan(accepted["scan_id"])["status"], "running")
        self._drain()  # the lease still runs: nothing is recovered yet
        self.assertEqual(_job(self.ctx, job_id).state, "running")

        expire_lease(self.ctx, job_id)
        since = len(self.sources)
        self._drain()

        # The queue redelivered the Job instead of leaving it uncertain behind the scan's back.
        events = _job_events(self.ctx, job_id)
        requeued = [details for event, details in events if event == "job.requeued"]
        self.assertEqual(requeued, [{"error_code": "lease_expired"}])
        self.assertNotIn({"error_code": "paid_call_outcome_unknown"}, [details for _, details in events])
        job = _job(self.ctx, job_id)
        self.assertEqual((job.state, job.error_code), ("uncertain", "market_data_outcome_unknown"))

        # The scan ends as an in-process loss does: uncertain, the page pending, nothing resent.
        scan = self._scan(accepted["scan_id"])
        self.assertEqual((scan["status"], scan["failure"]["code"]), ("uncertain", "market_data_outcome_unknown"))
        self.assertIsNotNone(scan["stage"])
        [entry] = scan["reconciliation"]
        self.assertEqual((entry["status"], entry["page"], entry["attempt_id"]), ("pending", 2, lost.id))
        record = self._record(accepted["scan_id"])
        [pending] = record.evidence_json["reconciliation"]
        self.assertEqual((pending["call_name"], pending["credential_version_id"]),
                         (call_name, self.credential_version_id))
        first, second = record.evidence_json["targets"][0]["pages"]
        self.assertEqual((first["status"], second["status"], second["error_code"], second["tries"]),
                         ("succeeded", "uncertain", "dispatch_interrupted", []))
        [lost] = [attempt for attempt in self._attempts(job_id) if attempt.call_semantic_key_hash == lost_key]
        self.assertEqual(lost.dispatch_state, "uncertain")
        self.assertEqual(self._resent(since), [])
        self.assertEqual(scan["observation_count"], 30)

        # Only the lost page's reservation stays open, for its reconciliation.
        reservations = self._scan_reservations(job_id)
        self.assertEqual([key for key, row in reservations.items() if row.state == "reserved"], [lost_key])
        self.assertIn(lost_key, reservations)

        # The page is resumable and reconcilable again.
        quote = self._resume_quote(accepted["scan_id"])
        self.assertEqual(quote["kind"], "resume")

    def test_the_dead_lease_keeps_the_key_and_the_charges_after_it_are_counted(self):
        acme = self._confirmed(ACME)
        first = self._killed([acme])
        second = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        # While the dead Worker's lease lasts, its scan still holds the key's turn.
        waiting = self._record(second["scan_id"])
        self.assertEqual((waiting.status, waiting.evidence_json["waits"][0]["holder_job_id"]),
                         ("queued", first["job_id"]))

        expire_lease(self.ctx, first["job_id"])
        with tenant_session(self.ctx) as session:
            session.execute(update(JobRecord).where(JobRecord.id == second["job_id"]).values(
                next_attempt_at=utcnow() - timedelta(seconds=1)))
        self._drain()
        lost = self._scan(first["scan_id"])
        other = self._scan(second["scan_id"])
        self.assertEqual((lost["status"], other["status"]), ("uncertain", "completed"))
        # As after an in-process loss, the uncertain scan no longer holds the key: the other
        # scan's charges land before the resume, and the reconciliation subtracts them.
        charged = other["cost"]["market_data"]["credits_charged"]
        self.assertGreater(charged, 0)
        self.balance = 99 - charged
        self._resume(first["scan_id"])
        self._drain()
        done = self._scan(first["scan_id"])
        [entry] = done["reconciliation"]
        self.assertEqual((done["status"], entry["status"], entry["balance_check"]["known_charges"],
                          entry["balance_check"]["difference"]), ("completed", "not_charged", charged, 0))

    def test_the_recovered_scan_is_hidden_from_others_and_only_the_owner_marks_it(self):
        acme = self._confirmed(ACME)
        accepted = self._killed([acme, self._confirmed(HALCYON)])
        expire_lease(self.ctx, accepted["job_id"])
        self._drain()
        scan_id = accepted["scan_id"]
        scan = self._scan(scan_id)
        self.assertEqual(scan["status"], "uncertain")
        attempt_id = scan["reconciliation"][0]["attempt_id"]

        stranger = self._make_context(role="member", actor="member_b")
        self.current = stranger
        for response in (
            self.client.get(f"/api/v1/competitor-scans/{scan_id}"),
            self.client.get(f"/api/v1/competitor-scans/{scan_id}/resume-quote"),
            self.client.post(f"/api/v1/competitor-scans/{scan_id}/resume", headers=self._headers()),
        ):
            self._problem(response, 404, "competitor_scan_not_found")
        self.current = self.owner_ctx(stranger)
        self._problem(self._reconcile(scan_id, attempt_id, "charged"), 404, "competitor_scan_not_found")

        self.current = self.ctx
        self._problem(self._reconcile(scan_id, attempt_id, "charged"), 403, "competitor_scan_reconcile_forbidden")
        self.current = self.owner_ctx(self.ctx)
        marked = self._reconcile(scan_id, attempt_id, "charged")
        self.assertEqual((marked.status_code, marked.json()["status"]), (200, "partial"), marked.text)
        self.current = self.ctx
        self._drain()
        lost_key = market_data_call_key(accepted["job_id"], f"page:{acme}:2")
        reservations = self._scan_reservations(accepted["job_id"])
        self.assertEqual(reservations[lost_key].state, "settled")
        self.assertNotIn("reserved", {row.state for row in reservations.values()})
        self.assertEqual(self._scan(scan_id)["cost"]["market_data"]["credits_charged"], 2)


class ResolveLeaseRecoveryTests(CompetitorFixtures):
    """``competitor.resolve`` had the same gap at the same place (its admission)."""

    def setUp(self) -> None:
        super().setUp()
        self._bind_market_data()

    def test_a_resolve_killed_past_the_fence_ends_uncertain_and_frees_the_key(self):
        competitor = self._competitor("Acme Charge", website="acme-charge.example")
        job_id = self._resolve(competitor["competitor_id"])["resolve_job_id"]

        def dying(mode):
            source = self._source(mode)
            original = source._send

            async def send(request, *, credential):
                if request.operation == PriceOperation.PROBE:
                    raise Killed("worker killed after the fence")
                return await original(request, credential=credential)

            source._send = send
            return source

        quarantine_foreign_jobs(self.ctx.workspace_id)
        runtime = WorkerRuntime(owner=f"competitors-{uuid.uuid4().hex[:6]}")
        runtime.ad_library_source = dying
        runtime._market_data_protector = PROTECTOR
        with self.assertRaises(Killed):
            for _ in range(5):
                asyncio.run(runtime.run_once())
        expire_lease(self.ctx, job_id)
        since = len(self.sources)
        self._drain()

        self.assertIn(("job.requeued", {"error_code": "lease_expired"}), _job_events(self.ctx, job_id))
        job = _job(self.ctx, job_id)
        self.assertEqual((job.state, job.error_code), ("uncertain", "market_data_outcome_unknown"))
        resolution = self._resolution(competitor["competitor_id"], job_id)
        self.assertEqual((resolution["status"], resolution["failure"]["code"]),
                         ("uncertain", "market_data_outcome_unknown"))
        probe_key = market_data_call_key(job_id, "probe:1")
        [probe] = [attempt for attempt in self._attempts(job_id) if attempt.call_semantic_key_hash == probe_key]
        self.assertEqual(probe.dispatch_state, "uncertain")
        self.assertEqual([request for source in self.sources[since:] for request in source.requests
                          if request.operation == PriceOperation.PROBE], [])  # never resent
        reservations = self._reservations(job_id)
        self.assertEqual(reservations[probe_key], "reserved")  # kept, as for an in-process loss
        self.assertEqual(sorted(reservations.values()), ["released", "released", "reserved", "settled"])

        # The uncertain resolve no longer holds the key: the next one runs without waiting.
        other = self._competitor("Brightvolt")
        next_job = self._resolve(other["competitor_id"])["resolve_job_id"]
        self._drain()
        done = self._resolution(other["competitor_id"], next_job)
        self.assertEqual((done["status"], done["waits"]), ("completed", 0))
