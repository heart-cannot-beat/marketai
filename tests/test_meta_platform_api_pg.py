"""The platform Meta API under the real API role on PostgreSQL (#91, #94).

``app.meta.platform_api`` starts without a TenantContext, and
``marketai_domain_api`` is NOBYPASSRLS with SELECT only on ``workspaces`` and
only the non-secret columns of ``credential_versions``. SQLite sees none of
that, so every route here runs through ``TestClient`` with the domain
sessions bound to the API role:

* bootstrap creates the platform Workspace through
  ``marketai_create_platform_workspace_v1`` (#91), with its audit event;
* bootstrap of an existing Workspace signs for its current epoch, and the
  ``internal_validation`` credential check reads only granted columns (#94);
* a customer Workspace the control plane has not projected is a 409, and an
  existing customer Workspace is never promoted;
* credential revocation learns the owning Workspace from
  ``marketai_credential_version_workspace_v1`` and revokes under RLS (#94);
* only the API may execute either function, and the API still cannot read
  the ciphertext.

Workspaces the control plane would project are seeded as the schema owner.
Rows are not deleted afterwards: ``audit_events`` is append-only and
references its Workspace.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``; ``DOMAIN_READONLY_DATABASE_URL`` adds the read-only role
to the refusal checks.
"""
from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, WorkspaceRecord
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credentials import CredentialVersionRecord
from app.meta.models import MetaConnectionRecord, MetaDataSourceBindingRecord
from app.platform.tenancy import TenantContext

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]
OPERATOR = "platform:pg-operator"
CREATE = "marketai_create_platform_workspace_v1(text, text)"
OWNER_OF = "marketai_credential_version_workspace_v1(text)"


