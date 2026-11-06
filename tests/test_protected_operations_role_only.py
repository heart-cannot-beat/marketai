"""The formerly step-up-protected commands check role only (ADR 0015, #378).

MFA is checked only at login. The commands ADR 0011 guarded with an MFA
step-up assertion now run on the caller's role alone: no
``X-Step-Up-Assertion`` header and no MFA evidence in the identity. For each
kind of command this file proves three things at the real FastAPI app:

* a role the command never allowed is still refused;
* an allowed role succeeds with no step-up header;
* another Workspace's target answers 404 and is left unchanged.

``config.DELEGATED_IDENTITY_ENABLED`` is patched on so the credential and
guardrail routes run as in production (before ADR 0015 they skipped step-up
only when delegated identity was off). Scale targets and Meta connections
were only ever gated at the BFF; their FastAPI role and tenant rules are
covered by ``tests/test_scale_targets.py`` and the Meta suites, and the BFF
side by ``web/__tests__/api/domain/v1/w/domain-proxy-handler.spec.ts``.
"""
from __future__ import annotations

import base64
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.support.library_fixtures import LibraryFixtures
from tests.test_library_datasource_api import _provision_credential_version

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import LibraryAuditRecord, LibraryVersionRecord
from app.entitlements.cost_models import SpendGuardrailRecord
from app.entitlements.credentials import CredentialVersionRecord
from app.library import service
from app.library.contracts import ENTRY_DATA_SOURCE, ROLE_MEMBER, ROLE_OWNER
from app.library.datasources import get_active_binding
from app.main import app
from app.platform.http_identity import request_tenant_context


#: ``ProblemDetails.required`` in ``contracts/openapi/marketai-domain-v1.json``.
PROBLEM_REQUIRED = ("type", "title", "status", "code", "trace_id", "retryable")


def _data_source_content() -> dict:
    return {
        "adapter_kind": "manual",
        "capabilities": ["price"],
        "market_context": ["US"],
        "config": {},
        "config_schema_version": "manual/1.0",
        "execution_mode": "real",
    }


class _RoleOnlyHarness(LibraryFixtures):
    """An Owner and a Member of one Workspace, an Owner of another, and a
    TestClient whose identity is whichever context ``_as`` selected."""

    def setUp(self):
        self.owner = self._make_context(role=ROLE_OWNER, actor="owner_role_only")
        self.member = self._make_context(
            workspace_id=self.owner.workspace_id, role=ROLE_MEMBER, actor="member_role_only",
        )
        self.outsider = self._make_context(role=ROLE_OWNER, actor="outsider_role_only")
        self._current = self.owner
        app.dependency_overrides[request_tenant_context] = lambda: self._current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        delegated = patch("app.config.DELEGATED_IDENTITY_ENABLED", True)
        delegated.start()
        self.addCleanup(delegated.stop)
        self._seq = 0

    def _as(self, context):
        self._current = context

    def _key(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self.owner.workspace_id}-{self._seq}"

    def _post(self, context, path: str, body: dict, **headers):
        self._as(context)
        return self.client.post(path, json=body, headers={"Idempotency-Key": self._key("ik"), **headers})

    def assertNoStepUpDemanded(self, resp):
        self.assertNotIn("step_up", resp.text)
        self.assertNotIn("mfa", resp.text.lower())


