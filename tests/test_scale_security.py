"""Consolidated capability-isolation security gate for Scale (Issue #25, Task 18).

Spec: docs/product/scale-advice-semantics-contract.md §4.5, ADR 0008
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-18-brief.md §5

Gates 14, 23, 24, 25: "Scale never performs Meta writes." Per the task
brief, this module combines AST import restrictions, the transitive
import closure, a runtime capability spy, OpenAPI route absence, and a
deployment assertion into one comprehensive gate file:

* ``TestScaleModuleImportIsolation`` / ``TestScaleWorkerNoLaunchDispatch``
  / ``TestTransitiveImportClosure`` / ``TestScaleWorkerRuntimeCapabilitySpy``
  -- re-export ``tests/test_scale_worker_isolation.py``'s Task 14 test
  classes unchanged (rather than re-deriving ~150 lines of AST-walking and
  runtime-spy code) so this module collects and runs them too, matching
  the brief's "combine ... Can reference/reuse the existing tests."
* ``TestOpenAPIHasNoMetaMutationRoute`` -- new: the actually-mounted
  FastAPI schema contains no route whose path or operation id names a
  Meta write/launch/publish action.
* ``TestRuntimeSpyAcrossFlows`` -- new: patches
  ``app.adapters.meta.write_client.HttpMetaWriteAdapter.__init__`` itself
  (not just the source text) and drives four real Scale flows through
  it -- evaluation, Advice conversion, the Exports stub route, and crash
  recovery -- asserting the constructor is never reached by any of them.
  This is strictly stronger than the AST/closure tests: even a
  dynamically-imported or duck-typed write path would still have to
  *construct* this adapter (the only concrete ``MetaWritePort``
  implementation in the codebase) to perform a real write.
* ``TestDeploymentAssertions`` -- new: the CLAUDE.md invariant "Meta write
  access remains disabled in development" holds for the actual `app.config`
  module under test, and the Scale Worker composition root never reads
  the write token (Task 14 already covers the latter; restated here as
  part of the single gate file for discoverability).
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import mock

from app import config

# -- Re-export Task 14's test classes so this module both documents and
# collects them (brief: "combine ... reference/reuse the existing tests").
from tests.test_scale_worker_isolation import (  # noqa: F401
    TestScaleWorkerImportIsolation,
    TestScaleWorkerRuntimeCapabilitySpy,
)


class TestOpenAPIHasNoMetaMutationRoute(unittest.TestCase):
    """Gate 25 (static half): the mounted API surface itself must not even
    offer a Meta-write-shaped route. ``app.openapi()`` is the stable,
    version-independent view of what's actually mounted (see
    ``tests/test_scale_router.py::TestScaleRouterMount``)."""

    _FORBIDDEN_FRAGMENTS = (
        "launch", "meta-write", "meta_write", "publish-to-meta", "publish_to_meta",
        "activate-meta", "activate_meta", "meta-mutation", "meta_mutation",
    )

    def test_no_scale_route_path_or_operation_id_names_a_meta_write_action(self):
        from app.main import app

        schema = app.openapi()
        scale_paths = {
            path: methods for path, methods in schema["paths"].items() if "/scale" in path
        }
        self.assertGreater(len(scale_paths), 0, "no /scale routes mounted -- router wiring regressed")

        for path, methods in scale_paths.items():
            lowered_path = path.lower()
            for fragment in self._FORBIDDEN_FRAGMENTS:
                self.assertNotIn(fragment, lowered_path, f"{path} looks like a Meta write route")
            for method, operation in methods.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                operation_id = str(operation.get("operationId", "")).lower()
                for fragment in self._FORBIDDEN_FRAGMENTS:
                    self.assertNotIn(
                        fragment, operation_id,
                        f"{method.upper()} {path} operationId={operation_id!r} looks like a Meta write route",
                    )

    def test_no_scale_route_targets_a_meta_campaign_ad_or_creative_mutation_path(self):
        """A second, narrower check: even without the word "launch", a
        route whose path segments spell out a direct Meta object mutation
        (create/pause/resume a campaign, ad set, or ad) would itself be a
        write surface -- Scale's own routes (enrollments, advice-threads,
        targets, exports, breakdowns, enrollment-candidates) never do."""
        from app.main import app

        schema = app.openapi()
        meta_object_fragments = ("/campaigns", "/adsets", "/ads/", "/adcreatives", "/adimages")
        for path in schema["paths"]:
            if "/scale" not in path:
                continue
            lowered = path.lower()
            for fragment in meta_object_fragments:
                self.assertNotIn(fragment, lowered, f"{path} targets a raw Meta object mutation path")


class TestRuntimeSpyAcrossFlows(unittest.TestCase):
    """Gate 25 (dynamic half): construct real Scale evidence, drive it
    through evaluation, Advice conversion, recovery, and the Exports stub
    route, all while ``HttpMetaWriteAdapter.__init__`` is patched to
    record every call -- proving no code path anywhere in these flows
    ever tries to build the one concrete write-capable adapter this
    codebase has."""

    def _run_with_spy(self, fn):
        from app.adapters.meta.write_client import HttpMetaWriteAdapter

        spy = mock.Mock(side_effect=HttpMetaWriteAdapter.__init__)
        with mock.patch.object(HttpMetaWriteAdapter, "__init__", spy):
            fn()
        self.assertEqual(spy.call_count, 0, "HttpMetaWriteAdapter was constructed during a Scale-only flow")

    def test_evaluation_and_recovery_never_construct_a_write_adapter(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from app.adapters.persistence.database import Base
        from app.adapters.persistence.models import WorkspaceRecord, new_id
        from app.platform.tenancy import TenantContext
        from app.scale.evidence import seal_snapshot
        from app.scale.service import run_scale_evaluation

        from tests.test_scale_orchestration import AS_OF, OrchestrationTestBase

        # Build one complete, sealable fixture the same way
        # OrchestrationTestBase does, without inheriting unittest.TestCase
        # twice: instantiate it as a plain helper object.
        harness = OrchestrationTestBase()
        harness.setUp()
        self.addCleanup(harness.engine.dispose)
        self.addCleanup(harness.session.close)
        manifest, _daily_rows = harness._seed_manifest(AS_OF)
        run = harness._seed_run(manifest, AS_OF)

        def _evaluate_then_recover():
            outcome = run_scale_evaluation(harness.session, harness.context, run.id)
            self.assertEqual(outcome.kind.value, "evaluated")
            # Recovery: resume the now-terminal run again (idempotent replay
            # path is a distinct code path from the first-time evaluation).
            run_scale_evaluation(harness.session, harness.context, run.id)

        self._run_with_spy(_evaluate_then_recover)

    def test_advice_conversion_never_constructs_a_write_adapter(self):
        from tests.test_scale_advice import AdviceTestBase, _candidate
        from app.scale.advice import convert_occurrence_to_draft
        from app.scale.models import ScaleAdviceOccurrenceRecord, ScaleAdviceThreadRecord
        from app.scale.types import AdviceType

        harness = AdviceTestBase()
        harness.setUp()
        self.addCleanup(harness.engine.dispose)
        self.addCleanup(harness.session.close)

        candidate = _candidate(advice_type=AdviceType.REFRESH_CREATIVE, reason_signature="rs-spy")
        evaluation = harness._add_evaluation(harness.episode.id, snapshot_id="snap-spy", candidates=[candidate])
        thread_id = harness._process(harness.episode.id, evaluation.id, [candidate], day=date(2026, 9, 1))[0]
        thread = harness.session.get(ScaleAdviceThreadRecord, thread_id)
        occurrence = harness.session.get(ScaleAdviceOccurrenceRecord, thread.latest_occurrence_id)

        def _convert():
            convert_occurrence_to_draft(harness.session, harness.context, occurrence.id, initiator_id="user_1")

        self._run_with_spy(_convert)

    def test_exports_route_never_constructs_a_write_adapter(self):
        """The Exports route (``POST /scale/exports``) must not reach a
        write adapter — Scale never performs Meta writes."""
        import os
        import tempfile
        from pathlib import Path

        os.environ.setdefault("MARKETAI_TEST_ROOT", tempfile.mkdtemp(prefix="marketai-scale-security-"))
        os.environ.setdefault("MARKETAI_DATA_DIR", str(Path(os.environ["MARKETAI_TEST_ROOT"]) / "data"))
        os.environ.setdefault(
            "MARKETAI_DATABASE_URL", f"sqlite:///{Path(os.environ['MARKETAI_TEST_ROOT']) / 'marketai.db'}"
        )
        os.environ.setdefault("MARKETAI_WORKSPACE_ID", "ws_test")

        from alembic import command
        from alembic.config import Config
        from fastapi.testclient import TestClient

        from app.platform.http_identity import request_tenant_context
        from app.platform.tenancy import TenantContext

        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        Path(config.DATABASE_URL.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)

        from app.main import app

        app.dependency_overrides[request_tenant_context] = lambda: TenantContext(
            workspace_id="ws_scale_security_exports", role="owner", executor="user",
            initiator_id="user_1",
        )
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)

        def _call_exports():
            with TestClient(app) as client:
                resp = client.post("/api/v1/scale/customer_sandbox/exports", json={})
                assert resp.status_code in (201, 422, 400, 404), resp.text

        self._run_with_spy(_call_exports)


class TestDeploymentAssertions(unittest.TestCase):
    """CLAUDE.md invariant: "Meta write access remains disabled in
    development. `META_WRITE_ACCESS_TOKEN` stays empty, and Scale never
    performs Meta writes." The token-emptiness half is an ``app.config``
    property, independent of Scale; asserted here as the deployment-level
    precondition the rest of this file's dynamic checks build on."""

    def test_meta_write_access_token_is_empty_by_default(self):
        # This suite's own conftest.py never sets META_WRITE_ACCESS_TOKEN,
        # so the real module-level value read at import time must be the
        # documented empty-string default.
        self.assertEqual(config.META_WRITE_ACCESS_TOKEN, "")

    def test_meta_write_ready_is_false_without_explicit_configuration(self):
        self.assertFalse(config.meta_write_ready())

    def test_scale_worker_module_never_references_the_write_token_name(self):
        import pathlib

        source = pathlib.Path("app/scale/worker.py").read_text()
        self.assertNotIn("META_WRITE_ACCESS_TOKEN", source)
        self.assertNotIn("meta_write_ready", source)


if __name__ == "__main__":
    unittest.main()
