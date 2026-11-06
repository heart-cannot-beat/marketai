import pytest
from app.entitlements.models import (
    EntitlementSnapshotInboxRecord,
    EntitlementProjectionRecord,
)

def test_inbox_table_name():
    assert EntitlementSnapshotInboxRecord.__tablename__ == "entitlement_snapshot_inbox"

def test_projection_table_name():
    assert EntitlementProjectionRecord.__tablename__ == "entitlement_projections"

def test_inbox_has_required_columns():
    columns = {c.name for c in EntitlementSnapshotInboxRecord.__table__.columns}
    required = {
        "snapshot_id", "workspace_id", "entitlement_version",
        "lease_sequence", "projection_version", "schema_version",
        "delivery_kind", "canonical_envelope", "envelope_sha256",
        "semantic_hash", "signature", "signature_key_id",
        "signature_algorithm", "outcome", "received_at",
        "processed_at", "projection_applied_at",
    }
    assert required.issubset(columns)

def test_projection_has_required_columns():
    columns = {c.name for c in EntitlementProjectionRecord.__table__.columns}
    required = {
        "workspace_id", "current_snapshot_id", "entitlement_version",
        "lease_sequence", "projection_version", "schema_version",
        "plan_version_id", "resolved_entitlements_json",
        "semantic_hash", "issued_at", "valid_until", "applied_at",
    }
    assert required.issubset(columns)
