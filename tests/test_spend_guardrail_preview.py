"""A spend guardrail preview is a stored, single-use, bound proposal (#386).

``preview_update`` used to keep the proposal in a module-level dict keyed by
an HMAC that ``confirm_update`` never checked, so:

* a restart or another process lost every unconfirmed preview;
* another Workspace's Owner could confirm the token against their own
  guardrail, and a token from an older revision still applied;
* nothing expired, and a token was confirmed by whoever held it.

Each test runs the domain service the way the routes do, one unit of work per
call (commit on success, roll back on a refusal), on a file SQLite database
so a second process can open it. ``tests/test_spend_guardrail_preview_pg.py``
covers RLS, the grants and concurrent confirms on PostgreSQL.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements import guardrail_admin
from app.entitlements.cost_models import SpendGuardrailEventRecord, SpendGuardrailRecord
from app.entitlements.guardrail_admin import GuardrailAdmin
from app.platform.tenancy import TenantContext

ROOT = Path(__file__).resolve().parents[1]

LOWER = {
    "per_operation_limit": "0.50",
    "rolling_24h_limit": "2.00",
    "period_limit": "10.00",
    "outstanding_exposure_limit": "1.00",
}
HIGHER = {
    "per_operation_limit": "4.00",
    "rolling_24h_limit": "15.00",
    "period_limit": "80.00",
    "outstanding_exposure_limit": "8.00",
}

#: Confirms one preview in a fresh interpreter: no module state of the process
#: that previewed it survives, as after a restart or on another replica.
_CONFIRM_IN_A_NEW_PROCESS = """
import json, sys
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import app.adapters.persistence.models  # noqa: F401
from app.entitlements.guardrail_admin import GuardrailAdmin
from app.platform.tenancy import TenantContext

url, workspace_id, actor, token = sys.argv[1:5]
context = TenantContext(workspace_id=workspace_id, role="owner", initiator_id=actor, initiator_role="owner")
with Session(create_engine(url)) as session:
    try:
        result = GuardrailAdmin().confirm_update(session, context, preview_token=token, expected_revision=1)
    except ValueError as exc:
        print(json.dumps({"code": getattr(exc, "code", None)}))
    else:
        session.commit()
        print(json.dumps({"revision": result["revision"], "per_operation_limit": str(result["per_operation_limit"])}))
"""


def _owner(workspace_id: str, actor: str) -> TenantContext:
    return TenantContext(
        workspace_id=workspace_id, role="owner", initiator_id=actor, initiator_role="owner",
    )


def _later(minutes: int):
    """``guardrail_admin``'s clock, ``minutes`` from now."""
    real = datetime

    class _Later(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + timedelta(minutes=minutes)

    return patch.object(guardrail_admin, "datetime", _Later)


class _GuardrailPreviewFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp(dir=os.environ.get("MARKETAI_TEST_ROOT"))
        self.url = f"sqlite:///{Path(directory) / 'guardrail.db'}"
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.ws_a = _owner("ws_guardrail_a", "owner_a")
        self.ws_b = _owner("ws_guardrail_b", "owner_b")
        now = datetime.now(timezone.utc)
        with self._unit_of_work() as session:
            for workspace_id in (self.ws_a.workspace_id, self.ws_b.workspace_id):
                session.add(WorkspaceRecord(id=workspace_id, name=workspace_id, created_at=now))
                session.flush()
                session.add(SpendGuardrailRecord(
                    workspace_id=workspace_id, revision=1, budget_currency="USD",
                    per_operation_limit=Decimal("1.00"), rolling_24h_limit=Decimal("5.00"),
                    period_limit=Decimal("20.00"), outstanding_exposure_limit=Decimal("3.00"),
                    platform_policy_version=1, created_at=now, updated_at=now, updated_by="seed",
                ))

    @contextmanager
    def _unit_of_work(self):
        """What ``tenant_session`` does around each route call."""
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _preview(self, context: TenantContext, limits: dict = LOWER, revision: int = 1) -> dict:
        with self._unit_of_work() as session:
            return GuardrailAdmin().preview_update(
                session, context, new_limits=limits, expected_revision=revision,
                mfa_evidence_ref=None,
            )

    def _confirm(self, context: TenantContext, token: str, revision: int = 1) -> dict:
        with self._unit_of_work() as session:
            return GuardrailAdmin().confirm_update(
                session, context, preview_token=token, expected_revision=revision,
            )

    def _refused(self, context: TenantContext, token: str, revision: int = 1) -> str:
        with self.assertRaises(ValueError) as caught:
            self._confirm(context, token, revision)
        return getattr(caught.exception, "code", None)

    def _guardrail(self, context: TenantContext) -> tuple[int, Decimal]:
        with self._unit_of_work() as session:
            record = session.get(SpendGuardrailRecord, context.workspace_id)
            return record.revision, record.per_operation_limit

    def _events(self, context: TenantContext) -> list[tuple[int, int]]:
        with self._unit_of_work() as session:
            return [
                (event.revision_before, event.revision_after)
                for event in session.query(SpendGuardrailEventRecord)
                .filter_by(workspace_id=context.workspace_id)
                .order_by(SpendGuardrailEventRecord.revision_after)
            ]

    def _preview_row(self, token: str) -> dict:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT workspace_id, creator_id, base_revision, per_operation_limit, "
                    "created_at, expires_at, consumed_at, result_revision "
                    "FROM spend_guardrail_previews WHERE id = :id"
                ),
                {"id": token},
            ).mappings().one()
        return dict(row)