class TestLibraryPublishAndWithdraw(_RoleOnlyHarness):
    """``publish_library_entry`` / ``withdraw_library_version`` on a data
    source, the entry type ADR 0011 used to require step-up for."""

    def _data_source(self) -> dict:
        return service.create_entry(
            self.owner, entry_type=ENTRY_DATA_SOURCE, display_name="Role-only source",
            content=_data_source_content(),
        )

    def _publish(self, context, entry_id: str):
        return self._post(context, f"/api/v1/library/entries/{entry_id}/publish", {}, **{"If-Match": "1"})

    def _versions(self, entry_id: str) -> list[LibraryVersionRecord]:
        with platform_session() as session:
            return list(session.scalars(
                select(LibraryVersionRecord).where(LibraryVersionRecord.entry_id == entry_id)
            ).all())

    def _audits(self, command: str, entry_id: str) -> list[LibraryAuditRecord]:
        with platform_session() as session:
            return list(session.scalars(
                select(LibraryAuditRecord).where(
                    LibraryAuditRecord.command == command,
                    LibraryAuditRecord.entry_id == entry_id,
                ).order_by(LibraryAuditRecord.created_at)
            ).all())

    def test_member_still_cannot_publish_a_data_source(self):
        entry = self._data_source()

        resp = self._publish(self.member, entry["id"])

        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["code"], "library_forbidden")
        self.assertEqual(self._versions(entry["id"]), [])

    def test_owner_publishes_a_data_source_without_any_step_up(self):
        entry = self._data_source()

        resp = self._publish(self.owner, entry["id"])

        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["state"], "effective")
        rows = self._audits("publish", entry["id"])
        self.assertEqual([row.result for row in rows], ["ok"])
        self.assertEqual(rows[0].initiator_id, "owner_role_only")
        self.assertNotIn("step_up", rows[0].detail_json)

    def test_another_workspace_cannot_publish_it(self):
        entry = self._data_source()

        resp = self._publish(self.outsider, entry["id"])

        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self._versions(entry["id"]), [])

    def _published_version_id(self) -> tuple[str, str]:
        entry = self._data_source()
        resp = self._publish(self.owner, entry["id"])
        self.assertEqual(resp.status_code, 201, resp.text)
        return entry["id"], resp.json()["id"]

    def _withdraw(self, context, version_id: str):
        return self._post(context, f"/api/v1/library/versions/{version_id}/withdraw", {"reason": "role-only test"})

    def test_member_still_cannot_withdraw(self):
        _entry_id, version_id = self._published_version_id()

        resp = self._withdraw(self.member, version_id)

        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["code"], "library_forbidden")
        self.assertEqual(service.get_version(self.owner, version_id)["state"], "effective")

    def test_owner_withdraws_without_any_step_up(self):
        entry_id, version_id = self._published_version_id()

        resp = self._withdraw(self.owner, version_id)

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["state"], "withdrawn")
        row = self._audits("withdraw", entry_id)[-1]
        self.assertEqual(row.result, "ok")
        self.assertNotIn("step_up", row.detail_json)

    def test_another_workspace_cannot_withdraw(self):
        _entry_id, version_id = self._published_version_id()

        resp = self._withdraw(self.outsider, version_id)

        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(service.get_version(self.owner, version_id)["state"], "effective")


class TestDataSourceCredentialBinding(_RoleOnlyHarness):
    """``bind_library_data_source_credential``."""

    def setUp(self):
        super().setUp()
        self.entry = service.create_entry(
            self.owner, entry_type=ENTRY_DATA_SOURCE, display_name="Bound source",
            content={"adapter_kind": "manual", "label": "Bound source"},
        )
        with tenant_session(self.owner) as session:
            self.credential_version_id = _provision_credential_version(self.owner, session)

    def _bind(self, context):
        return self._post(
            context, f"/api/v1/library/entries/{self.entry['id']}/credential-binding",
            {"credential_version_id": self.credential_version_id},
        )

    def test_member_still_cannot_bind(self):
        resp = self._bind(self.member)

        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertIsNone(get_active_binding(self.owner, self.entry["id"]))

    def test_owner_binds_without_any_step_up(self):
        resp = self._bind(self.owner)

        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["credential_version_id"], self.credential_version_id)
        self.assertNoStepUpDemanded(resp)

    def test_another_workspace_cannot_bind(self):
        resp = self._bind(self.outsider)

        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertIsNone(get_active_binding(self.owner, self.entry["id"]))


