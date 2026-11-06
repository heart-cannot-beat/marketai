"""PostgreSQL-only genuine-concurrency tests for Scale's idempotency and
single-use claim primitives (issue #86 Task 14).

Every other Scale concurrency test in this repo
(``tests/test_scale_concurrency.py``) reproduces a race *deterministically*,
without real threads, on a single shared SQLite/in-memory connection: SQLite
allows only one writer transaction at a time, so a genuinely separate,
concurrently-held-open connection would just block forever with no thread
free to release it (see that module's own docstring). None of that
technique proves the thing this task is actually about: PostgreSQL's own
row-level blocking behavior when two *real, separate* transactions race the
exact same row or the exact same unique-index tuple --
``app.scale.command_journal``'s module docstring is explicit that this is
PostgreSQL-only, hosted-runtime behavior ("PostgreSQL waits when the
conflicting row is uncommitted ... No SELECT-then-INSERT window exists").
This module is the first to actually open two independent PostgreSQL
connections/transactions, synchronize their timing with
``threading.Barrier``, and assert on the outcome.

Three scenarios (spec §7.7), one per test class below:

* CC1 -- two threads racing ``app.scale.candidates.consume_candidate()``
  against the exact same unconsumed Candidate. PostgreSQL serializes the
  two ``UPDATE ... WHERE consumed_at IS NULL`` statements on the same row;
  exactly one succeeds, the other's ``WHERE`` no longer matches once
  unblocked and raises ``CandidateAlreadyConsumedError``.
* CC10 -- two threads racing ``app.scale.command_journal.claim_command()``
  with the same ``(workspace, mode, command_type, dedup_key)`` but
  different ``request_fingerprint``s. The loser's ``INSERT ... ON CONFLICT
  DO NOTHING`` blocks on the winner's uncommitted row; once unblocked, its
  own re-``SELECT`` finds a mismatched fingerprint and raises
  ``IdempotencyConflictError`` -- never a silent second replay, never a
  bare lifecycle error.
* CC2 -- two threads racing ``app.scale.enrollment.activate_enrollment()``
  against the same draft Enrollment, the same Preview and the exact same
  ``idempotency_key``. This is the *idempotent-replay* case, not a
  conflict: both must succeed and return the same activated Enrollment/
  Episode, because both threads' ``request_fingerprint`` (derived from
  ``activation_preview_id``/``expected_revision``, which are identical)
  agree.

CC1 and CC10 follow the exact fixture pattern
``tests/test_scale_db_integrity_pg.py`` (Task 13) already established for
this repo's PostgreSQL-only tests: connect as the real least-privilege
roles (``marketai_domain_owner``, BYPASSRLS, for setup; each thread opens
its *own* ``Session`` bound directly to that same owner engine -- no
``tenant_session``, no shared connection), raw SQL for fixture rows, gated
on ``DOMAIN_OWNER_DATABASE_URL``/``DOMAIN_API_DATABASE_URL``/
``MARKETAI_TENANT_CONTEXT_SECRET`` so ``make verify`` stays clean on a
SQLite-only checkout. CC10 reuses that module's ``_ScaleDbIntegrityTestCase``
base directly (Workspace creation/cleanup only -- no delivery-target/
lineage-guard chain needed for the command journal alone); CC1 additionally
reuses that module's ``_seed_curated_scenario_context``/``_seed_delivery_target``/
``_seed_lineage_guard``/``_seed_enrollment_candidate`` helpers to build one
real, unconsumed Candidate (Task 13 already solved this exact FK chain).

CC2 cannot follow that same raw-SQL recipe: ``activate_enrollment()``
transitively calls ``app.experiments.winner_eligibility.
validate_winner_eligibility``, which requires a genuinely eligible Winner
Decision (a confirmed Experiment/Judgement/Confirmation, an approved
AssetVersion review) plus a ready ``MarketAssetBindingRecord`` -- and that
record's own composite foreign keys reach through a further half-dozen
Localization tables (``localization_items``, ``localization_batches``,
``localization_output_publications``, ``localization_claim_decision_sets``,
...), each with their own nested FK webs. Hand-fabricating that whole graph
via raw SQL, with no real cluster available in this environment to prove it
against, would be pure guesswork. This repo already has tested, reusable
machinery for exactly this: ``tests/support/scale_candidate_fixtures.py``'s
``build_ready_winner_and_target`` (the same helper
``tests/test_scale_enrollment.py::ActivationFlowTestBase`` uses for its own
non-concurrent ``activate_enrollment`` coverage), plus
``create_enrollment_candidate``/``create_draft_enrollment``/
``create_activation_preview`` themselves. Those all go through
``app.adapters.persistence.database.tenant_session``, which is bound to
this process's single global ``SessionLocal`` -- there is no per-call
session override anywhere in that call chain. ``ConcurrentActivationTests``
therefore repoints that one module attribute at a ``sessionmaker`` bound to
the same ``DOMAIN_OWNER_DATABASE_URL`` role Task 13's tests already connect
with (BYPASSRLS, so none of this fixture-building needs its own tenant
scoping), for the lifetime of the test class only, restoring the original
in ``tearDownClass``. This is not a new assumption this file invents: this
repo's own CI (``.github/workflows/ci.yml``, the domain-role RLS job) already
points ``MARKETAI_DATABASE_URL`` -- this process's *global* engine -- at the
very same ``marketai_domain_owner`` role it exposes to that job as
``DOMAIN_OWNER_DATABASE_URL``; this class does not depend on that happening
to be true here, it makes it true itself, scoped to its own lifetime. The
actual concurrent ``activate_enrollment()`` calls under test still use their
own independent ``Session`` objects bound directly to the owner engine, never
``tenant_session`` -- each thread genuinely holds its own PostgreSQL
connection/transaction, exactly like CC1/CC10 above.
"""
from __future__ import annotations

