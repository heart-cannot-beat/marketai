"""One market-data job at a time per key (spec §5.1).

Resolves, scans and resumes that pay with the same CredentialVersion run
one at a time inside a Workspace; the others wait their turn and record the
wait. That keeps MarketAI from slipping another charge between an uncertain
call and its reconciliation (``credits_remaining`` differences only mean
something when nothing else spent in between).

A job *holds* the turn while its domain row says it is running and its Job
still holds a live lease -- a Worker that died stops holding it when its
lease expires. Checking and taking the turn happen in one transaction under
a PostgreSQL advisory lock on (Workspace, CredentialVersion); SQLite has a
single writer. Each kind of market-data job contributes its running rows to
:func:`turn_holder`: resolves (C-13) and scans, resumes included (C-21).
A scan's key can change between runs (a resume may use a newer version),
so a running scan names the version its current run took the turn with in
``evidence_json.run_credential_version_id``.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.adapters.persistence.models import JobRecord
from app.platform.tenancy import TenantContext

#: Job states that may be executing right now.
_LIVE_JOB_STATES = ("leased", "running")


def _lock(session: Session, ctx: TenantContext, credential_version_id: str) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"market_data_turn:{ctx.workspace_id}:{credential_version_id}".encode()).digest()
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int.from_bytes(digest[:8], "big", signed=True)},
    )


def _running_resolves(session: Session, ctx: TenantContext, credential_version_id: str, now: datetime):
    from app.competitors.models import CompetitorResolutionRecord

    return (
        select(CompetitorResolutionRecord.job_id)
        .join(
            JobRecord,
            (JobRecord.workspace_id == CompetitorResolutionRecord.workspace_id)
            & (JobRecord.id == CompetitorResolutionRecord.job_id),
        )
        .where(
            CompetitorResolutionRecord.workspace_id == ctx.workspace_id,
            CompetitorResolutionRecord.credential_version_id == credential_version_id,
            CompetitorResolutionRecord.status == "running",
            JobRecord.state.in_(_LIVE_JOB_STATES),
            JobRecord.lease_expires_at > now,
        )
    )


def _running_scans(session: Session, ctx: TenantContext, credential_version_id: str, now: datetime):
    from app.competitors.models import CompetitorScanRecord

    rows = session.execute(
        select(CompetitorScanRecord.job_id, CompetitorScanRecord.evidence_json)
        .join(
            JobRecord,
            (JobRecord.workspace_id == CompetitorScanRecord.workspace_id)
            & (JobRecord.id == CompetitorScanRecord.job_id),
        )
        .where(
            CompetitorScanRecord.workspace_id == ctx.workspace_id,
            CompetitorScanRecord.execution_mode == "byok",
            CompetitorScanRecord.status == "running",
            JobRecord.state.in_(_LIVE_JOB_STATES),
            JobRecord.lease_expires_at > now,
        )
    ).all()
    return [
        job_id for job_id, evidence in rows
        if (evidence or {}).get("run_credential_version_id") == credential_version_id
    ]


def turn_holder(
    session: Session, ctx: TenantContext, *, credential_version_id: str, job_id: str,
) -> str | None:
    """Lock the key's turn for this transaction; the Job holding it, if not ``job_id``.

    When this returns ``None`` the caller marks its own row running in the
    same transaction, which is what taking the turn means.
    """
    _lock(session, ctx, credential_version_id)
    now = datetime.now(timezone.utc)
    holders = [
        *session.scalars(_running_resolves(session, ctx, credential_version_id, now)),
        *_running_scans(session, ctx, credential_version_id, now),
    ]
    return next((holder for holder in holders if holder != job_id), None)
