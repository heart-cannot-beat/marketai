"""Database-backed FakeMetaAdapter for customer_sandbox and controlled_mock.

All 'write' operations create/update FakeMetaObjectRecord rows in the
local database — no network call is ever made. Read operations query
FakeMetaObjectRecord rows. Idempotency is guaranteed within a scenario
version via deterministic remote_object_id generation.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id, utcnow
from app.meta.models import FakeMetaObjectRecord


class AdapterFaultTriggered(RuntimeError):
    """Raised when a controlled-mock fault injects uncertainty."""

    def __init__(self, effect: str, object_type: str, idempotency_key: str) -> None:
        self.effect = effect
        self.object_type = object_type
        self.idempotency_key = idempotency_key
        super().__init__(f"Fault triggered: {effect} on {object_type}/{idempotency_key}")


@dataclass(frozen=True)
class FakeAdapterResult:
    remote_object_id: str
    object_type: str
    recovered: bool
    state: dict[str, Any]


class MetaAdapterPort(Protocol):
    is_test_adapter: bool

    def ensure_object(
        self,
        object_type: str,
        idempotency_key: str,
        state: dict[str, Any],
    ) -> FakeAdapterResult: ...

    def read_object(
        self,
        object_type: str,
        remote_object_id: str,
    ) -> dict[str, Any] | None: ...


_TYPE_PREFIX = {
    "campaign": "cmp_fake_",
    "adset": "set_fake_",
    "ad": "ad_fake_",
    "creative": "creative_fake_",
    "image": "img_fake_",
}


class FakeMetaAdapter:
    is_test_adapter = True

    def __init__(
        self,
        session: Session,
        workspace_id: str,
        meta_mode: str,
        scenario_version_id: str,
        *,
        fault_config: dict[str, Any] | None = None,
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._meta_mode = meta_mode
        self._scenario_version_id = scenario_version_id
        self._fault_config = fault_config or {}

    def _deterministic_id(self, object_type: str, idempotency_key: str) -> str:
        prefix = _TYPE_PREFIX.get(object_type, f"{object_type}_fake_")
        digest = hashlib.sha256(
            f"{self._scenario_version_id}:{object_type}:{idempotency_key}".encode()
        ).hexdigest()[:12]
        return f"{prefix}{digest}"

    def deterministic_object_id(self, object_type: str, idempotency_key: str) -> str:
        """Public, read-only recomputation of the id ``ensure_object`` would use.

        Pure hash arithmetic over ``(scenario_version_id, object_type,
        idempotency_key)`` -- no session access, no write. This is what lets
        Operation recovery (``app.launch.recovery``) ask "does the object this
        write would have created already exist?" via :meth:`read_object`
        without ever calling :meth:`ensure_object` again -- recovery reads
        adapter state, it never re-executes write effects.
        """
        return self._deterministic_id(object_type, idempotency_key)

    def _match_fault(self, object_type: str) -> dict[str, Any] | None:
        """Check if a fault rule matches this ensure_object call.

        Rules use match_ordinal (1-based) among ensure_object calls of the
        same object_type within this scenario version. The ordinal is derived
        from the scenario version's durable objects, not from this instance:
        callers such as the Launch saga resolve a fresh adapter per step.
        Idempotent replays return before this check, so they consume no
        ordinal; nor does a call faulted before apply, which leaves no object
        behind -- retrying its key hits the same rule again.
        """
        rules = (self._fault_config or {}).get("rules", [])
        if not rules:
            return None

        created = self._session.execute(
            select(func.count()).select_from(FakeMetaObjectRecord).where(
                FakeMetaObjectRecord.workspace_id == self._workspace_id,
                FakeMetaObjectRecord.meta_mode == self._meta_mode,
                FakeMetaObjectRecord.scenario_version_id == self._scenario_version_id,
                FakeMetaObjectRecord.object_type == object_type,
            )
        ).scalar_one()
        current = created + 1

        for rule in rules:
            if (
                rule.get("object_type") == object_type
                and rule.get("match_ordinal") == current
            ):
                return rule
        return None

    def ensure_object(
        self,
        object_type: str,
        idempotency_key: str,
        state: dict[str, Any],
    ) -> FakeAdapterResult:
        remote_id = self._deterministic_id(object_type, idempotency_key)

        # Idempotent return for already-created objects
        existing = self._session.execute(
            select(FakeMetaObjectRecord).where(
                FakeMetaObjectRecord.workspace_id == self._workspace_id,
                FakeMetaObjectRecord.meta_mode == self._meta_mode,
                FakeMetaObjectRecord.scenario_version_id == self._scenario_version_id,
                FakeMetaObjectRecord.remote_object_id == remote_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return FakeAdapterResult(
                remote_object_id=remote_id,
                object_type=object_type,
                recovered=True,
                state=existing.state,
            )

        fault = self._match_fault(object_type)
        effect = fault.get("effect") if fault else None

        if effect == "uncertain_before_apply":
            raise AdapterFaultTriggered(effect, object_type, idempotency_key)

        if effect == "pause_before_apply":
            duration_ms = fault.get("duration_ms", 0)
            if not isinstance(duration_ms, (int, float)) or duration_ms <= 0:
                raise ValueError("pause_before_apply requires positive duration_ms")
            time.sleep(duration_ms / 1000.0)

        applied_state = dict(state)
        if effect == "return_state_patch":
            patch = fault.get("state_patch", {})
            if "status" in patch:
                raise ValueError("state_patch may not modify status")
            applied_state.update(patch)

        obj = FakeMetaObjectRecord(
            id=new_id("fmo"),
            workspace_id=self._workspace_id,
            meta_mode=self._meta_mode,
            scenario_version_id=self._scenario_version_id,
            object_type=object_type,
            remote_object_id=remote_id,
            state=applied_state,
        )
        self._session.add(obj)
        self._session.flush()

        if effect == "uncertain_after_apply":
            raise AdapterFaultTriggered(effect, object_type, idempotency_key)

        return FakeAdapterResult(
            remote_object_id=remote_id,
            object_type=object_type,
            recovered=False,
            state=applied_state,
        )

    def read_object(
        self,
        object_type: str,
        remote_object_id: str,
    ) -> dict[str, Any] | None:
        obj = self._session.execute(
            select(FakeMetaObjectRecord).where(
                FakeMetaObjectRecord.workspace_id == self._workspace_id,
                FakeMetaObjectRecord.meta_mode == self._meta_mode,
                FakeMetaObjectRecord.scenario_version_id == self._scenario_version_id,
                FakeMetaObjectRecord.remote_object_id == remote_object_id,
                FakeMetaObjectRecord.object_type == object_type,
            )
        ).scalar_one_or_none()
        return obj.state if obj is not None else None
