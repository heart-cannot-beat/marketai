"""Platform workspace flag and audit meta_mode column (SQLite path).

Flat in ``tests/`` and using a scratch SQLite engine/session -- mirroring
``tests/test_idempotency.py`` -- since no shared ``db_session`` fixture
exists in ``tests/conftest.py`` for this suite.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import AuditEventRecord, WorkspaceRecord, new_id, utcnow


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


class MetaFoundationMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-meta-foundation-"))
        self.addCleanup(shutil.rmtree, self._tmp_dir, ignore_errors=True)
        self.engine = create_engine(
            _sqlite_url(self._tmp_dir / "meta_foundation.db"),
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(
            bind=self.engine,
            tables=[WorkspaceRecord.__table__, AuditEventRecord.__table__],
        )
        self.session = Session(bind=self.engine)
        self.addCleanup(self.session.close)

    def test_workspace_has_platform_flag(self) -> None:
        ws = WorkspaceRecord(
            id=new_id("ws"),
            name="Platform Ops",
            created_at=utcnow(),
            is_platform_workspace=True,
        )
        self.session.add(ws)
        self.session.flush()
        assert ws.is_platform_workspace is True

    def test_workspace_platform_flag_defaults_false(self) -> None:
        ws = WorkspaceRecord(id=new_id("ws"), name="Customer", created_at=utcnow())
        self.session.add(ws)
        self.session.flush()
        assert ws.is_platform_workspace is False

    def test_audit_event_accepts_meta_mode(self) -> None:
        ws = WorkspaceRecord(id=new_id("ws"), name="T", created_at=utcnow())
        self.session.add(ws)
        self.session.flush()
        evt = AuditEventRecord(
            id=new_id("aud"),
            workspace_id=ws.id,
            initiator_id="usr_test",
            initiator_role="member",
            executor="user",
            event_type="meta.intent.created",
            resource_type="meta_call_intent",
            resource_id="mci_123",
            details_json={},
            meta_mode="customer_sandbox",
        )
        self.session.add(evt)
        self.session.flush()
        assert evt.meta_mode == "customer_sandbox"

    def test_audit_event_meta_mode_nullable(self) -> None:
        ws = WorkspaceRecord(id=new_id("ws"), name="T", created_at=utcnow())
        self.session.add(ws)
        self.session.flush()
        evt = AuditEventRecord(
            id=new_id("aud"),
            workspace_id=ws.id,
            initiator_id="usr_test",
            initiator_role="member",
            executor="user",
            event_type="library.entry.published",
            resource_type="library_entry",
            resource_id="le_123",
            details_json={},
        )
        self.session.add(evt)
        self.session.flush()
        assert evt.meta_mode is None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
