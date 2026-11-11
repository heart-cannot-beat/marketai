"""DeliveryTargets name the Landing Page by its frozen evidence hash (#129).

Revision ID: zzh129a1b2c3
Revises: zzg126a1b2c3

The Landing Page belongs to the OfferVersion (creative-library contract
§5.2); an ExperimentVersion freezes it as ``landing_page_evidence_hash``. No
Landing Page version entity exists, yet ``scale_delivery_targets`` referenced
one through ``landing_page_version_id -> library_versions``, so the seeds
had to create a ``landing_page`` LibraryVersion to stand in for it. The
maintainer decided on #129 that the lineage gate compares the Winner's frozen
hash with the DeliveryTarget's, for legacy and marketing-handoff Winners
alike.

Upgrade, on ``scale_delivery_targets``:

* drops ``fk_sdt_landing_page_version`` and the two unique constraints that
  name the column (``uq_sdt_exact_context``, ``uq_sdt_enrollment_binding``);
* renames ``landing_page_version_id`` to ``landing_page_evidence_hash``;
* backfills every row: a ``landing_page`` LibraryVersion yields the hash in
  its content (the seeds keyed it by that hash); an ``offer`` LibraryVersion
  (marketing-handoff Winners named the OfferVersion) yields the hash the
  Winner's ExperimentVersion froze, found through the target's candidates,
  lineage guards and Enrollments when they name exactly one; a value with no
  LibraryVersion is already a hash and stays; any other row keeps its value
  and is counted in the migration log. The lineage gate then fails closed for
  it, because no Winner froze that value;
* recreates the two unique constraints on the new column.

A fresh install builds the table from the current model
(``ab1_scale_lineage_tables``), so only the old shape is rewritten. Upgrade
also adds ``create_delivery_target`` to ``ck_scj_command_type``, which
``ad2_scale_target_preview_and_command_identity`` narrows on every install.

The ``landing_page_version_id`` columns of Enrollments, Episodes and delivery
configuration sources keep their name (maintainer decision on #129) and now
hold the hash too; they carry no foreign key.

No grant changes: the API already reads every table the new DeliveryTarget
entry reads, and writes the contract sets it creates
(``deploy/postgres/domain-tenant-roles.sql``). On PostgreSQL the backfill
runs with ``row_security`` off, like every revision run by the schema owner.

Downgrade restores the column name and the constraints, so a release can
always be rolled back. The command-type check stays widened while the
append-only journal holds a ``create_delivery_target`` row. A target made after the upgrade names a hash no
LibraryVersion has; while one exists, PostgreSQL gets the foreign key back as
``NOT VALID`` (checked for new rows only) and SQLite goes without it, and the
migration log says how many rows are the reason.
"""
from __future__ import annotations

import json
import logging
from collections import Counter

import sqlalchemy as sa
from alembic import op


revision = "zzh129a1b2c3"
down_revision = "zzg126a1b2c3"
branch_labels = None
depends_on = None

_log = logging.getLogger("alembic.runtime.migration")

_TABLE = "scale_delivery_targets"
_OLD = "landing_page_version_id"
_NEW = "landing_page_evidence_hash"
_FK = "fk_sdt_landing_page_version"

_EXACT_CONTEXT = (
    "workspace_id", "meta_mode", "id", "meta_connection_id",
    "ad_account_id", "scenario_scope_key", "campaign_remote_id",
    "adset_remote_id", "ad_remote_id", "ad_creative_id",
    "creative_asset_version_id", "market_code", "placement",
    "attribution_setting", "offer_version_id",
    "{lp}", "target_fingerprint",
)
_ENROLLMENT_BINDING = (
    "workspace_id", "meta_mode", "id", "meta_connection_id",
    "ad_account_id", "scenario_scope_key", "campaign_remote_id",
    "adset_remote_id", "ad_remote_id", "creative_asset_version_id",
    "market_code", "offer_version_id", "{lp}",
    "target_fingerprint",
)
_UNIQUES = {
    "uq_sdt_exact_context": _EXACT_CONTEXT,
    "uq_sdt_enrollment_binding": _ENROLLMENT_BINDING,
}

#: The short logical name; batch operations apply the ``ck_%(table_name)s_``
#: naming convention (see ``ad2_scale_target_preview_and_command_identity``).
_COMMAND_TYPE_CHECK = "ck_scj_command_type"
_COMMAND_TYPES_BEFORE = (
    "create_candidate", "create_enrollment", "create_preview",
    "activate_enrollment", "resume_enrollment",
    "create_target_publication_preview", "publish_scale_target",
    "create_target_withdrawal_preview", "withdraw_scale_target",
    "create_scale_export",
)
_COMMAND_TYPES_AFTER = _COMMAND_TYPES_BEFORE + ("create_delivery_target",)

#: Tables that tie a DeliveryTarget to the Winner Decision it was enrolled for.
_DECISION_LINKS = (
    ("scale_enrollment_candidates", "winner_decision_id"),
    ("scale_lineage_guards", "winner_decision_id"),
    ("scale_enrollments", "winner_decision_id"),
)


def _unique_columns(name: str, column: str) -> list[str]:
    return [column if part == "{lp}" else part for part in _UNIQUES[name]]


def _column_names() -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _set_command_types(command_types: tuple[str, ...]) -> None:
    with op.batch_alter_table("scale_command_journal", schema=None) as batch_op:
        batch_op.drop_constraint(_COMMAND_TYPE_CHECK, type_="check")
        batch_op.create_check_constraint(
            _COMMAND_TYPE_CHECK,
            "command_type IN ('" + "', '".join(command_types) + "')",
        )


