"""Durable delivery of typed first-value facts to the control plane.

The domain transaction writes :class:`OnboardingFactOutboxRecord`; this
dispatcher is the only network side effect. PostgreSQL claims are performed by
a narrow SECURITY DEFINER function so a Worker can find one pending row without
gaining cross-Workspace table access. Settlement is still tenant-scoped and
RLS-enforced. The control plane deduplicates by the stable outbox event id.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_, select, text, update

from app import config
from app.adapters.persistence.database import engine, platform_session, tenant_session
from app.adapters.persistence.models import utcnow
from app.generation.models import OnboardingFactOutboxRecord
from app.platform.tenancy import system_context
from app.platform.runtime_role import WORKER_ROLE, runs_as


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedOnboardingFact:
    event_id: str
    workspace_id: str
    fact_type: str
    source_resource_id: str
    observed_at: datetime
    authorization_epoch: int


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def claim_next_onboarding_fact(
    owner: str,
    *,
    lease_seconds: int = 120,
) -> ClaimedOnboardingFact | None:
    if not owner or len(owner) < 3:
        raise ValueError("a stable dispatcher owner is required")
    if lease_seconds < 1 or lease_seconds > 600:
        raise ValueError("lease_seconds must be between 1 and 600")

    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM marketai_claim_next_onboarding_fact("
                    "CAST(:owner AS text), CAST(:lease_seconds AS integer))"
                ),
                {"owner": owner, "lease_seconds": lease_seconds},
            ).mappings().first()
        if row is None:
            return None
        return ClaimedOnboardingFact(
            event_id=str(row["event_id"]),
            workspace_id=str(row["workspace_id"]),
            fact_type=str(row["fact_type"]),
            source_resource_id=str(row["source_resource_id"]),
            observed_at=_as_utc(row["observed_at"]),
            authorization_epoch=int(row["authorization_epoch"]),
        )

    now = utcnow()
    with platform_session() as session:
        record = session.scalar(
            select(OnboardingFactOutboxRecord)
            .where(
                OnboardingFactOutboxRecord.delivered_at.is_(None),
                or_(
                    OnboardingFactOutboxRecord.delivery_lease_expires_at.is_(None),
                    OnboardingFactOutboxRecord.delivery_lease_expires_at <= now,
                ),
            )
            .order_by(
                OnboardingFactOutboxRecord.observed_at,
                OnboardingFactOutboxRecord.id,
            )
            .limit(1)
        )
        if record is None:
            return None
        record.attempts += 1
        record.delivery_lease_id = owner
        record.delivery_lease_expires_at = now + timedelta(seconds=lease_seconds)
        record.last_error = None
        session.flush()
        workspace_epoch = session.scalar(
            text(
                "SELECT authorization_epoch FROM workspaces "
                "WHERE id = :workspace_id"
            ),
            {"workspace_id": record.workspace_id},
        )
        if workspace_epoch is None:
            raise RuntimeError("onboarding fact references a missing Workspace")
        return ClaimedOnboardingFact(
            event_id=record.id,
            workspace_id=record.workspace_id,
            fact_type=record.fact_type,
            source_resource_id=record.source_resource_id,
            observed_at=_as_utc(record.observed_at),
            authorization_epoch=int(workspace_epoch),
        )


def settle_onboarding_fact(
    fact: ClaimedOnboardingFact,
    *,
    owner: str,
    delivered: bool,
    error: str = "",
    retry_seconds: int = 5,
) -> bool:
    now = utcnow()
    context = system_context(
        fact.workspace_id,
        initiator_id=None,
        executor="system:onboarding-fact-dispatcher",
        authorization_epoch=fact.authorization_epoch,
    )
    values = (
        {
            "delivered_at": now,
            "delivery_lease_id": None,
            "delivery_lease_expires_at": None,
            "last_error": None,
        }
        if delivered
        else {
            "delivery_lease_id": None,
            "delivery_lease_expires_at": now
            + timedelta(seconds=max(0, min(retry_seconds, 300))),
            "last_error": error[:500] or "delivery_failed",
        }
    )
    with tenant_session(context) as session:
        result = session.execute(
            update(OnboardingFactOutboxRecord)
            .where(
                OnboardingFactOutboxRecord.id == fact.event_id,
                OnboardingFactOutboxRecord.workspace_id == fact.workspace_id,
                OnboardingFactOutboxRecord.delivery_lease_id == owner,
                OnboardingFactOutboxRecord.delivered_at.is_(None),
            )
            .values(**values)
        )
        return result.rowcount == 1


@runs_as(WORKER_ROLE)
async def dispatch_one_onboarding_fact(
    owner: str,
    http_client: httpx.AsyncClient,
    *,
    base_url: str | None = None,
    service_token: str | None = None,
    lease_seconds: int = 120,
) -> bool:
    """Claim and attempt one delivery; return whether a row was claimed."""
    target = (base_url if base_url is not None else config.CONTROL_PLANE_INTERNAL_BASE_URL).rstrip(
        "/"
    )
    token = (
        service_token
        if service_token is not None
        else config.DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN
    )
    if not target or not token:
        return False

    fact = claim_next_onboarding_fact(owner, lease_seconds=lease_seconds)
    if fact is None:
        return False

    try:
        response = await http_client.post(
            f"{target}/api/internal/domain/onboarding",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            json={
                "workspaceId": fact.workspace_id,
                "sourceEventId": fact.event_id,
                "factType": fact.fact_type,
                "sourceResourceId": fact.source_resource_id,
                "observedAt": fact.observed_at.isoformat(),
            },
            timeout=config.PROJECTION_SYNC_TIMEOUT_MS / 1000,
        )
        if 200 <= response.status_code < 300:
            if not settle_onboarding_fact(fact, owner=owner, delivered=True):
                logger.warning(
                    "onboarding fact delivery lease was lost before settlement event_id=%s",
                    fact.event_id,
                    extra={"event_id": fact.event_id},
                )
            return True
        failure = f"control plane returned HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        failure = f"control plane request failed: {type(exc).__name__}"

    settle_onboarding_fact(
        fact,
        owner=owner,
        delivered=False,
        error=failure,
        retry_seconds=min(2 ** min(8, max(1, lease_seconds // 15)), 300),
    )
    # Without a configured handler only the message text reaches the log, so
    # the fields are in it too. `failure` carries a status code or an error
    # class name only, never the token or the URL.
    logger.warning(
        "onboarding fact delivery failed event_id=%s reason=%s",
        fact.event_id,
        failure,
        extra={"event_id": fact.event_id, "reason": failure},
    )
    return True
