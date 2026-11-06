import pytest
from app.platform.meta_mode_auth import (
    MetaModeNotPermitted,
    list_available_meta_modes,
    require_meta_mode,
)
from app.platform.tenancy import IdentityKind, TenantContext


def _delegated_ctx(**kw):
    return TenantContext(
        workspace_id="ws_1", role="owner", initiator_id="user_1",
        identity_kind=IdentityKind.DELEGATED_USER, **kw,
    )

def _platform_ctx(*, is_platform_workspace=True, **kw):
    return TenantContext(
        workspace_id="ws_1", role="system", initiator_id="op_1",
        executor="system:platform_operator",
        identity_kind=IdentityKind.PLATFORM_OPERATOR,
        is_platform_workspace=is_platform_workspace, **kw,
    )

def _system_ctx(*, is_platform_workspace=False, **kw):
    return TenantContext(
        workspace_id="ws_1", role="system", initiator_id="user_1",
        executor="system:worker",
        identity_kind=IdentityKind.SYSTEM,
        is_platform_workspace=is_platform_workspace, **kw,
    )


# MA1: delegated user cannot use internal_validation
def test_delegated_user_internal_validation_rejected():
    with pytest.raises(MetaModeNotPermitted) as exc_info:
        require_meta_mode(_delegated_ctx(), "internal_validation")
    assert exc_info.value.code == "meta_mode_not_permitted"

# MA2: delegated user can use customer_sandbox
def test_delegated_user_customer_sandbox():
    result = require_meta_mode(_delegated_ctx(), "customer_sandbox")
    assert result == "customer_sandbox"

# MA3: delegated user can use controlled_mock
def test_delegated_user_controlled_mock():
    result = require_meta_mode(_delegated_ctx(), "controlled_mock")
    assert result == "controlled_mock"

# MA4: platform operator + platform workspace + internal_validation
def test_platform_operator_internal_validation_platform_workspace():
    result = require_meta_mode(_platform_ctx(), "internal_validation")
    assert result == "internal_validation"

# MA5: platform operator can use customer_sandbox
def test_platform_operator_customer_sandbox():
    result = require_meta_mode(_platform_ctx(), "customer_sandbox")
    assert result == "customer_sandbox"

# MA10: platform operator + ordinary workspace + internal_validation
def test_platform_operator_ordinary_workspace_internal_validation():
    with pytest.raises(MetaModeNotPermitted):
        require_meta_mode(
            _platform_ctx(is_platform_workspace=False),
            "internal_validation",
        )

# MA10 variant: system + ordinary workspace + internal_validation
def test_system_ordinary_workspace_internal_validation():
    with pytest.raises(MetaModeNotPermitted):
        require_meta_mode(
            _system_ctx(is_platform_workspace=False),
            "internal_validation",
        )

# System + platform workspace + internal_validation → allowed
def test_system_platform_workspace_internal_validation():
    result = require_meta_mode(
        _system_ctx(is_platform_workspace=True),
        "internal_validation",
    )
    assert result == "internal_validation"

# Invalid meta_mode is rejected before any identity_kind branching
@pytest.mark.parametrize("ctx_factory", [_delegated_ctx, _platform_ctx, _system_ctx])
def test_unknown_meta_mode_rejected_regardless_of_identity_kind(ctx_factory):
    with pytest.raises(MetaModeNotPermitted) as exc_info:
        require_meta_mode(ctx_factory(), "banana")
    assert exc_info.value.code == "invalid_meta_mode"

# Available modes for delegated user
def test_available_modes_delegated():
    modes = list_available_meta_modes(_delegated_ctx())
    assert set(modes) == {"customer_sandbox", "controlled_mock"}

# Available modes for platform operator
def test_available_modes_platform_operator():
    modes = list_available_meta_modes(_platform_ctx())
    assert set(modes) == {"customer_sandbox", "controlled_mock", "internal_validation"}
