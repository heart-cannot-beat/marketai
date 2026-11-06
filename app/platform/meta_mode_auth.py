"""Centralized Meta-mode authorization gate.

`internal_validation` is the attested-evidence pipeline mode (ADR 0009):
only a Platform Workspace, authenticated as a platform operator, may use
it. Every other Meta mode (`customer_sandbox`, `controlled_mock`) is
available to any delegated user acting within their own Workspace.

This gate checks `TenantContext.identity_kind` -- the discriminant Task 1
added -- never the `executor` string. `executor` values are namespaced by
convention (`system:*`) but are not proof of *which* system actor is
running; `identity_kind` is the explicit, validated axis for that (see
`app.platform.tenancy.TenantContext.__post_init__`).
"""

from __future__ import annotations

from .tenancy import IdentityKind, TenantContext


class MetaModeNotPermitted(Exception):
    """Raised when a context's identity is not permitted to use a Meta mode."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


DELEGATED_USER_ALLOWED_MODES = frozenset({"customer_sandbox", "controlled_mock"})
PLATFORM_OPERATOR_ALLOWED_MODES = frozenset({
    "customer_sandbox", "controlled_mock", "internal_validation",
})
ALL_VALID_MODES = PLATFORM_OPERATOR_ALLOWED_MODES


def require_meta_mode(context: TenantContext, meta_mode: str) -> str:
    """Authorize ``meta_mode`` for ``context``'s identity, or raise.

    Fail-closed: an unrecognized ``identity_kind`` is rejected rather than
    defaulted into any mode.
    """
    if meta_mode not in ALL_VALID_MODES:
        raise MetaModeNotPermitted(
            "invalid_meta_mode",
            f"Unknown meta_mode: {meta_mode!r}",
        )
    if context.identity_kind == IdentityKind.DELEGATED_USER:
        if meta_mode not in DELEGATED_USER_ALLOWED_MODES:
            raise MetaModeNotPermitted(
                "meta_mode_not_permitted",
                f"{meta_mode!r} requires platform-operator identity",
            )
    elif context.identity_kind == IdentityKind.PLATFORM_OPERATOR:
        if meta_mode not in PLATFORM_OPERATOR_ALLOWED_MODES:
            raise MetaModeNotPermitted(
                "meta_mode_not_permitted",
                f"{meta_mode!r} is not a valid meta mode",
            )
        if meta_mode == "internal_validation" and not context.is_platform_workspace:
            raise MetaModeNotPermitted(
                "meta_mode_not_permitted",
                "internal_validation requires a Platform Workspace",
            )
    elif context.identity_kind == IdentityKind.SYSTEM:
        if meta_mode not in PLATFORM_OPERATOR_ALLOWED_MODES:
            raise MetaModeNotPermitted(
                "meta_mode_not_permitted",
                f"{meta_mode!r} is not a valid meta mode",
            )
        if meta_mode == "internal_validation" and not context.is_platform_workspace:
            raise MetaModeNotPermitted(
                "meta_mode_not_permitted",
                "internal_validation system work requires a Platform Workspace",
            )
    else:
        raise MetaModeNotPermitted(
            "meta_mode_not_permitted", "unrecognized identity kind",
        )
    return meta_mode


def list_available_meta_modes(context: TenantContext) -> list[str]:
    """The Meta modes ``context``'s identity may select, sorted."""
    if context.identity_kind == IdentityKind.DELEGATED_USER:
        return sorted(DELEGATED_USER_ALLOWED_MODES)
    return sorted(PLATFORM_OPERATOR_ALLOWED_MODES)