@unittest.skipUnless(
    all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
    "requires isolated PostgreSQL runtime roles",
)
class PlatformMetaApiPostgresTests(unittest.TestCase):
    def setUp(self):
        from app.main import app
        from app.meta.platform_api import LegacyPlatformContext, request_platform_context

        self.owner, self.api, self.worker = [create_engine(url) for url in URLS]
        for engine in (self.owner, self.api, self.worker):
            self.addCleanup(engine.dispose)
        api_sessions = sessionmaker(bind=self.api, expire_on_commit=False, autoflush=False)
        as_api = patch("app.adapters.persistence.database.SessionLocal", api_sessions)
        as_api.start()
        self.addCleanup(as_api.stop)

        app.dependency_overrides[request_platform_context] = lambda: LegacyPlatformContext(
            actor_id=OPERATOR, workspace_id=None, auth_method="jwt",
        )
        self.addCleanup(app.dependency_overrides.pop, request_platform_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _workspace_id(prefix: str) -> str:
        return f"ws_meta_platform_pg_{prefix}_{uuid.uuid4().hex[:10]}"

    def _seed_workspace(self, workspace_id: str, *, epoch: int, is_platform: bool = False) -> None:
        """What the control plane's projection (or an earlier bootstrap) left."""
        with Session(self.owner) as session, session.begin():
            session.add(WorkspaceRecord(
                id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}",
                authorization_epoch=epoch, is_platform_workspace=is_platform,
            ))

    def _bootstrap(self, workspace_id: str, meta_mode: str = "internal_validation"):
        body = {"workspace_id": workspace_id, "meta_mode": meta_mode, "ad_account_id": "act_acme_charge"}
        if meta_mode == "internal_validation":
            body |= {"access_token_material": "token-acme", "app_secret_material": "secret-acme"}
        return self.client.post("/api/platform/v1/meta/bootstrap", json=body)

    def _workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        with Session(self.owner) as session:
            row = session.get(WorkspaceRecord, workspace_id)
            if row is not None:
                session.expunge(row)
            return row

    def _workspace_audit(self, workspace_id: str) -> list[tuple]:
        with Session(self.owner) as session:
            return [
                (row.event_type, row.executor, row.initiator_id, row.initiator_role,
                 row.resource_id, row.details_json)
                for row in session.scalars(select(AuditEventRecord).where(
                    AuditEventRecord.workspace_id == workspace_id,
                    AuditEventRecord.resource_type == "workspace",
                ))
            ]

    def _credential(self, version_id: str) -> tuple[str, str, str | None]:
        with Session(self.owner) as session:
            row = session.get(CredentialVersionRecord, version_id)
            return row.workspace_id, row.state, row.revoked_by

    def _credential_ids(self, workspace_id: str) -> list[str]:
        with Session(self.owner) as session:
            return list(session.scalars(select(CredentialVersionRecord.id).where(
                CredentialVersionRecord.workspace_id == workspace_id)))

    # -- bootstrap (#91) ------------------------------------------------------

    def test_bootstrap_creates_the_platform_workspace_as_the_api_role(self):
        workspace_id = self._workspace_id("new")
        resp = self._bootstrap(workspace_id)
        self.assertEqual(resp.status_code, 201, resp.text)
        created = resp.json()

        workspace = self._workspace(workspace_id)
        self.assertTrue(workspace.is_platform_workspace)
        self.assertEqual((workspace.name, workspace.authorization_epoch, workspace.lifecycle),
                         (workspace_id, 0, "active"))
        self.assertEqual(self._workspace_audit(workspace_id), [(
            "workspace.platform_created", "system:platform_admin", OPERATOR, "system",
            workspace_id, {"is_platform_workspace": True},
        )])
        with Session(self.owner) as session:
            binding = session.get(MetaDataSourceBindingRecord, created["binding_id"])
            self.assertEqual(
                (binding.meta_mode, binding.access_token_version_id, binding.app_secret_version_id),
                ("internal_validation", created["access_token_version_id"], created["app_secret_version_id"]),
            )
        self.assertEqual(self._credential(created["access_token_version_id"])[1], "active")

    def test_bootstrap_of_an_existing_platform_workspace_validates_its_credentials(self):
        """The internal_validation credential check (#94) runs as the API in a
        context signed for the Workspace's current epoch; the row is kept."""
        workspace_id = self._workspace_id("existing")
        self._seed_workspace(workspace_id, epoch=4, is_platform=True)

        resp = self._bootstrap(workspace_id)
        self.assertEqual(resp.status_code, 201, resp.text)
        created = resp.json()

        with Session(self.owner) as session:
            binding = session.get(MetaDataSourceBindingRecord, created["binding_id"])
            self.assertEqual(binding.state, "active")
            self.assertEqual(binding.app_secret_version_id, created["app_secret_version_id"])
        workspace = self._workspace(workspace_id)
        self.assertEqual((workspace.authorization_epoch, workspace.is_platform_workspace), (4, True))
        self.assertEqual(self._workspace_audit(workspace_id), [], "nothing was created")

    def test_bootstrap_of_a_projected_customer_workspace_signs_for_its_epoch(self):
        workspace_id = self._workspace_id("customer")
        self._seed_workspace(workspace_id, epoch=2)

        resp = self._bootstrap(workspace_id, "customer_sandbox")
        self.assertEqual(resp.status_code, 201, resp.text)
        with Session(self.owner) as session:
            connection = session.get(MetaConnectionRecord, resp.json()["connection_id"])
            self.assertEqual((connection.workspace_id, connection.meta_mode),
                             (workspace_id, "customer_sandbox"))
        self.assertFalse(self._workspace(workspace_id).is_platform_workspace)

    def test_a_customer_workspace_the_control_plane_has_not_projected_is_a_409(self):
        for meta_mode in ("customer_sandbox", "controlled_mock"):
            with self.subTest(meta_mode=meta_mode):
                workspace_id = self._workspace_id("unprojected")
                resp = self._bootstrap(workspace_id, meta_mode)
                self.assertEqual(resp.status_code, 409, resp.text)
                self.assertEqual(resp.json()["code"], "workspace_not_provisioned")
                self.assertIsNone(self._workspace(workspace_id))

    def test_an_existing_customer_workspace_is_never_promoted(self):
        workspace_id = self._workspace_id("promote")
        self._seed_workspace(workspace_id, epoch=2)

        resp = self._bootstrap(workspace_id)
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("platform workspace", resp.json()["detail"])
        self.assertEqual(self._credential_ids(workspace_id), [], "the refusal rolled the credentials back")

        # The function itself, called by the API for the same Workspace.
        with self.api.begin() as connection:
            created = connection.execute(
                text(f"SELECT public.marketai_create_platform_workspace_v1(:ws, :who)"),
                {"ws": workspace_id, "who": OPERATOR},
            ).scalar_one()
        self.assertFalse(created)
        workspace = self._workspace(workspace_id)
        self.assertEqual(
            (workspace.is_platform_workspace, workspace.authorization_epoch, workspace.name),
            (False, 2, f"Acme Charge {workspace_id[-4:]}"),
        )
        self.assertEqual(self._workspace_audit(workspace_id), [])

    def test_the_create_function_refuses_an_empty_workspace_or_operator(self):
        for workspace_id, initiator in (("", OPERATOR), (self._workspace_id("blank"), ""),
                                        ("w" * 65, OPERATOR)):
            with self.subTest(workspace_id=workspace_id[:8], initiator=initiator):
                with self.api.begin() as connection, self.assertRaisesRegex(
                    Exception, "invalid platform workspace request",
                ):
                    connection.execute(
                        text("SELECT public.marketai_create_platform_workspace_v1(:ws, :who)"),
                        {"ws": workspace_id, "who": initiator},
                    )

    # -- revocation (#94) ---------------------------------------------------

    def test_revoke_a_platform_credential_as_the_api_role(self):
        workspace_id = self._workspace_id("revoke")
        created = self._bootstrap(workspace_id).json()
        version_id = created["access_token_version_id"]

        resp = self.client.post(f"/api/platform/v1/credentials/{version_id}/revoke",
                                json={"reason": "rotation drill"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._credential(version_id), (workspace_id, "revoked", OPERATOR))
        self.assertEqual(self._credential(created["app_secret_version_id"])[1], "active")

        again = self.client.post(f"/api/platform/v1/credentials/{version_id}/revoke",
                                 json={"reason": "again"})
        self.assertEqual(again.status_code, 404, again.text)
        unknown = self.client.post("/api/platform/v1/credentials/cv_does_not_exist/revoke",
                                   json={"reason": "n/a"})
        self.assertEqual(unknown.status_code, 404, unknown.text)

    def test_revoke_a_customer_credential_at_a_later_epoch(self):
        workspace_id = self._workspace_id("customer_revoke")
        self._seed_workspace(workspace_id, epoch=5)
        context = TenantContext(workspace_id=workspace_id, role="owner", initiator_id="owner_acme",
                                authorization_epoch=5)
        from app.meta.platform_api import _get_protector

        with tenant_session(context) as session:  # the Workspace Owner, as the API
            version_id = CredentialAdmin(_get_protector()).create(
                session, context, credential_key="openai_api_key", provider="openai",
                plaintext=b"customer-material",
            ).credential_version_id

        resp = self.client.post(f"/api/platform/v1/credentials/{version_id}/revoke",
                                json={"reason": "reported leak"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._credential(version_id), (workspace_id, "revoked", OPERATOR))

    # -- the functions and the grants ---------------------------------------

    def test_the_owner_function_returns_only_the_workspace_id(self):
        workspace_id = self._workspace_id("owner_of")
        version_id = self._bootstrap(workspace_id).json()["app_secret_version_id"]
        with self.api.connect() as connection:
            query = text("SELECT public.marketai_credential_version_workspace_v1(:id)")
            self.assertEqual(connection.execute(query, {"id": version_id}).scalar_one(), workspace_id)
            self.assertIsNone(connection.execute(query, {"id": "cv_does_not_exist"}).scalar_one())

    def test_only_the_api_may_execute_the_functions(self):
        """Each runs as the owner with an empty search_path, PUBLIC has no
        EXECUTE, and the Worker and Read-only roles are refused."""
        expected = {CREATE: ("v", "boolean"), OWNER_OF: ("s", "text")}
        with self.owner.connect() as connection:
            for signature, (volatility, result) in expected.items():
                with self.subTest(signature=signature):
                    row = connection.execute(text(
                        "SELECT p.prosecdef, p.proconfig, p.provolatile, pg_get_function_result(p.oid) "
                        "  FROM pg_proc p WHERE p.oid = CAST(:signature AS regprocedure)"
                    ), {"signature": f"public.{signature}"}).one()
                    self.assertEqual(tuple(row), (True, ['search_path=""'], volatility, result))
                    for role, allowed in (("marketai_domain_api", True), ("marketai_domain_worker", False),
                                          ("marketai_domain_readonly", False), ("marketai_domain_app", False)):
                        self.assertEqual(connection.scalar(text(
                            "SELECT has_function_privilege(CAST(:role AS text), "
                            "CAST(:signature AS regprocedure), 'EXECUTE')"
                        ), {"role": role, "signature": f"public.{signature}"}), allowed, role)
                    self.assertFalse(connection.scalar(text(
                        "SELECT coalesce(bool_or(acl.privilege_type = 'EXECUTE'), false) "
                        "  FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl "
                        " WHERE p.oid = CAST(:signature AS regprocedure) AND acl.grantee = 0"
                    ), {"signature": f"public.{signature}"}), "PUBLIC may not execute")

        engines = [self.worker]
        if os.getenv("DOMAIN_READONLY_DATABASE_URL"):
            engines.append(create_engine(os.environ["DOMAIN_READONLY_DATABASE_URL"]))
            self.addCleanup(engines[-1].dispose)
        queries = (
            "SELECT public.marketai_create_platform_workspace_v1('ws_meta_platform_pg_refused', 'x')",
            "SELECT public.marketai_credential_version_workspace_v1('cv_any')",
        )
        for engine in engines:
            for query in queries:
                with self.subTest(role=engine.url.username, query=query):
                    with engine.connect() as connection, self.assertRaisesRegex(Exception, "permission denied"):
                        connection.execute(text(query))
        self.assertIsNone(self._workspace("ws_meta_platform_pg_refused"))

    def test_the_api_still_cannot_read_the_ciphertext(self):
        for column in ("encrypted_secret", "encrypted_data_key", "nonce"):
            with self.subTest(column=column):
                with self.api.connect() as connection, self.assertRaisesRegex(Exception, "permission denied"):
                    connection.execute(text(f"SELECT {column} FROM public.credential_versions LIMIT 1"))


if __name__ == "__main__":
    unittest.main()
