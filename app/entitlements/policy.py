"""EntitlementPolicy: validate workspace lifecycle, lease, features and numeric caps.

Spec section 3.3.  Fails closed on unknown keys, missing projection, or
expired lease.  The authorization check is a prerequisite for admission and
must complete before any quoting or reservation work begins.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.models import EntitlementProjectionRecord
from app.entitlements.types import EntitlementRef, EntitlementRequirements
from app.platform.tenancy import TenantContext


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class WorkspaceLifecycleError(Exception):
    """Base for errors caused by the workspace lifecycle state."""


class NoEntitlementProjectionError(WorkspaceLifecycleError):
    """No entitlement projection exists for the workspace."""

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        super().__init__(
            f"no entitlement projection for workspace {workspace_id}"
        )


class LeaseExpiredError(WorkspaceLifecycleError):
    """The entitlement lease for the workspace has expired."""

    def __init__(self, workspace_id: str, valid_until: datetime, now: datetime) -> None:
        self.workspace_id = workspace_id
        self.valid_until = valid_until
        self.now = now
        super().__init__(
            f"entitlement lease expired for workspace {workspace_id}: "
            f"valid_until={valid_until}, now={now}"
        )


class WorkspaceSuspendedError(WorkspaceLifecycleError):
    """The workspace lifecycle is not active."""

    def __init__(self, workspace_id: str, lifecycle: str) -> None:
        self.workspace_id = workspace_id
        self.lifecycle = lifecycle
        super().__init__(
            f"workspace {workspace_id} is {lifecycle}, not active"
        )


class FeatureNotEntitledError(Exception):
    """A required feature is not present or not enabled in the entitlements."""

    def __init__(self, feature_key: str, workspace_id: str) -> None:
        self.feature_key = feature_key
        self.workspace_id = workspace_id
        super().__init__(
            f"feature {feature_key!r} is not entitled for workspace {workspace_id}"
        )


class NumericEntitlementExceededError(Exception):
    """A numeric entitlement requirement exceeds the entitled limit."""

    def __init__(self, entitlement_key: str, required: object, available: object) -> None:
        self.entitlement_key = entitlement_key
        self.required = required
        self.available = available
        super().__init__(
            f"numeric entitlement {entitlement_key!r}: "
            f"required {required}, available {available}"
        )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class EntitlementPolicy:
    """Validate workspace lifecycle, lease, features and numeric caps."""

    def authorize(
        self,
        session: Session,
        context: TenantContext,
        requirements: EntitlementRequirements,
        now: datetime,
    ) -> EntitlementRef:
        """Validate entitlement state and return a ref on success.

        Steps:
          1. Read EntitlementProjectionRecord for workspace.
          2. If missing, raise NoEntitlementProjectionError.
          3. If valid_until < now, raise LeaseExpiredError.
          4. Read WorkspaceRecord, check lifecycle is "active".
          5. Parse resolved_entitlements_json.
          6. Check each feature_key exists and is truthy.
          7. Check each numeric_requirement meets minimum.
          8. Return EntitlementRef.
        """
        workspace_id = context.workspace_id

        # Step 1-2: Read projection
        projection = session.query(EntitlementProjectionRecord).filter_by(
            workspace_id=workspace_id,
        ).first()

        if projection is None:
            raise NoEntitlementProjectionError(workspace_id)

        # Step 3: Check lease expiry
        # SQLite returns naive datetimes; normalise both sides before comparing.
        valid_until = projection.valid_until
        if valid_until is not None and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=now.tzinfo)
        if valid_until is not None and valid_until < now:
            raise LeaseExpiredError(workspace_id, projection.valid_until, now)

        # Step 4: Check workspace lifecycle
        workspace = session.query(WorkspaceRecord).filter_by(
            id=workspace_id,
        ).first()

        if workspace is None:
            raise WorkspaceSuspendedError(workspace_id, "missing")

        if workspace.lifecycle != "active":
            raise WorkspaceSuspendedError(workspace_id, workspace.lifecycle)

        # Step 5: Parse resolved entitlements
        raw = projection.resolved_entitlements_json
        if isinstance(raw, str):
            entitlements: dict = json.loads(raw)
        elif isinstance(raw, dict):
            entitlements = raw
        else:
            entitlements = {}

        # Step 6: Check feature keys
        for feature_key in requirements.feature_keys:
            if feature_key not in entitlements or not entitlements[feature_key]:
                raise FeatureNotEntitledError(feature_key, workspace_id)

        # Step 7: Check numeric requirements
        for num_req in requirements.numeric_requirements:
            available = entitlements.get(num_req.entitlement_key)
            if available is None:
                raise NumericEntitlementExceededError(
                    num_req.entitlement_key, num_req.quantity, None,
                )
            try:
                available_value = type(num_req.quantity)(available)
            except (TypeError, ValueError):
                raise NumericEntitlementExceededError(
                    num_req.entitlement_key, num_req.quantity, available,
                )
            if available_value < num_req.quantity:
                raise NumericEntitlementExceededError(
                    num_req.entitlement_key, num_req.quantity, available_value,
                )

        # Step 8: Return EntitlementRef
        return EntitlementRef(
            entitlement_version=projection.entitlement_version,
            lease_sequence=projection.lease_sequence,
            period_start=projection.period_start,
            period_end=projection.period_end,
            semantic_hash=projection.semantic_hash,
        )
