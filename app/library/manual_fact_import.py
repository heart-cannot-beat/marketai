"""Manual fact import: turn a finalized Upload Intent into a Manual Fact Set draft.

Spec §4.3 manual-fact-import path. Called by the Worker after the route
enqueues the job. The Upload Intent must be finalized with purpose
``library_manual_fact_import`` before this module touches it.

Only problems with the file as a whole fail the import (#23): its size,
encoding, MIME, a JSON or CSV that does not parse, no ``facts`` list, no
facts or more than ``MAX_FACT_COUNT``, and a target that is not an active
data source. Problems inside a row never do. The draft is created anyway,
carrying the same ValidationReport the publish command computes
(``_build_report``), so the Owner reads it row by row (``facts[i].<field>``)
on the draft before publishing (contract §11.4).
"""
from __future__ import annotations

import csv
import io
import json

from sqlalchemy.orm import Session

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
    DRAFT_READY,
    DRAFT_VALIDATION_FAILED,
    ENTRY_MANUAL_FACT_SET,
    LIFECYCLE_ACTIVE,
)
from .fact_types import FACT_VALUE_FIELDS, FieldKind
from .service import (
    LibraryError,
    _audit,
    _build_report,
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
        try:
            if not reader.fieldnames:
                raise ManualFactImportError("Upload Intent CSV 缺少表头")
            return {"facts": [_csv_fact(row) for row in reader]}
        except csv.Error as exc:
            raise ManualFactImportError("Upload Intent CSV 格式无效") from exc

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


_CSV_BOOLS = {"true": True, "false": False}


def _csv_value(kind: FieldKind, text: str) -> object:
    """A CSV cell as its fact family's value type, or the cell itself.

    A cell that does not convert stays as written: the draft's
    ValidationReport then names it at ``facts[i].<field>``.
    """
    if kind == "bool":
        return _CSV_BOOLS.get(text.strip().lower(), text)
    if kind == "days":
        digits = text.strip()
        return int(digits) if digits.isascii() and digits.isdigit() else text
    return text


def _csv_fact(row: dict[str | None, str | None]) -> dict:
    """One CSV row as a fact: CSV only has strings (#50).

    An empty cell is no value, so a row of one fact family can share a file
    with rows of others. Cells of the row's family that hold a boolean or a
    number of days (``FACT_VALUE_FIELDS``) are converted; decimals,
    currencies and text stay strings, as in a JSON file.
    """
    fact: dict[str, object] = {
        key: value for key, value in row.items() if key is not None and value not in (None, "")
    }
    fact_type = fact.get("fact_type")
    fields = FACT_VALUE_FIELDS.get(fact_type) if isinstance(fact_type, str) else None
    for field, kind in (fields or {}).items():
        value = fact.get(field)
        if isinstance(value, str):
            fact[field] = _csv_value(kind, value)
    return fact


def _file_facts(content_payload: dict) -> list:
    """The file's fact rows, or the file-level reason it cannot be imported."""
    facts = content_payload.get("facts")
    if not isinstance(facts, list):
        raise ManualFactImportError("Upload Intent 内容缺少 facts 列表")
    if not facts:
        raise ManualFactImportError("Upload Intent 内容至少需要一条事实")
    if len(facts) > MAX_FACT_COUNT:
        raise ManualFactImportError(f"事实数量 {len(facts)} 超过限制 {MAX_FACT_COUNT}")
    return facts


def process_manual_fact_import(
    context: TenantContext,
    *,
    data_source_entry_id: str,
    upload_intent_id: str,
    content_payload: dict | None = None,
) -> dict:
    """Create a Manual Fact Set entry whose draft carries the file's ValidationReport.

    ``content_payload`` is the parsed body of the finalized Upload Intent.
    The Worker writes through :func:`write_manual_fact_set` inside the
    transaction that settles its Operation; this wrapper commits on its own.
    """
    with tenant_session(context) as session:
        entry = write_manual_fact_set(
            session,
            context,
            data_source_entry_id=data_source_entry_id,
            upload_intent_id=upload_intent_id,
            content_payload=content_payload,
        )
        return _entry_dict(session, context, entry)


def write_manual_fact_set(
    session: Session,
    context: TenantContext,
    *,
    data_source_entry_id: str,
    upload_intent_id: str,
    content_payload: dict | None,
) -> LibraryEntryRecord:
    """Add a Manual Fact Set and its validated draft to ``session``'s transaction.

    Raises :class:`ManualFactImportError` only for the file as a whole; rows
    with problems become blocking findings of the draft's ValidationReport.
    """
    facts = _file_facts(content_payload or {})

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
    draft = LibraryDraftRecord(
        id=new_id("libdraft"),
        workspace_id=context.workspace_id,
        entry_id=entry.id,
        content_json=content,
        revision=1,
        state=DRAFT_EDITING,
        origin_command="manual_fact_import",
        source_version_id=None,
    )
    session.add(draft)
    _emit(
        session, context, entry.id, "draft_created", to_state=DRAFT_EDITING,
        payload={"origin": "manual_fact_import", "upload_intent_id": upload_intent_id},
    )
    # The row-by-row report is the one ``validate_draft`` would store for
    # this revision; saving the draft clears it, validating writes it again.
    report, _resolved = _build_report(session, context, entry, content)
    draft.state = DRAFT_READY if report.ok else DRAFT_VALIDATION_FAILED
    draft.validation_json = report.as_dict()
    _emit(
        session, context, entry.id, "validated", to_state=draft.state,
        payload={"blocking": len(report.blocking), "warnings": len(report.warnings)},
    )
    _audit(
        session, context, "manual_fact_import",
        entry_id=entry.id,
        detail={
            "data_source_entry_id": data_source_entry_id,
            "upload_intent_id": upload_intent_id,
            "fact_count": len(facts),
            "content_hash": report.content_hash,
            "blocking": len(report.blocking),
            "warnings": len(report.warnings),
        },
    )
    return entry
