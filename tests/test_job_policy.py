"""The crash-policy registry every Job type is enqueued through (#22).

A Job whose Worker dies after ``mark_running`` is requeued or left
``uncertain`` by one column, ``safe_to_retry_after_crash``. These tests pin
that the column comes from ``app.platform.job_policy``: every type a Worker
dispatches is listed there, an enqueue derives the column from it, a value
that contradicts it is refused, and an unlisted type is refused before
anything is written.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from pathlib import Path

from sqlalchemy import func, select

from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord
from app.platform import jobs
from app.platform.job_policy import (
    JOB_POLICIES,
    CrashPolicy,
    JobPolicyMismatchError,
    SideEffect,
    UnregisteredJobTypeError,
    policy_for,
    resolve_safe_to_retry_after_crash,
)

APP = Path(__file__).resolve().parents[1] / "app"

#: The six Job types #22 moved from "uncertain" to "requeue".
NO_SIDE_EFFECT_REQUEUED = (
    "library.reference_gate",
    "generation.export_package",
    "scale_export",
    "localization.run_deterministic_qc",
    "localization.finalize_item",
    "localization.fold_batch",
)
#: Paid or external-write types whose crash must stay ambiguous.
STILL_UNCERTAIN = ("generate_asset", "generation.run_item", "launch_provisioning")


def _dispatched_types(runtime_cls) -> set[str]:
    """Every ``job.type`` a runtime's ``run_once`` compares against.

    Read from the source rather than listed by hand, so a new branch in
    either Worker cannot be added without the registry hearing about it.
    """
    module = inspect.getmodule(runtime_cls)
    module_tree = ast.parse(inspect.getsource(module))
    constants: dict[str, set[str]] = {}
    for node in module_tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                strings = {
                    sub.value for sub in ast.walk(node.value)
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                }
                if strings:
                    constants[target.id] = strings
    run_once = textwrap.dedent(inspect.getsource(runtime_cls.run_once))
    found: set[str] = set()
    for node in ast.walk(ast.parse(run_once)):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (
            isinstance(left, ast.Attribute) and left.attr == "type"
            and isinstance(left.value, ast.Name) and left.value.id == "job"
        ):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                found.add(comparator.value)
            elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                found.update(
                    element.value for element in comparator.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                )
            elif isinstance(comparator, ast.Name):
                found.update(constants.get(comparator.id, set()))
    return found


def _literal_enqueued_types() -> set[str]:
    """Every literal ``job_type=`` an app module passes to a Job constructor."""
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {
            node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name not in ("enqueue_in_session", "JobSpec", "_JobPlan"):
                continue
            for keyword in node.keywords:
                if keyword.arg != "job_type":
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(value.value)
                elif isinstance(value, ast.Name) and value.id in constants:
                    found.add(constants[value.id])
    return found


class TestRegistryCoversEveryJobType(unittest.TestCase):
    def test_every_type_the_worker_dispatches_is_registered(self):
        from app.worker_runtime import WorkerRuntime

        dispatched = _dispatched_types(WorkerRuntime)
        self.assertIn("library.reference_gate", dispatched, "the source scan found the branches")
        self.assertIn("localization.fold_batch", dispatched, "named type sets are expanded")
        self.assertEqual(sorted(dispatched - set(JOB_POLICIES)), [])

    def test_every_type_the_scale_worker_dispatches_is_registered(self):
        from app.scale.worker import ScaleWorkerRuntime

        dispatched = _dispatched_types(ScaleWorkerRuntime)
        self.assertEqual(
            dispatched, {
                "scale_evaluate", "scale_advice_expire", "scale_collect_sources", "scale_target_change",
                "scale_export",
            },
        )
        self.assertEqual(sorted(dispatched - set(JOB_POLICIES)), [])

    def test_every_literal_enqueue_point_names_a_registered_type(self):
        enqueued = _literal_enqueued_types()
        self.assertIn("generation.export_package", enqueued, "the source scan found the call sites")
        self.assertEqual(sorted(enqueued - set(JOB_POLICIES)), [])

    def test_the_deployed_worker_dispatches_every_type_the_app_enqueues(self):
        # Production and docker compose run only ``WorkerRuntime``
        # (``python -m worker.main``). A type it cannot dispatch is failed as
        # ``unknown_job_type`` and its Operation never leaves ``queued`` (#55).
        from app.worker_runtime import WorkerRuntime

        enqueued = _literal_enqueued_types()
        self.assertIn("scale_export", enqueued, "the source scan found the Scale export enqueue")
        # #97: the evaluation and Advice-expiry enqueues, which the deployed
        # Worker did not forward (``scale_advice_expire``) before.
        self.assertLessEqual({"scale_evaluate", "scale_advice_expire"}, enqueued)
        self.assertEqual(sorted(enqueued - _dispatched_types(WorkerRuntime)), [])


class TestRegisteredPolicies(unittest.TestCase):
    def test_the_six_local_job_types_are_requeued(self):
        for job_type in NO_SIDE_EFFECT_REQUEUED:
            with self.subTest(job_type=job_type):
                policy = policy_for(job_type)
                self.assertIs(policy.side_effect, SideEffect.NONE)
                self.assertIs(policy.crash, CrashPolicy.REQUEUE)
                self.assertTrue(resolve_safe_to_retry_after_crash(job_type, None))

    def test_paid_and_external_write_types_stay_uncertain(self):
        for job_type in STILL_UNCERTAIN:
            with self.subTest(job_type=job_type):
                policy = policy_for(job_type)
                self.assertIsNot(policy.side_effect, SideEffect.NONE)
                self.assertIs(policy.crash, CrashPolicy.UNCERTAIN)
                self.assertFalse(resolve_safe_to_retry_after_crash(job_type, None))

    def test_no_job_without_a_side_effect_is_left_uncertain(self):
        # The defect #22 fixes: a crash reported as a paid call that never was.
        self.assertEqual(
            sorted(
                job_type for job_type, policy in JOB_POLICIES.items()
                if policy.side_effect is SideEffect.NONE and policy.crash is CrashPolicy.UNCERTAIN
            ),
            [],
        )

    def test_mock_mode_ambiguity_is_written_down(self):
        for job_type in ("products.intake", "test_plans.draft", "external_creatives.process"):
            with self.subTest(job_type=job_type):
                policy = policy_for(job_type)
                self.assertIs(policy.side_effect, SideEffect.PAID)
                self.assertIs(policy.crash, CrashPolicy.UNCERTAIN)
                self.assertIn("Mock 模式可能被误标", policy.note)

    def test_an_enqueuer_may_not_contradict_the_registry(self):
        with self.assertRaises(JobPolicyMismatchError):
            resolve_safe_to_retry_after_crash("generation.export_package", False)
        with self.assertRaises(JobPolicyMismatchError):
            resolve_safe_to_retry_after_crash("launch_provisioning", True)

    def test_only_a_fenced_paid_type_may_be_marked_requeue_safe(self):
        # Controlled Mock generation, and the selection-recipe RunItem fence.
        self.assertTrue(resolve_safe_to_retry_after_crash("generate_asset", True))
        self.assertTrue(resolve_safe_to_retry_after_crash("generation.run_item", True))
        with self.assertRaises(JobPolicyMismatchError):
            resolve_safe_to_retry_after_crash("generation.qc_asset_version", True)

    def test_an_unregistered_type_is_refused(self):
        with self.assertRaises(UnregisteredJobTypeError):
            resolve_safe_to_retry_after_crash("ci-only.unregistered", None)


class TestEnqueueDerivesTheColumn(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context()
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)

    def _enqueue(self, job_type: str, key: str, **kwargs) -> JobRecord:
        with tenant_session(self.ctx) as session:
            job = jobs.enqueue_in_session(
                session, context=self.ctx, job_type=job_type, payload={},
                idempotency_key=key, **kwargs,
            )
            session.expunge(job)
            return job

    def _count(self) -> int:
        with tenant_session(self.ctx) as session:
            return session.scalar(
                select(func.count()).select_from(JobRecord)
                .where(JobRecord.workspace_id == self.ctx.workspace_id)
            )

    def test_a_requeue_type_is_enqueued_safe_to_retry(self):
        for job_type in NO_SIDE_EFFECT_REQUEUED:
            with self.subTest(job_type=job_type):
                job = self._enqueue(job_type, f"ci-only-policy:{job_type}")
                self.assertTrue(job.safe_to_retry_after_crash)

    def test_a_paid_type_is_enqueued_uncertain_on_crash(self):
        for job_type in STILL_UNCERTAIN:
            with self.subTest(job_type=job_type):
                job = self._enqueue(job_type, f"ci-only-policy:{job_type}")
                self.assertFalse(job.safe_to_retry_after_crash)

    def test_an_unregistered_or_contradicting_enqueue_writes_nothing(self):
        before = self._count()
        with self.assertRaises(UnregisteredJobTypeError):
            self._enqueue("ci-only.unregistered", "ci-only-policy:unregistered")
        with self.assertRaises(JobPolicyMismatchError):
            self._enqueue(
                "library.reference_gate", "ci-only-policy:mismatch",
                safe_to_retry_after_crash=False,
            )
        self.assertEqual(self._count(), before)


if __name__ == "__main__":
    unittest.main()
