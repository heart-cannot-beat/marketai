from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

import httpx
import pytest
from alembic import command
from alembic.config import Config

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord, utcnow
from app.generation import onboarding_dispatcher
from app.generation.models import OnboardingFactOutboxRecord
from app.generation.onboarding_dispatcher import (
    ClaimedOnboardingFact,
    claim_next_onboarding_fact,
    dispatch_one_onboarding_fact,
    settle_onboarding_fact,
)
from app.generation.onboarding_facts import emit_onboarding_fact
from app.platform.tenancy import TenantContext


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _Client:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.requests: list[dict] = []

    async def post(self, url: str, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return _Response(self.status_code)


def _context(workspace_id: str) -> TenantContext:
    return TenantContext(
        workspace_id=workspace_id,
        role="owner",
        initiator_id="owner-onboarding",
        authorization_epoch=0,
    )


def _workspace_with_fact() -> tuple[str, str]:
    command.upgrade(Config("alembic.ini"), "head")
    workspace_id = "ws_onboarding_" + uuid.uuid4().hex[:12]
    with platform_session() as session:
        session.query(OnboardingFactOutboxRecord).delete()
        session.add(
            WorkspaceRecord(
                id=workspace_id,
                name=workspace_id,
                default_currency="USD",
                lifecycle="active",
                budget_currency="USD",
                authorization_epoch=0,
            )
        )
    with tenant_session(_context(workspace_id)) as session:
        event = emit_onboarding_fact(
            session,
            _context(workspace_id),
            fact_type="resolution_preview.validated",
            source_resource_id="preview-" + uuid.uuid4().hex[:12],
        )
        return workspace_id, event.id


def test_dispatch_failure_survives_restart_and_then_delivers() -> None:
    workspace_id, event_id = _workspace_with_fact()
    first = claim_next_onboarding_fact("worker-first", lease_seconds=30)
    assert first is not None
    assert first.event_id == event_id
    assert settle_onboarding_fact(
        first,
        owner="worker-first",
        delivered=False,
        error="control plane returned HTTP 503",
        retry_seconds=0,
    )

    client = _Client(200)
    assert asyncio.run(
        dispatch_one_onboarding_fact(
            "worker-after-restart",
            client,  # type: ignore[arg-type]
            base_url="http://control.test",
            service_token="service-token",
        )
    )
    assert client.requests[0]["json"] == {
        "workspaceId": workspace_id,
        "sourceEventId": event_id,
        "factType": "resolution_preview.validated",
        "sourceResourceId": client.requests[0]["json"]["sourceResourceId"],
        "observedAt": client.requests[0]["json"]["observedAt"],
    }
    with tenant_session(_context(workspace_id)) as session:
        record = session.get(OnboardingFactOutboxRecord, event_id)
        assert record is not None
        assert record.delivered_at is not None
        assert record.attempts == 2
        assert record.last_error is None


def test_settlement_is_bound_to_the_claimed_workspace_and_lease() -> None:
    workspace_id, event_id = _workspace_with_fact()
    claimed = claim_next_onboarding_fact("worker-bound", lease_seconds=30)
    assert claimed is not None
    forged = ClaimedOnboardingFact(
        event_id=event_id,
        workspace_id="ws_other_" + uuid.uuid4().hex[:12],
        fact_type=claimed.fact_type,
        source_resource_id=claimed.source_resource_id,
        observed_at=utcnow() - timedelta(seconds=1),
        authorization_epoch=0,
    )
    assert not settle_onboarding_fact(
        forged,
        owner="worker-bound",
        delivered=True,
    )
    with tenant_session(_context(workspace_id)) as session:
        record = session.get(OnboardingFactOutboxRecord, event_id)
        assert record is not None
        assert record.delivered_at is None


_SENTINEL_HOST = "control-plane-sentinel.internal"
_SENTINEL_TOKEN = "svc_sentinel_token_4b2e"


class _UnreachableClient:
    async def post(self, url: str, **kwargs):
        raise httpx.ConnectError(f"cannot connect to {url}")


class _Messages(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _dispatch_warnings(client) -> list[str]:
    # `_workspace_with_fact` migrates through Alembic, whose `fileConfig`
    # disables every already-imported logger and replaces the root handlers
    # (pytest's `caplog` among them); listen on the dispatcher logger itself.
    logger = logging.getLogger(onboarding_dispatcher.__name__)
    handler = _Messages()
    disabled = logger.disabled
    logger.disabled = False
    logger.addHandler(handler)
    try:
        assert asyncio.run(
            dispatch_one_onboarding_fact(
                "worker-logging",
                client,
                base_url=f"http://{_SENTINEL_HOST}:4002",
                service_token=_SENTINEL_TOKEN,
            )
        )
    finally:
        logger.removeHandler(handler)
        logger.disabled = disabled
    return handler.messages


def test_rejected_delivery_logs_event_and_reason_without_credentials() -> None:
    _, event_id = _workspace_with_fact()
    messages = _dispatch_warnings(_Client(403))
    assert messages == [
        "onboarding fact delivery failed "
        f"event_id={event_id} reason=control plane returned HTTP 403"
    ]
    assert _SENTINEL_TOKEN not in messages[0]
    assert _SENTINEL_HOST not in messages[0]


def test_unreachable_control_plane_logs_only_the_error_class() -> None:
    _, event_id = _workspace_with_fact()
    messages = _dispatch_warnings(_UnreachableClient())
    assert messages == [
        "onboarding fact delivery failed "
        f"event_id={event_id} reason=control plane request failed: ConnectError"
    ]
    assert _SENTINEL_HOST not in messages[0]


def test_lost_lease_after_delivery_logs_the_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _, event_id = _workspace_with_fact()
    monkeypatch.setattr(onboarding_dispatcher, "settle_onboarding_fact", lambda *a, **k: False)
    messages = _dispatch_warnings(_Client(200))
    assert messages == [
        f"onboarding fact delivery lease was lost before settlement event_id={event_id}"
    ]
