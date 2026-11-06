"""Scale Worker capability isolation (Issue #25, Task 14).

Spec: docs/product/scale-advice-semantics-contract.md, ADR 0008
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-14-brief.md

Gates 23-25 (AST/transitive-import proof that Scale never gets a Meta write
capability), plus a runtime capability spy (Step 4) that proves the actual
``ScaleWorkerRuntime`` object holds no write adapter, no write token in its
environment, and cannot dispatch a Launch job.
"""
import ast
import pathlib
import unittest


class TestScaleWorkerImportIsolation(unittest.TestCase):
    def test_scale_module_no_meta_write_imports(self):
        """Gate 23: Scale module cannot import Meta write capability."""
        scale_dir = pathlib.Path("app/scale")
        forbidden = {
            "MetaWritePort", "MetaWriteError", "meta_write_client",
            "launch_service", "launch.service",
        }
        for py_file in scale_dir.rglob("*.py"):
            source = py_file.read_text()
            tree = ast.parse(source, filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        self.assertNotIn(
                            alias.name, forbidden,
                            f"{py_file}:{node.lineno} imports forbidden {alias.name}",
                        )
                        if alias.asname:
                            self.assertNotIn(alias.asname, forbidden)
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split(".")
                        for part in parts:
                            self.assertNotIn(
                                part, {"meta_write_client", "launch"},
                                f"{py_file}:{node.lineno} imports forbidden {alias.name}",
                            )

    def test_scale_worker_no_launch_dispatch(self):
        """Gate 24: Scale Worker has no Launch runtime."""
        source = pathlib.Path("app/scale/worker.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", "") or ""
                names = [a.name for a in node.names]
                self.assertNotIn("launch", module.lower())
                for n in names:
                    self.assertNotIn("launch", n.lower())

    def test_transitive_import_closure(self):
        """Gate 25: Transitive import closure of app.scale has no write port.

        Runs in a fresh subprocess rather than deleting ``app.scale`` from
        this process's own ``sys.modules`` and re-importing it: any other
        test module that already ran in this same process (in this suite,
        anything importing ``app.main`` -- e.g. ``tests/test_scale_router.py``,
        ``tests/test_delegated_identity.py``) permanently loads
        ``app.launch.api``/``app.adapters.meta.write_client`` process-wide,
        and deleting only the ``app.scale`` entry can never undo that.
        Re-importing ``app.scale.evaluator`` in the *same* process would
        then find those modules already present in ``sys.modules`` for
        reasons having nothing to do with ``app.scale``'s own transitive
        closure -- a false failure purely from full-suite test order, not
        a real capability leak. A subprocess with nothing imported yet is
        the only way to observe *only* what importing ``app.scale.evaluator``
        itself pulls in."""
        import pathlib
        import subprocess
        import sys

        repo_root = pathlib.Path(__file__).resolve().parents[1]
        probe = (
            "import sys\n"
            "import app.scale.evaluator\n"
            "forbidden = {\n"
            "    'app.adapters.meta.write_client',\n"
            "    'app.launch.service',\n"
            "    'app.launch.api',\n"
            "}\n"
            "violations = forbidden & set(sys.modules.keys())\n"
            "print(','.join(sorted(violations)))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, cwd=str(repo_root),
        )
        self.assertEqual(result.returncode, 0, f"probe subprocess failed: {result.stderr}")
        violations = set(result.stdout.strip().split(",")) if result.stdout.strip() else set()
        self.assertEqual(violations, set(),
                         f"Transitive closure loaded: {violations}")


class TestScaleWorkerRuntimeCapabilitySpy(unittest.TestCase):
    """Step 4: prove the constructed runtime object itself has no write
    capability, not merely that its module source is clean."""

    def test_scale_worker_runtime_takes_no_write_adapter(self):
        """ScaleWorkerRuntime's constructor accepts no meta_client,
        launch_adapter, or coordinator -- unlike WorkerRuntime, there is no
        parameter through which a write-capable object could be injected."""
        import inspect

        from app.scale.worker import ScaleWorkerRuntime

        signature = inspect.signature(ScaleWorkerRuntime.__init__)
        param_names = set(signature.parameters.keys()) - {"self"}
        self.assertEqual(
            param_names, {"owner"},
            f"ScaleWorkerRuntime constructor exposes unexpected params: {param_names}",
        )

    def test_scale_worker_runtime_instance_has_no_write_attributes(self):
        """The constructed instance carries no attribute that could hold a
        MetaWritePort, write client, launch adapter, or coordinator."""
        from app.scale.worker import ScaleWorkerRuntime

        runtime = ScaleWorkerRuntime(owner="test-owner")
        forbidden_attr_fragments = ("meta_client", "launch", "write", "coordinator")
        for attr_name, value in vars(runtime).items():
            lowered = attr_name.lower()
            for fragment in forbidden_attr_fragments:
                self.assertNotIn(
                    fragment, lowered,
                    f"ScaleWorkerRuntime instance carries forbidden attribute "
                    f"'{attr_name}' (value={value!r})",
                )

    def test_scale_worker_runtime_does_not_read_meta_write_token_env(self):
        """Constructing the runtime with META_WRITE_ACCESS_TOKEN set in the
        environment must not surface that token anywhere on the instance --
        the Scale Worker composition root never reads it."""
        import os
        from unittest import mock

        from app.scale.worker import ScaleWorkerRuntime

        sentinel_token = "sentinel-write-token-should-never-be-read"
        with mock.patch.dict(os.environ, {"META_WRITE_ACCESS_TOKEN": sentinel_token}):
            runtime = ScaleWorkerRuntime(owner="test-owner")
        for value in vars(runtime).values():
            self.assertNotEqual(
                value, sentinel_token,
                "ScaleWorkerRuntime instance captured META_WRITE_ACCESS_TOKEN",
            )

    def test_scale_worker_runtime_only_dispatches_scale_job_types(self):
        """The dispatch table (source-level) only ever compares job.type
        against the two Scale job types -- it has no branch that could reach
        a Launch/Meta-write handler, unlike the shared WorkerRuntime which
        dispatches ``launch_canary``."""
        source = pathlib.Path("app/scale/worker.py").read_text()
        self.assertIn('"scale_evaluate"', source)
        self.assertIn('"scale_advice_expire"', source)
        self.assertNotIn("launch_canary", source)
        self.assertNotIn("meta_read_ingestion", source)

    def test_scale_worker_runtime_cannot_dispatch_launch_job(self):
        """Feeding a launch_canary-typed ClaimedJob through run_once must not
        reach any Launch code path -- it is rejected as an unsupported job
        type, exactly like any other job type this Worker does not own."""
        from unittest import mock

        from app.platform.jobs import ClaimedJob
        from app.platform.tenancy import system_context
        from app.scale.worker import ScaleWorkerRuntime

        context = system_context("ws_test", initiator_id="system:test")
        fake_job = ClaimedJob(
            id="job_fake",
            type="launch_canary",
            payload={"operation_id": "op_fake"},
            attempt=1,
            max_attempts=3,
            lease_owner="test-owner",
            context=context,
        )
        runtime = ScaleWorkerRuntime(owner="test-owner")
        with mock.patch("app.scale.worker.jobs.claim_next", return_value=fake_job) as claim_mock, \
             mock.patch("app.scale.worker.jobs.fail") as fail_mock:
            handled = runtime.run_once()
        self.assertTrue(handled)
        claim_mock.assert_called_once()
        fail_mock.assert_called_once()
        _, kwargs = fail_mock.call_args
        self.assertEqual(kwargs.get("code"), "unsupported_job_type")


if __name__ == "__main__":
    unittest.main()
