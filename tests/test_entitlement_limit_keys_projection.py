"""The domain Entitlement Projection keeps the three Beta limit keys (#156).

The control plane now fills ``members.max``, ``provider_calls.concurrent.max``
and ``storage.bytes.max`` with the contract §8 Beta values whenever no source
sets them (contract §4.2), and a Workspace whose snapshot predates that gets
them as its next ``entitlement_version``. Nothing on the domain side enforces
the limits yet (#157, #158); this only shows that the projection stores the
values verbatim and returns them as numbers, that a renewal cannot change them
(contract §7), and that the projection fails closed while they are absent.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.models import EntitlementProjectionRecord
from app.entitlements.policy import EntitlementPolicy, NumericEntitlementExceededError
from app.entitlements.projection import EntitlementProjectionModule
from app.entitlements.types import (
    EntitlementRequirements,
    InboxOutcome,
    NumericRequirement,
)
from app.platform.tenancy import TenantContext

KEY_ID = "beta-v1"
KEY = b"limit-keys-projection-test-key-32-bytes!"
WS_ID = "ws_acme_charge"
LIMITS = {
    "members.max": 5,
    "provider_calls.concurrent.max": 2,
    "storage.bytes.max": 10737418240,
}
UNITS = {
    "members.max": "member",
    "provider_calls.concurrent.max": "call",
    "storage.bytes.max": "byte",
}


def _envelope(
    *,
    entitlement_version: int,
    lease_sequence: int,
    resolved: dict,
    semantic_hash: str,
    now: datetime,
) -> bytes:
    payload = {
        "snapshot_id": f"es_{entitlement_version}_{lease_sequence}",
        "workspace_id": WS_ID,
        "workspace_name": "Acme Charge",
        "authorization_epoch": 1,
        "entitlement_version": entitlement_version,
        "lease_sequence": lease_sequence,
        "projection_version": lease_sequence,
        "lifecycle": "active",
        "suspended": False,
        "entitlement_snapshot": {
            "plan_version_id": "pv_beta_1",
            "source_refs": [],
            "period_start": "2026-11-01T00:00:00+00:00",
            "period_end": "2026-12-01T00:00:00+00:00",
            "period_anchor": "2026-10-01T00:00:00+00:00",
            "resolved_entitlements": resolved,
            "restriction_summary": [],
            "semantic_hash": semantic_hash,
            "entitlement_registry_version": 1,
            "resolution_policy_version": 1,
            "platform_limits_version": 1,
            "issued_at": now.isoformat(),
            "valid_until": (now + timedelta(hours=24)).isoformat(),
            "audit_event_id": f"evt_{lease_sequence}",
        },
    }
    header = {
        "schema_version": 2,
        "signature_key_id": KEY_ID,
        "signature_algorithm": "hmac-sha256",
    }
    signed = json.dumps(
        {"protected_header": header, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    signature = hmac.new(KEY, signed.encode(), hashlib.sha256).hexdigest()
    return json.dumps(
        {"protected_header": header, "payload": payload, "signature": signature},
    ).encode()


def _requirements(**quantities: int) -> EntitlementRequirements:
    return EntitlementRequirements(
        feature_keys=(),
        numeric_requirements=tuple(
            NumericRequirement(
                entitlement_key=key, quantity=Decimal(quantity), unit=UNITS[key],
            )
            for key, quantity in quantities.items()
        ),
    )


class LimitKeysProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with self.sessions() as session:
            session.add(WorkspaceRecord(id=WS_ID, name="Acme Charge"))
            session.commit()
        self.module = EntitlementProjectionModule(
            session_factory=self.sessions,
            verification_key_ring={KEY_ID: KEY},
        )
        self.now = datetime.now(timezone.utc)
        self.context = TenantContext(
            workspace_id=WS_ID, role="owner", initiator_id="user_owner",
        )

    def _ingest(self, **fields: object) -> InboxOutcome:
        return asyncio.run(
            self.module.ingest(
                _envelope(now=self.now, **fields), delivery_kind="push",
            )
        )

    def _projection(self) -> EntitlementProjectionRecord:
        with self.sessions() as session:
            return session.get(EntitlementProjectionRecord, WS_ID)

    def _authorize(self, **quantities: int):
        with self.sessions() as session:
            return EntitlementPolicy().authorize(
                session, self.context, _requirements(**quantities), self.now,
            )

    def test_new_version_with_the_keys_is_stored_and_readable(self) -> None:
        # The snapshot published before the platform defaults.
        self.assertEqual(
            self._ingest(
                entitlement_version=1, lease_sequence=1,
                resolved={"controlled_mock": True}, semantic_hash="hash_without",
            ),
            InboxOutcome.APPLIED,
        )
        # Missing keys fail closed, as before.
        with self.assertRaises(NumericEntitlementExceededError):
            self._authorize(**{"members.max": 1})

        # The first renewal after the deploy: the next version, with the keys.
        self.assertEqual(
            self._ingest(
                entitlement_version=2, lease_sequence=2,
                resolved={"controlled_mock": True, **LIMITS},
                semantic_hash="hash_with",
            ),
            InboxOutcome.APPLIED,
        )
        projection = self._projection()
        self.assertEqual(projection.entitlement_version, 2)
        self.assertEqual(
            projection.resolved_entitlements_json,
            {"controlled_mock": True, **LIMITS},
        )
        for key, value in LIMITS.items():
            stored = projection.resolved_entitlements_json[key]
            self.assertIs(type(stored), int, key)
            self.assertEqual(stored, value, key)

        # The existing numeric check reads each value at its exact size.
        ref = self._authorize(**LIMITS)
        self.assertEqual(
            (ref.entitlement_version, ref.semantic_hash), (2, "hash_with"),
        )
        for key, value in LIMITS.items():
            with self.assertRaises(NumericEntitlementExceededError) as raised:
                self._authorize(**{key: value + 1})
            self.assertEqual(raised.exception.available, Decimal(value))

    def test_renewal_cannot_change_the_keys(self) -> None:
        resolved = {"controlled_mock": True, **LIMITS}
        self._ingest(
            entitlement_version=2, lease_sequence=2,
            resolved=resolved, semantic_hash="hash_with",
        )
        # A lease renewal: same version and hash, higher lease sequence.
        self.assertEqual(
            self._ingest(
                entitlement_version=2, lease_sequence=3,
                resolved=resolved, semantic_hash="hash_with",
            ),
            InboxOutcome.APPLIED,
        )
        self.assertEqual(self._projection().lease_sequence, 3)

        # A renewal that tries to change the values under the same version.
        self.assertEqual(
            self._ingest(
                entitlement_version=2, lease_sequence=4,
                resolved={**resolved, "members.max": 50},
                semantic_hash="hash_changed",
            ),
            InboxOutcome.CONFLICT,
        )
        projection = self._projection()
        self.assertEqual(projection.lease_sequence, 3)
        self.assertEqual(projection.resolved_entitlements_json, resolved)


if __name__ == "__main__":
    unittest.main()