import threading
import unittest
import uuid
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence import database as db_module
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id
from app.platform.tenancy import TenantContext
from app.scale.activation_preview import create_activation_preview
from app.scale.candidates import (
    CandidateAlreadyConsumedError,
    consume_candidate,
    create_enrollment_candidate,
)
from app.scale.command_journal import IdempotencyConflictError, claim_command
from app.scale.enrollment import activate_enrollment, create_draft_enrollment
from tests.support.scale_candidate_fixtures import (
    build_ready_winner_and_target,
    ensure_governance_prereqs,
    seed_activation_evidence_prereqs,
)
from tests.test_scale_db_integrity_pg import (
    _ScaleDbIntegrityTestCase,
    _seed_curated_scenario_context,
    _seed_delivery_target,
    _seed_enrollment_candidate,
    _seed_lineage_guard,
)
from tests.test_scale_migration import CONFIGURED, OWNER_URL, SKIP_REASON

_BARRIER_TIMEOUT = 5


# ---------------------------------------------------------------------------
# CC1: two threads consuming the same Candidate.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class ConcurrentCandidateConsumptionTests(_ScaleDbIntegrityTestCase):
    """CC1 (spec §7.7): ``consume_candidate()``'s ``UPDATE ... WHERE
    consumed_at IS NULL`` is a CAS with no SELECT-then-UPDATE window (see
    ``app.scale.candidates`` module docstring); this proves that CAS
    actually holds against a second, real, concurrently-committing
    PostgreSQL transaction -- not just against a second call on the same
    connection."""

    def test_two_threads_consume_same_candidate_exactly_one_succeeds(self) -> None:
        ws = self.make_workspace()
        meta_mode = "controlled_mock"
        scenario_scope_key = "scn_cc1"
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws, meta_mode)
            target_id = _seed_delivery_target(
                conn, ws, meta_mode, scenario_scope_key, upstream,
            )
            guard_id = _seed_lineage_guard(
                conn, ws, meta_mode, scenario_scope_key, target_id,
                upstream["meta_connection_id"], upstream["contract_generation_id"],
            )
            candidate_id = _seed_enrollment_candidate(
                conn, ws, meta_mode, scenario_scope_key, target_id, guard_id,
            )

        context = TenantContext(
            workspace_id=ws, role="owner", initiator_id="cc1-test", meta_mode=meta_mode,
        )
        barrier = threading.Barrier(2)
        results: list[object] = [None, None]

        def consume(idx: int) -> None:
            session = Session(
                bind=self.owner_engine, expire_on_commit=False, future=True,
            )
            try:
                barrier.wait(timeout=_BARRIER_TIMEOUT)
                results[idx] = consume_candidate(session, context, candidate_id)
                session.commit()
            except Exception as exc:  # noqa: BLE001 -- captured for assertion below
                results[idx] = exc
                session.rollback()
            finally:
                session.close()

        threads = [threading.Thread(target=consume, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(failures), 1, results)
        self.assertIsInstance(failures[0], CandidateAlreadyConsumedError)
        self.assertIsNotNone(successes[0].consumed_at)


# ---------------------------------------------------------------------------
# CC10: same dedup key, different fingerprint.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class ConcurrentIdempotencyConflictTests(_ScaleDbIntegrityTestCase):
    """CC10 (spec §7.7): two threads racing ``claim_command()`` with the
    *same* ``(workspace, mode, command_type, dedup_key)`` but different
    ``request_fingerprint``s. PostgreSQL's ``INSERT ... ON CONFLICT DO
    NOTHING`` makes the loser block on the winner's uncommitted row (module
    docstring); once the winner commits, the loser's own re-``SELECT`` finds
    a mismatched fingerprint and must raise ``IdempotencyConflictError`` --
    never treat the winner's commit as its own replay."""

    def test_same_dedup_key_different_fingerprint_one_conflict(self) -> None:
        ws = self.make_workspace()
        context = TenantContext(
            workspace_id=ws, role="owner", initiator_id="cc10-test",
            meta_mode="customer_sandbox",
        )
        barrier = threading.Barrier(2)
        results: list[object] = [None, None]

        def claim(idx: int, fingerprint: str) -> None:
            session = Session(bind=self.owner_engine, future=True)
            try:
                barrier.wait(timeout=_BARRIER_TIMEOUT)
                results[idx] = claim_command(
                    session, context,
                    command_type="activate_enrollment",
                    deduplication_key="cc10-same-key",
                    result_id=f"senr_cc10_{idx}",
                    request_fingerprint=fingerprint,
                )
                session.commit()
            except Exception as exc:  # noqa: BLE001
                results[idx] = exc
                session.rollback()
            finally:
                session.close()

        threads = [
            threading.Thread(target=claim, args=(0, "fp_a")),
            threading.Thread(target=claim, args=(1, "fp_b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(failures), 1, results)
        self.assertIsInstance(failures[0], IdempotencyConflictError)
        self.assertFalse(successes[0].replay)


# ---------------------------------------------------------------------------
# CC2: same idempotency key, concurrent activation -- idempotent replay.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class ConcurrentActivationIdempotentReplayTests(unittest.TestCase):
    """CC2 (spec §7.7): two threads racing ``activate_enrollment()`` against
    the same draft Enrollment, the same consumed Preview, and the exact same
    ``idempotency_key``. Both must succeed -- this is a replay, not a
    conflict (both requests' ``request_fingerprint``, derived from
    ``activation_preview_id``/``expected_revision``, agree) -- and both must
    observe the same activated Enrollment/Episode. See the module docstring
    for why this class repoints the global ``SessionLocal`` for its fixture
    setup rather than hand-writing raw SQL for the winner/target chain."""

    mode = "customer_sandbox"

    @classmethod
    def setUpClass(cls) -> None:
        cls.owner_engine = create_engine(OWNER_URL, future=True)
        cls._owner_session_local = sessionmaker(
            bind=cls.owner_engine, expire_on_commit=False, autoflush=False, future=True,
        )
        cls._session_local_patcher = mock.patch.object(
            db_module, "SessionLocal", cls._owner_session_local,
        )
        cls._session_local_patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._session_local_patcher.stop()
        cls.owner_engine.dispose()

    def setUp(self) -> None:
        tag = uuid.uuid4().hex[:10]
        self.workspace_id = f"ws_cc2_{tag}"
        self.ctx = TenantContext(
            workspace_id=self.workspace_id, role="owner", initiator_id="cc2-test",
            meta_mode=self.mode,
        )
        ensure_governance_prereqs(self.ctx, f"prod_{tag}")
        self._chain = build_ready_winner_and_target(
            self.ctx, f"prod_{tag}", tag, target_meta_mode=self.mode,
        )
        seed_activation_evidence_prereqs(
            self.ctx, delivery_target_id=self._chain["delivery_target_id"],
            meta_mode=self.mode, tag=tag,
        )

    def _draft(self) -> tuple[str, int]:
        with tenant_session(self.ctx) as session:
            candidate = create_enrollment_candidate(
                session, self.ctx,
                decision_id=self._chain["decision_id"],
                delivery_target_id=self._chain["delivery_target_id"],
                idempotency_key=new_id("cc2-candidate"),
            )
            candidate_id = candidate.id
        with tenant_session(self.ctx) as session:
            enrollment = create_draft_enrollment(
                session, self.ctx,
                enrollment_candidate_id=candidate_id,
                idempotency_key=new_id("cc2-draft"),
            )
            return enrollment.id, enrollment.revision

    def _create_preview(self, enrollment_id: str, expected_revision: int) -> str:
        with tenant_session(self.ctx) as session:
            preview = create_activation_preview(
                session, self.ctx,
                enrollment_id=enrollment_id,
                expected_revision=expected_revision,
                idempotency_key=new_id("cc2-preview"),
            )
            return preview.id

    def test_concurrent_activate_same_idempotency_key_both_succeed(self) -> None:
        enrollment_id, revision = self._draft()
        preview_id = self._create_preview(enrollment_id, revision)

        barrier = threading.Barrier(2)
        results: list[object] = [None, None]

        def activate(idx: int) -> None:
            session = Session(bind=self.owner_engine, future=True)
            try:
                barrier.wait(timeout=_BARRIER_TIMEOUT)
                enrollment, episode = activate_enrollment(
                    session, self.ctx,
                    enrollment_id=enrollment_id,
                    activation_preview_id=preview_id,
                    expected_revision=revision,
                    idempotency_key="cc2-same-key",
                )
                session.commit()
                results[idx] = (
                    enrollment.id,
                    enrollment.lifecycle,
                    enrollment.revision,
                    episode.id if episode is not None else None,
                )
            except Exception as exc:  # noqa: BLE001
                results[idx] = exc
                session.rollback()
            finally:
                session.close()

        threads = [threading.Thread(target=activate, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        failures = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(failures, [], f"expected both threads to succeed, got: {failures}")

        enrollment_ids = {r[0] for r in results}
        lifecycles = {r[1] for r in results}
        revisions = {r[2] for r in results}
        episode_ids = {r[3] for r in results}
        self.assertEqual(enrollment_ids, {enrollment_id})
        self.assertEqual(lifecycles, {"active"})
        self.assertEqual(revisions, {revision + 1})
        self.assertEqual(len(episode_ids), 1)
        self.assertIsNotNone(next(iter(episode_ids)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
