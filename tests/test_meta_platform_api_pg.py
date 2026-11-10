"""The platform Meta API under the real API role on PostgreSQL (#91, #94, #102).

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
* the connection list, a connection by id and its reconcile learn where a
  connection lives from ``marketai_meta_connection_ids_v1`` and
  ``marketai_meta_connection_workspace_v1`` and read it in a context signed
  for its Workspace, Meta mode and current epoch (#102);
* only the API may execute these functions, and the API still cannot read
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
from app.meta.platform_api import CONNECTION_LIST_LIMIT, _binding_dict, _connection_dict
from app.platform.tenancy import TenantContext

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]
OPERATOR = "platform:pg-operator"
CREATE = "marketai_create_platform_workspace_v1(text, text)"
OWNER_OF = "marketai_credential_version_workspace_v1(text)"
CONNECTION_IDS = "marketai_meta_connection_ids_v1(text, integer)"
CONNECTION_OWNER = "marketai_meta_connection_workspace_v1(text)"
CONNECTIONS = "/api/platform/v1/meta/connections"


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

    def _bootstrap(self, workspace_id: str, meta_mode: str = "internal_validation",
                   ad_account_id: str = "act_acme_charge"):
        body = {"workspace_id": workspace_id, "meta_mode": meta_mode, "ad_account_id": ad_account_id}
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

    def _bump_epoch(self, workspace_id: str) -> None:
        """A membership or entitlement change after the connection was made."""
        with Session(self.owner) as session, session.begin():
            session.execute(text(
                "UPDATE public.workspaces SET authorization_epoch = authorization_epoch + 1 WHERE id = :ws"
            ), {"ws": workspace_id})

    def _connections_in(self, *workspace_ids: str) -> list[dict]:
        """These Workspaces' connections as the owner sees them, in list order."""
        with Session(self.owner) as session:
            return [_connection_dict(row) for row in session.scalars(
                select(MetaConnectionRecord)
                .where(MetaConnectionRecord.workspace_id.in_(workspace_ids))
                .order_by(MetaConnectionRecord.created_at, MetaConnectionRecord.id)
            )]

    def _binding(self, binding_id: str) -> dict:
        with Session(self.owner) as session:
            return _binding_dict(session.get(MetaDataSourceBindingRecord, binding_id))

    def _list_all(self) -> list[dict]:
        """Every page of the list: the database is shared, so ours may not be first."""
        items: list[dict] = []
        params: dict = {}
        while True:
            resp = self.client.get(CONNECTIONS, params=params)
            self.assertEqual(resp.status_code, 200, resp.text)
            page = resp.json()["items"]
            items.extend(page)
            if len(page) < CONNECTION_LIST_LIMIT:
                return items
            params = {"after": page[-1]["id"]}

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

    # -- connection reads (#102) ---------------------------------------------

    def test_the_list_reads_connections_across_workspaces_as_the_api_role(self):
        """Three connections in two Workspaces and three Meta modes, one
        Workspace at a later epoch than when its connections were made."""
        platform_ws = self._workspace_id("list_platform")
        customer_ws = self._workspace_id("list_customer")
        self._seed_workspace(customer_ws, epoch=3)
        self.assertEqual(self._bootstrap(platform_ws).status_code, 201)
        for meta_mode in ("customer_sandbox", "controlled_mock"):
            self.assertEqual(self._bootstrap(customer_ws, meta_mode).status_code, 201)
        self._bump_epoch(customer_ws)

        expected = self._connections_in(platform_ws, customer_ws)
        self.assertEqual(
            [(row["workspace_id"], row["meta_mode"]) for row in expected],
            [(platform_ws, "internal_validation"), (customer_ws, "customer_sandbox"),
             (customer_ws, "controlled_mock")],
        )
        listed = [row for row in self._list_all() if row["workspace_id"] in (platform_ws, customer_ws)]
        self.assertEqual(listed, expected)

        # What the route read before #102: untenanted, the API sees none of them.
        with self.api.connect() as connection:
            self.assertEqual(connection.scalar(text(
                "SELECT count(*) FROM public.meta_connection_records WHERE workspace_id IN (:a, :b)"
            ), {"a": platform_ws, "b": customer_ws}), 0)

    def test_the_list_pages_after_a_connection(self):
        workspace_ids = [self._workspace_id(f"page{index}") for index in range(3)]
        for workspace_id in workspace_ids:
            self._seed_workspace(workspace_id, epoch=1)
            self.assertEqual(self._bootstrap(workspace_id, "customer_sandbox").status_code, 201)
        ours = self._connections_in(*workspace_ids)

        first = self.client.get(CONNECTIONS, params={"after": ours[0]["id"], "limit": 1})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["items"], [ours[1]])
        rest = self.client.get(CONNECTIONS, params={"after": ours[1]["id"], "limit": 2})
        self.assertEqual(rest.json()["items"][0], ours[2])
        last = self.client.get(CONNECTIONS, params={"after": ours[2]["id"]})
        self.assertEqual(last.json()["items"], [], "the newest connection ends the list")

        for limit in (0, CONNECTION_LIST_LIMIT + 1):
            with self.subTest(limit=limit):
                self.assertEqual(self.client.get(CONNECTIONS, params={"limit": limit}).status_code, 422)

    def test_get_and_reconcile_find_a_connection_in_its_own_workspace(self):
        platform_ws = self._workspace_id("get_platform")
        customer_ws = self._workspace_id("get_customer")
        self._seed_workspace(customer_ws, epoch=2)
        created = {
            platform_ws: self._bootstrap(platform_ws).json(),
            customer_ws: self._bootstrap(customer_ws, "customer_sandbox").json(),
        }
        self._bump_epoch(customer_ws)

        for workspace_id, made in created.items():
            with self.subTest(workspace_id=workspace_id):
                [expected] = self._connections_in(workspace_id)
                got = self.client.get(f"{CONNECTIONS}/{made['connection_id']}")
                self.assertEqual(got.status_code, 200, got.text)
                self.assertEqual(got.json(), expected)

                reconciled = self.client.post(f"{CONNECTIONS}/{made['connection_id']}/reconcile")
                self.assertEqual(reconciled.status_code, 200, reconciled.text)
                self.assertEqual(reconciled.json(), {
                    "connection": expected, "active_binding": self._binding(made["binding_id"]),
                })

    def test_one_workspaces_binding_never_answers_for_anothers_connection(self):
        """Two Workspaces, same Meta mode, same ad account: each connection is
        listed and reconciled with its own Workspace's rows only."""
        first_ws = self._workspace_id("mix_a")
        second_ws = self._workspace_id("mix_b")
        self._seed_workspace(first_ws, epoch=1)
        self._seed_workspace(second_ws, epoch=7)
        created = {
            workspace_id: self._bootstrap(workspace_id, "customer_sandbox", "act_shared_account").json()
            for workspace_id in (first_ws, second_ws)
        }

        listed = {row["id"]: row for row in self._list_all() if row["workspace_id"] in created}
        for workspace_id, made in created.items():
            with self.subTest(workspace_id=workspace_id):
                [expected] = self._connections_in(workspace_id)
                self.assertEqual(listed[made["connection_id"]], expected)

                reconciled = self.client.post(f"{CONNECTIONS}/{made['connection_id']}/reconcile").json()
                binding = reconciled["active_binding"]
                self.assertEqual(
                    (reconciled["connection"]["workspace_id"], binding["id"], binding["workspace_id"],
                     binding["meta_connection_id"]),
                    (workspace_id, made["binding_id"], workspace_id, made["connection_id"]),
                )

    def test_an_unknown_connection_is_a_404_and_an_unknown_cursor_a_400(self):
        missing = f"{CONNECTIONS}/mcon_does_not_exist"
        for resp in (self.client.get(missing), self.client.post(f"{missing}/reconcile")):
            with self.subTest(path=resp.request.url.path):
                self.assertEqual(resp.status_code, 404, resp.text)
                self.assertEqual(resp.json()["detail"], "Meta connection not found")

        resp = self.client.get(CONNECTIONS, params={"after": "mcon_does_not_exist"})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["code"], "invalid_cursor")

    def test_the_connection_functions_return_only_where_a_connection_lives(self):
        workspace_id = self._workspace_id("where")
        self._seed_workspace(workspace_id, epoch=6)
        connection_id = self._bootstrap(workspace_id, "controlled_mock").json()["connection_id"]
        with self.owner.connect() as connection:
            before = connection.scalar(text(
                "SELECT id FROM public.meta_connection_records "
                " WHERE (created_at, id) < (SELECT created_at, id FROM public.meta_connection_records "
                "                            WHERE id = :id) "
                " ORDER BY created_at DESC, id DESC LIMIT 1"
            ), {"id": connection_id})

        with self.api.connect() as connection:
            owner_of = text("SELECT * FROM public.marketai_meta_connection_workspace_v1(:id)")
            row = connection.execute(owner_of, {"id": connection_id}).one()
            self.assertEqual(row._asdict(), {"workspace_id": workspace_id, "meta_mode": "controlled_mock"})
            self.assertIsNone(connection.execute(owner_of, {"id": "mcon_does_not_exist"}).first())

            listed = text("SELECT * FROM public.marketai_meta_connection_ids_v1(:after, :limit)")
            row = connection.execute(listed, {"after": before, "limit": 1}).one()
            self.assertEqual(row._asdict(), {
                "workspace_id": workspace_id, "meta_mode": "controlled_mock",
                "authorization_epoch": 6, "is_platform_workspace": False,
                "connection_id": connection_id,
            })
            for limit in (0, -1, None):
                with self.subTest(limit=limit):
                    self.assertEqual(connection.execute(listed, {"after": None, "limit": limit}).all(), [])
            self.assertEqual(
                connection.execute(listed, {"after": "mcon_does_not_exist", "limit": 5}).all(), [],
            )

    def test_the_list_function_returns_at_most_500_rows(self):
        """Inside one owner transaction that is rolled back, so the shared
        database keeps no extra connections."""
        workspace_id = self._workspace_id("cap")
        with self.owner.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text(
                    "INSERT INTO public.workspaces (id, name, default_currency, created_at, lifecycle, "
                    "budget_currency) VALUES (:ws, :ws, 'USD', now(), 'active', 'USD')"
                ), {"ws": workspace_id})
                connection.execute(text(
                    "INSERT INTO public.meta_connection_records "
                    "(id, workspace_id, meta_mode, ad_account_id, state, created_at, updated_at, retired_at) "
                    "SELECT :ws || '_' || n, :ws, 'customer_sandbox', 'act_acme_charge', 'retired', "
                    "       now(), now(), now() FROM generate_series(1, 501) AS n"
                ), {"ws": workspace_id})
                self.assertEqual(connection.scalar(text(
                    "SELECT count(*) FROM public.marketai_meta_connection_ids_v1(NULL, 100000)"
                )), 500)
            finally:
                transaction.rollback()
        self.assertIsNone(self._workspace(workspace_id))

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
        expected = {
            CREATE: ("v", "boolean"),
            OWNER_OF: ("s", "text"),
            CONNECTION_IDS: ("s", "TABLE(workspace_id character varying, meta_mode character varying, "
                                  "authorization_epoch integer, is_platform_workspace boolean, "
                                  "connection_id character varying)"),
            CONNECTION_OWNER: ("s", "TABLE(workspace_id character varying, meta_mode character varying)"),
        }
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
            "SELECT * FROM public.marketai_meta_connection_ids_v1(NULL, 10)",
            "SELECT * FROM public.marketai_meta_connection_workspace_v1('mcon_any')",
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
