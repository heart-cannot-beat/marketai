"""Executable gates for the TenantContext runtime cut-over."""
from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.adapters.persistence import database
from app import config
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext


def context(workspace_id: str = "ws_alpha") -> TenantContext:
    return TenantContext(
        workspace_id=workspace_id,
        role="owner",
        initiator_id="user_1",
        request_id="req_1",
        trace_id="trace_1",
        authorization_epoch=7,
    )


class HttpIdentitySeamTests(unittest.TestCase):
    def test_accepts_only_a_verified_typed_context(self) -> None:
        expected = context()
        request = SimpleNamespace(state=SimpleNamespace(tenant_context=expected))
        self.assertIs(request_tenant_context(request), expected)

    def test_rejects_a_missing_or_untyped_context(self) -> None:
        for value in (None, {"workspace_id": "ws_alpha"}, "ws_alpha"):
            request = SimpleNamespace(state=SimpleNamespace(tenant_context=value))
            with self.subTest(value=value), self.assertRaises(HTTPException) as caught:
                request_tenant_context(request)
            self.assertEqual(caught.exception.status_code, 401)

    def test_explicit_sqlite_local_identity_is_bounded_and_typed(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace(), headers={})
        with (
            patch.object(config, "LOCAL_IDENTITY_ENABLED", True),
            patch.object(config, "LOCAL_WORKSPACE_ID", "ws_local_test"),
            patch.object(config, "LOCAL_INITIATOR_ID", "local_test_owner"),
            patch.object(database.engine.dialect, "name", "sqlite"),
        ):
            admitted = request_tenant_context(request)

        self.assertIsInstance(admitted, TenantContext)
        self.assertEqual(admitted.workspace_id, "ws_local_test")
        self.assertEqual(admitted.role, "owner")
        self.assertTrue(admitted.request_id.startswith("req_"))
        self.assertEqual(admitted.trace_id, admitted.request_id)
        self.assertEqual(admitted.ui_language, "en")

    def test_local_identity_takes_the_interface_language_and_nothing_else_from_headers(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace(), headers={"x-ui-language": "zh-CN", "x-workspace-id": "ws_other"})
        with (
            patch.object(config, "LOCAL_IDENTITY_ENABLED", True),
            patch.object(config, "LOCAL_WORKSPACE_ID", "ws_local_test"),
            patch.object(config, "LOCAL_INITIATOR_ID", "local_test_owner"),
            patch.object(database.engine.dialect, "name", "sqlite"),
        ):
            admitted = request_tenant_context(request)

        self.assertEqual((admitted.workspace_id, admitted.ui_language), ("ws_local_test", "zh-CN"))

    def test_local_identity_cannot_be_enabled_on_postgres(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace())
        with (
            patch.object(config, "LOCAL_IDENTITY_ENABLED", True),
            patch.object(database.engine.dialect, "name", "postgresql"),
            self.assertRaises(HTTPException) as caught,
        ):
            request_tenant_context(request)

        self.assertEqual(caught.exception.status_code, 500)

    def test_every_domain_http_route_requires_the_identity_seam(self) -> None:
        from fastapi.routing import APIRoute

        from app.main import app

        health = {"/api/health/live", "/api/health/ready"}
        domain_routes = [
            route
            for route in app.routes
            if isinstance(route, APIRoute)
            and (route.path.startswith("/api/") or route.path.startswith("/files/"))
            and route.path not in health
        ]
        missing = []
        for route in domain_routes:
            dependency_calls = {
                dependency.call for dependency in route.dependant.dependencies
            }
            if request_tenant_context not in dependency_calls:
                missing.append(f"{','.join(sorted(route.methods))} {route.path}")

        self.assertGreater(len(domain_routes), 20)
        self.assertEqual(missing, [])


