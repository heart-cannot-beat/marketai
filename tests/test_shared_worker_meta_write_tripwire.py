"""Tripwire: Scale handlers share the main Worker only while it cannot write to Meta.

ADR 0017 amends Scale contract §20: the shared Worker (``python -m worker.main``
-> ``app.worker_runtime``) may run ``scale_evaluate`` and ``scale_export`` only
while the process has

1. no Meta write port and no write-side client it can construct,
2. no Launch dispatch that reaches a real Meta write client, and
3. no ``META_WRITE_ACCESS_TOKEN``.

``app.worker_runtime._main`` refuses to start when any of the three fails
(``assert_no_meta_write_capability``). This module proves that check passes
for the Worker as composed today and fails for each way the process could gain
write capability. It also pins the facts the check relies on, so a new write
client, a new path to the Graph API, or a Scale handler that reaches Meta or
Launch fails here first. When it fails, split out the Scale Worker before
changing the shared one (ADR 0017, "Splitting later").
"""
from __future__ import annotations

import ast
import asyncio
import os
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from app import config, worker_runtime
from app.adapters.meta.client import MetaClient
from app.adapters.meta.fake_adapter import FakeMetaAdapter
from app.adapters.meta.fake_write import FakeMetaWriteAdapter
from app.adapters.meta.write_client import HttpMetaWriteAdapter
from app.platform import jobs
from app.platform.jobs import ClaimedJob
from app.platform.tenancy import system_context
from app.worker_runtime import (
    MetaWriteCapabilityError,
    WorkerRuntime,
    assert_no_meta_write_capability,
    meta_write_capability_findings,
)
from tests.test_job_policy import _dispatched_types

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
SCALE_JOB_TYPES = {"scale_evaluate", "scale_export"}
SENTINEL_TOKEN = "ci-only-write-token-sentinel"
#: Everything ``config.meta_write_ready()`` needs, with fictional values.
FULL_WRITE_CONFIG = {
    "META_WRITE_ENABLED": True,
    "META_WRITE_ACCESS_TOKEN": SENTINEL_TOKEN,
    "META_APP_SECRET": "ci-only-app-secret",
    "META_AD_ACCOUNT_ID": "act_ci_only",
    "META_API_VERSION": "v0.0",
    "LAUNCH_CONFIRMATION_SECRET": "ci-only-launch-secret",
}


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


class TheSharedWorkerRunsScaleHandlers(unittest.TestCase):
    def test_the_shared_worker_dispatches_scale_jobs(self):
        # The precondition that makes ADR 0017 bind this Worker. If Scale has
        # been split out, retire the startup check and this module on purpose
        # rather than leaving it to guard nothing.
        self.assertLessEqual(SCALE_JOB_TYPES, _dispatched_types(WorkerRuntime))


