from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import config as app_config
from app.adapters.persistence.database import Base
from app.adapters.persistence import models  # noqa: F401 - registers metadata
from app.entitlements import models as _ent_models  # noqa: F401
from app.entitlements import credentials as _ent_creds  # noqa: F401
from app.entitlements import cost_models as _ent_cost  # noqa: F401
from app.entitlements import usage_models as _ent_usage  # noqa: F401
from app.generation import models as _gen_models  # noqa: F401
from app.meta import models as _meta_models  # noqa: F401
from app.launch import models as _launch_models  # noqa: F401
from app.marketing import models as _marketing_models
from app.marketing import workflow_models as _marketing_workflow_models
from app.marketing import ai_qc_models as _marketing_ai_qc_models
from app.scale import models as _scale_models  # noqa: F401
from app.competitors import models as _competitor_models  # noqa: F401


alembic_config = context.config
alembic_config.set_main_option("sqlalchemy.url", app_config.DATABASE_URL)
if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

target_metadata = Base.metadata

_POSTGRES_ONLY_FOREIGN_KEYS = frozenset(
    {
        "fk_enrr_decision_lineage",
        "fk_enrr_candidate_asset",
        "fk_enrr_qc_attempt",
        "fk_epc_decision_lineage",
    }
)

_POSTGRES_ONLY_CHECK_CONSTRAINTS = frozenset(
    {
        "ck_asset_versions_ck_av_localized_requires_truth_parent",
        "ck_provider_call_attempts_chargeability",
        "ck_provider_call_attempts_chargeability_consistency",
        "ck_provider_call_attempts_truth_required",
    }
)

_POSTGRES_ONLY_INDEXES = frozenset(
    {
        "ix_sad_thread_idempotency",
        "ix_sas_thread_idempotency",
        "ix_sat_active_thread",
        "ix_senr_active_binding",
        "ix_stgt_active_target",
        "ix_scj_ws_mode_identity_hash",
    }
)


def include_object(object_, name, type_, reflected, compare_to):
    del object_, reflected, compare_to
    # PostgreSQL-only owner secret used by the SECURITY DEFINER RLS verifier.
    # It is intentionally absent from the application ORM.
    if type_ == "table" and name == "tenant_context_secrets":
        return False
    # SQLite reflection drops the DESC marker from expression indexes. The
    # migration and ORM definitions agree on PostgreSQL, where the ordering
    # matters; suppress only SQLite's lossy comparison of this known index.
    if (
        app_config.DATABASE_URL.startswith("sqlite")
        and type_ == "index"
        and name == "ix_cvp_asset_confirmed"
    ):
        return False
    # These immutable-lineage constraints are installed only on PostgreSQL.
    # Keep them in ORM metadata so PostgreSQL drift checks can verify them,
    # while suppressing the expected absence from the SQLite compatibility DB.
    if (
        app_config.DATABASE_URL.startswith("sqlite")
        and type_ == "foreign_key_constraint"
        and name in _POSTGRES_ONLY_FOREIGN_KEYS
    ):
        return False
    # These checks and partial indexes are installed with PostgreSQL-specific
    # DDL.  They remain in ORM metadata for production drift checks, while an
    # SQLite compatibility database intentionally has no equivalent objects.
    if app_config.DATABASE_URL.startswith("sqlite"):
        if type_ == "check_constraint" and name in _POSTGRES_ONLY_CHECK_CONSTRAINTS:
            return False
        if type_ == "index" and name in _POSTGRES_ONLY_INDEXES:
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=app_config.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=app_config.DATABASE_URL.startswith("sqlite"),
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = alembic_config.get_section(alembic_config.config_ini_section, {})
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    with connectable.connect() as connection:
        sqlite = app_config.DATABASE_URL.startswith("sqlite")
        if sqlite:
            # Alembic's SQLite batch mode replaces tables. A populated child
            # table prevents its referenced parent from being replaced while
            # immediate FK enforcement is active. This setting is scoped to
            # this migration connection; the complete graph is checked below
            # before the connection is allowed to commit.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
                raise RuntimeError("could not suspend SQLite FK checks for batch migration")
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                render_as_batch=sqlite,
                include_object=include_object,
            )
            with context.begin_transaction():
                context.run_migrations()
            if sqlite:
                violations = connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if violations:
                    raise RuntimeError(
                        f"SQLite foreign-key violations after migration: {violations}"
                    )
        except BaseException:
            if connection.in_transaction():
                connection.rollback()
            raise
        else:
            if connection.in_transaction():
                connection.commit()
            if sqlite:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                    raise RuntimeError("SQLite FK enforcement was not restored")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
