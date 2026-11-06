"""TenantContext is the tenant boundary; DEFAULT_WORKSPACE_ID is not.

These run on the SQLite compatibility path and assert the contract every
repository will depend on. The PostgreSQL RLS half — policies, database roles
and connection-pool reuse — lands with the RLS slice and is asserted separately.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.platform.tenancy import (
    EPOCH_SETTING,
    IdentityKind,
    SYSTEM_MIGRATION,
    SYSTEM_WORKER,
    TenantContext,
    TenantScopeError,
    WORKSPACE_SETTING,
    apply_to_session,
    clear_session_context,
    current_context,
    system_context,
    tenant_context_signature,
    tenant_scope,
)


def user_context(**overrides) -> TenantContext:
    base = {
        "workspace_id": "ws_alpha",
        "role": "owner",
        "initiator_id": "user_1",
        "request_id": "req_1",
        "trace_id": "tr_1",
        "authorization_epoch": 7,
    }
    base.update(overrides)
    return TenantContext(**base)


class TenantContextTests(unittest.TestCase):
    def test_requires_a_workspace(self):
        with self.assertRaises(TenantScopeError):
            TenantContext(workspace_id="", role="owner", initiator_id="user_1")

    def test_rejects_newline_in_workspace_id(self):
        with self.assertRaises(TenantScopeError):
            TenantContext(
                workspace_id="ws_alpha\n99", role="owner", initiator_id="user_1"
            )

    def test_rejects_an_unknown_role(self):
        with self.assertRaises(TenantScopeError):
            TenantContext(
                workspace_id="ws_alpha", role="admin", initiator_id="user_1"
            )

    def test_a_user_context_must_name_the_user(self):
        # Otherwise an audit record would attribute the action to nobody.
        with self.assertRaises(TenantScopeError):
            TenantContext(workspace_id="ws_alpha", role="member")

    def test_is_immutable(self):
        context = user_context()

        with self.assertRaises(Exception):
            context.workspace_id = "ws_beta"  # type: ignore[misc]

    def test_system_execution_preserves_the_human_initiator(self):
        context = user_context()
        worker = context.for_system_execution()

        # The Worker executes, but the person who asked is still attributable.
        self.assertEqual(worker.executor, SYSTEM_WORKER)
        self.assertEqual(worker.role, "system")
        self.assertEqual(worker.initiator_role, "owner")
        self.assertEqual(worker.audit_initiator_role, "owner")
        self.assertTrue(worker.is_system)
        self.assertEqual(worker.initiator_id, "user_1")
        self.assertEqual(worker.workspace_id, "ws_alpha")
        # Deriving must not mutate the request context it came from.
        self.assertEqual(context.executor, "user")
        self.assertFalse(context.is_system)

    def test_system_executor_must_be_namespaced(self):
        with self.assertRaises(TenantScopeError):
            user_context().for_system_execution("worker")

    def test_a_system_context_may_have_no_initiator(self):
        # Scheduled work nobody asked for, e.g. a migration.
        context = system_context(
            "ws_alpha", initiator_id=None, executor=SYSTEM_MIGRATION
        )

        self.assertIsNone(context.initiator_id)
        self.assertEqual(context.executor, SYSTEM_MIGRATION)

    def test_audit_records_both_actors(self):
        actors = user_context().for_system_execution().audit_actors()

        self.assertEqual(actors["initiator_id"], "user_1")
        self.assertEqual(actors["initiator_role"], "owner")
        self.assertEqual(actors["executor"], SYSTEM_WORKER)
        self.assertEqual(actors["workspace_id"], "ws_alpha")


class SessionBindingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", future=True)
        self.addCleanup(self.engine.dispose)

    def session(self) -> Session:
        session = Session(bind=self.engine, future=True)
        self.addCleanup(session.close)
        return session

    def test_an_unscoped_session_fails_loudly(self):
        # The point of the seam: a query written without a scope raises rather
        # than quietly reading whatever DEFAULT_WORKSPACE_ID happened to be.
        with self.assertRaises(TenantScopeError):
            current_context(self.session())

    def test_binding_exposes_the_context(self):
        session = self.session()
        apply_to_session(session, user_context())

        self.assertEqual(current_context(session).workspace_id, "ws_alpha")

    def test_scope_restores_the_previous_context(self):
        session = self.session()
        apply_to_session(session, user_context())

        # Same Workspace, escalated executor: a request handing off to the
        # context the Worker will run under. This is what nesting is for.
        with tenant_scope(session, user_context().for_system_execution()):
            self.assertEqual(current_context(session).executor, SYSTEM_WORKER)

        self.assertEqual(current_context(session).workspace_id, "ws_alpha")
        self.assertEqual(current_context(session).executor, "user")

    def test_scope_refuses_a_second_workspace_in_one_transaction(self):
        # The settings are transaction-local, so a nested scope runs inside the
        # outer transaction. One transaction reading two tenants is not
        # something the domain should be able to express.
        session = self.session()
        apply_to_session(session, user_context())

        with self.assertRaises(TenantScopeError) as caught:
            with tenant_scope(session, user_context(workspace_id="ws_beta")):
                pass  # pragma: no cover - the scope must not open

        self.assertIn("ws_beta", str(caught.exception))
        # Refusing must not disturb the scope that was already bound.
        self.assertEqual(current_context(session).workspace_id, "ws_alpha")

    def test_scope_clears_when_there_was_no_previous_context(self):
        session = self.session()

        with tenant_scope(session, user_context()):
            self.assertEqual(current_context(session).workspace_id, "ws_alpha")

        with self.assertRaises(TenantScopeError):
            current_context(session)

    def test_scope_restores_even_when_the_block_raises(self):
        session = self.session()

        with self.assertRaises(ValueError):
            with tenant_scope(session, user_context()):
                raise ValueError("boom")

        with self.assertRaises(TenantScopeError):
            current_context(session)

    def test_clearing_removes_the_context(self):
        session = self.session()
        apply_to_session(session, user_context())
        clear_session_context(session)

        with self.assertRaises(TenantScopeError):
            current_context(session)

    def test_sqlite_takes_no_database_settings(self):
        # SQLite has no RLS; binding must not emit PostgreSQL-only SQL.
        session = self.session()
        apply_to_session(session, user_context())

        self.assertEqual(session.scalar(text("SELECT 1")), 1)


class SettingNameTests(unittest.TestCase):
    def test_settings_are_namespaced(self):
        # An un-namespaced name could collide with a real PostgreSQL GUC.
        for setting in (WORKSPACE_SETTING, EPOCH_SETTING):
            self.assertTrue(setting.startswith("marketai."))

    def test_context_signature_binds_workspace_and_epoch(self):
        secret = "test-only-context-secret-with-32-bytes"
        original = tenant_context_signature(user_context(), secret)

        self.assertEqual(original, tenant_context_signature(user_context(), secret))
        self.assertNotEqual(
            original,
            tenant_context_signature(user_context(workspace_id="ws_beta"), secret),
        )
        self.assertNotEqual(
            original,
            tenant_context_signature(user_context(authorization_epoch=8), secret),
        )

    def test_context_signature_is_unambiguous_across_field_boundaries(self):
        secret = "test-only-context-secret-with-32-bytes"
        # ws_alpha7 epoch 0 vs ws_alpha epoch 70 — fields must not bleed.
        sig_a = tenant_context_signature(
            user_context(workspace_id="ws_alpha7", authorization_epoch=0), secret
        )
        sig_b = tenant_context_signature(
            user_context(workspace_id="ws_alpha", authorization_epoch=70), secret
        )
        self.assertNotEqual(sig_a, sig_b)

    def test_context_signature_rejects_a_short_secret(self):
        with self.assertRaisesRegex(TenantScopeError, "at least 32"):
            tenant_context_signature(user_context(), "too-short")


def test_identity_kind_defaults_to_delegated_user():
    ctx = TenantContext(
        workspace_id="ws_1", role="owner", initiator_id="user_1",
    )
    assert ctx.identity_kind == IdentityKind.DELEGATED_USER
    assert ctx.is_platform_workspace is False


def test_delegated_user_rejects_system_role():
    """A delegated user context must have role in {owner, member}."""
    with pytest.raises(TenantScopeError, match="delegated_user.*requires.*owner.*member"):
        TenantContext(
            workspace_id="ws_1", role="system", initiator_id="user_1",
            identity_kind=IdentityKind.DELEGATED_USER,
        )


def test_platform_operator_requires_system_role():
    with pytest.raises(TenantScopeError, match="platform_operator.*requires.*system"):
        TenantContext(
            workspace_id="ws_1", role="owner", initiator_id="user_1",
            identity_kind=IdentityKind.PLATFORM_OPERATOR,
        )


def test_platform_operator_requires_system_executor():
    with pytest.raises(TenantScopeError, match="system:"):
        TenantContext(
            workspace_id="ws_1", role="system", initiator_id="op_1",
            executor="user",
            identity_kind=IdentityKind.PLATFORM_OPERATOR,
        )


def test_platform_operator_valid():
    ctx = TenantContext(
        workspace_id="ws_1", role="system", initiator_id="op_1",
        executor="system:platform_operator",
        identity_kind=IdentityKind.PLATFORM_OPERATOR,
        is_platform_workspace=True,
    )
    assert ctx.identity_kind == IdentityKind.PLATFORM_OPERATOR
    assert ctx.is_platform_workspace is True


def test_system_context_sets_identity_kind():
    ctx = system_context(
        workspace_id="ws_1", initiator_id="user_1",
    )
    assert ctx.identity_kind == IdentityKind.SYSTEM


def test_for_system_execution_preserves_identity_kind_system():
    ctx = TenantContext(
        workspace_id="ws_1", role="owner", initiator_id="user_1",
    )
    system = ctx.for_system_execution()
    assert system.identity_kind == IdentityKind.SYSTEM
    assert system.role == "system"


def test_context_signature_includes_is_platform_workspace():
    ctx_a = TenantContext(
        workspace_id="ws_1", role="system", initiator_id="op_1",
        executor="system:platform_operator",
        identity_kind=IdentityKind.PLATFORM_OPERATOR,
        is_platform_workspace=False,
    )
    ctx_b = replace(ctx_a, is_platform_workspace=True)
    secret = "a" * 32
    sig_a = tenant_context_signature(ctx_a, secret)
    sig_b = tenant_context_signature(ctx_b, secret)
    assert sig_a != sig_b


def test_ui_language_defaults_to_english_and_rejects_unoffered_languages():
    assert user_context().ui_language == "en"
    assert user_context(ui_language="zh-CN").ui_language == "zh-CN"
    with pytest.raises(TenantScopeError):
        user_context(ui_language="fr")


def test_ui_language_is_not_part_of_the_tenant_signature_or_audit_actors():
    # Wording only: two requests that differ just in language are the same
    # tenant, the same actors and the same RLS context.
    secret = "a" * 32
    english, chinese = user_context(), user_context(ui_language="zh-CN")
    assert tenant_context_signature(english, secret) == tenant_context_signature(chinese, secret)
    assert english.audit_actors() == chinese.audit_actors()


def test_system_execution_keeps_the_request_language_in_process():
    assert user_context(ui_language="zh-CN").for_system_execution().ui_language == "zh-CN"


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