class StartupCheck(unittest.TestCase):
    """``_main`` builds the Worker, then refuses to run it if it could write."""

    def setUp(self):
        self.run_once = AsyncMock(return_value=False)
        for patcher in (
            mock.patch.object(worker_runtime, "assert_runtime_role"),
            mock.patch.object(WorkerRuntime, "run_once", self.run_once),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _main(build_runtime=None) -> None:
        if build_runtime is None:
            asyncio.run(worker_runtime._main(once=True))
            return
        with mock.patch.object(worker_runtime, "WorkerRuntime", build_runtime):
            asyncio.run(worker_runtime._main(once=True))

    def test_the_worker_as_composed_today_starts(self):
        built = []

        def build():
            built.append(WorkerRuntime())
            return built[-1]

        self._main(build)

        self.run_once.assert_awaited_once()
        self.assertEqual(meta_write_capability_findings(built[0]), [])
        self.assertIsNone(built[0].launch_adapter)
        self.assertIsNone(built[0].meta_client)

    def test_a_write_token_in_the_environment_stops_the_worker_before_it_claims_a_job(self):
        with mock.patch.dict(os.environ, {"META_WRITE_ACCESS_TOKEN": SENTINEL_TOKEN}):
            with self.assertRaises(MetaWriteCapabilityError) as raised:
                self._main()
        self.assertIn("meta_write_access_token_set", str(raised.exception))
        self.assertNotIn(SENTINEL_TOKEN, str(raised.exception))
        self.run_once.assert_not_awaited()

    def test_a_write_port_in_the_composition_root_stops_the_worker(self):
        with self.assertRaises(MetaWriteCapabilityError) as raised:
            self._main(lambda: WorkerRuntime(launch_adapter=FakeMetaWriteAdapter()))
        self.assertIn("meta_write_port_held:launch_adapter", str(raised.exception))
        self.run_once.assert_not_awaited()

    def test_full_write_configuration_stops_the_worker(self):
        with mock.patch.multiple(config, **FULL_WRITE_CONFIG):
            with self.assertRaises(MetaWriteCapabilityError) as raised:
                self._main()
        self.run_once.assert_not_awaited()
        message = str(raised.exception)
        self.assertIn("meta_write_access_token_set", message)
        self.assertIn("meta_write_client_constructible:HttpMetaWriteAdapter", message)
        for value in FULL_WRITE_CONFIG.values():
            if isinstance(value, str):
                self.assertNotIn(value, message)


class WriteCapabilityFindings(unittest.TestCase):
    """Each condition trips on its own, and read capability does not."""

    def test_a_non_empty_token_in_the_environment_trips(self):
        with mock.patch.dict(os.environ, {"META_WRITE_ACCESS_TOKEN": SENTINEL_TOKEN}):
            self.assertEqual(
                meta_write_capability_findings(WorkerRuntime()), ["meta_write_access_token_set"]
            )

    def test_a_non_empty_token_in_config_trips(self):
        with mock.patch.object(config, "META_WRITE_ACCESS_TOKEN", SENTINEL_TOKEN):
            self.assertEqual(
                meta_write_capability_findings(WorkerRuntime()), ["meta_write_access_token_set"]
            )

    def test_a_blank_token_is_no_token(self):
        with mock.patch.dict(os.environ, {"META_WRITE_ACCESS_TOKEN": "  "}):
            self.assertEqual(meta_write_capability_findings(WorkerRuntime()), [])

    def test_a_write_port_held_by_the_runtime_trips(self):
        sandbox_port = object.__new__(FakeMetaAdapter)
        cases = {
            "launch_adapter": WorkerRuntime(launch_adapter=FakeMetaWriteAdapter()),
            "meta_client": WorkerRuntime(meta_client=FakeMetaWriteAdapter()),
        }
        scale_side = WorkerRuntime()
        scale_side.scale_port = sandbox_port
        cases["scale_port"] = scale_side
        for attribute, runtime in cases.items():
            with self.subTest(attribute=attribute):
                self.assertEqual(
                    meta_write_capability_findings(runtime), [f"meta_write_port_held:{attribute}"]
                )
                with self.assertRaises(MetaWriteCapabilityError):
                    assert_no_meta_write_capability(runtime)

    def test_a_real_write_client_handed_to_the_runtime_trips(self):
        with mock.patch.object(config, "meta_write_ready", return_value=True):
            client = HttpMetaWriteAdapter()
        runtime = WorkerRuntime(launch_adapter=client)
        self.assertEqual(
            meta_write_capability_findings(runtime), ["meta_write_port_held:launch_adapter"]
        )

    def test_a_constructible_write_client_trips_without_the_token(self):
        # The constructibility check stands on its own: a write client that
        # found its credentials somewhere other than the Token still trips.
        with mock.patch.object(config, "meta_write_ready", return_value=True):
            self.assertEqual(
                meta_write_capability_findings(WorkerRuntime()),
                ["meta_write_client_constructible:HttpMetaWriteAdapter"],
            )

    def test_the_read_client_is_not_write_capability(self):
        self.assertEqual(meta_write_capability_findings(WorkerRuntime(meta_client=MetaClient())), [])


class LaunchDispatchStopsAtTheTokenGate(unittest.TestCase):
    """``launch_canary`` is the shared Worker's only Launch path to a real
    write client: with no port injected it builds ``HttpMetaWriteAdapter``
    itself. Dispatched through the Worker as composed, it stops at the
    client's constructor while the Token is empty, even with the kill switch
    open and every other gate passed."""

    def _dispatch_launch_canary(self, extra_config: dict) -> tuple[list, list, mock.Mock]:
        context = system_context(
            "ws_ci_only_launch", initiator_id="user_ci_only", initiator_role="owner",
        )
        job = ClaimedJob(
            id="job_ci_only_launch", type="launch_canary",
            payload={"operation_id": "op_ci_only_launch"},
            attempt=1, max_attempts=1, lease_owner="ci-only-worker", context=context,
        )
        attempts: list = []
        built: list = []
        original_init = HttpMetaWriteAdapter.__init__

        def spy_init(adapter):
            attempts.append(adapter)
            original_init(adapter)
            built.append(adapter)

        settings = {
            "META_WRITE_ENABLED": True,
            "META_CREDENTIAL_WORKSPACE_ID": context.workspace_id,
            **extra_config,
        }
        network = mock.Mock(side_effect=AssertionError("no Graph API call in this test"))
        runtime = WorkerRuntime(owner="ci-only-worker")
        with mock.patch.multiple(config, **settings), \
             mock.patch.object(HttpMetaWriteAdapter, "__init__", spy_init), \
             mock.patch("httpx.AsyncClient.request", network), \
             mock.patch.object(jobs, "claim_next", return_value=job), \
             mock.patch.object(jobs, "mark_running"), \
             mock.patch.object(jobs, "heartbeat", return_value=False), \
             mock.patch.object(jobs, "succeed") as succeed, \
             mock.patch.object(jobs, "fail", return_value="failed") as fail, \
             mock.patch.object(worker_runtime.launch_service, "record_failure"):
            self.assertTrue(asyncio.run(runtime.run_once()))
        network.assert_not_called()
        succeed.assert_not_called()
        return attempts, built, fail

    def test_without_the_token_the_dispatch_builds_no_write_client(self):
        attempts, built, fail = self._dispatch_launch_canary({})

        self.assertEqual(len(attempts), 1, "launch_canary reached the write client's constructor")
        self.assertEqual(built, [], "the constructor refused")
        self.assertEqual(fail.call_args.kwargs["code"], "meta_write_disabled")

    def test_with_the_token_the_same_dispatch_builds_one(self):
        # The contrast case: the Token (with the rest of the write
        # configuration) is the only thing between this Launch route and a
        # live write client -- which is why the startup check refuses it.
        _attempts, built, fail = self._dispatch_launch_canary(FULL_WRITE_CONFIG)

        self.assertEqual(len(built), 1)
        self.assertNotEqual(fail.call_args.kwargs["code"], "meta_write_disabled")


def _class_methods(node: ast.ClassDef) -> set[str]:
    return {
        item.name for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _declares_test_adapter(node: ast.ClassDef) -> bool:
    for item in node.body:
        if isinstance(item, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "is_test_adapter" for target in item.targets
        ):
            return isinstance(item.value, ast.Constant) and item.value.value is True
    return False


def _is_protocol(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "Protocol")
        or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
        for base in node.bases
    )


class TheStartupCheckCoversEveryWritePath(unittest.TestCase):
    """Static facts the startup check depends on."""

    def test_every_meta_write_client_in_the_app_is_checked_at_startup(self):
        real, sandbox = set(), set()
        for path in APP.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.ClassDef) or _is_protocol(node):
                    continue
                if not _class_methods(node) & set(worker_runtime._META_WRITE_PORT_METHODS):
                    continue
                (sandbox if _declares_test_adapter(node) else real).add(node.name)
        self.assertEqual(
            real, {client.__name__ for client in worker_runtime._META_WRITE_CLIENTS},
            "a Meta write client the startup check does not try to construct",
        )
        # A sandbox adapter claims ``is_test_adapter``; pin the list so a real
        # client cannot join it without review.
        self.assertEqual(sandbox, {"FakeMetaAdapter", "FakeMetaWriteAdapter"})

    def test_only_the_two_meta_adapters_address_the_graph_api(self):
        addressing = {
            _relative(path) for path in APP.rglob("*.py")
            if "graph.facebook.com" in path.read_text(encoding="utf-8")
        }
        self.assertEqual(
            addressing, {"app/adapters/meta/client.py", "app/adapters/meta/write_client.py"},
        )

    def test_the_read_adapter_only_sends_get_requests(self):
        tree = ast.parse((APP / "adapters/meta/client.py").read_text(encoding="utf-8"))
        verbs = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "post", "put", "patch", "delete", "request", "send", "stream"}
        }
        self.assertEqual(verbs, {"get"})

    def test_only_launch_canary_builds_the_write_client(self):
        # Provisioning and recovery resolve only the sandbox adapter; the one
        # construction of the real client is Launch's ``execute_operation``,
        # which the shared Worker calls from ``_launch_canary`` alone.
        constructing = {}
        for path in APP.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if (
                        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "HttpMetaWriteAdapter"
                    ):
                        constructing.setdefault(_relative(path), set()).add(function.name)
        self.assertEqual(constructing, {"app/launch/service.py": {"execute_operation"}})

        tree = ast.parse((APP / "worker_runtime.py").read_text(encoding="utf-8"))
        callers = {
            function.name
            for function in ast.walk(tree)
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(function)
            if isinstance(node, ast.Attribute) and node.attr == "execute_operation"
        }
        self.assertEqual(callers, {"_launch_canary"})


