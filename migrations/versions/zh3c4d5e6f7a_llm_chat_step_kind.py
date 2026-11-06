"""Admit the governed ``llm_chat`` call semantic on provider call attempts.

Revision ID: zh3c4d5e6f7a
Revises: zg2b3c4d5e6f

Issue #160 routes every LLM call (product intake, plan drafting, asset
tagging, VLM QC) through ``ExternalCallCoordinator``, so each call is a
``provider_call_attempts`` row with ``step_kind = 'llm_chat'``. The step-kind
CHECK enumerates the allowed kinds; this revision widens it by that one value
and changes nothing else -- ``llm_chat`` attempts use the existing
``charged``/``byok`` (reserved) and ``free_mock``/``mock`` chargeability rows
and need no truth package.

The physical constraint is ``ck_provider_call_attempts_ck_provider_call_attempt_step_kind``
on both dialects (``ad1_check_names`` converged PostgreSQL on the naming
convention's name). On PostgreSQL it already lists the seven Generation and
Localization kinds; on the local SQLite compatibility path the batch
migrations left the original two-kind predicate, which is widened to the
full ORM list here so SQLite and PostgreSQL agree from this revision on.

Downgrade refuses while any ``llm_chat`` attempt exists (the narrower CHECK
could not be re-added), then restores each dialect's previous predicate.
"""

import sqlalchemy as sa
from alembic import op


revision = "zh3c4d5e6f7a"
down_revision = "zg2b3c4d5e6f"
branch_labels = None
depends_on = None


_TABLE = "provider_call_attempts"
_PHYSICAL_NAME = "ck_provider_call_attempts_ck_provider_call_attempt_step_kind"
_LEGACY_SHORT_NAME = "ck_provider_call_attempt_step_kind"

_LOCALIZATION_KINDS = (
    "'generation', 'visual_qc', 'source_analysis', 'translation', "
    "'localization_edit', 'localization_ocr', 'localization_visual_qc'"
)
_WIDENED = f"step_kind IN ({_LOCALIZATION_KINDS}, 'llm_chat')"
_POSTGRES_PREVIOUS = f"step_kind IN ({_LOCALIZATION_KINDS})"
_SQLITE_PREVIOUS = "step_kind IN ('generation', 'visual_qc')"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _replace_check(predicate: str) -> None:
    if _is_postgres():
        op.execute(f'ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "{_LEGACY_SHORT_NAME}"')
        op.execute(f'ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "{_PHYSICAL_NAME}"')
        op.execute(
            f'ALTER TABLE {_TABLE} ADD CONSTRAINT "{_PHYSICAL_NAME}" CHECK ({predicate})'
        )
        return
    existing = {
        item["name"] for item in sa.inspect(op.get_bind()).get_check_constraints(_TABLE)
    }
    with op.batch_alter_table(_TABLE, schema=None, recreate="always") as batch_op:
        if _PHYSICAL_NAME in existing:
            batch_op.drop_constraint(op.f(_PHYSICAL_NAME), type_="check")
        batch_op.create_check_constraint(op.f(_PHYSICAL_NAME), predicate)


def upgrade() -> None:
    _replace_check(_WIDENED)


def downgrade() -> None:
    in_use = op.get_bind().execute(
        sa.text(f"SELECT 1 FROM {_TABLE} WHERE step_kind = 'llm_chat' LIMIT 1")
    ).first()
    if in_use is not None:
        raise RuntimeError(
            "cannot downgrade zh3c4d5e6f7a: provider_call_attempts holds llm_chat attempts"
        )
    _replace_check(_POSTGRES_PREVIOUS if _is_postgres() else _SQLITE_PREVIOUS)
