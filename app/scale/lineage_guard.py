"""Persistent per-Winner/DeliveryTarget lineage guard (spec §7.3, issue #86
Task 8).

``get_or_create_lineage_guard()`` is the only writer of new guard rows: an
``INSERT ... ON CONFLICT (workspace_id, meta_mode, winner_decision_id,
delivery_target_id) DO NOTHING`` (dialect-adapted, same two-Adapter shape as
``app.scale.command_journal.claim_command``), immediately followed by a
``SELECT ... FOR UPDATE`` that returns the row locked -- whether this call
created it or a concurrent/earlier call did. ``FOR UPDATE`` is a no-op on
SQLite (spec §7.2's same fallback note); the bounded local/test path relies
on SQLite's serialized single-writer behavior instead.

Lock ordering (spec §7.4): the guard is always the *second* lock a Scale
command takes, right after the command journal claim and before any
Candidate/Enrollment/Preview/Episode row.

This module only creates and locks the guard; it does not itself validate
lineage. The caller (Task 9's ``lock_and_validate_enrollment_lineage``) is
what discards any preliminary identity resolution done before the lock and
authoritatively re-validates every field while holding it. On PostgreSQL,
``revision`` is incremented out-of-band by the ``marketai_bump_scale_
lineage_guards()`` trigger installed by this table's migration, whenever a
covered upstream row (Winner decision/confirmation/eligibility, Quality
Review, Product/Offer lifecycle, MarketAssetBinding, MetaConnection,
contract-generation retirement, evidence attestation) changes -- so a Scale
command that locked the guard first orders its COMMIT before any such
mutation's trigger can run.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.platform.tenancy import TenantContext, is_postgres
from .models import ScaleLineageGuardRecord, new_id

__all__ = ["get_or_create_lineage_guard"]

#: Mirrors ``app.scale.command_journal.NO_SCENARIO_MARKER``.
NO_SCENARIO_MARKER = "__no_scenario__"

_GUARD_CONFLICT_COLUMNS = (
    "workspace_id", "meta_mode", "winner_decision_id", "delivery_target_id",
)


def get_or_create_lineage_guard(
    session: Session,
    context: TenantContext,
    *,
    winner_decision_id: str,
    winner_confirmation_id: str,
    asset_version_id: str,
    product_version_id: str,
    offer_version_id: str,
    market_asset_binding_id: str,
    delivery_target_id: str,
    meta_connection_id: str,
    contract_generation_id: str,
    scenario_scope_key: str | None = None,
) -> ScaleLineageGuardRecord:
    """Create (if absent) and return the locked guard for this exact
    Winner/DeliveryTarget pair.

    ``scenario_scope_key`` defaults to the reserved no-scenario marker for
    every mode except ``controlled_mock``, which must supply one explicitly
    (mirrors ``app.scale.command_journal.claim_command``). When the guard
    already exists, every identity field this call was given is ignored --
    the persisted row is authoritative and is returned unchanged except for
    the lock itself; a caller that suspects drift must re-validate the
    returned row's fields against fresh upstream reads, not overwrite them.
    """
    if scenario_scope_key is None:
        if context.meta_mode == "controlled_mock":
            raise ValueError(
                "scenario_scope_key is required when meta_mode='controlled_mock'"
            )
        scenario_scope_key = NO_SCENARIO_MARKER

    now = datetime.now(timezone.utc)
    values = dict(
        id=new_id("slg"),
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        scenario_scope_key=scenario_scope_key,
        winner_decision_id=winner_decision_id,
        winner_confirmation_id=winner_confirmation_id,
        asset_version_id=asset_version_id,
        product_version_id=product_version_id,
        offer_version_id=offer_version_id,
        market_asset_binding_id=market_asset_binding_id,
        delivery_target_id=delivery_target_id,
        meta_connection_id=meta_connection_id,
        contract_generation_id=contract_generation_id,
        revision=1,
        updated_at=now,
        created_at=now,
    )

    dialect_module = postgresql if is_postgres(session) else sqlite
    stmt = (
        dialect_module.insert(ScaleLineageGuardRecord)
        .values(**values)
        .on_conflict_do_nothing(index_elements=list(_GUARD_CONFLICT_COLUMNS))
    )
    session.execute(stmt)

    guard = session.execute(
        select(ScaleLineageGuardRecord)
        .filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            winner_decision_id=winner_decision_id,
            delivery_target_id=delivery_target_id,
        )
        .with_for_update()
    ).scalar_one()
    return guard
