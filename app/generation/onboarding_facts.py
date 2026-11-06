"""Transactional first-value facts emitted only by authoritative producers."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id, utcnow
from app.generation.models import OnboardingFactOutboxRecord
from app.platform.tenancy import TenantContext


def emit_onboarding_fact(
    session: Session,
    ctx: TenantContext,
    *,
    fact_type: str,
    source_resource_id: str,
) -> OnboardingFactOutboxRecord:
    existing = session.execute(
        select(OnboardingFactOutboxRecord).where(
            OnboardingFactOutboxRecord.workspace_id == ctx.workspace_id,
            OnboardingFactOutboxRecord.fact_type == fact_type,
            OnboardingFactOutboxRecord.source_resource_id == source_resource_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    event = OnboardingFactOutboxRecord(
        id=new_id("obf"),
        workspace_id=ctx.workspace_id,
        fact_type=fact_type,
        source_resource_id=source_resource_id,
        observed_at=utcnow(),
    )
    session.add(event)
    session.flush()
    return event
