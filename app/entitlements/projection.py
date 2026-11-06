"""EntitlementProjectionModule: signature verification and projection application.

Spec section 3.2: verifies control-plane signed envelopes, applies them to the
local entitlement projection, and writes immutable inbox evidence.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.models import (
    EntitlementProjectionRecord,
    EntitlementSnapshotInboxRecord,
)
from app.entitlements.types import InboxOutcome


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


class EntitlementProjectionModule:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        verification_key_ring: dict[str, bytes],
        max_lease_seconds: int = 86400,
    ):
        self._session_factory = session_factory
        self._key_ring = verification_key_ring
        self._max_lease_seconds = max_lease_seconds

    def _verify_envelope(self, raw: bytes) -> tuple[dict, dict, str]:
        envelope = json.loads(raw)
        header = envelope["protected_header"]
        payload = envelope["payload"]
        sig = envelope["signature"]

        if header.get("schema_version") != 2:
            raise ValueError(f"unsupported schema_version: {header.get('schema_version')}")
        if header.get("signature_algorithm") != "hmac-sha256":
            raise ValueError(f"unsupported algorithm: {header.get('signature_algorithm')}")

        key_id = header["signature_key_id"]
        key = self._key_ring.get(key_id)
        if key is None:
            raise ValueError(f"unknown signature_key_id: {key_id}")

        signed_content = json.dumps(
            {"protected_header": header, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hmac_mod.new(
            key, signed_content.encode(), hashlib.sha256,
        ).hexdigest()
        if not hmac_mod.compare_digest(sig, expected):
            raise ValueError("signature verification failed")

        return header, payload, sig

    async def ingest(
        self,
        signed_envelope: bytes,
        *,
        delivery_kind: Literal["push", "full_repair"],
    ) -> InboxOutcome:
        header, payload, signature = self._verify_envelope(signed_envelope)
        envelope_sha256 = hashlib.sha256(signed_envelope).hexdigest()

        session = self._session_factory()
        try:
            dialect = session.get_bind().dialect.name
            if dialect == "postgresql":
                outcome = self._apply_postgres(
                    session, header, payload, signature,
                    envelope_sha256, signed_envelope, delivery_kind,
                )
            else:
                outcome = self._apply_sqlite(
                    session, header, payload, signature,
                    envelope_sha256, signed_envelope, delivery_kind,
                )
            session.commit()
            return outcome
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # PostgreSQL path: delegate to the SECURITY DEFINER SQL function
    # ------------------------------------------------------------------

    def _apply_postgres(
        self,
        session: Session,
        header: dict,
        payload: dict,
        signature: str,
        envelope_sha256: str,
        raw: bytes,
        delivery_kind: str,
    ) -> InboxOutcome:
        es = payload.get("entitlement_snapshot", {})
        result = session.execute(
            text(
                "SELECT marketai_apply_workspace_projection_v2("
                ":snapshot_id, :workspace_id, :workspace_name, "
                ":authorization_epoch, :entitlement_version, :lease_sequence, "
                ":projection_version, :lifecycle, :suspended, "
                ":schema_version, :delivery_kind, :canonical_envelope, "
                ":envelope_sha256, :semantic_hash, :signature, "
                ":signature_key_id, :signature_algorithm, :plan_version_id, "
                "CAST(:source_refs_json AS jsonb), :period_start, :period_end, "
                ":period_anchor, CAST(:resolved_entitlements_json AS jsonb), "
                "CAST(:restriction_summary_json AS jsonb), "
                ":entitlement_registry_version, :resolution_policy_version, "
                ":platform_limits_version, :issued_at, :valid_until, "
                ":control_plane_audit_event_id)"
            ),
            {
                "snapshot_id": payload["snapshot_id"],
                "workspace_id": payload["workspace_id"],
                "workspace_name": payload.get("workspace_name", ""),
                "authorization_epoch": payload.get("authorization_epoch", 0),
                "entitlement_version": payload["entitlement_version"],
                "lease_sequence": payload["lease_sequence"],
                "projection_version": payload["projection_version"],
                "lifecycle": payload.get("lifecycle", "active"),
                "suspended": payload.get("suspended", False),
                "schema_version": header["schema_version"],
                "delivery_kind": delivery_kind,
                "canonical_envelope": raw,
                "envelope_sha256": envelope_sha256,
                "semantic_hash": es["semantic_hash"],
                "signature": signature,
                "signature_key_id": header["signature_key_id"],
                "signature_algorithm": header["signature_algorithm"],
                "plan_version_id": es.get("plan_version_id"),
                "source_refs_json": json.dumps(es.get("source_refs")),
                "period_start": es.get("period_start"),
                "period_end": es.get("period_end"),
                "period_anchor": es.get("period_anchor"),
                "resolved_entitlements_json": json.dumps(
                    es.get("resolved_entitlements"),
                ),
                "restriction_summary_json": json.dumps(
                    es.get("restriction_summary"),
                ),
                "entitlement_registry_version": es.get(
                    "entitlement_registry_version", 1,
                ),
                "resolution_policy_version": es.get(
                    "resolution_policy_version", 1,
                ),
                "platform_limits_version": es.get("platform_limits_version", 1),
                "issued_at": es.get("issued_at"),
                "valid_until": es.get("valid_until"),
                "control_plane_audit_event_id": es.get("audit_event_id"),
            },
        )
        return InboxOutcome(result.scalar())

    # ------------------------------------------------------------------
    # SQLite path: same logic as the SQL function, in Python
    # ------------------------------------------------------------------

    def _apply_sqlite(
        self,
        session: Session,
        header: dict,
        payload: dict,
        signature: str,
        envelope_sha256: str,
        raw: bytes,
        delivery_kind: str,
    ) -> InboxOutcome:
        workspace_id = payload["workspace_id"]
        ev = payload["entitlement_version"]
        ls = payload["lease_sequence"]
        snapshot_id = payload["snapshot_id"]
        es = payload.get("entitlement_snapshot", {})
        semantic_hash = es["semantic_hash"]
        now = datetime.now(timezone.utc)

        proj = session.get(EntitlementProjectionRecord, workspace_id)

        if proj is None:
            outcome = InboxOutcome.APPLIED
        elif ev < proj.entitlement_version:
            outcome = InboxOutcome.STALE
        elif ev == proj.entitlement_version and ls <= proj.lease_sequence:
            existing = (
                session.query(EntitlementSnapshotInboxRecord)
                .filter_by(
                    workspace_id=workspace_id,
                    entitlement_version=ev,
                    lease_sequence=ls,
                )
                .first()
            )
            if existing and existing.envelope_sha256 != envelope_sha256:
                outcome = InboxOutcome.CONFLICT
            else:
                outcome = InboxOutcome.DUPLICATE
        elif ev == proj.entitlement_version and ls > proj.lease_sequence:
            outcome = (
                InboxOutcome.CONFLICT
                if semantic_hash != proj.semantic_hash
                else InboxOutcome.APPLIED
            )
        elif ev == proj.entitlement_version + 1:
            outcome = InboxOutcome.APPLIED
        elif delivery_kind == "full_repair":
            outcome = InboxOutcome.APPLIED
        else:
            outcome = InboxOutcome.VERSION_JUMP_PAUSED

        # Insert inbox row (ON CONFLICT DO NOTHING equivalent)
        if not session.get(EntitlementSnapshotInboxRecord, snapshot_id):
            uc_exists = (
                session.query(EntitlementSnapshotInboxRecord)
                .filter_by(
                    workspace_id=workspace_id,
                    entitlement_version=ev,
                    lease_sequence=ls,
                )
                .first()
            ) is not None
            if not uc_exists:
                session.add(
                    EntitlementSnapshotInboxRecord(
                        snapshot_id=snapshot_id,
                        workspace_id=workspace_id,
                        entitlement_version=ev,
                        lease_sequence=ls,
                        projection_version=payload["projection_version"],
                        schema_version=header["schema_version"],
                        delivery_kind=delivery_kind,
                        canonical_envelope=raw,
                        envelope_sha256=envelope_sha256,
                        semantic_hash=semantic_hash,
                        signature=signature,
                        signature_key_id=header["signature_key_id"],
                        signature_algorithm=header["signature_algorithm"],
                        outcome=outcome.value,
                        received_at=now,
                        processed_at=now,
                        projection_applied_at=(
                            now if outcome == InboxOutcome.APPLIED else None
                        ),
                    )
                )
                session.flush()

        if outcome == InboxOutcome.APPLIED:
            self._upsert_projection(
                session, header, payload, signature, snapshot_id, now,
            )
            self._update_workspace(session, payload)

        return outcome

    def _upsert_projection(
        self,
        session: Session,
        header: dict,
        payload: dict,
        signature: str,
        snapshot_id: str,
        now: datetime,
    ) -> None:
        workspace_id = payload["workspace_id"]
        es = payload.get("entitlement_snapshot", {})
        proj = session.get(EntitlementProjectionRecord, workspace_id)
        if proj is None:
            proj = EntitlementProjectionRecord(workspace_id=workspace_id)
            session.add(proj)

        proj.current_snapshot_id = snapshot_id
        proj.entitlement_version = payload["entitlement_version"]
        proj.lease_sequence = payload["lease_sequence"]
        proj.projection_version = payload["projection_version"]
        proj.schema_version = header["schema_version"]
        proj.plan_version_id = es.get("plan_version_id")
        proj.source_refs_json = es.get("source_refs")
        proj.period_start = _parse_dt(es.get("period_start"))
        proj.period_end = _parse_dt(es.get("period_end"))
        proj.period_anchor = _parse_dt(es.get("period_anchor"))
        proj.resolved_entitlements_json = es.get("resolved_entitlements")
        proj.restriction_summary_json = es.get("restriction_summary")
        proj.semantic_hash = es["semantic_hash"]
        proj.entitlement_registry_version = es.get(
            "entitlement_registry_version", 1,
        )
        proj.resolution_policy_version = es.get("resolution_policy_version", 1)
        proj.platform_limits_version = es.get("platform_limits_version", 1)
        proj.issued_at = _parse_dt(es.get("issued_at"))
        proj.valid_until = _parse_dt(es.get("valid_until"))
        proj.control_plane_audit_event_id = es.get("audit_event_id")
        proj.signature = signature
        proj.signature_key_id = header["signature_key_id"]
        proj.signature_algorithm = header["signature_algorithm"]
        proj.applied_at = now

    def _update_workspace(self, session: Session, payload: dict) -> None:
        ws = session.get(WorkspaceRecord, payload["workspace_id"])
        if ws is not None:
            ws.name = payload.get("workspace_name", ws.name)
            ws.authorization_epoch = max(
                ws.authorization_epoch, payload.get("authorization_epoch", 0),
            )
            ws.entitlement_version = payload["entitlement_version"]
            ws.suspended = payload.get("suspended", False)
            ws.lifecycle = payload.get("lifecycle", ws.lifecycle)
            ws.projection_version = payload["projection_version"]

    async def repair(self, workspace_id: str) -> InboxOutcome:
        raise NotImplementedError("repair() requires control-plane pull (Task 19)")
