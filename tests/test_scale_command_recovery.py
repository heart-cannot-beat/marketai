"""Command-outcome recovery via hashed identity lookup (spec §7.1, issue
#87 Task 4).

Flat scratch-SQLite style, mirroring ``tests/test_scale_command_journal.py``:
a fresh temp-file SQLite database per test. ``workspace_id`` is a real
foreign key into ``workspaces`` (the project's SQLite connections run with
``PRAGMA foreign_keys=ON``, see ``app.adapters.persistence.database``), so
tests create a real ``WorkspaceRecord`` rather than an arbitrary literal id.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord, new_id
from app.platform.tenancy import TenantContext
from app.scale.command_journal import (
    CommandOutcomeNotFoundError,
    claim_command,
    compute_identity_hash,
    lookup_command_outcome,
)


@pytest.fixture
def session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-scj-recovery-"))
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


def _make_workspace(session: Session, name: str = "Command recovery test") -> WorkspaceRecord:
    ws = WorkspaceRecord(id=new_id("ws"), name=name)
    session.add(ws)
    session.flush()
    return ws


@pytest.fixture
def ws(session):
    return _make_workspace(session)


@pytest.fixture
def ctx(ws):
    return TenantContext(
        workspace_id=ws.id, role="owner",
        initiator_id="user-1", executor="user-1",
        meta_mode="controlled_mock",
    )


class TestComputeIdentityHash:
    def test_deterministic(self):
        h1 = compute_identity_hash("create_enrollment", "draft:ws-1:key-1")
        h2 = compute_identity_hash("create_enrollment", "draft:ws-1:key-1")
        assert h1 == h2
        assert len(h1) == 64

    def test_different_inputs_differ(self):
        h1 = compute_identity_hash("create_enrollment", "key-1")
        h2 = compute_identity_hash("create_enrollment", "key-2")
        assert h1 != h2

    def test_different_command_types_differ(self):
        """The canonical string joins command_type and dedup_key with a
        delimiter -- two different splits of the same concatenation must
        still hash differently."""
        h1 = compute_identity_hash("create", "enrollment:key-1")
        h2 = compute_identity_hash("create_enrollment", "key-1")
        assert h1 != h2


class TestClaimCommandStoresIdentityHash:
    def test_claim_command_writes_matching_identity_hash(self, session, ctx):
        claim = claim_command(
            session, ctx,
            command_type="create_enrollment",
            deduplication_key="test-key-1",
            result_id="enr-1",
            request_fingerprint="fp-1",
            scenario_scope_key="scenario-1",
        )
        assert claim.record.identity_hash == compute_identity_hash(
            "create_enrollment", "test-key-1"
        )

    def test_replay_keeps_original_identity_hash(self, session, ctx):
        first = claim_command(
            session, ctx,
            command_type="create_enrollment",
            deduplication_key="test-key-1",
            result_id="enr-1",
            request_fingerprint="fp-1",
            scenario_scope_key="scenario-1",
        )
        session.flush()

        replay = claim_command(
            session, ctx,
            command_type="create_enrollment",
            deduplication_key="test-key-1",
            result_id="enr-2",
            request_fingerprint="fp-1",
            scenario_scope_key="scenario-1",
        )
        assert replay.replay is True
        assert replay.record.identity_hash == first.record.identity_hash


class TestLookupCommandOutcome:
    def test_finds_claimed_command(self, session, ctx):
        claim_command(
            session, ctx,
            command_type="create_enrollment",
            deduplication_key="test-key-1",
            result_id="enr-1",
            request_fingerprint="fp-1",
            scenario_scope_key="scenario-1",
        )
        session.flush()
        identity = compute_identity_hash("create_enrollment", "test-key-1")
        outcome = lookup_command_outcome(session, ctx, identity)
        assert outcome.result_resource_id == "enr-1"
        assert outcome.command_type == "create_enrollment"
        assert outcome.dedup_key == "test-key-1"
        assert outcome.result_resource_type == "enrollment"
        assert outcome.request_fingerprint == "fp-1"
        assert outcome.initiator_id == "user-1"
        assert outcome.identity_hash == identity

    def test_not_found_returns_error(self, session, ctx):
        with pytest.raises(CommandOutcomeNotFoundError):
            lookup_command_outcome(session, ctx, "nonexistent_hash")

    def test_scoped_to_workspace(self, session, ctx):
        claim_command(
            session, ctx,
            command_type="create_enrollment",
            deduplication_key="ws-scoped-key",
            result_id="enr-2",
            request_fingerprint="fp-2",
            scenario_scope_key="scenario-1",
        )
        session.flush()

        other_ws = _make_workspace(session, name="Other workspace")
        other_ctx = TenantContext(
            workspace_id=other_ws.id, role="owner",
            initiator_id="user-1", executor="user-1",
            meta_mode="controlled_mock",
        )
        identity = compute_identity_hash("create_enrollment", "ws-scoped-key")
        with pytest.raises(CommandOutcomeNotFoundError):
            lookup_command_outcome(session, other_ctx, identity)

    def test_scoped_to_meta_mode(self, session, ws):
        sandbox_ctx = TenantContext(
            workspace_id=ws.id, role="owner",
            initiator_id="user-1", executor="user-1",
            meta_mode="customer_sandbox",
        )
        claim_command(
            session, sandbox_ctx,
            command_type="create_candidate",
            deduplication_key="mode-scoped-key",
            result_id="cand-1",
            request_fingerprint="fp-3",
        )
        session.flush()

        mock_ctx = TenantContext(
            workspace_id=ws.id, role="owner",
            initiator_id="user-1", executor="user-1",
            meta_mode="controlled_mock",
        )
        identity = compute_identity_hash("create_candidate", "mode-scoped-key")
        with pytest.raises(CommandOutcomeNotFoundError):
            lookup_command_outcome(session, mock_ctx, identity)

    def test_command_kind_narrows_lookup(self, session, ctx):
        claim_command(
            session, ctx,
            command_type="create_enrollment",
            deduplication_key="narrow-key",
            result_id="enr-3",
            request_fingerprint="fp-4",
            scenario_scope_key="scenario-1",
        )
        session.flush()
        identity = compute_identity_hash("create_enrollment", "narrow-key")

        outcome = lookup_command_outcome(
            session, ctx, identity, command_kind="create_enrollment"
        )
        assert outcome.result_resource_id == "enr-3"

        with pytest.raises(CommandOutcomeNotFoundError):
            lookup_command_outcome(
                session, ctx, identity, command_kind="create_candidate"
            )
