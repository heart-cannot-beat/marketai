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

Every row names its Offer entry in ``offer_entry_id`` (#54). The import may
carry a default Offer: it fills the rows that name none, and a row that
names its own Offer keeps it. Whether each row's Offer exists in this
Workspace and covers the row's market is a row finding like any other.
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
    DRAFT_READY,
    DRAFT_VALIDATION_FAILED,
    ENTRY_MANUAL_FACT_SET,
    ENTRY_OFFER,
    LIFECYCLE_ACTIVE,
)
from .fact_types import FACT_VALUE_FIELDS, FieldKind
from .service import (
    LibraryError,
    _audit,
    _build_report,
    _effective_version,
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


def with_default_offer(facts: list, default_offer_entry_id: str | None) -> tuple[list, int]:
    """``facts`` with the default Offer in every row that names none (#54).

    A row with its own ``offer_entry_id`` keeps it; a row that is not an
    object stays as it is for the report to name. Returns the rows and how
    many of them took the default.
    """
    default = (default_offer_entry_id or "").strip()
    if not default:
        return facts, 0
    filled = 0
    rows: list = []
    for fact in facts:
        if isinstance(fact, dict) and not _names_offer(fact):
            fact = {**fact, "offer_entry_id": default}
            filled += 1
        rows.append(fact)
    return rows, filled


def default_offer_problem(context: TenantContext, default_offer_entry_id: str) -> str | None:
    """Why an import cannot take ``default_offer_entry_id`` as its default Offer, or None.

    The default must be an active Offer entry of this Workspace with a
    published version, or every row it fills would fail on it. Anything
    that is not an Offer entry of this Workspace reads alike, so an id
    guessed from another Workspace learns nothing.
    """
    with tenant_session(context) as session:
        entry = session.get(LibraryEntryRecord, default_offer_entry_id)
        if entry is None or entry.workspace_id != context.workspace_id or entry.entry_type != ENTRY_OFFER:
            return "默认 Offer 不是本 Workspace 的 Offer 条目"
        if entry.lifecycle != LIFECYCLE_ACTIVE:
            return "默认 Offer 已归档"
        if _effective_version(session, entry) is None:
            return "默认 Offer 尚无已发布版本"
    return None


def _names_offer(fact: dict) -> bool:
    value = fact.get("offer_entry_id")
    # A JSON null or an empty string names no Offer; any other value is the
    # row's own choice, and the report says if it is not an Offer id.
    return value is not None and not (isinstance(value, str) and not value.strip())


def process_manual_fact_import(
    context: TenantContext,
    *,
    data_source_entry_id: str,
    upload_intent_id: str,
    content_payload: dict | None = None,
    default_offer_entry_id: str | None = None,
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
            default_offer_entry_id=default_offer_entry_id,
        )
        return _entry_dict(session, context, entry)


def write_manual_fact_set(
    session: Session,
    context: TenantContext,
    *,
    data_source_entry_id: str,
    upload_intent_id: str,
    content_payload: dict | None,
    default_offer_entry_id: str | None = None,
) -> LibraryEntryRecord:
    """Add a Manual Fact Set and its validated draft to ``session``'s transaction.

    Raises :class:`ManualFactImportError` only for the file as a whole; rows
    with problems become blocking findings of the draft's ValidationReport.
    ``default_offer_entry_id`` fills the rows that name no Offer.
    """
    facts, default_offer_rows = with_default_offer(
        _file_facts(content_payload or {}), default_offer_entry_id
    )

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
    # The row-by-row report is the one ``validate_draft`` would store for
    # this revision; saving the draft clears it, validating writes it again.
    # The Worker's database role may insert a draft but not update one
    # (deploy/postgres/domain-tenant-roles.sql), so the draft row is written
    # once, already carrying its report and the state that follows from it.
    report, _resolved = _build_report(session, context, entry, content)
    state = DRAFT_READY if report.ok else DRAFT_VALIDATION_FAILED
    session.add(
        LibraryDraftRecord(
            id=new_id("libdraft"),
            workspace_id=context.workspace_id,
            entry_id=entry.id,
            content_json=content,
            revision=1,
            state=state,
            validation_json=report.as_dict(),
            origin_command="manual_fact_import",
            source_version_id=None,
        )
    )
    _emit(
        session, context, entry.id, "draft_created", to_state=state,
        payload={"origin": "manual_fact_import", "upload_intent_id": upload_intent_id},
    )
    _emit(
        session, context, entry.id, "validated", to_state=state,
        payload={"blocking": len(report.blocking), "warnings": len(report.warnings)},
    )
    _audit(
        session, context, "manual_fact_import",
        entry_id=entry.id,
        detail={
            "data_source_entry_id": data_source_entry_id,
            "upload_intent_id": upload_intent_id,
            "fact_count": len(facts),
            "default_offer_entry_id": (default_offer_entry_id or "").strip() or None,
            "default_offer_rows": default_offer_rows,
            "content_hash": report.content_hash,
            "blocking": len(report.blocking),
            "warnings": len(report.warnings),
        },
    )
    return entry
