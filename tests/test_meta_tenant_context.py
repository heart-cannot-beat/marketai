"""meta_mode is immutable provenance carried on TenantContext.

It records which Meta evidence mode produced a piece of work — customer
sandbox, controlled mock, or internal validation — and is signed into the
same HMAC the RLS policy already trusts for workspace_id and
authorization_epoch, so it cannot be forged or silently promoted.
"""

from __future__ import annotations

import pytest

from app.platform.tenancy import (
    TenantContext,
    TenantScopeError,
    META_MODE_SETTING,
    tenant_context_signature,
    _settings_for,
)


def test_tenant_context_accepts_meta_mode():
    ctx = TenantContext(
        workspace_id="ws_test",
        role="member",
        initiator_id="usr_alice",
        meta_mode="customer_sandbox",
    )
    assert ctx.meta_mode == "customer_sandbox"


def test_tenant_context_meta_mode_defaults_to_none():
    ctx = TenantContext(workspace_id="ws_test", role="system", executor="system:worker")
    assert ctx.meta_mode is None


def test_tenant_context_rejects_invalid_meta_mode():
    with pytest.raises(TenantScopeError, match="unsupported meta_mode"):
        TenantContext(
            workspace_id="ws_test",
            role="member",
            initiator_id="usr_alice",
            meta_mode="invalid_mode",
        )


def test_signature_includes_meta_mode():
    ctx = TenantContext(
        workspace_id="ws_test",
        role="member",
        initiator_id="usr_alice",
        meta_mode="customer_sandbox",
        authorization_epoch=42,
    )
    secret = "a" * 32
    sig = tenant_context_signature(ctx, secret)
    # The signature must differ from the same context without meta_mode
    ctx_no_mode = TenantContext(
        workspace_id="ws_test",
        role="member",
        initiator_id="usr_alice",
        authorization_epoch=42,
    )
    sig_no_mode = tenant_context_signature(ctx_no_mode, secret)
    assert sig != sig_no_mode


def test_signature_without_meta_mode_unchanged():
    """Non-meta contexts produce the same signature as before."""
    ctx = TenantContext(
        workspace_id="ws_test",
        role="member",
        initiator_id="usr_alice",
        authorization_epoch=42,
    )
    secret = "a" * 32
    sig = tenant_context_signature(ctx, secret)
    # Recompute manually with the original payload format
    import hashlib, hmac as _hmac
    payload = "marketai-tenant-context-v1\nws_test\n42".encode("utf-8")
    expected = _hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    assert sig == expected


def test_settings_include_meta_mode():
    ctx = TenantContext(
        workspace_id="ws_test",
        role="member",
        initiator_id="usr_alice",
        meta_mode="customer_sandbox",
    )
    settings = _settings_for(ctx, signature="sig123")
    assert settings[META_MODE_SETTING] == "customer_sandbox"


def test_settings_meta_mode_empty_when_none():
    ctx = TenantContext(
        workspace_id="ws_test",
        role="system",
        executor="system:worker",
    )
    settings = _settings_for(ctx, signature="sig123")
    assert settings[META_MODE_SETTING] == ""