class ScaleHandlersReachNeitherMetaNorLaunch(unittest.TestCase):
    """#55: the shared Worker's Scale handlers delegate to ``app.scale`` and
    never touch the runtime's Meta clients or Launch."""

    FORBIDDEN_NAMES = {
        "launch_service", "meta_service", "HttpMetaWriteAdapter", "MetaWritePort",
        "execute_provisioning", "execute_recovery",
    }
    FORBIDDEN_ATTRIBUTES = {"meta_client", "launch_adapter", "_coordinator"}

    def _scale_handlers(self) -> dict[str, ast.AST]:
        tree = ast.parse((APP / "worker_runtime.py").read_text(encoding="utf-8"))
        runtime = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WorkerRuntime"
        )
        return {
            item.name: item for item in runtime.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("_scale_")
        }

    def test_there_is_one_handler_per_scale_job_type(self):
        self.assertEqual(set(self._scale_handlers()), {f"_{job_type}" for job_type in SCALE_JOB_TYPES})

    def test_scale_handlers_import_only_scale_and_touch_no_meta_or_launch(self):
        for name, handler in self._scale_handlers().items():
            with self.subTest(handler=name):
                for node in ast.walk(handler):
                    if isinstance(node, ast.ImportFrom):
                        self.assertTrue((node.module or "").startswith("app.scale."), node.module)
                    if isinstance(node, ast.Import):
                        self.fail(f"{name} imports {[alias.name for alias in node.names]}")
                    if isinstance(node, ast.Name):
                        self.assertNotIn(node.id, self.FORBIDDEN_NAMES)
                    if (
                        isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == "self"
                    ):
                        self.assertNotIn(node.attr, self.FORBIDDEN_ATTRIBUTES)

    def test_scale_code_names_no_launch_job_type(self):
        # Scale cannot enqueue a Launch job without naming its type.
        launch_types = {t for t in _dispatched_types(WorkerRuntime) if t.startswith("launch_")}
        self.assertEqual(launch_types, {"launch_canary", "launch_provisioning", "launch_recovery"})
        for path in (APP / "scale").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(node.value, launch_types, _relative(path))


class DeploymentsOfTheSharedWorkerGetNoToken(unittest.TestCase):
    """Every checked-in deployment of ``python -m worker.main`` leaves the
    Token out; the startup check then refuses one that sets it anyway."""

    def test_the_railway_worker_contract_denies_the_token(self):
        lines = (ROOT / "deploy/railway/env/worker.env.contract").read_text(encoding="utf-8").splitlines()
        entries = {
            parts[1]: parts[0] for parts in (line.split() for line in lines)
            if len(parts) >= 2 and parts[0] in {"REQUIRED", "OPTIONAL", "DENIED"}
        }
        self.assertEqual(entries.get("META_WRITE_ACCESS_TOKEN"), "DENIED")

    def test_the_compose_worker_gets_an_empty_token(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('command: ["python", "-m", "worker.main"]', compose)
        self.assertNotIn("META_WRITE_ACCESS_TOKEN", compose)
        env = (ROOT / "deploy/docker/.env.compose").read_text(encoding="utf-8").splitlines()
        self.assertIn("META_WRITE_ACCESS_TOKEN=", env)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