class GuardrailPreviewPersistenceTests(_GuardrailPreviewFixture):
    def test_a_preview_confirms_in_a_new_process_within_its_lifetime(self):
        token = self._preview(self.ws_a)["preview_token"]
        self.engine.dispose()

        completed = subprocess.run(
            [sys.executable, "-c", _CONFIRM_IN_A_NEW_PROCESS,
             self.url, self.ws_a.workspace_id, "owner_a", token],
            cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        confirmed = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(confirmed.get("code"), None, confirmed)
        self.assertEqual((confirmed["revision"], Decimal(confirmed["per_operation_limit"])), (2, Decimal("0.5")))
        self.assertEqual(self._guardrail(self.ws_a), (2, Decimal("0.5")))
        self.assertEqual(self._events(self.ws_a), [(1, 2)])
        self.assertFalse(hasattr(guardrail_admin, "_pending_previews"))

    def test_a_preview_records_its_workspace_creator_revision_limits_and_lifetime(self):
        preview = self._preview(self.ws_a)
        row = self._preview_row(preview["preview_token"])

        self.assertEqual(
            (row["workspace_id"], row["creator_id"], row["base_revision"], Decimal(str(row["per_operation_limit"]))),
            (self.ws_a.workspace_id, "owner_a", 1, Decimal("0.5")),
        )
        created_at = datetime.fromisoformat(str(row["created_at"]))
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        self.assertEqual(expires_at - created_at, timedelta(minutes=15))
        self.assertEqual(preview["expires_at"].replace(tzinfo=None), expires_at.replace(tzinfo=None))
        self.assertIsNone(row["consumed_at"])
        self.assertIsNone(row["result_revision"])

        self._confirm(self.ws_a, preview["preview_token"])

        row = self._preview_row(preview["preview_token"])
        self.assertIsNotNone(row["consumed_at"])
        self.assertEqual(row["result_revision"], 2)
        self.assertEqual(self._events(self.ws_a), [(1, row["result_revision"])])

    def test_each_preview_gets_its_own_random_token(self):
        first = self._preview(self.ws_a)["preview_token"]
        second = self._preview(self.ws_a)["preview_token"]

        self.assertNotEqual(first, second)

    def test_no_tenant_context_secret_is_needed(self):
        with patch("app.config.TENANT_CONTEXT_SECRET", ""):
            token = self._preview(self.ws_a)["preview_token"]
            self._confirm(self.ws_a, token)

        self.assertEqual(self._guardrail(self.ws_a), (2, Decimal("0.5")))


class GuardrailConfirmRefusalTests(_GuardrailPreviewFixture):
    def test_an_expired_preview_is_refused_and_left_unconsumed(self):
        token = self._preview(self.ws_a)["preview_token"]

        with _later(16):
            self.assertEqual(self._refused(self.ws_a, token), "preview_expired")

        self.assertEqual(self._guardrail(self.ws_a), (1, Decimal("1")))
        self.assertEqual(self._events(self.ws_a), [])
        self.assertIsNone(self._preview_row(token)["consumed_at"])

    def test_a_preview_confirms_just_before_it_expires(self):
        token = self._preview(self.ws_a)["preview_token"]

        with _later(14):
            self._confirm(self.ws_a, token)

        self.assertEqual(self._guardrail(self.ws_a), (2, Decimal("0.5")))

    def test_a_second_confirm_is_refused_as_already_consumed(self):
        token = self._preview(self.ws_a)["preview_token"]
        self._confirm(self.ws_a, token)

        for revision in (1, 2):
            with self.subTest(expected_revision=revision):
                self.assertEqual(self._refused(self.ws_a, token, revision), "preview_already_consumed")

        self.assertEqual(self._guardrail(self.ws_a), (2, Decimal("0.5")))
        self.assertEqual(self._events(self.ws_a), [(1, 2)])

    def test_a_preview_from_an_older_revision_is_refused(self):
        """Both previews were made at revision 1; once one is confirmed the
        other no longer describes the guardrail, whatever revision the
        client now claims."""
        stale = self._preview(self.ws_a, HIGHER)["preview_token"]
        self._confirm(self.ws_a, self._preview(self.ws_a, LOWER)["preview_token"])

        for revision in (1, 2):
            with self.subTest(expected_revision=revision):
                self.assertEqual(self._refused(self.ws_a, stale, revision), "revision_conflict")

        self.assertEqual(self._guardrail(self.ws_a), (2, Decimal("0.5")))
        self.assertEqual(self._events(self.ws_a), [(1, 2)])
        self.assertIsNone(self._preview_row(stale)["consumed_at"])

    def test_another_owner_cannot_confirm_someone_elses_preview(self):
        token = self._preview(self.ws_a)["preview_token"]
        co_owner = _owner(self.ws_a.workspace_id, "owner_a2")

        self.assertEqual(self._refused(co_owner, token), "spend_guardrail_preview_missing")

        self.assertEqual(self._guardrail(self.ws_a), (1, Decimal("1")))
        self.assertIsNone(self._preview_row(token)["consumed_at"])
        self._confirm(self.ws_a, token)
        self.assertEqual(self._guardrail(self.ws_a), (2, Decimal("0.5")))

    def test_another_workspace_cannot_confirm_this_workspace_s_preview(self):
        """Workspace B has its own guardrail at the same revision, so only the
        preview's binding can refuse A's token."""
        token = self._preview(self.ws_a, HIGHER)["preview_token"]

        self.assertEqual(self._refused(self.ws_b, token), "spend_guardrail_preview_missing")

        self.assertEqual(self._guardrail(self.ws_b), (1, Decimal("1")))
        self.assertEqual(self._events(self.ws_b), [])
        self.assertEqual(self._guardrail(self.ws_a), (1, Decimal("1")))
        self.assertIsNone(self._preview_row(token)["consumed_at"])
        self._confirm(self.ws_a, token)
        self.assertEqual(self._guardrail(self.ws_a), (2, Decimal("4")))

    def test_a_platform_maximum_lowered_after_the_preview_is_enforced(self):
        token = self._preview(self.ws_a, HIGHER)["preview_token"]

        with patch("app.config.MARKETAI_PLATFORM_MAX_PER_OPERATION", Decimal("3")):
            self.assertEqual(self._refused(self.ws_a, token), "spend_guardrail_limit_above_platform")

        self.assertEqual(self._guardrail(self.ws_a), (1, Decimal("1")))
        self.assertIsNone(self._preview_row(token)["consumed_at"])


if __name__ == "__main__":
    unittest.main()