class TestProviderCredentials(_RoleOnlyHarness):
    """``create_credential`` / ``bind_credential`` / ``revoke_credential``
    (``rotate_credential`` shares the same guard and route shape)."""

    def _create(self, context, key: str = "openai_api_key"):
        self._as(context)
        return self.client.post("/api/v1/credentials", json={
            "credential_key": key,
            "provider": "openai",
            "plaintext_b64": base64.b64encode(b"sk-proj-role-only").decode("ascii"),
            "display_label": "Role-only key",
        })

    def _active_versions(self, context) -> list[str]:
        with platform_session() as session:
            return list(session.scalars(
                select(CredentialVersionRecord.id).where(
                    CredentialVersionRecord.workspace_id == context.workspace_id,
                    CredentialVersionRecord.state == "active",
                )
            ).all())

    def test_member_still_cannot_create_a_credential(self):
        resp = self._create(self.member)

        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["code"], "owner_role_required")
        self.assertEqual(self._active_versions(self.owner), [])

    def test_owner_creates_binds_and_revokes_without_any_step_up(self):
        created = self._create(self.owner)
        self.assertEqual(created.status_code, 200, created.text)
        version_id = created.json()["credential_version_id"]
        self.assertNoStepUpDemanded(created)

        self._as(self.owner)
        bound = self.client.put(
            f"/api/v1/credentials/{version_id}/bindings/generation", json={"provider": "openai"},
        )
        self.assertEqual(bound.status_code, 200, bound.text)

        revoked = self.client.post(
            f"/api/v1/credentials/{version_id}/revoke", json={"reason": "role-only test"},
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(self._active_versions(self.owner), [])

    def test_another_workspace_cannot_bind_or_revoke_it(self):
        created = self._create(self.owner)
        self.assertEqual(created.status_code, 200, created.text)
        version_id = created.json()["credential_version_id"]

        self._as(self.outsider)
        revoked = self.client.post(
            f"/api/v1/credentials/{version_id}/revoke", json={"reason": "not mine"},
        )
        self.assertEqual(revoked.status_code, 404, revoked.text)
        bound = self.client.put(
            f"/api/v1/credentials/{version_id}/bindings/generation", json={"provider": "openai"},
        )
        self.assertEqual(bound.status_code, 404, bound.text)
        self.assertEqual(self._active_versions(self.owner), [version_id])


class TestSpendGuardrail(_RoleOnlyHarness):
    """``confirm_spend_guardrail`` (with its preview)."""

    LIMITS = {
        "per_operation_limit": "0.50",
        "rolling_24h_limit": "2.00",
        "period_limit": "10.00",
        "outstanding_exposure_limit": "1.00",
    }

    def setUp(self):
        super().setUp()
        self._seed_guardrail(self.owner.workspace_id)

    def _seed_guardrail(self, workspace_id: str) -> None:
        now = datetime.now(timezone.utc)
        with platform_session() as session:
            session.add(SpendGuardrailRecord(
                workspace_id=workspace_id, revision=1, budget_currency="USD",
                per_operation_limit=Decimal("1.00"), rolling_24h_limit=Decimal("5.00"),
                period_limit=Decimal("20.00"), outstanding_exposure_limit=Decimal("3.00"),
                platform_policy_version=1, created_at=now, updated_at=now, updated_by="seed",
            ))
            session.commit()

    def _preview_response(self):
        self._as(self.owner)
        return self.client.post(
            "/api/v1/spend-guardrail/preview", json={**self.LIMITS, "expected_revision": 1},
        )

    def _preview(self) -> str:
        resp = self._preview_response()
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["preview_token"]

    def _confirm(self, context, token: str):
        self._as(context)
        return self.client.post(
            "/api/v1/spend-guardrail/confirm", json={"preview_token": token, "expected_revision": 1},
        )

    def _revision(self, workspace_id: str | None = None) -> int:
        with platform_session() as session:
            return session.get(SpendGuardrailRecord, workspace_id or self.owner.workspace_id).revision

    def test_member_still_cannot_confirm(self):
        token = self._preview()

        resp = self._confirm(self.member, token)

        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["code"], "owner_role_required")
        self.assertEqual(self._revision(), 1)

    def test_owner_confirms_without_any_step_up(self):
        token = self._preview()

        resp = self._confirm(self.owner, token)

        self.assertEqual(resp.status_code, 200, resp.text)
        # Read back from the stored preview and guardrail, as GET shows it.
        self.assertEqual(Decimal(resp.json()["per_operation_limit"]), Decimal("0.50"))
        self.assertEqual(self._revision(), 2)

    def test_another_workspace_cannot_change_it(self):
        """The guardrail has no id in its path: it is the caller's own
        Workspace's, and the outsider's Workspace has none. The confirm
        route renders that as its existing 400 ``spend_guardrail_not_found``
        rather than a 404, and this Workspace's guardrail is kept."""
        token = self._preview()

        resp = self._confirm(self.outsider, token)

        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["code"], "spend_guardrail_not_found")
        self.assertEqual(self._revision(), 1)

    def test_another_workspace_with_its_own_guardrail_cannot_use_the_token(self):
        """The outsider's Workspace has a guardrail at the same revision, so
        only the preview's Workspace binding refuses the token (#386)."""
        self._seed_guardrail(self.outsider.workspace_id)
        token = self._preview()

        resp = self._confirm(self.outsider, token)

        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertTrue(set(PROBLEM_REQUIRED) <= body.keys(), sorted(body))
        self.assertEqual(body["code"], "spend_guardrail_preview_missing")
        self.assertEqual(self._revision(self.outsider.workspace_id), 1)
        self.assertEqual(self._revision(), 1)
        self.assertEqual(self._confirm(self.owner, token).status_code, 200)
        self.assertEqual(self._revision(), 2)

    def test_the_preview_says_when_it_expires(self):
        before = datetime.now(timezone.utc)
        resp = self._preview_response()

        self.assertEqual(resp.status_code, 200, resp.text)
        expires_at = datetime.fromisoformat(resp.json()["expires_at"])
        self.assertIsNotNone(expires_at.tzinfo)
        self.assertLessEqual(before + timedelta(minutes=15), expires_at)
        self.assertLessEqual(expires_at, datetime.now(timezone.utc) + timedelta(minutes=15))


