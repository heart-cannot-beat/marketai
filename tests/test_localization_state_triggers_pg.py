"""PostgreSQL state-only mutation guards for Localization headers."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not OWNER_URL,
    reason="set DOMAIN_OWNER_DATABASE_URL against a PostgreSQL database at head",
)


@pytest.fixture()
def owner():
    engine = create_engine(OWNER_URL)
    yield engine
    engine.dispose()


def _copy_probe(connection) -> None:
    connection.execute(text("""
        CREATE TEMP TABLE copy_state_probe (
            id text PRIMARY KEY,
            state text NOT NULL,
            content text NOT NULL
        ) ON COMMIT DROP
    """))
    connection.execute(text("""
        CREATE TRIGGER copy_state_probe_guard
        BEFORE UPDATE OR DELETE ON copy_state_probe
        FOR EACH ROW
        EXECUTE FUNCTION marketai_guard_localized_copy_version_mutation()
    """))
    connection.execute(text("""
        INSERT INTO copy_state_probe (id, state, content)
        VALUES ('copy-1', 'draft', 'immutable')
    """))


def _attempt_probe(connection, *, state: str = "authorized") -> None:
    connection.execute(text("""
        CREATE TEMP TABLE attempt_state_probe (
            id text PRIMARY KEY,
            workspace_id text NOT NULL,
            state text NOT NULL,
            input_hash text NOT NULL
        ) ON COMMIT DROP
    """))
    connection.execute(text("""
        CREATE TRIGGER attempt_state_probe_guard
        BEFORE UPDATE OR DELETE ON attempt_state_probe
        FOR EACH ROW
        EXECUTE FUNCTION marketai_guard_localization_attempt_mutation()
    """))
    connection.execute(
        text("""
            INSERT INTO attempt_state_probe (id, workspace_id, state, input_hash)
            VALUES ('attempt-1', 'workspace-1', :state, 'immutable')
        """),
        {"state": state},
    )


def test_head_replaces_only_the_two_overbroad_append_only_triggers(owner) -> None:
    with owner.connect() as connection:
        rows = connection.execute(text("""
            SELECT event_object_table, trigger_name
              FROM information_schema.triggers
             WHERE event_object_table IN (
                 'localized_copy_versions', 'localization_attempts'
             )
             ORDER BY event_object_table, trigger_name
        """)).all()
    names = {(row[0], row[1]) for row in rows}
    assert (
        "localized_copy_versions",
        "trg_localized_copy_versions_state_only",
    ) in names
    assert (
        "localization_attempts",
        "trg_localization_attempts_state_only",
    ) in names
    assert not any(name.endswith("_append_only") for _, name in names)


def test_head_removes_obsolete_two_value_provider_step_constraint(owner) -> None:
    with owner.connect() as connection:
        constraints = dict(connection.execute(text("""
            SELECT conname, pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conrelid = 'public.provider_call_attempts'::regclass
               AND contype = 'c'
        """)).all())
    assert (
        "ck_provider_call_attempts_ck_provider_call_attempt_step_kind"
        not in constraints
    )
    assert "localization_edit" in constraints["ck_provider_call_attempt_step_kind"]


def _asset_origin_probe(connection) -> None:
    connection.execute(text("""
        CREATE TEMP TABLE asset_origin_probe
        (LIKE public.asset_versions INCLUDING CONSTRAINTS)
        ON COMMIT DROP
    """))
    connection.execute(text("""
        DO $$
        DECLARE
            column_record record;
        BEGIN
            FOR column_record IN
                SELECT attname
                  FROM pg_attribute
                 WHERE attrelid = 'pg_temp.asset_origin_probe'::regclass
                   AND attnum > 0
                   AND NOT attisdropped
                   AND attnotnull
                   AND attname <> 'origin_kind'
            LOOP
                EXECUTE format(
                    'ALTER TABLE pg_temp.asset_origin_probe '
                    'ALTER COLUMN %I DROP NOT NULL',
                    column_record.attname
                );
            END LOOP;
        END;
        $$
    """))


def test_head_keeps_only_localization_aware_asset_origin_constraint(owner) -> None:
    with owner.connect() as connection:
        constraints = dict(connection.execute(text("""
            SELECT conname, pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conrelid = 'public.asset_versions'::regclass
               AND contype = 'c'
               AND conname LIKE '%origin_kind%'
        """)).all())
    assert "ck_asset_versions_ck_asset_version_origin_kind" not in constraints
    assert list(constraints) == ["ck_asset_version_origin_kind"]
    assert "localized" in constraints["ck_asset_version_origin_kind"]


def test_asset_origin_constraint_accepts_localized(owner) -> None:
    with owner.begin() as connection:
        _asset_origin_probe(connection)
        connection.execute(text("""
            INSERT INTO asset_origin_probe (
                origin_kind, parent_version_id, truth_package_id
            ) VALUES ('localized', 'parent-probe', 'truth-probe')
        """))


def test_asset_origin_constraint_rejects_unknown_origin(owner) -> None:
    with pytest.raises(DBAPIError, match="ck_asset_version_origin_kind"):
        with owner.begin() as connection:
            _asset_origin_probe(connection)
            connection.execute(text("""
                INSERT INTO asset_origin_probe (origin_kind)
                VALUES ('uncontracted-origin')
            """))


def test_copy_guard_allows_only_contracted_forward_state_edges(owner) -> None:
    with owner.begin() as connection:
        _copy_probe(connection)
        for state in ("needs_review", "approved", "superseded"):
            connection.execute(
                text("UPDATE copy_state_probe SET state = :state WHERE id = 'copy-1'"),
                {"state": state},
            )
        assert connection.scalar(text(
            "SELECT state FROM copy_state_probe WHERE id = 'copy-1'"
        )) == "superseded"


@pytest.mark.parametrize(
    "statement, message",
    [
        (
            "UPDATE copy_state_probe SET state = 'approved' WHERE id = 'copy-1'",
            "invalid localized copy state transition",
        ),
        (
            "UPDATE copy_state_probe SET content = 'changed' WHERE id = 'copy-1'",
            "UPDATE denied outside state",
        ),
        (
            "DELETE FROM copy_state_probe WHERE id = 'copy-1'",
            "DELETE denied",
        ),
    ],
)
def test_copy_guard_rejects_skips_content_changes_and_delete(
    owner, statement: str, message: str,
) -> None:
    with pytest.raises(DBAPIError, match=message):
        with owner.begin() as connection:
            _copy_probe(connection)
            connection.execute(text(statement))


def test_attempt_guard_allows_authorized_dispatching_terminal(owner) -> None:
    with owner.begin() as connection:
        _attempt_probe(connection)
        connection.execute(text("""
            UPDATE attempt_state_probe SET state = 'dispatching'
            WHERE id = 'attempt-1'
        """))
        connection.execute(text("""
            UPDATE attempt_state_probe SET state = 'completed'
            WHERE id = 'attempt-1'
        """))
        assert connection.scalar(text(
            "SELECT state FROM attempt_state_probe WHERE id = 'attempt-1'"
        )) == "completed"


@pytest.mark.parametrize(
    "initial_state, statement, message",
    [
        (
            "authorized",
            "UPDATE attempt_state_probe SET state = 'completed' WHERE id = 'attempt-1'",
            "invalid localization attempt state transition",
        ),
        (
            "completed",
            "UPDATE attempt_state_probe SET state = 'dispatching' WHERE id = 'attempt-1'",
            "invalid localization attempt state transition",
        ),
        (
            "authorized",
            "UPDATE attempt_state_probe SET input_hash = 'changed' WHERE id = 'attempt-1'",
            "UPDATE denied outside state",
        ),
        (
            "authorized",
            "DELETE FROM attempt_state_probe WHERE id = 'attempt-1'",
            "DELETE denied",
        ),
        (
            "uncertain",
            "UPDATE attempt_state_probe SET state = 'completed' WHERE id = 'attempt-1'",
            "lacks terminal provider evidence",
        ),
    ],
)
def test_attempt_guard_rejects_illegal_or_unproven_mutation(
    owner, initial_state: str, statement: str, message: str,
) -> None:
    with pytest.raises(DBAPIError, match=message):
        with owner.begin() as connection:
            _attempt_probe(connection, state=initial_state)
            connection.execute(text(statement))
