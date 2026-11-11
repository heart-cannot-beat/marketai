"""Issue #126 against a migrated PostgreSQL with the real API, Worker and Readonly roles.

Provisioning (``marketai_apply_workspace_projection_v2``, as migration
``zzg126a1b2c3`` defines it) used to create a new Workspace's SpendGuardrail
with every limit at 0, so the Workspace could make no paid call. Now:

* the first signed snapshot creates the guardrail at the Beta defaults of
  docs/architecture/subscription-entitlement-usage-cost-ownership.md §11, in
  the Workspace's budget currency;
* a redelivered or later snapshot never changes an existing guardrail: not one
  its Owner changed, and not one an earlier provisioning wrote at 0, which is
  not backfilled;
* a Member reads the limits but can neither preview nor confirm a change,
  another Workspace's context can neither read nor change them, and a raise
  above the platform maximum is refused and changes nothing;
* the function stays SECURITY DEFINER with ``search_path=''`` and keeps its
  ACL (the API executes it, nobody else), and the migration's downgrade puts
  the 0-limit guardrail back.

The API calls go through FastAPI as the API role, the snapshots through
``EntitlementProjectionModule`` as the API role, as in production. Run with
``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``DOMAIN_READONLY_DATABASE_URL`` and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker

from app import config
from app.adapters.persistence.database import tenant_session
from app.entitlements.cost_models import SpendGuardrailPreviewRecord, SpendGuardrailRecord
from app.entitlements.guardrail_defaults import SPEND_GUARDRAIL_DEFAULTS, SPEND_GUARDRAIL_DEFAULTS_V1
from app.entitlements.projection import EntitlementProjectionModule
from app.library.contracts import ROLE_MEMBER, ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

ROLE_URLS = {
    "owner": os.getenv("DOMAIN_OWNER_DATABASE_URL"),
    "api": os.getenv("DOMAIN_API_DATABASE_URL"),
    "worker": os.getenv("DOMAIN_WORKER_DATABASE_URL"),
    "readonly": os.getenv("DOMAIN_READONLY_DATABASE_URL"),
}
ROOT = Path(__file__).resolve().parents[1]
#: The migration that provisions at the defaults, and the one below it.
REVISION, DOWN_REVISION = "zzg126a1b2c3", "zzg7d8e9f0a1"
FUNCTION = "marketai_apply_workspace_projection_v2"
SIGNING_KEY = b"isolated-provisioning-regression-only"
#: Contract §11, as decimal strings the API serialises (Numeric(24, 12)).
BETA = {
    "per_operation_limit": Decimal("5"),
    "rolling_24h_limit": Decimal("20"),
    "period_limit": Decimal("100"),
    "outstanding_exposure_limit": Decimal("10"),
}
ZERO = {field: Decimal("0") for field in BETA}
LOWER = {
    "per_operation_limit": "3",
    "rolling_24h_limit": "12",
    "period_limit": "60",
    "outstanding_exposure_limit": "6",
}


@unittest.skipUnless(all(ROLE_URLS.values()) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class SpendGuardrailProvisioningPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = create_engine(ROLE_URLS["owner"])
        cls.api = create_engine(ROLE_URLS["api"])
        cls.owner_sessions = sessionmaker(bind=cls.owner, expire_on_commit=False, autoflush=False)
        cls.api_sessions = sessionmaker(bind=cls.api, expire_on_commit=False, autoflush=False)

    @classmethod
    def tearDownClass(cls):
        cls.owner.dispose()
        cls.api.dispose()

    def setUp(self):
        self.current: TenantContext | None = None
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    def _snapshot(self, workspace_id: str, *, version: int = 1) -> bytes:
        """A signed control-plane envelope; ``version`` is the entitlement version."""
        now = datetime.now(timezone.utc)
        header = {"schema_version": 2, "signature_key_id": "test", "signature_algorithm": "hmac-sha256"}
        payload = {
            "snapshot_id": f"snapshot_{uuid.uuid4().hex}", "workspace_id": workspace_id,
            "workspace_name": "Acme Charge", "authorization_epoch": 0,
            "entitlement_version": version, "lease_sequence": 1, "projection_version": version,
            "lifecycle": "active", "suspended": False,
            "entitlement_snapshot": {
                "plan_version_id": "beta-plan", "source_refs": [],
                "period_start": now.isoformat(), "period_end": (now + timedelta(days=30)).isoformat(),
                "period_anchor": now.isoformat(), "resolved_entitlements": {"controlled_mock": True},
                "restriction_summary": [], "semantic_hash": f"semantic-{version}",
                "issued_at": now.isoformat(), "valid_until": (now + timedelta(hours=1)).isoformat(),
                "audit_event_id": f"audit-{version}",
            },
        }
        content = {"protected_header": header, "payload": payload}
        signature = hmac.new(
            SIGNING_KEY, json.dumps(content, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256,
        ).hexdigest()
        return json.dumps({**content, "signature": signature}).encode()

    def _project(self, envelope: bytes) -> str:
        """Apply a snapshot as the API role does, through the SECURITY DEFINER function."""
        module = EntitlementProjectionModule(self.api_sessions, {"test": SIGNING_KEY})
        return asyncio.run(module.ingest(envelope, delivery_kind="push")).value

    def _provisioned(self, prefix: str) -> tuple[str, bytes]:
        workspace_id = f"ws_{prefix}_{uuid.uuid4().hex[:10]}"
        envelope = self._snapshot(workspace_id)
        self.assertEqual(self._project(envelope), "applied")
        return workspace_id, envelope

    def _row(self, workspace_id: str) -> dict | None:
        """The guardrail as the owner reads it (no RLS in the way)."""
        with self.owner.connect() as connection:
            row = connection.execute(text(
                "SELECT revision, budget_currency, per_operation_limit, rolling_24h_limit, period_limit,"
                " outstanding_exposure_limit, platform_policy_version, updated_by, updated_at"
                " FROM spend_guardrails WHERE workspace_id = :w"
            ), {"w": workspace_id}).mappings().one_or_none()
        return dict(row) if row is not None else None

    @staticmethod
    def _limits(row: dict) -> dict:
        return {field: row[field] for field in BETA}

    def _seed_workspace(self, workspace_id: str, *, currency: str = "USD") -> None:
        """A Workspace row that exists before its first snapshot, as the control plane may leave it."""
        with self.owner.begin() as connection:
            connection.execute(text(
                "INSERT INTO workspaces (id, name, default_currency, authorization_epoch, entitlement_version,"
                " suspended, projection_version, created_at, lifecycle, budget_currency)"
                " VALUES (:w, 'Acme Charge', :c, 0, 0, false, 0, now(), 'provisioning', :c)"
            ), {"w": workspace_id, "c": currency})

    def test_the_constant_is_the_contract_and_what_a_new_workspace_gets(self):
        self.assertEqual(SPEND_GUARDRAIL_DEFAULTS_V1.limits(), BETA)
        self.assertIs(SPEND_GUARDRAIL_DEFAULTS, SPEND_GUARDRAIL_DEFAULTS_V1)

    def test_the_first_snapshot_provisions_the_beta_defaults_and_redelivery_keeps_them(self):
        workspace_id, envelope = self._provisioned("first")
        row = self._row(workspace_id)
        self.assertEqual(self._limits(row), BETA)
        self.assertEqual((row["revision"], row["budget_currency"], row["platform_policy_version"], row["updated_by"]),
                         (1, "USD", 1, "system:provisioning"))

        self.assertEqual(self._project(envelope), "duplicate")
        self.assertEqual(self._project(self._snapshot(workspace_id, version=2)), "applied")
        self.assertEqual(self._row(workspace_id), row, "a later snapshot rewrote the guardrail")

    def test_the_guardrail_is_in_the_workspace_budget_currency(self):
        workspace_id = f"ws_eur_{uuid.uuid4().hex[:10]}"
        self._seed_workspace(workspace_id, currency="EUR")
        self.assertEqual(self._project(self._snapshot(workspace_id)), "applied")
        row = self._row(workspace_id)
        self.assertEqual((row["budget_currency"], self._limits(row)), ("EUR", BETA))

    def test_redelivery_keeps_a_guardrail_its_owner_changed(self):
        workspace_id, envelope = self._provisioned("edited")
        owner = self._ctx(workspace_id, ROLE_OWNER)
        token = self._preview(owner, LOWER, revision=1)
        confirmed = self._post(owner, "confirm", {"preview_token": token, "expected_revision": 1})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        edited = self._row(workspace_id)
        self.assertEqual((edited["revision"], self._limits(edited)),
                         (2, {field: Decimal(value) for field, value in LOWER.items()}))

        self.assertEqual(self._project(envelope), "duplicate")
        self.assertEqual(self._project(self._snapshot(workspace_id, version=2)), "applied")
        self.assertEqual(self._row(workspace_id), edited, "a snapshot overwrote the Owner's limits")

    def test_an_existing_zero_guardrail_is_not_backfilled(self):
        workspace_id = f"ws_zero_{uuid.uuid4().hex[:10]}"
        self._seed_workspace(workspace_id)
        with self.owner.begin() as connection:  # what provisioning wrote before zzg126a1b2c3
            connection.execute(text(
                "INSERT INTO spend_guardrails (workspace_id, revision, budget_currency, per_operation_limit,"
                " rolling_24h_limit, period_limit, outstanding_exposure_limit, platform_policy_version,"
                " created_at, updated_at, updated_by)"
                " VALUES (:w, 1, 'USD', 0, 0, 0, 0, 1, now(), now(), 'system:provisioning')"
            ), {"w": workspace_id})
        zero = self._row(workspace_id)

        self.assertEqual(self._project(self._snapshot(workspace_id)), "applied")
        self.assertEqual(self._project(self._snapshot(workspace_id, version=2)), "applied")
        self.assertEqual(self._row(workspace_id), zero)
        self.assertEqual(self._limits(zero), ZERO)

    # ------------------------------------------------------------------
    # Who reads and changes it
    # ------------------------------------------------------------------

    @staticmethod
    def _ctx(workspace_id: str, role: str) -> TenantContext:
        actor = f"user_{role}_{workspace_id[-6:]}"
        return TenantContext(workspace_id=workspace_id, role=role, initiator_id=actor,
                             initiator_role=role, request_id="test-req", trace_id="trace-test")

    def _as_api(self):
        return patch("app.adapters.persistence.database.SessionLocal", self.api_sessions)

    def _get(self, caller: TenantContext):
        self.current = caller
        with self._as_api():
            return self.client.get("/api/v1/spend-guardrail")

    def _post(self, caller: TenantContext, path: str, body: dict):
        self.current = caller
        with self._as_api():
            return self.client.post(f"/api/v1/spend-guardrail/{path}", json=body)

    def _preview(self, caller: TenantContext, limits: dict, *, revision: int) -> str:
        response = self._post(caller, "preview", {**limits, "expected_revision": revision})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["preview_token"]

    def _previews(self, workspace_id: str) -> int:
        with self.owner.connect() as connection:
            return connection.execute(text(
                "SELECT count(*) FROM spend_guardrail_previews WHERE workspace_id = :w"
            ), {"w": workspace_id}).scalar()

    def _refused(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(body["code"], code)
        return body

    def test_a_member_reads_the_limits_but_can_neither_preview_nor_confirm(self):
        workspace_id, _ = self._provisioned("member")
        member, owner = self._ctx(workspace_id, ROLE_MEMBER), self._ctx(workspace_id, ROLE_OWNER)
        provisioned = self._row(workspace_id)

        read = self._get(member)
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual({field: Decimal(read.json()[field]) for field in BETA}, BETA)

        self._refused(self._post(member, "preview", {**LOWER, "expected_revision": 1}), 403, "owner_role_required")
        self.assertEqual(self._previews(workspace_id), 0)
        token = self._preview(owner, LOWER, revision=1)
        self._refused(self._post(member, "confirm", {"preview_token": token, "expected_revision": 1}),
                      403, "owner_role_required")
        with self.owner.connect() as connection:
            consumed = connection.execute(text(
                "SELECT consumed_at FROM spend_guardrail_previews WHERE id = :id"
            ), {"id": token}).scalar()
        self.assertIsNone(consumed)
        self.assertEqual(self._row(workspace_id), provisioned)

    def test_another_workspace_can_neither_read_nor_change_it(self):
        mine, _ = self._provisioned("mine")
        theirs, _ = self._provisioned("theirs")
        mine_owner, their_owner = self._ctx(mine, ROLE_OWNER), self._ctx(theirs, ROLE_OWNER)
        before = self._row(mine)

        read = self._get(their_owner)
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()["workspace_id"], theirs)

        with self._as_api(), tenant_session(their_owner) as session:
            self.assertIsNone(session.get(SpendGuardrailRecord, mine))
            self.assertIsNone(session.scalar(select(SpendGuardrailPreviewRecord).where(
                SpendGuardrailPreviewRecord.workspace_id == mine)))
            moved = session.execute(
                update(SpendGuardrailRecord)
                .where(SpendGuardrailRecord.workspace_id == mine)
                .values(per_operation_limit=Decimal("0"))
                .execution_options(synchronize_session=False)
            )
            self.assertEqual(moved.rowcount, 0)

        token = self._preview(mine_owner, LOWER, revision=1)
        self._refused(self._post(their_owner, "confirm", {"preview_token": token, "expected_revision": 1}),
                      400, "spend_guardrail_preview_missing")
        self.assertEqual(self._row(mine), before)
        self.assertEqual(self._limits(self._row(theirs)), BETA)

    def test_a_raise_above_the_platform_maximum_is_refused_and_changes_nothing(self):
        workspace_id, _ = self._provisioned("platform")
        owner = self._ctx(workspace_id, ROLE_OWNER)
        provisioned = self._row(workspace_id)

        with patch.object(config, "MARKETAI_PLATFORM_MAX_PER_OPERATION", Decimal("5")):
            body = self._refused(
                self._post(owner, "preview", {**{f: str(v) for f, v in BETA.items()},
                                              "per_operation_limit": "6", "expected_revision": 1}),
                400, "spend_guardrail_limit_above_platform",
            )
        self.assertEqual(body["params"], {"field": "per_operation_limit", "limit": "5"})
        self.assertEqual(self._previews(workspace_id), 0)
        self.assertEqual(self._row(workspace_id), provisioned)

        # Up to the maximum is a raise the Owner may confirm.
        token = self._preview(owner, LOWER, revision=1)
        self.assertEqual(self._post(owner, "confirm", {"preview_token": token, "expected_revision": 1}).status_code,
                         200)
        with patch.object(config, "MARKETAI_PLATFORM_MAX_PER_OPERATION", Decimal("5")):
            token = self._preview(owner, {**LOWER, "per_operation_limit": "5"}, revision=2)
            raised = self._post(owner, "confirm", {"preview_token": token, "expected_revision": 2})
        self.assertEqual(raised.status_code, 200, raised.text)
        self.assertEqual(Decimal(raised.json()["per_operation_limit"]), Decimal("5"))

    # ------------------------------------------------------------------
    # The function and its migration
    # ------------------------------------------------------------------

    def test_the_function_keeps_security_definer_an_empty_search_path_and_its_acl(self):
        with self.owner.connect() as connection:
            definer, settings, owner_role = connection.execute(text(
                "SELECT p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner)"
                " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'public' AND p.proname = :f"
            ), {"f": FUNCTION}).one()
            self.assertTrue(definer)
            self.assertEqual(settings, ['search_path=""'])
            self.assertEqual(owner_role, "marketai_domain_owner")
            executes = dict(connection.execute(text(
                "SELECT r.rolname, has_function_privilege(r.rolname, p.oid, 'EXECUTE')"
                " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace CROSS JOIN pg_roles r"
                " WHERE n.nspname = 'public' AND p.proname = :f AND r.rolname IN"
                " ('marketai_domain_api', 'marketai_domain_worker', 'marketai_domain_readonly')"
            ), {"f": FUNCTION}).all())
            public = connection.execute(text(
                "SELECT bool_or(acl.grantee = 0) FROM pg_proc p"
                " CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) AS acl"
                " WHERE p.proname = :f"
            ), {"f": FUNCTION}).scalar()
        self.assertEqual(executes, {"marketai_domain_api": True, "marketai_domain_worker": False,
                                    "marketai_domain_readonly": False})
        self.assertFalse(public, "PUBLIC may execute the provisioning function")

    def _alembic(self, *arguments: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "MARKETAI_DATABASE_URL": ROLE_URLS["owner"]}, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def test_the_downgrade_provisions_at_zero_and_the_upgrade_at_the_defaults_again(self):
        self.assertIn("(head)", self._alembic("current"))  # later migrations may follow REVISION
        self.addCleanup(self._alembic, "upgrade", "head")  # never leave the cluster below head

        self._alembic("downgrade", DOWN_REVISION)
        self.assertIn(DOWN_REVISION, self._alembic("current"))
        below, _ = self._provisioned("below")
        self.assertEqual(self._limits(self._row(below)), ZERO)

        self._alembic("upgrade", REVISION)
        self.assertIn(REVISION, self._alembic("current"))
        above, _ = self._provisioned("above")
        self.assertEqual(self._limits(self._row(above)), BETA)
        self.assertEqual(self._limits(self._row(below)), ZERO, "the upgrade backfilled an existing guardrail")
        self.test_the_function_keeps_security_definer_an_empty_search_path_and_its_acl()


if __name__ == "__main__":
    unittest.main()
