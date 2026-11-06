"""Immutable contextual experiment decisions (Issue #22).

Eight-step migration per approved §4.8:
  1. Preflight assertions (orphan/duplicate checks)
  2. Add nullable columns to existing tables
  3. Run backfills (workspace_id, truth-dependency, QC sequence/evidence)
  4. Set NOT NULL, create parent composite unique anchors
  5. Create 15 new tables in topological order
  6. Deferred FKs, CHECKs, partial uniques on existing tables
  7. Immutability enforcement, RLS, grants, indexes (PostgreSQL only)
  8. Post-migration integrity verification

Revision ID: t1a2b3c4d5e6
Revises: s1a2b3c4d5e6
"""
from __future__ import annotations

import hashlib
import json
import decimal
import datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy import column as sa_col, table as sa_table, text
from sqlalchemy import JSON as SA_JSON

revision = "t1a2b3c4d5e6"
down_revision = "s1a2b3c4d5e6"
branch_labels = None
depends_on = None

_MIGRATION_CANON_VERSION = "p1-migration-v1"


# ---------------------------------------------------------------------------
# §4.7 Migration-local canonical encoder (frozen in this revision)
# ---------------------------------------------------------------------------

def _reject_floats(obj):
    if isinstance(obj, float):
        raise TypeError(f"Binary float not allowed in canonical encoding: {obj!r}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"Non-string dict key: {k!r}")
            _reject_floats(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _reject_floats(v)


def _canonical_decimal_str(d: decimal.Decimal) -> str:
    d = d.normalize()
    if d.is_zero():
        return "0"
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _json_default(o):
    if isinstance(o, decimal.Decimal):
        if not o.is_finite():
            raise ValueError(f"Non-finite Decimal: {o}")
        return _canonical_decimal_str(o)
    if isinstance(o, datetime.datetime):
        if o.tzinfo is None:
            raise ValueError(f"Naive datetime: {o}")
        utc_dt = o.astimezone(datetime.timezone.utc)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    if isinstance(o, tuple):
        return list(o)
    raise TypeError(f"Not serializable: {type(o)}")


def _canonical_json(obj: object) -> str:
    _reject_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=_json_default, allow_nan=False)


def _canonical_bytes(tup: tuple) -> bytes:
    return _canonical_json(list(tup)).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_legacy_numerics(obj):
    if isinstance(obj, float):
        return decimal.Decimal(repr(obj))
    if isinstance(obj, dict):
        return {k: _normalize_legacy_numerics(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_legacy_numerics(v) for v in obj]
    return obj


def _is_pg(conn) -> bool:
    return conn.dialect.name == "postgresql"


# ---------------------------------------------------------------------------
# Step 1: Preflight assertions (§4.1)
# ---------------------------------------------------------------------------

def _step1_preflight(conn):
    orphan_entries = conn.execute(text(
        "SELECT COUNT(*) FROM experiment_entries ee "
        "WHERE NOT EXISTS (SELECT 1 FROM experiments e WHERE e.id = ee.experiment_id)"
    )).scalar()
    if orphan_entries:
        raise RuntimeError(f"Preflight: {orphan_entries} orphan experiment_entries")

    orphan_bindings = conn.execute(text(
        "SELECT COUNT(*) FROM experiment_ad_bindings eab "
        "WHERE NOT EXISTS (SELECT 1 FROM experiment_entries ee WHERE ee.id = eab.experiment_entry_id)"
    )).scalar()
    if orphan_bindings:
        raise RuntimeError(f"Preflight: {orphan_bindings} orphan experiment_ad_bindings")

    orphan_judgements = conn.execute(text(
        "SELECT COUNT(*) FROM judgements j "
        "WHERE NOT EXISTS (SELECT 1 FROM experiments e WHERE e.id = j.experiment_id)"
    )).scalar()
    if orphan_judgements:
        raise RuntimeError(f"Preflight: {orphan_judgements} orphan judgements")

    orphan_decisions = conn.execute(text(
        "SELECT COUNT(*) FROM experiment_decisions ed "
        "WHERE NOT EXISTS (SELECT 1 FROM experiments e WHERE e.id = ed.experiment_id)"
    )).scalar()
    if orphan_decisions:
        raise RuntimeError(f"Preflight: {orphan_decisions} orphan experiment_decisions")

    orphan_state_events = conn.execute(text(
        "SELECT COUNT(*) FROM experiment_state_events ese "
        "WHERE NOT EXISTS (SELECT 1 FROM experiments e WHERE e.id = ese.experiment_id)"
    )).scalar()
    if orphan_state_events:
        raise RuntimeError(f"Preflight: {orphan_state_events} orphan experiment_state_events")


# ---------------------------------------------------------------------------
# Step 2: Add nullable columns (§4.3)
# ---------------------------------------------------------------------------

def _step2_add_columns():
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("product_profile_entry_id", sa.String(64), nullable=True)
        )

    with op.batch_alter_table("experiments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("record_schema_version", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
        )

    with op.batch_alter_table("experiment_entries", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("workspace_id", sa.String(64), nullable=True)
        )

    with op.batch_alter_table("experiment_ad_bindings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("workspace_id", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("experiment_version_candidate_id", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("record_schema_version", sa.Integer(), nullable=True)
        )

    with op.batch_alter_table("judgements", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("workspace_id", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("experiment_version_id", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("record_schema_version", sa.Integer(), nullable=True)
        )

    with op.batch_alter_table("experiment_decisions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("workspace_id", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("experiment_version_id", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("experiment_version_candidate_id", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("proposal_id", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("confirmation_id", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("actor_id", sa.String(100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("context_hash", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("decision_schema_version", sa.Integer(), nullable=True)
        )

    with op.batch_alter_table("experiment_state_events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("workspace_id", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("evidence_json", sa.JSON(), nullable=True)
        )

    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("truth_dependency_expected", sa.Boolean(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("truth_dependency_policy_version", sa.String(40), nullable=True)
        )
        batch_op.add_column(
            sa.Column("truth_dependency_evidence_json", sa.JSON(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("truth_dependency_evidence_hash", sa.String(64), nullable=True)
        )

    with op.batch_alter_table("asset_qc_attempts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("attempt_sequence", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("remediates_attempt_id", sa.String(64), nullable=True)
        )

    with op.batch_alter_table("asset_qc_check_results", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("evidence_hash", sa.String(64), nullable=True)
        )


# ---------------------------------------------------------------------------
# Step 3: Run backfills (§4.4, §4.5, §4.6)
# ---------------------------------------------------------------------------

def _step3_backfills(conn):
    # §4.6: Workspace-id backfill from parent experiments
    _backfill_workspace_id(conn, "experiment_entries", "experiment_id")
    _backfill_workspace_id(conn, "experiment_state_events", "experiment_id")
    _backfill_workspace_id(conn, "judgements", "experiment_id")
    _backfill_workspace_id(conn, "experiment_decisions", "experiment_id")

    # experiment_ad_bindings: workspace_id from grandparent via experiment_entries
    conn.execute(text(
        "UPDATE experiment_ad_bindings SET workspace_id = ("
        "  SELECT e.workspace_id FROM experiment_entries ee"
        "  JOIN experiments e ON e.id = ee.experiment_id"
        "  WHERE ee.id = experiment_ad_bindings.experiment_entry_id"
        ") WHERE workspace_id IS NULL"
    ))

    # §4.4: Truth-dependency backfill
    _backfill_truth_dependency(conn)

    # §4.5: QC attempt sequence
    _backfill_qc_attempt_sequence(conn)

    # §4.5: QC terminal_at
    conn.execute(text(
        "UPDATE asset_qc_attempts SET terminal_at = updated_at "
        "WHERE state IN ('passed', 'failed_blocking', 'failed_nonblocking', 'uncertain') "
        "AND terminal_at IS NULL"
    ))

    # §4.5: QC check evidence hash
    _backfill_qc_evidence_hash(conn)

    # Verify completeness
    _verify_backfill_completeness(conn)


def _backfill_workspace_id(conn, table_name, experiment_col):
    conn.execute(text(
        f"UPDATE {table_name} SET workspace_id = ("
        f"  SELECT e.workspace_id FROM experiments e"
        f"  WHERE e.id = {table_name}.{experiment_col}"
        f") WHERE workspace_id IS NULL"
    ))


def _backfill_truth_dependency(conn):
    _av = sa_table("asset_versions",
        sa_col("id"), sa_col("truth_package_id"),
        sa_col("truth_dependency_expected"), sa_col("truth_dependency_policy_version"),
        sa_col("truth_dependency_evidence_json", SA_JSON),
        sa_col("truth_dependency_evidence_hash"))

    # Pass 1: truth_package_id IS NULL → expected=false
    evidence_obj_no_truth = {
        "migration_reason": "grandfathered_pre_truth_system",
        "original_truth_package_id": None,
    }
    evidence_hash_no_truth = _sha256(_canonical_bytes(
        ("v1-bootstrap", False, None, "grandfathered_pre_truth_system")
    ))
    conn.execute(
        _av.update()
        .where(_av.c.truth_package_id.is_(None))
        .where(_av.c.truth_dependency_expected.is_(None))
        .values(
            truth_dependency_expected=False,
            truth_dependency_policy_version="v1-bootstrap",
            truth_dependency_evidence_json=evidence_obj_no_truth,
            truth_dependency_evidence_hash=evidence_hash_no_truth,
        )
    )

    # Pass 2: truth_package_id IS NOT NULL → expected=true (per-row hash)
    rows = conn.execute(text(
        "SELECT id, truth_package_id FROM asset_versions "
        "WHERE truth_package_id IS NOT NULL AND truth_dependency_expected IS NULL"
    ))
    for row in rows:
        evidence_obj = {
            "migration_reason": "grandfathered_pre_truth_system",
            "original_truth_package_id": row.truth_package_id,
        }
        evidence_hash = _sha256(_canonical_bytes(
            ("v1-bootstrap", True, row.truth_package_id, "grandfathered_pre_truth_system")
        ))
        conn.execute(
            _av.update()
            .where(_av.c.id == row.id)
            .where(_av.c.truth_dependency_expected.is_(None))
            .values(
                truth_dependency_expected=True,
                truth_dependency_policy_version="v1-bootstrap",
                truth_dependency_evidence_json=evidence_obj,
                truth_dependency_evidence_hash=evidence_hash,
            )
        )


def _backfill_qc_attempt_sequence(conn):
    if _is_pg(conn):
        conn.execute(text(
            "WITH sequenced AS ("
            "  SELECT id, ROW_NUMBER() OVER ("
            "    PARTITION BY workspace_id, asset_version_id "
            "    ORDER BY created_at ASC, id ASC"
            "  ) AS seq FROM asset_qc_attempts WHERE attempt_sequence IS NULL"
            ") "
            "UPDATE asset_qc_attempts SET attempt_sequence = sequenced.seq "
            "FROM sequenced WHERE asset_qc_attempts.id = sequenced.id"
        ))
    else:
        rows = conn.execute(text(
            "SELECT id, workspace_id, asset_version_id, created_at "
            "FROM asset_qc_attempts WHERE attempt_sequence IS NULL "
            "ORDER BY workspace_id, asset_version_id, created_at ASC, id ASC"
        )).fetchall()
        counter = {}
        for row in rows:
            key = (row.workspace_id, row.asset_version_id)
            counter[key] = counter.get(key, 0) + 1
            conn.execute(text(
                "UPDATE asset_qc_attempts SET attempt_sequence = :seq WHERE id = :id"
            ), {"seq": counter[key], "id": row.id})


def _backfill_qc_evidence_hash(conn):
    _qc = sa_table("asset_qc_check_results",
        sa_col("id"), sa_col("rule_id"), sa_col("rule_version"),
        sa_col("check_type"), sa_col("severity"), sa_col("outcome"),
        sa_col("evidence_json", SA_JSON), sa_col("evidence_hash"))
    rows = conn.execute(
        _qc.select().where(_qc.c.evidence_hash.is_(None))
    )
    for row in rows:
        evidence_obj = _normalize_legacy_numerics(row.evidence_json)
        h = _sha256(_canonical_bytes((
            row.rule_id, row.rule_version, row.check_type,
            row.severity, row.outcome, evidence_obj,
        )))
        conn.execute(
            _qc.update().where(_qc.c.id == row.id).values(evidence_hash=h)
        )


def _verify_backfill_completeness(conn):
    checks = [
        ("experiment_entries", "workspace_id"),
        ("experiment_state_events", "workspace_id"),
        ("experiment_ad_bindings", "workspace_id"),
        ("judgements", "workspace_id"),
        ("experiment_decisions", "workspace_id"),
        ("asset_versions", "truth_dependency_expected"),
        ("asset_versions", "truth_dependency_policy_version"),
        ("asset_versions", "truth_dependency_evidence_hash"),
    ]
    for table, col in checks:
        count = conn.execute(text(
            f"SELECT COUNT(*) FROM {table} WHERE {col} IS NULL"
        )).scalar()
        if count:
            raise RuntimeError(
                f"Backfill incomplete: {table}.{col} has {count} NULL rows"
            )


# ---------------------------------------------------------------------------
# Step 4: NOT NULL + parent composite unique anchors (§4.8 step 4)
# ---------------------------------------------------------------------------

def _step4_not_null_and_anchors():
    with op.batch_alter_table("experiment_entries", schema=None) as batch_op:
        batch_op.alter_column("workspace_id", existing_type=sa.String(64), nullable=False)
        batch_op.create_unique_constraint(
            "uq_experiment_entries_workspace_id_id", ["workspace_id", "id"]
        )

    with op.batch_alter_table("experiment_state_events", schema=None) as batch_op:
        batch_op.alter_column("workspace_id", existing_type=sa.String(64), nullable=False)
        batch_op.create_unique_constraint(
            "uq_experiment_state_events_workspace_id_id", ["workspace_id", "id"]
        )

    with op.batch_alter_table("experiment_ad_bindings", schema=None) as batch_op:
        batch_op.alter_column("workspace_id", existing_type=sa.String(64), nullable=False)
        batch_op.create_unique_constraint(
            "uq_experiment_ad_bindings_workspace_id_id", ["workspace_id", "id"]
        )

    with op.batch_alter_table("judgements", schema=None) as batch_op:
        batch_op.alter_column("workspace_id", existing_type=sa.String(64), nullable=False)
        batch_op.create_unique_constraint(
            "uq_judgements_workspace_id_id", ["workspace_id", "id"]
        )
        batch_op.create_unique_constraint(
            "uq_judgements_workspace_exp_ver_id",
            ["workspace_id", "experiment_id", "experiment_version_id", "id"],
        )

    with op.batch_alter_table("experiment_decisions", schema=None) as batch_op:
        batch_op.alter_column("workspace_id", existing_type=sa.String(64), nullable=False)
        batch_op.create_unique_constraint(
            "uq_experiment_decisions_workspace_id_id", ["workspace_id", "id"]
        )


# ---------------------------------------------------------------------------
# Step 5: Create new tables in topological order (§4.8 step 5)
# ---------------------------------------------------------------------------

def _step5_create_tables():
    # --- Item 1: experiment_versions ---
    op.create_table(
        "experiment_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_id", sa.String(64), nullable=False),
        sa.Column("canonicalization_version", sa.String(40), nullable=False),
        sa.Column("frozen_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("frozen_snapshot_hash", sa.String(64), nullable=False, index=True),
        sa.Column("metric_contract_hash", sa.String(64), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("placement", sa.String(40), nullable=False),
        sa.Column("landing_page_evidence_hash", sa.String(64), nullable=False),
        sa.Column("audience_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("audience_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("daily_budget_amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("total_budget_cap", sa.Numeric(18, 6), nullable=False),
        sa.Column("budget_currency", sa.String(3), nullable=False),
        sa.Column("schedule_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_timezone", sa.String(100), nullable=False),
        sa.Column("attribution_click_window_days", sa.Integer(), nullable=False),
        sa.Column("attribution_view_window_days", sa.Integer(), nullable=False),
        sa.Column("statistical_plan_json", sa.JSON(), nullable=False),
        sa.Column("statistical_plan_hash", sa.String(64), nullable=False),
        sa.Column("marketing_vars_schema_version", sa.String(40), nullable=False),
        sa.Column("product_version_id", sa.String(64), nullable=False),
        sa.Column("offer_version_id", sa.String(64), nullable=False),
        sa.Column("candidates_hash", sa.String(64), nullable=False),
        sa.Column("concept_resolutions_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_experiment_versions_workspace_id"),
        sa.UniqueConstraint("experiment_id", name="uq_experiment_versions_experiment_id"),
        sa.UniqueConstraint("workspace_id", "experiment_id", "id", name="uq_experiment_versions_workspace_exp_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_experiment_versions_workspace_experiment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "product_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_experiment_versions_product_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_experiment_versions_offer_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("attribution_click_window_days BETWEEN 1 AND 90", name="ck_ev_click_window"),
        sa.CheckConstraint("attribution_view_window_days BETWEEN 1 AND 90", name="ck_ev_view_window"),
        sa.CheckConstraint("daily_budget_amount > 0", name="ck_ev_daily_budget_positive"),
        sa.CheckConstraint("total_budget_cap >= daily_budget_amount", name="ck_ev_total_budget_gte_daily"),
        sa.CheckConstraint("schedule_end_at > schedule_start_at", name="ck_ev_schedule_order"),
    )

    # --- Item 2: experiment_version_candidates ---
    op.create_table(
        "experiment_version_candidates",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_version_id", sa.String(64), nullable=False, index=True),
        sa.Column("source_entry_id", sa.String(64), nullable=True),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_run_item_id", sa.String(64), nullable=True),
        sa.Column("source_evidence_json", sa.JSON(), nullable=True),
        sa.Column("source_evidence_hash", sa.String(64), nullable=True),
        sa.Column("asset_version_id", sa.String(64), nullable=False),
        sa.Column("asset_version_content_sha256", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("marketing_vars_json", sa.JSON(), nullable=False),
        sa.Column("marketing_vars_hash", sa.String(64), nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column("eligibility_evidence_json", sa.JSON(), nullable=False),
        sa.Column("eligibility_evidence_hash", sa.String(64), nullable=False),
        sa.Column("concepts_hash", sa.String(64), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evc_workspace_id"),
        sa.UniqueConstraint("workspace_id", "experiment_version_id", "id", name="uq_evc_workspace_version_id"),
        sa.UniqueConstraint("experiment_version_id", "position", name="uq_evc_version_position"),
        sa.UniqueConstraint("experiment_version_id", "asset_version_id", name="uq_evc_version_asset"),
        sa.UniqueConstraint("workspace_id", "experiment_version_id", "id", "asset_version_id", name="uq_evc_workspace_version_id_asset"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_version_id"],
            ["experiment_versions.workspace_id", "experiment_versions.id"],
            name="fk_evc_workspace_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_evc_workspace_asset_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("source_kind IN ('generation', 'upload', 'reuse', 'manual')", name="ck_evc_source_kind"),
        sa.CheckConstraint("execution_mode IN ('byok', 'sandbox', 'mock')", name="ck_evc_execution_mode"),
    )

    # --- Item 3: experiment_version_candidate_concepts ---
    op.create_table(
        "experiment_version_candidate_concepts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_version_id", sa.String(64), nullable=False),
        sa.Column(
            "experiment_version_candidate_id",
            sa.String(64),
            nullable=False,
            index=True,
        ),
        sa.Column("dimension_key", sa.String(200), nullable=False),
        sa.Column("stable_dimension_signature", sa.String(200), nullable=False),
        sa.Column("concept_key_id", sa.String(64), sa.ForeignKey("dimension_concept_keys.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("concept_key_scope", sa.String(20), nullable=False),
        sa.Column("concept_key_name", sa.String(300), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("duplicate_group_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evcc_workspace_id"),
        sa.UniqueConstraint("experiment_version_candidate_id", "dimension_key", name="uq_evcc_candidate_dimension"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_version_id", "experiment_version_candidate_id"],
            ["experiment_version_candidates.workspace_id", "experiment_version_candidates.experiment_version_id", "experiment_version_candidates.id"],
            name="fk_evcc_workspace_version_candidate",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("concept_key_scope IN ('workspace', 'platform')", name="ck_evcc_scope"),
    )

    # --- Item 4: experiment_version_concept_resolutions ---
    op.create_table(
        "experiment_version_concept_resolutions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_version_id", sa.String(64), nullable=False, index=True),
        sa.Column("duplicate_group_hash", sa.String(64), nullable=False),
        sa.Column("dimension_key", sa.String(200), nullable=False),
        sa.Column("concept_key_id", sa.String(64), sa.ForeignKey("dimension_concept_keys.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_candidate_fingerprints_json", sa.JSON(), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("outcome_evidence_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("authorization_evidence_json", sa.JSON(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evcr_workspace_id"),
        sa.UniqueConstraint("experiment_version_id", "duplicate_group_hash", name="uq_evcr_version_group"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_version_id"],
            ["experiment_versions.workspace_id", "experiment_versions.id"],
            name="fk_evcr_workspace_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "action IN ('allow_controlled_duplicate', 'exclude_source_candidate', 'remap_before_freeze')",
            name="ck_evcr_action",
        ),
    )

    # --- Item 5: decision_proposals ---
    op.create_table(
        "decision_proposals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_id", sa.String(64), nullable=False, index=True),
        sa.Column("judgement_id", sa.String(64), nullable=False, index=True),
        sa.Column("experiment_version_id", sa.String(64), nullable=False),
        sa.Column("eligibility_context_hash", sa.String(64), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("proposed_entries_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_dp_workspace_id"),
        sa.UniqueConstraint("workspace_id", "id", "experiment_id", "experiment_version_id", "judgement_id",
                            name="uq_dp_full_lineage"),
        sa.UniqueConstraint("workspace_id", "judgement_id", "eligibility_context_hash",
                            name="uq_dp_judgement_eligibility"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_dp_workspace_experiment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id", "experiment_version_id"],
            ["experiment_versions.workspace_id", "experiment_versions.experiment_id", "experiment_versions.id"],
            name="fk_dp_workspace_exp_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id", "experiment_version_id", "judgement_id"],
            ["judgements.workspace_id", "judgements.experiment_id", "judgements.experiment_version_id", "judgements.id"],
            name="fk_dp_workspace_exp_ver_judgement",
            ondelete="RESTRICT",
        ),
    )

    # --- Item 6: decision_proposal_state_events ---
    op.create_table(
        "decision_proposal_state_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("proposal_id", sa.String(64), nullable=False, index=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_dpse_workspace_id"),
        sa.UniqueConstraint("workspace_id", "proposal_id", "sequence", name="uq_dpse_proposal_sequence"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["decision_proposals.workspace_id", "decision_proposals.id"],
            name="fk_dpse_workspace_proposal",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "event_type IN ('created', 'consumed', 'invalidated', 'superseded')",
            name="ck_dpse_event_type",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_dpse_sequence_positive"),
    )

    # --- Item 7: decision_confirmations ---
    op.create_table(
        "decision_confirmations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("proposal_id", sa.String(64), nullable=False, index=True),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("expected_proposal_sequence", sa.Integer(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint_hash", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("authorization_epoch", sa.Integer(), nullable=False),
        sa.Column("authorization_evidence_json", sa.JSON(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_dc_workspace_id"),
        sa.UniqueConstraint("workspace_id", "id", "proposal_id", name="uq_dc_workspace_id_proposal"),
        sa.UniqueConstraint("workspace_id", "proposal_id", name="uq_dc_workspace_proposal"),
        sa.UniqueConstraint("workspace_id", "idempotency_key_hash", name="uq_dc_workspace_idempotency"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["decision_proposals.workspace_id", "decision_proposals.id"],
            name="fk_dc_workspace_proposal",
            ondelete="RESTRICT",
        ),
    )

    # --- Item 8: experiment_eligibility_events ---
    op.create_table(
        "experiment_eligibility_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_id", sa.String(64), nullable=False, index=True),
        sa.Column("experiment_version_id", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=True),
        sa.Column("upstream_event_type", sa.String(40), nullable=False),
        sa.Column("upstream_resource_type", sa.String(40), nullable=False),
        sa.Column("upstream_resource_id", sa.String(64), nullable=False),
        sa.Column("upstream_event_id", sa.String(64), nullable=False),
        sa.Column("previous_effective_eligibility", sa.String(20), nullable=False),
        sa.Column("current_effective_eligibility", sa.String(20), nullable=False),
        sa.Column("reversible", sa.Boolean(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_eee_workspace_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id", "experiment_version_id"],
            ["experiment_versions.workspace_id", "experiment_versions.experiment_id", "experiment_versions.id"],
            name="fk_eee_workspace_exp_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "upstream_event_type IN ('review_rejected', 'qc_failed_blocking', "
            "'product_version_withdrawn', 'offer_version_withdrawn', "
            "'product_version_eligibility_changed', 'offer_version_eligibility_changed', "
            "'stale_truth_detected', 'review_re_approved', 'qc_remediated')",
            name="ck_eee_upstream_event_type",
        ),
        sa.CheckConstraint(
            "upstream_resource_type IN ('quality_review', 'asset_qc_attempt', "
            "'library_version', 'truth_package', 'market_asset_binding')",
            name="ck_eee_upstream_resource_type",
        ),
        sa.CheckConstraint(
            "previous_effective_eligibility IN ('eligible', 'ineligible', 'unknown')",
            name="ck_eee_prev_eligibility",
        ),
        sa.CheckConstraint(
            "current_effective_eligibility IN ('eligible', 'ineligible')",
            name="ck_eee_curr_eligibility",
        ),
    )

    # --- Item 9: experiment_eligibility_projections ---
    op.create_table(
        "experiment_eligibility_projections",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_id", sa.String(64), nullable=False, index=True),
        sa.Column("experiment_version_id", sa.String(64), nullable=False),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=True),
        sa.Column("current_eligibility", sa.String(20), nullable=False),
        sa.Column("irreversible_active", sa.Boolean(), nullable=False),
        sa.Column("head_event_id", sa.String(64), nullable=True),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("head_sequence", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_eep_workspace_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id", "experiment_version_id"],
            ["experiment_versions.workspace_id", "experiment_versions.experiment_id", "experiment_versions.id"],
            name="fk_eep_workspace_exp_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("level IN ('version', 'candidate')", name="ck_eep_level"),
        sa.CheckConstraint("current_eligibility IN ('eligible', 'ineligible')", name="ck_eep_eligibility"),
        sa.CheckConstraint(
            "(level = 'version' AND candidate_id IS NULL) OR (level = 'candidate' AND candidate_id IS NOT NULL)",
            name="ck_eep_level_candidate",
        ),
    )

    # --- Item 10: contextual_verdict_projections ---
    op.create_table(
        "contextual_verdict_projections",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_id", sa.String(64), nullable=False, index=True),
        sa.Column("experiment_version_id", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("confirmation_id", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("metric", sa.String(20), nullable=False),
        sa.Column("estimate", sa.Numeric(24, 12), nullable=True),
        sa.Column("asset_version_id", sa.String(64), nullable=False),
        sa.Column("product_version_id", sa.String(64), nullable=False),
        sa.Column("offer_version_id", sa.String(64), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("placement", sa.String(40), nullable=False),
        sa.Column("landing_page_evidence_hash", sa.String(64), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_cvp_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "experiment_id", "asset_version_id", "offer_version_id",
            "market", "placement", "landing_page_evidence_hash",
            name="uq_cvp_current_key",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_cvp_workspace_experiment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_version_id"],
            ["experiment_versions.workspace_id", "experiment_versions.id"],
            name="fk_cvp_workspace_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("verdict IN ('win', 'learn', 'lose')", name="ck_cvp_verdict"),
    )
    op.create_index(
        "ix_cvp_asset_confirmed",
        "contextual_verdict_projections",
        ["workspace_id", "asset_version_id", sa.text("confirmed_at DESC")],
    )

    # --- Item 11: experiment_timeline_heads ---
    op.create_table(
        "experiment_timeline_heads",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_id", sa.String(64), nullable=False),
        sa.Column("head_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("workspace_id", "experiment_id", name="uq_eth_workspace_experiment"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_eth_workspace_experiment",
            ondelete="RESTRICT",
        ),
    )

    # --- Item 12: experiment_timeline_events ---
    op.create_table(
        "experiment_timeline_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_id", sa.String(64), nullable=False, index=True),
        sa.Column("timeline_sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("source_resource_type", sa.String(64), nullable=False),
        sa.Column("source_resource_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_ete_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "experiment_id", "timeline_sequence", name="uq_ete_sequence"),
        sa.UniqueConstraint("workspace_id", "source_resource_type", "source_resource_id", "event_type", name="uq_ete_source_dedup"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_ete_workspace_experiment",
            ondelete="RESTRICT",
        ),
    )

    # --- Item 13: experiment_notification_outbox ---
    op.create_table(
        "experiment_notification_outbox",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_id", sa.String(64), nullable=False, index=True),
        sa.Column("timeline_event_id", sa.String(64), nullable=False),
        sa.Column("notification_type", sa.String(64), nullable=False),
        sa.Column("payload_canonical_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "timeline_event_id", "notification_type", name="uq_eno_event_type"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "timeline_event_id"],
            ["experiment_timeline_events.workspace_id", "experiment_timeline_events.id"],
            name="fk_eno_workspace_timeline_event",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("payload_schema_version IN (1)", name="ck_eno_schema_version"),
    )

    # --- Item 14: experiment_next_round_records ---
    op.create_table(
        "experiment_next_round_records",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_id", sa.String(64), nullable=False, index=True),
        sa.Column("experiment_version_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("source_candidate_id", sa.String(64), nullable=False),
        sa.Column("source_asset_version_id", sa.String(64), nullable=False),
        sa.Column("generation_draft_id", sa.String(64), nullable=False),
        sa.Column("intent_kind", sa.String(32), nullable=False),
        sa.Column("qc_attempt_id", sa.String(64), nullable=True),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("generation_draft_id", name="uq_enrr_draft"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_enrr_workspace_experiment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "generation_draft_id"],
            ["generation_drafts.workspace_id", "generation_drafts.id"],
            name="fk_enrr_workspace_draft",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "intent_kind IN ('same_recipe', 'new_seed', 'edit_prompt', 'quality_feedback')",
            name="ck_enrr_intent_kind",
        ),
        sa.CheckConstraint(
            "(intent_kind = 'quality_feedback' AND qc_attempt_id IS NOT NULL) OR "
            "(intent_kind != 'quality_feedback' AND qc_attempt_id IS NULL)",
            name="ck_enrr_qc_intent",
        ),
    )

    # --- Item 15: experiment_pattern_candidates ---
    op.create_table(
        "experiment_pattern_candidates",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("experiment_id", sa.String(64), nullable=False, index=True),
        sa.Column("experiment_version_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("source_concepts_hash", sa.String(64), nullable=False),
        sa.Column("concept_count", sa.Integer(), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("pattern_evidence_json", sa.JSON(), nullable=False),
        sa.Column("pattern_evidence_hash", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "decision_id", name="uq_epc_workspace_decision"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_epc_workspace_experiment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_version_id", "candidate_id"],
            ["experiment_version_candidates.workspace_id", "experiment_version_candidates.experiment_version_id", "experiment_version_candidates.id"],
            name="fk_epc_workspace_version_candidate",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("concept_count > 0", name="ck_epc_concept_count"),
        sa.CheckConstraint("execution_mode IN ('byok', 'sandbox', 'mock')", name="ck_epc_mode"),
        sa.CheckConstraint("verdict IN ('win', 'learn', 'lose')", name="ck_epc_verdict"),
    )


# ---------------------------------------------------------------------------
# Step 6: Deferred FKs, CHECKs, partial uniques on existing tables (§4.8 step 6)
# ---------------------------------------------------------------------------

def _step6_deferred_constraints(conn):
    # Replace legacy single-column links with tenant-safe composite links now
    # that the existing child tables have a non-null workspace_id.
    with op.batch_alter_table("experiment_entries", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_experiment_entries_experiment_id_experiments",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_experiment_entries_asset_version_id_asset_versions",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_experiment_entries_workspace_experiment_experiments",
            "experiments",
            ["workspace_id", "experiment_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_experiment_entries_workspace_asset_version_asset_versions",
            "asset_versions",
            ["workspace_id", "asset_version_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )

    legacy_ad_binding_fk = next(
        (
            foreign_key["name"]
            for foreign_key in sa.inspect(conn).get_foreign_keys(
                "experiment_ad_bindings"
            )
            if foreign_key["constrained_columns"] == ["experiment_entry_id"]
            and foreign_key["referred_table"] == "experiment_entries"
        ),
        None,
    )
    if not legacy_ad_binding_fk:
        raise RuntimeError(
            "experiment_ad_bindings legacy experiment_entry_id foreign key not found"
        )

    with op.batch_alter_table("experiment_ad_bindings", schema=None) as batch_op:
        batch_op.drop_constraint(
            legacy_ad_binding_fk,
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_experiment_ad_bindings_workspace_entry_experiment_entries",
            "experiment_entries",
            ["workspace_id", "experiment_entry_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("experiment_state_events", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_experiment_state_events_experiment_id_experiments",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_experiment_state_events_workspace_experiment_experiments",
            "experiments",
            ["workspace_id", "experiment_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("judgements", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_judgements_experiment_id_experiments", type_="foreignkey"
        )
        batch_op.create_foreign_key(
            "fk_judgements_workspace_experiment_experiments",
            "experiments",
            ["workspace_id", "experiment_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("experiment_decisions", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_experiment_decisions_experiment_id_experiments",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_experiment_decisions_judgement_id_judgements",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_experiment_decisions_experiment_entry_id_experiment_entries",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_experiment_decisions_workspace_experiment_experiments",
            "experiments",
            ["workspace_id", "experiment_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_experiment_decisions_workspace_judgement_judgements",
            "judgements",
            ["workspace_id", "judgement_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_experiment_decisions_workspace_entry_experiment_entries",
            "experiment_entries",
            ["workspace_id", "experiment_entry_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("launch_previews", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_launch_previews_workspace_entry_experiment_entries",
            "experiment_entries",
            ["workspace_id", "experiment_entry_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )

    # D-S4-01: Additional lineage anchors on existing tables
    with op.batch_alter_table("experiment_decisions", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_ed_workspace_id_exp_ver_cand",
            ["workspace_id", "id", "experiment_id", "experiment_version_id", "experiment_version_candidate_id"],
        )

    with op.batch_alter_table("experiment_version_candidates", schema=None) as batch_op:
        pass  # uq_evc_workspace_version_id_asset already created in step 5

    with op.batch_alter_table("asset_qc_attempts", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_aqc_workspace_id_av", ["workspace_id", "id", "asset_version_id"]
        )

    # Composite lineage FKs for next_round and pattern_candidates
    # These reference the anchors we just created
    if _is_pg(conn):
        conn.execute(text("""
            ALTER TABLE experiment_next_round_records
            ADD CONSTRAINT fk_enrr_decision_lineage
            FOREIGN KEY (workspace_id, decision_id, experiment_id, experiment_version_id, source_candidate_id)
            REFERENCES experiment_decisions (workspace_id, id, experiment_id, experiment_version_id, experiment_version_candidate_id)
            ON DELETE RESTRICT
        """))
        conn.execute(text("""
            ALTER TABLE experiment_next_round_records
            ADD CONSTRAINT fk_enrr_candidate_asset
            FOREIGN KEY (workspace_id, experiment_version_id, source_candidate_id, source_asset_version_id)
            REFERENCES experiment_version_candidates (workspace_id, experiment_version_id, id, asset_version_id)
            ON DELETE RESTRICT
        """))
        conn.execute(text("""
            ALTER TABLE experiment_next_round_records
            ADD CONSTRAINT fk_enrr_qc_attempt
            FOREIGN KEY (workspace_id, qc_attempt_id, source_asset_version_id)
            REFERENCES asset_qc_attempts (workspace_id, id, asset_version_id)
            ON DELETE RESTRICT
            NOT VALID
        """))
        conn.execute(text("""
            ALTER TABLE experiment_pattern_candidates
            ADD CONSTRAINT fk_epc_decision_lineage
            FOREIGN KEY (workspace_id, decision_id, experiment_id, experiment_version_id, candidate_id)
            REFERENCES experiment_decisions (workspace_id, id, experiment_id, experiment_version_id, experiment_version_candidate_id)
            ON DELETE RESTRICT
        """))

    # Experiments: replace frozen_snapshot_hash UNIQUE with INDEX, add schema-version CHECK
    with op.batch_alter_table("experiments", schema=None) as batch_op:
        try:
            batch_op.drop_constraint("uq_experiments_frozen_snapshot_hash", type_="unique")
        except Exception:
            pass  # may not exist on all setups
        batch_op.create_check_constraint(
            "ck_experiments_schema_version",
            "record_schema_version IS NULL OR record_schema_version = 2",
        )

    op.create_index("ix_experiments_frozen_snapshot_hash", "experiments", ["frozen_snapshot_hash"])

    # Products: partial unique for product_profile_entry_id
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_products_workspace_profile",
            ["workspace_id", "product_profile_entry_id"],
        )

    # QC attempts: NOT NULL on attempt_sequence after backfill
    with op.batch_alter_table("asset_qc_attempts", schema=None) as batch_op:
        batch_op.alter_column("attempt_sequence", existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint(
            "uq_aqc_workspace_av_seq", ["workspace_id", "asset_version_id", "attempt_sequence"]
        )

    # QC check results: NOT NULL on evidence_hash after backfill
    with op.batch_alter_table("asset_qc_check_results", schema=None) as batch_op:
        batch_op.alter_column("evidence_hash", existing_type=sa.String(64), nullable=False)


# ---------------------------------------------------------------------------
# Step 7: Immutability enforcement, RLS, grants (§4.8 step 7, PG only)
# ---------------------------------------------------------------------------

NEW_WORKSPACE_OWNED = (
    "experiment_versions", "experiment_version_candidates",
    "experiment_version_candidate_concepts", "experiment_version_concept_resolutions",
    "decision_proposals", "decision_proposal_state_events", "decision_confirmations",
    "experiment_eligibility_events", "experiment_eligibility_projections",
    "contextual_verdict_projections",
    "experiment_timeline_heads", "experiment_timeline_events",
    "experiment_notification_outbox", "experiment_next_round_records",
    "experiment_pattern_candidates",
)

APPEND_ONLY_TABLES = (
    "experiment_versions", "experiment_version_candidates",
    "experiment_version_candidate_concepts", "experiment_version_concept_resolutions",
    "decision_proposals", "decision_proposal_state_events", "decision_confirmations",
    "experiment_eligibility_events",
    "quality_reviews", "experiment_state_events", "judgements", "experiment_decisions",
    "asset_qc_check_results",
    "experiment_timeline_events", "experiment_notification_outbox",
    "experiment_next_round_records", "experiment_pattern_candidates",
)


def _step7_pg_enforcement(conn):
    if not _is_pg(conn):
        return

    # Create shared erasure-aware trigger function
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION marketai_reject_update_or_delete() RETURNS TRIGGER AS $$
        DECLARE
            erasure_ws TEXT;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'UPDATE denied on append-only table %', TG_TABLE_NAME;
            END IF;
            erasure_ws := current_setting('marketai.erasure_workspace_id', true);
            IF erasure_ws IS NOT NULL THEN
                IF NOT pg_has_role(session_user, 'marketai_erasure_principal', 'MEMBER') THEN
                    RAISE EXCEPTION 'DELETE denied: caller is not marketai_erasure_principal on table %', TG_TABLE_NAME;
                END IF;
                IF OLD.workspace_id = erasure_ws THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'DELETE denied: workspace_id does not match erasure target on table %', TG_TABLE_NAME;
            END IF;
            RAISE EXCEPTION 'DELETE denied on append-only table %', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql SET search_path = ''
    """))

    # Replace #21's asset_qc_check_results per-table trigger
    conn.execute(text("DROP TRIGGER IF EXISTS trg_asset_qc_check_results_immutable ON asset_qc_check_results"))
    conn.execute(text("DROP FUNCTION IF EXISTS reject_update_delete_asset_qc_check_results()"))

    # Install append-only triggers
    for table_name in APPEND_ONLY_TABLES:
        _create_append_only_trigger(conn, table_name)

    # Eligibility projections: no-delete trigger (UPDATE allowed for folding)
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION marketai_reject_eligibility_delete() RETURNS TRIGGER AS $$
        DECLARE
            erasure_ws TEXT;
        BEGIN
            erasure_ws := current_setting('marketai.erasure_workspace_id', true);
            IF erasure_ws IS NOT NULL THEN
                IF NOT pg_has_role(session_user, 'marketai_erasure_principal', 'MEMBER') THEN
                    RAISE EXCEPTION 'DELETE denied: caller is not marketai_erasure_principal on table %', TG_TABLE_NAME;
                END IF;
                IF OLD.workspace_id = erasure_ws THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'DELETE denied: workspace_id mismatch on table %', TG_TABLE_NAME;
            END IF;
            RAISE EXCEPTION 'DELETE denied on eligibility projection table %', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql SET search_path = ''
    """))
    _create_trigger_if_absent(conn, "experiment_eligibility_projections",
                              "trg_eep_no_delete", "BEFORE DELETE",
                              "marketai_reject_eligibility_delete")

    # Timeline heads: identity-immutable trigger
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION marketai_reject_timeline_head_identity_change() RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.id != OLD.id OR NEW.workspace_id != OLD.workspace_id
               OR NEW.experiment_id != OLD.experiment_id THEN
                RAISE EXCEPTION 'UPDATE denied: identity columns immutable on experiment_timeline_heads';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = ''
    """))
    _create_trigger_if_absent(conn, "experiment_timeline_heads",
                              "trg_eth_identity_immutable", "BEFORE UPDATE",
                              "marketai_reject_timeline_head_identity_change")

    # Column-specific write-once triggers
    _create_column_write_once_triggers(conn)

    # RLS on all new workspace-owned tables
    for table_name in NEW_WORKSPACE_OWNED:
        conn.execute(text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        conn.execute(text(f"""
            CREATE POLICY tenant_isolation ON {table_name}
            USING (marketai_context_is_current(workspace_id))
            WITH CHECK (marketai_context_is_current(workspace_id))
        """))

    # Role grants
    _apply_grants(conn)


def _create_append_only_trigger(conn, table_name):
    trigger_name = f"trg_{table_name}_append_only"
    exists = conn.execute(text(
        "SELECT 1 FROM pg_trigger "
        "WHERE tgname = :name AND tgrelid = to_regclass(:table)"
    ), {"name": trigger_name, "table": table_name}).scalar()
    if exists:
        return
    conn.execute(text(f"""
        CREATE TRIGGER {trigger_name}
        BEFORE UPDATE OR DELETE ON {table_name}
        FOR EACH ROW EXECUTE FUNCTION marketai_reject_update_or_delete()
    """))


def _create_trigger_if_absent(conn, table_name, trigger_name, timing, func_name):
    exists = conn.execute(text(
        "SELECT 1 FROM pg_trigger "
        "WHERE tgname = :name AND tgrelid = to_regclass(:table)"
    ), {"name": trigger_name, "table": table_name}).scalar()
    if exists:
        return
    conn.execute(text(f"""
        CREATE TRIGGER {trigger_name}
        {timing} ON {table_name}
        FOR EACH ROW EXECUTE FUNCTION {func_name}()
    """))


def _create_column_write_once_triggers(conn):
    # products.product_profile_entry_id set-once
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION trg_products_profile_set_once_fn() RETURNS TRIGGER AS $$
        BEGIN
            IF OLD.product_profile_entry_id IS NOT NULL AND
               (NEW.product_profile_entry_id IS DISTINCT FROM OLD.product_profile_entry_id) THEN
                RAISE EXCEPTION 'product_profile_entry_id is set-once; cannot change after initial assignment';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = ''
    """))
    _create_trigger_if_absent(conn, "products",
                              "trg_products_profile_set_once", "BEFORE UPDATE",
                              "trg_products_profile_set_once_fn")

    # experiments.record_schema_version immutable
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION trg_experiments_schema_version_set_once_fn() RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.record_schema_version IS DISTINCT FROM OLD.record_schema_version THEN
                RAISE EXCEPTION 'record_schema_version is set-once; cannot change after INSERT';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = ''
    """))
    _create_trigger_if_absent(conn, "experiments",
                              "trg_experiments_schema_version_set_once", "BEFORE UPDATE",
                              "trg_experiments_schema_version_set_once_fn")

    # experiments.started_at set-once
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION trg_experiments_started_at_set_once_fn() RETURNS TRIGGER AS $$
        BEGIN
            IF OLD.started_at IS NOT NULL AND
               (NEW.started_at IS DISTINCT FROM OLD.started_at) THEN
                RAISE EXCEPTION 'started_at is set-once; cannot change after initial non-NULL write';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = ''
    """))
    _create_trigger_if_absent(conn, "experiments",
                              "trg_experiments_started_at_set_once", "BEFORE UPDATE",
                              "trg_experiments_started_at_set_once_fn")

    # experiment_ad_bindings.record_schema_version set-once
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION trg_ad_bindings_schema_set_once_fn() RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.record_schema_version IS DISTINCT FROM OLD.record_schema_version THEN
                RAISE EXCEPTION 'record_schema_version is set-once on experiment_ad_bindings';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = ''
    """))
    _create_trigger_if_absent(conn, "experiment_ad_bindings",
                              "trg_ad_bindings_schema_set_once", "BEFORE UPDATE",
                              "trg_ad_bindings_schema_set_once_fn")

    # asset_versions truth-dependency write-once
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION trg_asset_versions_truth_write_once_fn() RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF (NEW.truth_dependency_expected IS NOT NULL OR NEW.truth_dependency_policy_version IS NOT NULL
                    OR NEW.truth_dependency_evidence_json IS NOT NULL OR NEW.truth_dependency_evidence_hash IS NOT NULL) THEN
                    IF NEW.truth_dependency_expected IS NULL OR NEW.truth_dependency_policy_version IS NULL
                       OR NEW.truth_dependency_evidence_hash IS NULL THEN
                        RAISE EXCEPTION 'truth-dependency fields must be all-NULL or all-non-NULL on INSERT';
                    END IF;
                    IF NEW.truth_dependency_policy_version = 'v1-bootstrap' THEN
                        RAISE EXCEPTION 'v1-bootstrap policy_version rejected on post-cutover INSERT';
                    END IF;
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.truth_dependency_expected IS NOT NULL THEN
                IF NEW.truth_dependency_expected IS DISTINCT FROM OLD.truth_dependency_expected
                   OR NEW.truth_dependency_policy_version IS DISTINCT FROM OLD.truth_dependency_policy_version
                   OR NEW.truth_dependency_evidence_hash IS DISTINCT FROM OLD.truth_dependency_evidence_hash THEN
                    RAISE EXCEPTION 'truth-dependency fields are write-once after initialization';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.truth_dependency_expected IS NOT NULL THEN
                IF NEW.truth_dependency_policy_version IS NULL OR NEW.truth_dependency_evidence_hash IS NULL THEN
                    RAISE EXCEPTION 'truth-dependency fields must be initialized atomically';
                END IF;
                IF NEW.truth_dependency_policy_version = 'v1-bootstrap' THEN
                    RAISE EXCEPTION 'v1-bootstrap policy_version rejected on post-cutover initialization';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = ''
    """))
    _create_trigger_if_absent(conn, "asset_versions",
                              "trg_asset_versions_truth_write_once", "BEFORE INSERT OR UPDATE",
                              "trg_asset_versions_truth_write_once_fn")

    # asset_qc_attempts: write-once on attempt_sequence, remediates_attempt_id
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION trg_qc_attempts_seq_write_once_fn() RETURNS TRIGGER AS $$
        BEGIN
            IF OLD.attempt_sequence IS NOT NULL AND
               NEW.attempt_sequence IS DISTINCT FROM OLD.attempt_sequence THEN
                RAISE EXCEPTION 'attempt_sequence is write-once on asset_qc_attempts';
            END IF;
            IF OLD.remediates_attempt_id IS NOT NULL AND
               NEW.remediates_attempt_id IS DISTINCT FROM OLD.remediates_attempt_id THEN
                RAISE EXCEPTION 'remediates_attempt_id is write-once on asset_qc_attempts';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = ''
    """))
    _create_trigger_if_absent(conn, "asset_qc_attempts",
                              "trg_qc_attempts_seq_write_once", "BEFORE UPDATE",
                              "trg_qc_attempts_seq_write_once_fn")


def _apply_grants(conn):
    # Immutable tables: SELECT + INSERT for api and worker
    immutable_tables = (
        "experiment_versions", "experiment_version_candidates",
        "experiment_version_candidate_concepts", "experiment_version_concept_resolutions",
        "decision_proposals", "decision_proposal_state_events", "decision_confirmations",
        "experiment_eligibility_events",
        "experiment_timeline_events", "experiment_notification_outbox",
        "experiment_next_round_records", "experiment_pattern_candidates",
    )
    for t in immutable_tables:
        conn.execute(text(f"GRANT SELECT, INSERT ON {t} TO marketai_domain_api"))
        conn.execute(text(f"GRANT SELECT, INSERT ON {t} TO marketai_domain_worker"))
        conn.execute(text(f"GRANT SELECT ON {t} TO marketai_projection_writer"))
        conn.execute(text(f"GRANT SELECT ON {t} TO marketai_domain_readonly"))

    # Eligibility projections: SELECT + INSERT + UPDATE for api and worker
    conn.execute(text("GRANT SELECT, INSERT, UPDATE ON experiment_eligibility_projections TO marketai_domain_api"))
    conn.execute(text("GRANT SELECT, INSERT, UPDATE ON experiment_eligibility_projections TO marketai_domain_worker"))
    conn.execute(text("GRANT SELECT, UPDATE ON experiment_eligibility_projections TO marketai_projection_writer"))
    conn.execute(text("GRANT SELECT ON experiment_eligibility_projections TO marketai_domain_readonly"))

    # Verdict projections: SELECT + INSERT + UPDATE for api/worker; full DML for projection_writer
    conn.execute(text("GRANT SELECT, INSERT, UPDATE ON contextual_verdict_projections TO marketai_domain_api"))
    conn.execute(text("GRANT SELECT, INSERT, UPDATE ON contextual_verdict_projections TO marketai_domain_worker"))
    conn.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON contextual_verdict_projections TO marketai_projection_writer"))
    conn.execute(text("GRANT SELECT ON contextual_verdict_projections TO marketai_domain_readonly"))

    # Timeline heads: API gets UPDATE (for head_sequence); worker gets SELECT + INSERT only
    conn.execute(text("GRANT SELECT, INSERT, UPDATE ON experiment_timeline_heads TO marketai_domain_api"))
    conn.execute(text("GRANT SELECT, INSERT ON experiment_timeline_heads TO marketai_domain_worker"))
    conn.execute(text("GRANT SELECT ON experiment_timeline_heads TO marketai_projection_writer"))
    conn.execute(text("GRANT SELECT ON experiment_timeline_heads TO marketai_domain_readonly"))


# ---------------------------------------------------------------------------
# Step 8: Post-migration integrity queries (§4.11)
# ---------------------------------------------------------------------------

def _step8_postflight(conn):
    # Verify zero NULL workspace_ids
    for table in ("experiment_entries", "experiment_state_events",
                  "experiment_ad_bindings", "judgements", "experiment_decisions"):
        null_count = conn.execute(text(
            f"SELECT COUNT(*) FROM {table} WHERE workspace_id IS NULL"
        )).scalar()
        if null_count:
            raise RuntimeError(f"Postflight: {table}.workspace_id has {null_count} NULLs")

    # Verify truth-dependency completeness
    null_truth = conn.execute(text(
        "SELECT COUNT(*) FROM asset_versions WHERE truth_dependency_expected IS NULL"
    )).scalar()
    if null_truth:
        raise RuntimeError(f"Postflight: asset_versions.truth_dependency_expected has {null_truth} NULLs")


# ---------------------------------------------------------------------------
# Main upgrade / downgrade
# ---------------------------------------------------------------------------

def upgrade():
    conn = op.get_bind()

    _step1_preflight(conn)
    _step2_add_columns()
    _step3_backfills(conn)
    _step4_not_null_and_anchors()
    _step5_create_tables()
    _step6_deferred_constraints(conn)
    _step7_pg_enforcement(conn)
    _step8_postflight(conn)


def downgrade():
    conn = op.get_bind()

    # Preflight: abort if non-re-derivable data exists
    for table in ("experiment_versions", "experiment_version_candidates",
                  "experiment_version_candidate_concepts",
                  "experiment_version_concept_resolutions",
                  "decision_proposals", "decision_proposal_state_events",
                  "decision_confirmations", "experiment_eligibility_events",
                  "experiment_eligibility_projections",
                  "contextual_verdict_projections",
                  "experiment_next_round_records",
                  "experiment_pattern_candidates"):
        count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        if count:
            raise RuntimeError(
                f"Downgrade blocked: {table} has {count} non-re-derivable rows"
            )

    schema2_count = conn.execute(text(
        "SELECT COUNT(*) FROM experiments WHERE record_schema_version = 2"
    )).scalar()
    if schema2_count:
        raise RuntimeError(f"Downgrade blocked: {schema2_count} schema-2 experiments exist")

    profile_count = conn.execute(text(
        "SELECT COUNT(*) FROM products WHERE product_profile_entry_id IS NOT NULL"
    )).scalar()
    if profile_count:
        raise RuntimeError(f"Downgrade blocked: {profile_count} products have profile links")

    if _is_pg(conn):
        # Drop triggers and functions
        for table_name in APPEND_ONLY_TABLES:
            conn.execute(text(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only ON {table_name}"))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_eep_no_delete ON experiment_eligibility_projections"))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_eth_identity_immutable ON experiment_timeline_heads"))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_products_profile_set_once ON products"))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_experiments_schema_version_set_once ON experiments"))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_experiments_started_at_set_once ON experiments"))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_ad_bindings_schema_set_once ON experiment_ad_bindings"))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_asset_versions_truth_write_once ON asset_versions"))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_qc_attempts_seq_write_once ON asset_qc_attempts"))

        # Drop RLS policies
        for table_name in NEW_WORKSPACE_OWNED:
            conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))

        # Revoke grants
        for table_name in NEW_WORKSPACE_OWNED:
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_domain_api"))
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_domain_worker"))
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_projection_writer"))
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_domain_readonly"))

        # Drop functions
        conn.execute(text("DROP FUNCTION IF EXISTS marketai_reject_update_or_delete() CASCADE"))
        conn.execute(text("DROP FUNCTION IF EXISTS marketai_reject_eligibility_delete() CASCADE"))
        conn.execute(text("DROP FUNCTION IF EXISTS marketai_reject_timeline_head_identity_change() CASCADE"))
        conn.execute(text("DROP FUNCTION IF EXISTS trg_products_profile_set_once_fn() CASCADE"))
        conn.execute(text("DROP FUNCTION IF EXISTS trg_experiments_schema_version_set_once_fn() CASCADE"))
        conn.execute(text("DROP FUNCTION IF EXISTS trg_experiments_started_at_set_once_fn() CASCADE"))
        conn.execute(text("DROP FUNCTION IF EXISTS trg_ad_bindings_schema_set_once_fn() CASCADE"))
        conn.execute(text("DROP FUNCTION IF EXISTS trg_asset_versions_truth_write_once_fn() CASCADE"))
        conn.execute(text("DROP FUNCTION IF EXISTS trg_qc_attempts_seq_write_once_fn() CASCADE"))

        # Restore #21 per-table trigger for asset_qc_check_results
        conn.execute(text("""
            CREATE FUNCTION reject_update_delete_asset_qc_check_results() RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'UPDATE/DELETE denied on asset_qc_check_results';
            END;
            $$ LANGUAGE plpgsql
        """))
        conn.execute(text("""
            CREATE TRIGGER trg_asset_qc_check_results_immutable
            BEFORE UPDATE OR DELETE ON asset_qc_check_results
            FOR EACH ROW EXECUTE FUNCTION reject_update_delete_asset_qc_check_results()
        """))

    # Drop tables in leaf→root order
    for table in (
        "experiment_pattern_candidates",
        "experiment_next_round_records",
        "experiment_notification_outbox",
        "experiment_timeline_events",
        "experiment_timeline_heads",
        "contextual_verdict_projections",
        "experiment_eligibility_projections",
        "experiment_eligibility_events",
        "decision_confirmations",
        "decision_proposal_state_events",
        "decision_proposals",
        "experiment_version_concept_resolutions",
        "experiment_version_candidate_concepts",
        "experiment_version_candidates",
        "experiment_versions",
    ):
        op.drop_table(table)

    # Drop deferred constraints
    op.drop_index("ix_experiments_frozen_snapshot_hash", table_name="experiments")

    with op.batch_alter_table("launch_previews", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_launch_previews_workspace_entry_experiment_entries",
            type_="foreignkey",
        )

    with op.batch_alter_table("asset_qc_attempts", schema=None) as batch_op:
        batch_op.drop_constraint("uq_aqc_workspace_id_av", type_="unique")
        batch_op.drop_constraint("uq_aqc_workspace_av_seq", type_="unique")
        batch_op.alter_column("attempt_sequence", existing_type=sa.Integer(), nullable=True)

    with op.batch_alter_table("asset_qc_check_results", schema=None) as batch_op:
        batch_op.alter_column("evidence_hash", existing_type=sa.String(64), nullable=True)

    with op.batch_alter_table("experiment_decisions", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_experiment_decisions_workspace_experiment_experiments",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_experiment_decisions_workspace_judgement_judgements",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_experiment_decisions_workspace_entry_experiment_entries",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_experiment_decisions_experiment_id_experiments",
            "experiments",
            ["experiment_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_experiment_decisions_judgement_id_judgements",
            "judgements",
            ["judgement_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_experiment_decisions_experiment_entry_id_experiment_entries",
            "experiment_entries",
            ["experiment_entry_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_constraint("uq_ed_workspace_id_exp_ver_cand", type_="unique")
        batch_op.drop_constraint("uq_experiment_decisions_workspace_id_id", type_="unique")
        batch_op.alter_column("workspace_id", existing_type=sa.String(64), nullable=True)

    with op.batch_alter_table("judgements", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_judgements_workspace_experiment_experiments",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_judgements_experiment_id_experiments",
            "experiments",
            ["experiment_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_constraint("uq_judgements_workspace_exp_ver_id", type_="unique")
        batch_op.drop_constraint("uq_judgements_workspace_id_id", type_="unique")
        batch_op.alter_column("workspace_id", existing_type=sa.String(64), nullable=True)

    with op.batch_alter_table("experiment_ad_bindings", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_experiment_ad_bindings_workspace_entry_experiment_entries",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_experiment_ad_bindings_entry_experiment_entries",
            "experiment_entries",
            ["experiment_entry_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_constraint("uq_experiment_ad_bindings_workspace_id_id", type_="unique")
        batch_op.alter_column("workspace_id", existing_type=sa.String(64), nullable=True)

    with op.batch_alter_table("experiment_state_events", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_experiment_state_events_workspace_experiment_experiments",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_experiment_state_events_experiment_id_experiments",
            "experiments",
            ["experiment_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_constraint("uq_experiment_state_events_workspace_id_id", type_="unique")
        batch_op.alter_column("workspace_id", existing_type=sa.String(64), nullable=True)

    with op.batch_alter_table("experiment_entries", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_experiment_entries_workspace_experiment_experiments",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_experiment_entries_workspace_asset_version_asset_versions",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_experiment_entries_experiment_id_experiments",
            "experiments",
            ["experiment_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_experiment_entries_asset_version_id_asset_versions",
            "asset_versions",
            ["asset_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.drop_constraint("uq_experiment_entries_workspace_id_id", type_="unique")
        batch_op.alter_column("workspace_id", existing_type=sa.String(64), nullable=True)

    with op.batch_alter_table("experiments", schema=None) as batch_op:
        batch_op.drop_constraint("ck_experiments_schema_version", type_="check")

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_constraint("uq_products_workspace_profile", type_="unique")

    # Drop added columns
    with op.batch_alter_table("asset_qc_check_results", schema=None) as batch_op:
        batch_op.drop_column("evidence_hash")

    with op.batch_alter_table("asset_qc_attempts", schema=None) as batch_op:
        batch_op.drop_column("remediates_attempt_id")
        batch_op.drop_column("terminal_at")
        batch_op.drop_column("attempt_sequence")

    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.drop_column("truth_dependency_evidence_hash")
        batch_op.drop_column("truth_dependency_evidence_json")
        batch_op.drop_column("truth_dependency_policy_version")
        batch_op.drop_column("truth_dependency_expected")

    with op.batch_alter_table("experiment_state_events", schema=None) as batch_op:
        batch_op.drop_column("evidence_json")
        batch_op.drop_column("workspace_id")

    with op.batch_alter_table("experiment_decisions", schema=None) as batch_op:
        batch_op.drop_column("decision_schema_version")
        batch_op.drop_column("context_hash")
        batch_op.drop_column("confirmed_at")
        batch_op.drop_column("actor_id")
        batch_op.drop_column("confirmation_id")
        batch_op.drop_column("proposal_id")
        batch_op.drop_column("experiment_version_candidate_id")
        batch_op.drop_column("experiment_version_id")
        batch_op.drop_column("workspace_id")

    with op.batch_alter_table("judgements", schema=None) as batch_op:
        batch_op.drop_column("record_schema_version")
        batch_op.drop_column("experiment_version_id")
        batch_op.drop_column("workspace_id")

    with op.batch_alter_table("experiment_ad_bindings", schema=None) as batch_op:
        batch_op.drop_column("record_schema_version")
        batch_op.drop_column("experiment_version_candidate_id")
        batch_op.drop_column("workspace_id")

    with op.batch_alter_table("experiment_entries", schema=None) as batch_op:
        batch_op.drop_column("workspace_id")

    with op.batch_alter_table("experiments", schema=None) as batch_op:
        batch_op.drop_column("started_at")
        batch_op.drop_column("record_schema_version")

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_column("product_profile_entry_id")
