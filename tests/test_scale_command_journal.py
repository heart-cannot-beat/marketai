"""Tests for ``ScaleCommandJournalRecord`` and ``claim_command()`` (spec
§7.1, issue #86 Task 8).

Flat scratch-SQLite style (mirroring ``tests/test_scale_delivery_targets.py``
and ``tests/test_scale_migration.py``): a fresh temp-file SQLite database per
test via pytest fixtures, matching the plain-function ``session``/``ctx``
shape the task brief's own test code uses. ``ScaleCommandJournalRecord`` has
no composite FK into any other Scale/domain table (only ``workspace_id`` ->
``workspaces``), so no upstream fixture chain is required here, unlike
``tests/test_scale_lineage_guard.py``.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord, new_id
from app.platform.tenancy import TenantContext
from app.scale.command_journal import (
    IdempotencyConflictError,
    claim_command,
    command_request_fingerprint,
)
from app.scale.models import ScaleCommandJournalRecord


@pytest.fixture
def session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-scj-"))
    engine = create_engine(
        f"sqlite:///{(tmp_dir / 'scj.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    sess = Session(bind=engine)
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _make_workspace(session: Session, name: str = "Command journal test") -> WorkspaceRecord:
    ws = WorkspaceRecord(id=new_id("ws"), name=name)
    session.add(ws)
    session.flush()
    return ws


def test_command_request_fingerprint_is_canonical_and_fixed_width():
    first = command_request_fingerprint(decision_id="decision_1", target_id="target_1")
    reordered = command_request_fingerprint(target_id="target_1", decision_id="decision_1")
    changed = command_request_fingerprint(decision_id="decision_2", target_id="target_1")

    assert len(first) == 64
    assert first == reordered
    assert first != changed


@pytest.fixture
def ws(session):
    return _make_workspace(session)


@pytest.fixture
def ctx(ws):
    return TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        initiator_id="user_1", meta_mode="customer_sandbox",
    )


# ---------------------------------------------------------------------------
# claim_command(): first claim, replay, conflict (spec §7.7 CC2/CC10)
# ---------------------------------------------------------------------------


def test_first_claim_succeeds(session, ctx):
    claim = claim_command(
        session, ctx,
        command_type="create_candidate",
        deduplication_key="idem_1",
        result_id="cand_1",
        request_fingerprint="fp_abc",
    )
    assert claim.replay is False
    assert claim.record.result_resource_id == "cand_1"
    assert claim.record.result_resource_type == "candidate"
    assert claim.record.command_type == "create_candidate"
    assert claim.record.dedup_key == "idem_1"
    assert claim.record.request_fingerprint == "fp_abc"
    assert claim.record.workspace_id == ctx.workspace_id
    assert claim.record.meta_mode == "customer_sandbox"
    assert claim.record.scenario_scope_key == "__no_scenario__"
    assert claim.record.initiator_id == "user_1"


def test_replay_same_fingerprint(session, ctx):
    """CC2 pattern: same key, same fingerprint -> idempotent replay."""
    claim_command(session, ctx, command_type="create_candidate",
                   deduplication_key="idem_1", result_id="cand_1",
                   request_fingerprint="fp_abc")
    session.flush()

    claim = claim_command(session, ctx, command_type="create_candidate",
                           deduplication_key="idem_1", result_id="cand_2",
                           request_fingerprint="fp_abc")
    assert claim.replay is True
    assert claim.record.result_resource_id == "cand_1"  # original, not cand_2


def test_conflict_different_fingerprint(session, ctx):
    """CC10 pattern: same key, different fingerprint -> idempotency_conflict."""
    claim_command(session, ctx, command_type="create_candidate",
                   deduplication_key="idem_1", result_id="cand_1",
                   request_fingerprint="fp_abc")
    session.flush()

    with pytest.raises(IdempotencyConflictError) as excinfo:
        claim_command(session, ctx, command_type="create_candidate",
                       deduplication_key="idem_1", result_id="cand_2",
                       request_fingerprint="fp_different")
    assert excinfo.value.existing_fingerprint == "fp_abc"
    assert excinfo.value.incoming_fingerprint == "fp_different"


# ---------------------------------------------------------------------------
# result_resource_type default mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command_type,expected_type",
    [
        ("create_candidate", "candidate"),
        ("create_enrollment", "enrollment"),
        ("create_preview", "activation_preview"),
        ("activate_enrollment", "enrollment"),
        ("resume_enrollment", "enrollment"),
    ],
)
def test_default_result_resource_type_by_command_type(session, ctx, command_type, expected_type):
    claim = claim_command(
        session, ctx, command_type=command_type,
        deduplication_key=f"idem_{command_type}", result_id="res_1",
        request_fingerprint="fp_1",
    )
    assert claim.record.result_resource_type == expected_type


def test_explicit_result_resource_type_override(session, ctx):
    claim = claim_command(
        session, ctx, command_type="create_candidate",
        deduplication_key="idem_1", result_id="cand_1",
        request_fingerprint="fp_1", result_resource_type="custom_type",
    )
    assert claim.record.result_resource_type == "custom_type"


# ---------------------------------------------------------------------------
# Unique-constraint shape: command_type participates in the conflict key
# ---------------------------------------------------------------------------


def test_distinct_command_types_do_not_collide(session, ctx):
    """The unique constraint is (workspace, mode, command_type, dedup_key):
    the same dedup_key under two different command_types claims
    independently, never conflicting or replaying each other."""
    first = claim_command(session, ctx, command_type="create_candidate",
                           deduplication_key="shared_key", result_id="cand_1",
                           request_fingerprint="fp_1")
    second = claim_command(session, ctx, command_type="create_enrollment",
                            deduplication_key="shared_key", result_id="enr_1",
                            request_fingerprint="fp_2")
    assert first.replay is False
    assert second.replay is False
    assert first.record.id != second.record.id


def test_distinct_workspaces_do_not_collide(session, ctx):
    other_ws = _make_workspace(session, name="Other workspace")
    other_ctx = TenantContext(
        workspace_id=other_ws.id, role="system", executor="system:worker",
        initiator_id="user_2", meta_mode="customer_sandbox",
    )
    first = claim_command(session, ctx, command_type="create_candidate",
                           deduplication_key="idem_1", result_id="cand_1",
                           request_fingerprint="fp_1")
    second = claim_command(session, other_ctx, command_type="create_candidate",
                            deduplication_key="idem_1", result_id="cand_2",
                            request_fingerprint="fp_2")
    assert first.replay is False
    assert second.replay is False


# ---------------------------------------------------------------------------
# scenario_scope_key biconditional
# ---------------------------------------------------------------------------


def test_controlled_mock_requires_explicit_scenario_scope_key(session, ws):
    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        initiator_id="user_1", meta_mode="controlled_mock",
    )
    with pytest.raises(ValueError):
        claim_command(session, ctx, command_type="create_candidate",
                       deduplication_key="idem_1", result_id="cand_1",
                       request_fingerprint="fp_1")

    claim = claim_command(session, ctx, command_type="create_candidate",
                           deduplication_key="idem_1", result_id="cand_1",
                           request_fingerprint="fp_1", scenario_scope_key="scn_1")
    assert claim.record.scenario_scope_key == "scn_1"


# ---------------------------------------------------------------------------
# Database-level CHECK constraints (direct ORM construction bypasses
# claim_command()'s own validation, mirroring
# tests/test_scale_delivery_targets.py's shape tests)
# ---------------------------------------------------------------------------


def test_database_rejects_invalid_command_type(session, ctx):
    bad = ScaleCommandJournalRecord(
        id=new_id("scj"), workspace_id=ctx.workspace_id, meta_mode=ctx.meta_mode,
        scenario_scope_key="__no_scenario__", command_type="not_a_real_command",
        dedup_key="k1", request_fingerprint="fp1", result_resource_id="r1",
        result_resource_type="candidate", initiator_id="user_1",
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_database_rejects_invalid_meta_mode(session, ws):
    bad = ScaleCommandJournalRecord(
        id=new_id("scj"), workspace_id=ws.id, meta_mode="not_a_real_mode",
        scenario_scope_key="__no_scenario__", command_type="create_candidate",
        dedup_key="k1", request_fingerprint="fp1", result_resource_id="r1",
        result_resource_type="candidate", initiator_id="user_1",
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_database_rejects_scenario_biconditional_violation(session, ws):
    """controlled_mock with the reserved no-scenario marker violates the
    scenario biconditional CHECK."""
    bad = ScaleCommandJournalRecord(
        id=new_id("scj"), workspace_id=ws.id, meta_mode="controlled_mock",
        scenario_scope_key="__no_scenario__", command_type="create_candidate",
        dedup_key="k1", request_fingerprint="fp1", result_resource_id="r1",
        result_resource_type="candidate", initiator_id="user_1",
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_database_rejects_duplicate_dedup_key_direct_insert(session, ctx):
    """Proves the DB UNIQUE constraint itself enforces the claim key, not
    just claim_command()'s ON CONFLICT clause."""
    first = ScaleCommandJournalRecord(
        id=new_id("scj"), workspace_id=ctx.workspace_id, meta_mode=ctx.meta_mode,
        scenario_scope_key="__no_scenario__", command_type="create_candidate",
        dedup_key="dup_key", request_fingerprint="fp1", result_resource_id="r1",
        result_resource_type="candidate", initiator_id="user_1",
    )
    session.add(first)
    session.flush()

    second = ScaleCommandJournalRecord(
        id=new_id("scj"), workspace_id=ctx.workspace_id, meta_mode=ctx.meta_mode,
        scenario_scope_key="__no_scenario__", command_type="create_candidate",
        dedup_key="dup_key", request_fingerprint="fp2", result_resource_id="r2",
        result_resource_type="candidate", initiator_id="user_1",
    )
    session.add(second)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