def _content(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _winner_hashes(conn, workspace_id: str, target_id: str) -> set[str]:
    decisions: set[str] = set()
    for table, column in _DECISION_LINKS:
        decisions.update(conn.execute(
            sa.text(f'SELECT "{column}" FROM "{table}" '
                    "WHERE workspace_id = :workspace_id AND delivery_target_id = :target_id"),
            {"workspace_id": workspace_id, "target_id": target_id},
        ).scalars())
    hashes: set[str] = set()
    for decision_id in sorted(d for d in decisions if d):
        hashes.update(conn.execute(
            sa.text(
                "SELECT v.landing_page_evidence_hash FROM experiment_decisions d "
                "JOIN experiment_versions v "
                "ON v.workspace_id = d.workspace_id AND v.id = d.experiment_version_id "
                "WHERE d.workspace_id = :workspace_id AND d.id = :decision_id"
            ),
            {"workspace_id": workspace_id, "decision_id": decision_id},
        ).scalars())
    return {value for value in hashes if value}


def _backfill(conn) -> Counter:
    outcomes: Counter = Counter()
    rows = conn.execute(sa.text(f'SELECT id, workspace_id, "{_NEW}" AS value FROM "{_TABLE}"')).all()
    for row in rows:
        version = conn.execute(
            sa.text("SELECT entry_type, content_json FROM library_versions "
                    "WHERE workspace_id = :workspace_id AND id = :id"),
            {"workspace_id": row.workspace_id, "id": row.value},
        ).first()
        resolved = None
        if version is not None and version.entry_type == "landing_page":
            resolved = _content(version.content_json).get("landing_page_evidence_hash") or None
            outcome = "landing_page_version"
        elif version is not None and version.entry_type == "offer":
            hashes = _winner_hashes(conn, row.workspace_id, row.id)
            resolved = next(iter(hashes)) if len(hashes) == 1 else None
            outcome = "offer_version"
        elif version is None:
            # Already a hash: a target made after this revision, kept by a
            # downgrade that left no LibraryVersion behind it.
            outcomes["hash_unchanged"] += 1
            continue
        else:
            outcome = "other"
        if not isinstance(resolved, str) or not resolved:
            outcomes[f"{outcome}_kept"] += 1
            continue
        outcomes[f"{outcome}_resolved"] += 1
        if resolved != row.value:
            conn.execute(
                sa.text(f'UPDATE "{_TABLE}" SET "{_NEW}" = :value WHERE id = :id'),
                {"value": resolved, "id": row.id},
            )
    return outcomes


def upgrade() -> None:
    conn = op.get_bind()
    if _OLD in _column_names():
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.drop_constraint(_FK, type_="foreignkey")
            for name in _UNIQUES:
                batch_op.drop_constraint(name, type_="unique")
            batch_op.alter_column(
                _OLD, new_column_name=_NEW,
                existing_type=sa.String(64), existing_nullable=False,
            )
        if conn.dialect.name == "postgresql":
            conn.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
        outcomes = _backfill(conn)
        _log.info(
            "scale_delivery_targets landing page backfill: %s",
            ", ".join(f"{key}={count}" for key, count in sorted(outcomes.items())) or "no rows",
        )
        kept = sum(count for key, count in outcomes.items() if key.endswith("_kept"))
        if kept:
            _log.warning(
                "%d DeliveryTarget(s) keep a landing page value no Winner froze; "
                "the lineage gate refuses them", kept,
            )
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            for name in _UNIQUES:
                batch_op.create_unique_constraint(name, _unique_columns(name, _NEW))
    _set_command_types(_COMMAND_TYPES_AFTER)


def downgrade() -> None:
    conn = op.get_bind()
    postgres = conn.dialect.name == "postgresql"
    if postgres:
        conn.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    journaled = conn.execute(sa.text(
        "SELECT count(*) FROM scale_command_journal WHERE command_type = 'create_delivery_target'"
    )).scalar_one()
    if journaled:
        # The journal is append-only; the earlier code never writes this type.
        _log.warning(
            "%d journaled create_delivery_target command(s) keep ck_scj_command_type widened", journaled,
        )
    else:
        _set_command_types(_COMMAND_TYPES_BEFORE)
    if _NEW not in _column_names():
        return
    orphans = conn.execute(sa.text(
        f'SELECT count(*) FROM "{_TABLE}" t WHERE NOT EXISTS ('
        "SELECT 1 FROM library_versions v "
        f'WHERE v.workspace_id = t.workspace_id AND v.id = t."{_NEW}")'
    )).scalar_one()
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        for name in _UNIQUES:
            batch_op.drop_constraint(name, type_="unique")
        batch_op.alter_column(
            _NEW, new_column_name=_OLD,
            existing_type=sa.String(64), existing_nullable=False,
        )
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        for name in _UNIQUES:
            batch_op.create_unique_constraint(name, _unique_columns(name, _OLD))
        if not orphans:
            batch_op.create_foreign_key(
                _FK, "library_versions",
                ["workspace_id", _OLD], ["workspace_id", "id"],
                ondelete="RESTRICT",
            )
    if not orphans:
        return
    if postgres:
        op.execute(
            f'ALTER TABLE "{_TABLE}" ADD CONSTRAINT "{_FK}" '
            f'FOREIGN KEY (workspace_id, "{_OLD}") '
            "REFERENCES library_versions (workspace_id, id) ON DELETE RESTRICT NOT VALID"
        )
    _log.warning(
        "%d DeliveryTarget(s) name a landing page evidence hash with no LibraryVersion; "
        "%s", orphans,
        "the foreign key is back NOT VALID (new rows only)" if postgres
        else "the foreign key stays off while they exist",
    )
