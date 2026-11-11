"""Publish the Beta default ScalePolicyVersion (#127; #63 decision 2).

Revision ID: zzg7d8e9f0a1
Revises: zzf6b7c8d9e0

Until now only tests and E2E seeds created a ``scale_policy_versions`` row,
so a live Scale evaluation in production found no Policy and only recorded
a ``no_eligible_policy`` audit (``app.scale.live_trigger``). Contract §10.1
has the platform publish immutable, platform-global versions. This revision
publishes the first one, ``BETA_DEFAULT_POLICY_V1`` from
``app.scale.platform_policy``: every column, ``is_published`` and a fixed
``published_at``. The values and their sources are in that module and on
#127.

Upgrade inserts the row unless its id is already there, so running it again
adds nothing. It runs as the schema owner, like every revision. It grants
nothing: the API and the Worker keep their ``scale_policy_versions``
privileges (``deploy/postgres/domain-tenant-roles.sql`` and
``tests/support/grant_matrix.py`` are unchanged). The release order stays:
roles script, Alembic, roles script (``make db-release``).

Downgrade deletes the row only when no snapshot, evaluation run or
Evaluation names it; a referenced version stays published, because those
records replay under it (§11). On PostgreSQL the check runs with
``row_security`` off, so a role that RLS would filter fails instead of
seeing no reference; the owner that runs Alembic is ``BYPASSRLS``.

A later Policy change adds a new constant and a new revision. This one is
never edited.
"""

import logging

import sqlalchemy as sa
from alembic import op

from app.scale.platform_policy import BETA_DEFAULT_POLICY_V1


revision = "zzg7d8e9f0a1"
down_revision = "zzf6b7c8d9e0"
branch_labels = None
depends_on = None

_log = logging.getLogger("alembic.runtime.migration")

_POLICY = BETA_DEFAULT_POLICY_V1

# The table as f5eeeab4e9b9 created it; the ORM model may move on.
_policy_versions = sa.table(
    "scale_policy_versions",
    sa.column("id", sa.String(64)),
    sa.column("algorithm_version", sa.String(20)),
    sa.column("canonical_schema_version", sa.String(20)),
    sa.column("decimal_precision", sa.Integer()),
    sa.column("decimal_rounding", sa.String(30)),
    sa.column("z_value", sa.Numeric(12, 8)),
    sa.column("evidence_floor_minimum_spend", sa.Numeric(24, 12)),
    sa.column("evidence_floor_minimum_days", sa.Integer()),
    sa.column("evidence_floor_minimum_conversions", sa.Integer()),
    sa.column("activity_floor_minimum_impressions", sa.Integer()),
    sa.column("sample_floor_minimum_clicks", sa.Integer()),
    sa.column("spend_floor_minimum_daily_spend", sa.Numeric(24, 12)),
    sa.column("freshness_floor_maximum_lag_hours", sa.Integer()),
    sa.column("signal_thresholds", sa.JSON()),
    sa.column("streak_rules", sa.JSON()),
    sa.column("bootstrap_prng_algorithm", sa.String(40)),
    sa.column("bootstrap_draw_count", sa.Integer()),
    sa.column("bootstrap_valid_draw_ratio", sa.Numeric(6, 5)),
    sa.column("bootstrap_percentile_method", sa.String(30)),
    sa.column("bootstrap_draw_order", sa.String(20)),
    sa.column("breakdown_families", sa.JSON()),
    sa.column("breakdown_sample_gate_minimum", sa.Integer()),
    sa.column("breakdown_share_gate_minimum_ratio", sa.Numeric(6, 5)),
    sa.column("bh_fdr_alpha", sa.Numeric(6, 5)),
    sa.column("advice_severity_thresholds", sa.JSON()),
    sa.column("notification_cooldown_hours", sa.Integer()),
    sa.column("conversion_safe_values", sa.JSON()),
    sa.column("mode_permissions", sa.JSON()),
    sa.column("is_published", sa.Boolean()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("created_by", sa.String(100)),
    sa.column("published_at", sa.DateTime(timezone=True)),
)

# Every column that freezes a Policy id into a record that replays under it.
_REFERENCES = (
    ("scale_evidence_snapshots", "policy_version_id"),
    ("scale_evidence_snapshots", "prior_streak_compat_policy_version_id"),
    ("scale_evaluation_runs", "policy_version_id"),
    ("scale_evaluations", "policy_version_id"),
    ("scale_evaluations", "streak_compat_policy_version_id"),
)


def _published(conn) -> bool:
    return conn.execute(
        sa.select(_policy_versions.c.id).where(_policy_versions.c.id == _POLICY.id)
    ).first() is not None


def upgrade() -> None:
    conn = op.get_bind()
    if _published(conn):
        _log.info("ScalePolicyVersion %s is already published", _POLICY.id)
        return
    conn.execute(_policy_versions.insert().values(**_POLICY.row()))


def _references(conn) -> list[str]:
    found = []
    for table, column in _REFERENCES:
        referenced = conn.execute(
            sa.text(f'SELECT 1 FROM "{table}" WHERE "{column}" = :policy_id LIMIT 1'),
            {"policy_id": _POLICY.id},
        ).first()
        if referenced is not None:
            found.append(f"{table}.{column}")
    return found


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    references = _references(conn)
    if references:
        _log.warning(
            "ScalePolicyVersion %s stays published: %s name it",
            _POLICY.id, ", ".join(references),
        )
        return
    conn.execute(_policy_versions.delete().where(_policy_versions.c.id == _POLICY.id))