class TestScaleTargets(_RoleOnlyHarness):
    """``create_scale_target`` / ``withdraw_scale_target``. ADR 0011 gated
    them at the BFF only; this pins the FastAPI side, which never asked for
    an assertion, to the same role-only rules."""

    BASE = "/api/v1/scale/customer_sandbox"

    def _publication_preview(self) -> str:
        resp = self._post(self.owner, f"{self.BASE}/target-publication-previews", {
            "product_version_id": f"pv_{self.owner.workspace_id}",
            "offer_version_id": f"ov_{self.owner.workspace_id}",
            "market_code": "US",
            "currency": "USD",
            "target_cpa": "20.00",
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["id"]

    def _publish(self, context, preview_id: str):
        return self._post(context, f"{self.BASE}/targets", {"publication_preview_id": preview_id})

    def _published_target(self) -> str:
        resp = self._publish(self.owner, self._publication_preview())
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["id"]

    def _withdraw(self, context, target_id: str, preview_id: str):
        return self._post(
            context, f"{self.BASE}/targets/{target_id}/withdraw",
            {"withdrawal_preview_id": preview_id},
        )

    def _withdrawal_preview(self, target_id: str) -> str:
        resp = self._post(
            self.owner, f"{self.BASE}/targets/{target_id}/withdrawal-previews",
            {"reason": "role-only test"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["id"]

    def _state(self, target_id: str) -> str:
        self._as(self.owner)
        resp = self.client.get(f"{self.BASE}/targets/{target_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["lifecycle_state"]

    def assertBffProblem(self, resp, status: int, code: str) -> None:
        """The refusal must pass the BFF's ``ProblemDetails`` validator, or
        the browser sees 502 ``invalid_domain_response`` instead of ``code``."""
        self.assertEqual(resp.status_code, status, resp.text)
        self.assertTrue(
            resp.headers["content-type"].startswith("application/problem+json"),
            resp.headers["content-type"],
        )
        body = resp.json()
        self.assertTrue(set(PROBLEM_REQUIRED) <= body.keys(), sorted(body))
        self.assertEqual((body["status"], body["code"]), (status, code))
        self.assertEqual(body["trace_id"], resp.headers.get("X-Trace-Id"))

    def test_member_still_cannot_publish_or_withdraw(self):
        resp = self._publish(self.member, self._publication_preview())
        self.assertBffProblem(resp, 403, "owner_required")

        target_id = self._published_target()
        resp = self._withdraw(self.member, target_id, self._withdrawal_preview(target_id))
        self.assertBffProblem(resp, 403, "owner_required")
        self.assertEqual(self._state(target_id), "active")

    def test_owner_publishes_and_withdraws_without_any_step_up(self):
        target_id = self._published_target()
        self.assertEqual(self._state(target_id), "active")

        resp = self._withdraw(self.owner, target_id, self._withdrawal_preview(target_id))

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["lifecycle_state"], "withdrawn")
        self.assertNoStepUpDemanded(resp)

    def test_another_workspace_cannot_publish_or_withdraw(self):
        resp = self._publish(self.outsider, self._publication_preview())
        self.assertBffProblem(resp, 404, "target_not_found")

        target_id = self._published_target()
        resp = self._withdraw(self.outsider, target_id, self._withdrawal_preview(target_id))
        self.assertBffProblem(resp, 404, "target_not_found")
        self.assertEqual(self._state(target_id), "active")


if __name__ == "__main__":
    unittest.main()
