"""Manual fact import: validate and convert Upload Intent payload into a Manual Fact Set draft.

Spec §4.3 manual-fact-import path. Called by the Worker after the route
enqueues the job. The Upload Intent must be finalized with purpose
``library_manual_fact_import`` before this module touches it.
"""
from __future__ import annotations

import csv
import io
import json

from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import (
    LibraryDraftRecord,
    LibraryEntryRecord,
    new_id,
)
from ..platform.tenancy import TenantContext
from ..platform.blob_store import BlobStore, CommittedBlobRef
from .contracts import (
    DRAFT_EDITING,
    ENTRY_MANUAL_FACT_SET,
    FACT_TYPES,
    LIFECYCLE_ACTIVE,
)
from .service import (
    LibraryError,
    _audit,
    _emit,
    _entry,
    _entry_dict,
    _require_workspace,
    derived_display_name,
)

MAX_FACT_COUNT = 10_000
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB


class ManualFactImportError(LibraryError):
    pass


def parse_manual_fact_payload(
    raw: bytes,
    *,
    content_type: str,
) -> dict:
    """Parse a verified JSON/CSV upload into the shared ``{"facts": ...}`` shape."""
    if len(raw) > MAX_FILE_SIZE_BYTES:
        raise ManualFactImportError(
            f"Upload Intent 内容超过 {MAX_FILE_SIZE_BYTES} 字节限制"
        )

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ManualFactImportError("Upload Intent 内容必须是 UTF-8") from exc

    if content_type == "application/json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManualFactImportError("Upload Intent JSON 格式无效") from exc
        if not isinstance(payload, dict):
            raise ManualFactImportError("Upload Intent JSON 顶层必须是对象")
        return payload

    if content_type == "text/csv":
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames:
            raise ManualFactImportError("Upload Intent CSV 缺少表头")
        return {
            "facts": [
                {key: value for key, value in row.items() if key is not None}
                for row in reader
            ]
        }

    raise ManualFactImportError(f"Upload Intent MIME 不受支持: {content_type}")


async def read_manual_fact_payload(
    blob_store: BlobStore,
    ref: CommittedBlobRef,
) -> dict:
    """Read a finalized private blob with a hard cap, then parse its facts."""
    raw = bytearray()
    async for chunk in blob_store.open_committed(ref):
        raw.extend(chunk)
        if len(raw) > MAX_FILE_SIZE_BYTES:
            raise ManualFactImportError(
                f"Upload Intent 内容超过 {MAX_FILE_SIZE_BYTES} 字节限制"
            )
    if not raw:
        raise ManualFactImportError("Upload Intent 内容为空")
    return parse_manual_fact_payload(
        bytes(raw),
        content_type=ref.content_type_verified,
    )


def _validate_facts(facts: list[dict]) -> list[str]:
    """Return a list of validation error messages."""
    errors: list[str] = []
    if not facts:
        errors.append("至少需要一条事实")
        return errors
    if len(facts) > MAX_FACT_COUNT:
        errors.append(f"事实数量 {len(facts)} 超过限制 {MAX_FACT_COUNT}")
        return errors
    seen_ids: set[str] = set()
    for i, fact in enumerate(facts):
        if not isinstance(fact, dict):
            errors.append(f"facts[{i}]: 事实必须是对象")
            continue
        fact_id = fact.get("fact_id")
        if not isinstance(fact_id, str) or not fact_id:
            errors.append(f"facts[{i}]: 事实需要稳定 fact_id")
        elif fact_id in seen_ids:
            errors.append(f"facts[{i}]: 重复 fact_id: {fact_id}")
        else:
            seen_ids.add(fact_id)
        fact_type = fact.get("fact_type")
        if fact_type not in FACT_TYPES:
            errors.append(f"facts[{i}]: fact_type 必须是 {sorted(FACT_TYPES)} 之一")
        cc = fact.get("country_code")
        if not isinstance(cc, str) or len(cc) != 2 or cc != cc.upper():
            errors.append(f"facts[{i}]: country_code 必须是大写 2 位 ISO")
        if not isinstance(fact.get("valid_from"), str) or not fact.get("valid_from"):
            errors.append(f"facts[{i}]: 必须声明 valid_from")
        if not isinstance(fact.get("valid_to"), str) or not fact.get("valid_to"):
            errors.append(f"facts[{i}]: 必须声明 valid_to")
    return errors


def process_manual_fact_import(
    context: TenantContext,
    *,
    data_source_entry_id: str,
    upload_intent_id: str,
    content_payload: dict | None = None,
) -> dict:
    """Validate facts and create a Manual Fact Set entry with an editing draft.

    ``content_payload`` is the parsed JSON body from the finalized Upload
    Intent. In production the Worker reads and parses the blob; for testing
    it can be passed directly.
    """
    if content_payload is None:
        content_payload = {}

    facts = content_payload.get("facts")
    if not isinstance(facts, list):
        raise ManualFactImportError("Upload Intent 内容缺少 facts 列表")

    errors = _validate_facts(facts)
    if errors:
        raise ManualFactImportError(f"事实验证失败: {'; '.join(errors[:10])}")

    with tenant_session(context) as session:
        _require_workspace(session, context.workspace_id)
        ds_entry = _entry(session, context, data_source_entry_id)
        if ds_entry.entry_type != "data_source":
            raise ManualFactImportError("目标条目不是数据源")
        if ds_entry.lifecycle != LIFECYCLE_ACTIVE:
            raise ManualFactImportError("数据源已归档")

        content = {
            "data_source_entry_id": data_source_entry_id,
            "upload_intent_id": upload_intent_id,
            "facts": facts,
        }

        entry = LibraryEntryRecord(
            id=new_id("lib"),
            workspace_id=context.workspace_id,
            entry_type=ENTRY_MANUAL_FACT_SET,
            display_name=derived_display_name(f"Fact Import for {ds_entry.display_name}"),
            lifecycle=LIFECYCLE_ACTIVE,
        )
        session.add(entry)
        session.flush()
        session.add(
            LibraryDraftRecord(
                id=new_id("libdraft"),
                workspace_id=context.workspace_id,
                entry_id=entry.id,
                content_json=content,
                revision=1,
                state=DRAFT_EDITING,
                origin_command="manual_fact_import",
                source_version_id=None,
            )
        )
        _emit(
            session, context, entry.id, "draft_created", to_state=DRAFT_EDITING,
            payload={"origin": "manual_fact_import", "upload_intent_id": upload_intent_id},
        )
        _audit(
            session, context, "manual_fact_import",
            entry_id=entry.id,
            detail={
                "data_source_entry_id": data_source_entry_id,
                "upload_intent_id": upload_intent_id,
                "fact_count": len(facts),
            },
        )
        result = _entry_dict(session, context, entry)
        return result