class RuntimeDatabaseRoleTests(unittest.TestCase):
    def test_sqlite_has_no_runtime_role_to_assert(self) -> None:
        with patch.object(database.engine.dialect, "name", "sqlite"):
            database.assert_runtime_role("marketai_domain_api")

    def test_postgres_rejects_the_wrong_runtime_role(self) -> None:
        connection = MagicMock()
        connection.scalar.return_value = "marketai_domain_worker"
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False
        with (
            patch.object(database.engine.dialect, "name", "postgresql"),
            patch.object(database.engine, "connect", return_value=connection),
            self.assertRaisesRegex(RuntimeError, "marketai_domain_api"),
        ):
            database.assert_runtime_role("marketai_domain_api")

    def test_postgres_accepts_the_expected_runtime_role(self) -> None:
        connection = MagicMock()
        connection.scalar.return_value = "marketai_domain_api"
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False
        with (
            patch.object(database.engine.dialect, "name", "postgresql"),
            patch.object(database.engine, "connect", return_value=connection),
        ):
            database.assert_runtime_role("marketai_domain_api")


class LocalIdentityBoundaryTests(unittest.TestCase):
    """assert_local_identity_boundary: fail startup, not just the first
    request, when MARKETAI_LOCAL_IDENTITY_ENABLED is set against PostgreSQL."""

    def test_sqlite_with_local_identity_enabled_is_fine(self) -> None:
        with (
            patch.object(config, "LOCAL_IDENTITY_ENABLED", True),
            patch.object(database.engine.dialect, "name", "sqlite"),
        ):
            database.assert_local_identity_boundary()

    def test_postgres_without_local_identity_is_fine(self) -> None:
        with (
            patch.object(config, "LOCAL_IDENTITY_ENABLED", False),
            patch.object(database.engine.dialect, "name", "postgresql"),
        ):
            database.assert_local_identity_boundary()

    def test_postgres_with_local_identity_enabled_raises(self) -> None:
        with (
            patch.object(config, "LOCAL_IDENTITY_ENABLED", True),
            patch.object(database.engine.dialect, "name", "postgresql"),
            self.assertRaisesRegex(RuntimeError, "MARKETAI_LOCAL_IDENTITY_ENABLED"),
        ):
            database.assert_local_identity_boundary()


class ProjectionSyncConfigCheckTests(unittest.TestCase):
    """Startup names which projection-sync setting is empty, as booleans only,
    so a deploy log shows a missing setting without waiting for a new Workspace."""

    BASE_URL = "http://control-plane-sentinel.internal:4002"
    TOKEN = "svc_sentinel_token_7f3a"

    def setUp(self) -> None:
        # Migration tests run Alembic in this process, and its `fileConfig`
        # disables every already-imported logger (see `tests.test_asset_thumbnails`).
        logger = logging.getLogger("app.main")
        self.addCleanup(setattr, logger, "disabled", logger.disabled)
        logger.disabled = False

    def test_an_empty_setting_logs_one_warning_with_booleans_only(self) -> None:
        from app import main

        cases = (
            ("", self.TOKEN, "projection_sync_config base_url_set=false token_set=true"),
            (self.BASE_URL, "", "projection_sync_config base_url_set=true token_set=false"),
            ("", "", "projection_sync_config base_url_set=false token_set=false"),
        )
        for base_url, token, expected in cases:
            with self.subTest(expected=expected), self.assertLogs("app.main", level="WARNING") as captured:
                main._warn_if_projection_sync_unconfigured(base_url, token)
            self.assertEqual([record.getMessage() for record in captured.records], [expected])

    def test_both_settings_present_log_nothing(self) -> None:
        from app import main

        with self.assertNoLogs("app.main", level="DEBUG"):
            main._warn_if_projection_sync_unconfigured(self.BASE_URL, self.TOKEN)

    def test_startup_runs_the_check_only_with_delegated_identity(self) -> None:
        from app import main

        async def start_and_stop() -> None:
            async with main.lifespan(main.app):
                pass

        with (
            patch.object(main, "assert_runtime_role"),
            patch.object(main, "assert_local_identity_boundary"),
            patch.object(main, "_jwks_store", None),
            patch.object(main, "_jwks_http_client", None),
            patch.object(main, "_projection_http_client", None),
            patch.object(main, "_platform_jwks_store", None),
            patch.object(main, "_platform_jwks_http_client", None),
            patch.object(config, "CONTROL_PLANE_INTERNAL_BASE_URL", ""),
            patch.object(config, "PROJECTION_SYNC_SERVICE_TOKEN", self.TOKEN),
        ):
            with (
                patch.object(config, "DELEGATED_IDENTITY_ENABLED", True),
                self.assertLogs("app.main", level="WARNING") as captured,
            ):
                asyncio.run(start_and_stop())
            self.assertEqual(
                [record.getMessage() for record in captured.records],
                ["projection_sync_config base_url_set=false token_set=true"],
            )

            with (
                patch.object(config, "DELEGATED_IDENTITY_ENABLED", False),
                self.assertNoLogs("app.main", level="WARNING"),
            ):
                asyncio.run(start_and_stop())


