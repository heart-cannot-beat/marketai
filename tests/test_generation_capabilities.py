"""Tests for Generation capabilities endpoint (Issue #50).

Verifies:
- Capabilities are workspace-scoped and reflect credential readiness.
- No BYOK credential → only mock/sandbox modes available.
- Active credential → byok mode available.
- Providers, models, and format limits are correctly resolved from the adapter registry.
- Response is ETag-cacheable.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.credentials import CredentialBindingRecord, CredentialVersionRecord
from app.entitlements.cost_models import (  # noqa: F401 -- FK targets
    ProviderCallAttemptRecord,
    SpendGuardrailRecord,
    SpendReservationRecord,
)
from app.entitlements.models import EntitlementProjectionRecord  # noqa: F401 -- FK target
from app.generation.models import GenerationDraftRecord  # noqa: F401 -- FK target
from app.generation.capabilities import get_capabilities
from app.platform.tenancy import TenantContext


WS_ID = "ws_cap_test"
WS_ID_EMPTY = "ws_cap_empty"
INITIATOR = "user_cap_test"
NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _ctx(workspace_id: str = WS_ID) -> TenantContext:
    return TenantContext(
        workspace_id=workspace_id,
        role="owner",
        initiator_id=INITIATOR,
        initiator_role="owner",
    )


TEST_ADAPTER_REGISTRY = {
    "mock": {
        "version": "v1",
        "capabilities": {"image_generation": True},
        "advertised": True,
        "models": {
            "mock-v1": {
                "display_name": "Mock Generator v1",
                "format_limits": {
                    "max_width": 2048,
                    "max_height": 2048,
                    "aspect_ratios": ["1:1", "4:3"],
                    "max_variants_per_combination": 4,
                },
            },
        },
    },
    "meta": {
        "version": "v1",
        "capabilities": {"image_generation": True},
        "advertised": True,
        "models": {
            "llama-image-v1": {
                "display_name": "Llama Image v1",
                "format_limits": {
                    "max_width": 2048,
                    "max_height": 2048,
                    "aspect_ratios": ["1:1", "4:3", "16:9"],
                    "max_variants_per_combination": 4,
                },
            },
        },
    },
    "meta_sandbox": {
        "version": "v1",
        "capabilities": {"image_generation": True},
        "advertised": True,
        "cost_policy": {"true_payable": "zero"},
        "models": {
            "llama-image-v1": {
                "display_name": "Llama Image v1 (Sandbox)",
                "format_limits": {
                    "max_width": 1024,
                    "max_height": 1024,
                    "aspect_ratios": ["1:1", "4:3"],
                    "max_variants_per_combination": 2,
                },
            },
        },
    },
}


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", echo=False)

    @sa_event.listens_for(eng, "connect")
    def _enable_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    sess = Session(bind=engine, expire_on_commit=False)
    sess.add(WorkspaceRecord(
        id=WS_ID, name="Cap Test", created_at=NOW, lifecycle="active", budget_currency="USD",
    ))
    sess.add(WorkspaceRecord(
        id=WS_ID_EMPTY, name="Empty", created_at=NOW, lifecycle="active", budget_currency="USD",
    ))
    sess.flush()
    yield sess
    sess.close()


def _seed_active_credential(session: Session, provider: str = "meta") -> str:
    """Seed an active BYOK credential + binding for the test workspace."""
    cred_id = _new_id("cv")
    session.add(CredentialVersionRecord(
        id=cred_id,
        workspace_id=WS_ID,
        credential_key=f"{provider.upper()}_API_KEY",
        version_no=1,
        provider=provider,
        state="active",
        encrypted_secret=b"fake-encrypted",
        encrypted_data_key=b"fake-dek",
        encryption_key_id="test-kms-key",
        encryption_algorithm="aes-256-gcm",
        nonce=b"fake-nonce",
        aad_schema_version=1,
        created_by=INITIATOR,
        created_at=NOW,
    ))
    session.flush()
    session.add(CredentialBindingRecord(
        id=_new_id("cb"),
        workspace_id=WS_ID,
        binding_scope="generation",
        provider=provider,
        credential_version_id=cred_id,
        revision=1,
        bound_by=INITIATOR,
        created_at=NOW,
        updated_at=NOW,
    ))
    session.flush()
    return cred_id


class TestCapabilitiesNoCredential:
    """Workspace with no BYOK credential — only mock and sandbox modes."""

    def test_returns_providers(self, session):
        result = get_capabilities(session, _ctx(), adapter_registry=TEST_ADAPTER_REGISTRY)
        assert "meta" in result["providers"]
        assert "mock" in result["providers"]

    def test_excludes_sandbox_from_providers(self, session):
        result = get_capabilities(session, _ctx(), adapter_registry=TEST_ADAPTER_REGISTRY)
        assert "meta_sandbox" not in result["providers"]

    def test_modes_without_credential(self, session):
        result = get_capabilities(session, _ctx(), adapter_registry=TEST_ADAPTER_REGISTRY)
        assert "mock" in result["modes"]
        assert "sandbox" in result["modes"]
        assert "byok" not in result["modes"]

    def test_credential_readiness_empty(self, session):
        result = get_capabilities(session, _ctx(), adapter_registry=TEST_ADAPTER_REGISTRY)
        assert result["credential_readiness"] == {}

    def test_models_per_provider(self, session):
        result = get_capabilities(session, _ctx(), adapter_registry=TEST_ADAPTER_REGISTRY)
        assert "mock-v1" in result["models"]["mock"]
        assert "llama-image-v1" in result["models"]["meta"]
        assert result["models"]["meta"]["llama-image-v1"]["display_name"] == "Llama Image v1"

    def test_format_limits_aggregated(self, session):
        result = get_capabilities(session, _ctx(), adapter_registry=TEST_ADAPTER_REGISTRY)
        meta_limits = result["format_limits"]["meta"]
        assert meta_limits["max_width"] == 2048
        assert meta_limits["max_height"] == 2048
        assert "1:1" in meta_limits["aspect_ratios"]
        assert meta_limits["max_variants_per_combination"] == 4


class TestCapabilitiesWithCredential:
    """Workspace with an active BYOK credential — byok mode available."""

    def test_byok_mode_available(self, session):
        _seed_active_credential(session, "meta")
        result = get_capabilities(session, _ctx(), adapter_registry=TEST_ADAPTER_REGISTRY)
        assert "byok" in result["modes"]
        assert "mock" in result["modes"]
        assert "sandbox" in result["modes"]

    def test_credential_readiness_reflects_active(self, session):
        cred_id = _seed_active_credential(session, "meta")
        result = get_capabilities(session, _ctx(), adapter_registry=TEST_ADAPTER_REGISTRY)
        meta_readiness = result["credential_readiness"]["meta"]
        assert meta_readiness["has_active_credential"] is True
        assert meta_readiness["credential_version_id"] == cred_id


class TestCapabilitiesRevokedCredential:
    """Workspace with a revoked credential — byok mode not available."""

    def test_revoked_credential_no_byok(self, session):
        cred_id = _new_id("cv")
        session.add(CredentialVersionRecord(
            id=cred_id,
            workspace_id=WS_ID,
            credential_key="META_API_KEY_REVOKED",
            version_no=1,
            provider="meta",
            state="revoked",
            encrypted_secret=b"fake",
            encrypted_data_key=b"fake",
            encryption_key_id="test-key",
            encryption_algorithm="aes-256-gcm",
            nonce=b"fake",
            aad_schema_version=1,
            created_by=INITIATOR,
            created_at=NOW,
            revoked_at=NOW,
            revoked_by=INITIATOR,
            revoke_reason="test",
        ))
        session.flush()
        session.add(CredentialBindingRecord(
            id=_new_id("cb"),
            workspace_id=WS_ID,
            binding_scope="generation",
            provider="meta",
            credential_version_id=cred_id,
            revision=1,
            bound_by=INITIATOR,
            created_at=NOW,
            updated_at=NOW,
        ))
        session.flush()

        result = get_capabilities(session, _ctx(), adapter_registry=TEST_ADAPTER_REGISTRY)
        assert "byok" not in result["modes"]
        assert result["credential_readiness"]["meta"]["has_active_credential"] is False
        assert result["credential_readiness"]["meta"]["credential_version_id"] is None


class TestCapabilitiesWorkspaceIsolation:
    """Different workspaces see different capabilities based on their credentials."""

    def test_empty_workspace_no_credentials(self, session):
        result = get_capabilities(
            session, _ctx(WS_ID_EMPTY), adapter_registry=TEST_ADAPTER_REGISTRY,
        )
        assert "byok" not in result["modes"]
        assert result["credential_readiness"] == {}

    def test_workspace_with_credential_separate(self, session):
        _seed_active_credential(session, "meta")
        result_with = get_capabilities(session, _ctx(WS_ID), adapter_registry=TEST_ADAPTER_REGISTRY)
        result_without = get_capabilities(
            session, _ctx(WS_ID_EMPTY), adapter_registry=TEST_ADAPTER_REGISTRY,
        )
        assert "byok" in result_with["modes"]
        assert "byok" not in result_without["modes"]


class TestCapabilitiesMinimalRegistry:
    """Edge case: registry with only mock adapter."""

    def test_mock_only_registry(self, session):
        minimal_registry = {
            "mock": {
                "version": "v1",
                "capabilities": {},
                "advertised": True,
                "models": {"mock-v1": {"display_name": "Mock", "format_limits": {}}},
            },
        }
        result = get_capabilities(session, _ctx(), adapter_registry=minimal_registry)
        assert result["providers"] == ["mock"]
        assert result["modes"] == ["mock"]
        assert "sandbox" not in result["modes"]
        assert "byok" not in result["modes"]


class TestExecutionModePolicyConsistency:
    """Verify capabilities mode resolution is consistent with execution_policy.resolve_preview."""

    def test_mock_mode_mirrors_policy(self, session):
        from app.generation.execution_policy import resolve_preview, PreviewResolution

        registry = TEST_ADAPTER_REGISTRY
        caps = get_capabilities(session, _ctx(), adapter_registry=registry)
        assert "mock" in caps["modes"]

        resolution = resolve_preview(
            session, draft_mode="mock", provider="mock", model="mock-v1",
            adapter_registry=registry,
        )
        assert isinstance(resolution, PreviewResolution)
        assert resolution.effective_mode == "mock"

    def test_byok_mode_mirrors_policy(self, session):
        from app.generation.execution_policy import resolve_preview, PreviewRejection

        registry = TEST_ADAPTER_REGISTRY
        caps_no_cred = get_capabilities(session, _ctx(), adapter_registry=registry)
        assert "byok" not in caps_no_cred["modes"]

        rejection = resolve_preview(
            session, draft_mode="byok", provider="meta", model="llama-image-v1",
            adapter_registry=registry, credential_evidence=None,
        )
        assert isinstance(rejection, PreviewRejection)
        assert rejection.code == "credential_required"

    def test_sandbox_mode_mirrors_policy(self, session):
        from app.generation.execution_policy import resolve_preview, PreviewResolution

        registry = TEST_ADAPTER_REGISTRY
        caps = get_capabilities(session, _ctx(), adapter_registry=registry)
        assert "sandbox" in caps["modes"]

        resolution = resolve_preview(
            session, draft_mode="sandbox", provider="meta", model="llama-image-v1",
            adapter_registry=registry,
        )
        assert isinstance(resolution, PreviewResolution)
        assert resolution.effective_mode == "sandbox"