class OnboardingDeliveryConfigCheckTests(unittest.TestCase):
    """Worker startup names which onboarding-delivery setting is empty, as
    booleans only: without both, the Worker skips delivery without an error."""

    BASE_URL = "http://control-plane-sentinel.internal:4002"
    TOKEN = "svc_sentinel_token_9c41"

    def setUp(self) -> None:
        # Migration tests run Alembic in this process, and its `fileConfig`
        # disables every already-imported logger (see `tests.test_asset_thumbnails`).
        logger = logging.getLogger("app.worker_runtime")
        self.addCleanup(setattr, logger, "disabled", logger.disabled)
        logger.disabled = False

    def test_an_empty_setting_logs_one_warning_with_booleans_only(self) -> None:
        from app import worker_runtime

        cases = (
            ("", self.TOKEN, "onboarding_delivery_config base_url_set=false token_set=true"),
            (self.BASE_URL, "", "onboarding_delivery_config base_url_set=true token_set=false"),
            ("", "", "onboarding_delivery_config base_url_set=false token_set=false"),
        )
        for base_url, token, expected in cases:
            with (
                self.subTest(expected=expected),
                self.assertLogs("app.worker_runtime", level="WARNING") as captured,
            ):
                worker_runtime._warn_if_onboarding_delivery_unconfigured(base_url, token)
            self.assertEqual([record.getMessage() for record in captured.records], [expected])

    def test_both_settings_present_log_nothing(self) -> None:
        from app import worker_runtime

        with self.assertNoLogs("app.worker_runtime", level="DEBUG"):
            worker_runtime._warn_if_onboarding_delivery_unconfigured(self.BASE_URL, self.TOKEN)

    def test_worker_startup_runs_the_check_regardless_of_delegated_identity(self) -> None:
        # Production Worker contracts deny `DELEGATED_JWKS_URL`, so the flag is
        # always off there; a check gated on it would never run.
        from app import worker_runtime

        runtime = MagicMock()
        runtime.run_once = AsyncMock(return_value=False)
        runtime.close = AsyncMock()
        with (
            patch.object(worker_runtime, "assert_runtime_role"),
            patch.object(worker_runtime, "WorkerRuntime", return_value=runtime),
            patch.object(config, "CONTROL_PLANE_INTERNAL_BASE_URL", ""),
            patch.object(config, "DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN", self.TOKEN),
        ):
            for delegated_identity in (False, True):
                with (
                    self.subTest(delegated_identity=delegated_identity),
                    patch.object(config, "DELEGATED_IDENTITY_ENABLED", delegated_identity),
                    self.assertLogs("app.worker_runtime", level="WARNING") as captured,
                ):
                    asyncio.run(worker_runtime._main(once=True))
                messages = [record.getMessage() for record in captured.records]
                self.assertEqual(
                    messages, ["onboarding_delivery_config base_url_set=false token_set=true"]
                )
                self.assertNotIn(self.TOKEN, messages[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
