"""Pydantic response models for Library v1 routes (Issue #27 Task 2).

These models serve two purposes:
1. FastAPI uses them as ``response_model`` to generate OpenAPI schemas
   consumed by ``scripts/generate_openapi.py`` and the BFF codegen pipeline.
2. Routes return plain dicts from ``app.library.service``; Pydantic
   validates/serializes those dicts against the shapes below.

Field shapes mirror the dicts actually produced by ``service._entry_dict``,
``service._draft_dict``, ``service._version_dict``, ``service._impact_payload``
and the read helpers (``list_entries``, ``list_events``, ``list_bindings``) --
not an idealized contract -- so that ``response_model`` validation never
silently drops or 500s on real service output.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.platform.action_eligibility import ActionEligibility


class LibraryEntrySummary(BaseModel):
    """One row of ``GET /library/entries`` (service.list_entries)."""

    id: str
    entry_type: str
    display_name: str
    lifecycle: str
    current_version_id: str | None = None
    current_version_no: int | None = None
    current_version_state: str | None = None
    draft_revision: int | None = None
    draft_state: str | None = None
    updated_at: str | None = None


class DraftSummary(BaseModel):
    """Shape of ``service._draft_dict`` -- also the ``bind`` command result."""

    id: str
    entry_id: str
    revision: int
    state: str
    content_schema_version: int = 1
    base_version_id: str | None = None
    source_version_id: str | None = None
    origin_command: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] | None = None


class LibraryVersionSummary(BaseModel):
    """Version row embedded in ``LibraryEntryDetail.versions``."""

    id: str
    entry_id: str
    entry_type: str
    version_no: int
    state: str
    eligibility: str
    content_schema_version: int = 1
    lifecycle_revision: int = 1
    content: dict[str, Any] = Field(default_factory=dict)
    content_hash: str | None = None
    origin_command: str | None = None
    source_version_id: str | None = None
    base_version_id: str | None = None
    published_by: str | None = None
    published_at: str | None = None
    withdraw_reason: str | None = None


class LibraryVersionDetail(LibraryVersionSummary):
    """``GET /library/versions/{id}`` and the publish/withdraw command result.

    Versions are immutable once published (no draft-style revision counter),
    so there is no ``revision``/If-Match contract here -- ``version_no`` is
    informational only. ``allowed_actions`` is server-computed.
    """

    allowed_actions: list[ActionEligibility] = Field(default_factory=list)


class LibraryEntryDetail(BaseModel):
    """``GET /library/entries/{id}`` and the create/save/archive/fork/clone result.

    ``revision`` tracks the entry's open Draft (defaults to 1 once archived
    and the Draft has been dropped) and is the value clients must echo back
    via ``If-Match`` on the next mutating command.
    """

    id: str
    entry_type: str
    display_name: str
    lifecycle: str
    current_version_id: str | None = None
    draft: DraftSummary | None = None
    versions: list[LibraryVersionSummary] = Field(default_factory=list)
    revision: int = 1
    allowed_actions: list[ActionEligibility] = Field(default_factory=list)


class LibraryValidationResult(BaseModel):
    """``POST /library/entries/{id}/validate`` result (contracts.ValidationReport.as_dict)."""

    entry_type: str
    ok: bool
    content_hash: str
    blocking: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)


class LibraryBinding(BaseModel):
    """Row of ``GET /library/versions/{id}/bindings`` (service.list_bindings)."""

    id: str
    owner_version_id: str
    reference_version_id: str
    purpose: str
    viewpoint: str
    priority: int = 0
    note: str = ""
    conditions: dict[str, Any] = Field(default_factory=dict)
    reference_display_name: str = ""
    gate_status: str = ""


class LibraryEvent(BaseModel):
    """Row of ``GET /library/entries/{id}/events`` (service.list_events)."""

    id: str
    sequence: int
    event_type: str
    version_id: str | None = None
    from_state: str = ""
    to_state: str = ""
    initiator_id: str = ""
    executor: str = ""
    created_at: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ImpactDependent(BaseModel):
    version_id: str
    entry_id: str
    entry_type: str
    version_no: int
    state: str
    eligibility: str


class LibraryVersionImpact(BaseModel):
    """``GET /library/versions/{id}/impact`` (service._impact_payload)."""

    version_id: str
    entry_id: str
    entry_type: str
    state: str
    eligibility: str
    dependents: list[ImpactDependent] = Field(default_factory=list)
    reason: str = ""


class LibraryEntryTypeList(BaseModel):
    """``GET /library/entry-types`` -- service.contracts.iter_entry_types() is a
    flat sorted list of type keys (e.g. "product", "person"), not objects."""

    entry_types: list[str]


class LibraryCommandOutcomeBase(BaseModel):
    """Fields present on every authoritative command-outcome variant."""

    contract_schema_version: int
    command_kind: str
    command_identity: str
    created_at: datetime
    updated_at: datetime
    reconciled_at: datetime | None = None
    observed_at: datetime


class LibraryCommandPending(LibraryCommandOutcomeBase):
    outcome: Literal["pending"]


class LibraryCommandSucceeded(LibraryCommandOutcomeBase):
    outcome: Literal["succeeded"]
    result: dict[str, Any] = Field(default_factory=dict)
    result_reference: dict[str, str] | None = None


class LibraryProblemDetails(BaseModel):
    """Persisted RFC 9457-compatible terminal problem."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str
    instance: str = ""
    code: str
    trace_id: str = ""
    retryable: bool = False
    field_errors: list[dict[str, Any]] = Field(default_factory=list)


class LibraryCommandProblem(LibraryCommandOutcomeBase):
    outcome: Literal["problem"]
    problem: LibraryProblemDetails


class LibraryCommandReconciliationRequired(LibraryCommandOutcomeBase):
    outcome: Literal["reconciliation_required"]
    reason_code: str


class LibraryCommandNotApplied(LibraryCommandOutcomeBase):
    outcome: Literal["not_applied"]


LibraryCommandOutcome = Annotated[
    LibraryCommandPending
    | LibraryCommandSucceeded
    | LibraryCommandProblem
    | LibraryCommandReconciliationRequired
    | LibraryCommandNotApplied,
    Field(discriminator="outcome"),
]


class CommandImpactProjection(BaseModel):
    """Response from ``POST .../publish-impact``, ``archive-impact``, ``withdraw-impact``.

    The browser presents this to the user, then echoes ``impact_id`` +
    ``impact_hash`` back to the matching command for atomic verification.
    """

    impact_id: str
    impact_hash: str
    command_kind: str
    target_entry_id: str
    target_version_id: str | None = None
    target_revision: int | None = None
    content_hash: str = ""
    expires_at: str
    projection: dict[str, Any] = Field(default_factory=dict)


class InternalEntrySummary(BaseModel):
    """Row of ``GET /library/entries/{root}/internal`` (navigation.list_internal_entries)."""

    id: str
    entry_type: str
    display_name: str
    lifecycle: str
    current_version_id: str | None = None
    root_entry_id: str


class InternalEntryMeta(BaseModel):
    """``GET /library/internal/{type}/{id}`` — unscoped canonical metadata."""

    id: str
    entry_type: str
    display_name: str
    lifecycle: str
    current_version_id: str | None = None
    host_category: str | None = None
    context_count: int = 0


class InternalEntryContext(BaseModel):
    """``GET /library/entries/{root}/internal/{type}/{id}`` — verified context."""

    id: str
    entry_type: str
    display_name: str
    lifecycle: str
    current_version_id: str | None = None
    root_entry_id: str
    root_entry_type: str
    parent_entry_id: str | None = None
    relation_kind: str
    creation_evidence: str = ""


class TemplateDiffReportResponse(BaseModel):
    """``POST /library/versions/{id}/reapply-preview`` (templates.reapply_template)."""

    base_template_version_id: str
    new_template_version_id: str
    schema_version_id: str
    items: list[dict[str, Any]] = Field(default_factory=list)


class CredentialBindingSummary(BaseModel):
    """Shape of ``datasources.bind_credential`` / ``get_active_binding``."""

    id: str
    data_source_entry_id: str
    credential_version_id: str
    revision: int
    state: str


class DataSourceConnectionCheck(BaseModel):
    """``GET /entries/{id}/connection-checks/{check_id}``: one check's evidence (#383).

    The conclusion is ``health`` plus ``reason_code``; the Operation that ran
    the check is ``succeeded`` whatever it found. ``execution_mode`` is the
    checked version's, never the one a client asked for.
    """

    check_id: str
    data_source_entry_id: str
    health: Literal["unknown", "healthy", "degraded", "failed", "revoked"]
    reason_code: str = Field(
        description="One of the codes in app.library.connection_checks.REASON_HEALTH.",
    )
    adapter_kind: str | None
    adapter_version: str | None
    execution_mode: str | None
    data_source_version_id: str | None
    credential_state: str | None = Field(
        description="State of the bound CredentialVersion, or null when none is bound.",
    )
    checked_at: datetime


class OperationAccepted(BaseModel):
    """202 Accepted envelope for async domain commands (connection-checks, manual-fact-imports, reference-gate)."""

    operation_id: str
    state: str = "queued"
    command_type: str


class ReferenceDownloadGrant(BaseModel):
    """201 response for ``POST /versions/{id}/download-intent``.

    ``download_url`` is a short-lived signed capability issued by the
    ``BlobStore`` adapter for the controlled derivative -- the browser opens it
    directly against the storage origin (``NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS``),
    exactly like generated-asset downloads; it never transits FastAPI or Next.js.
    """

    version_id: str
    object_id: str
    download_url: str
    content_disposition: str
    expires_at: str
    derivative_mime: str
    derivative_byte_size: int


class ReferenceGateCheck(BaseModel):
    """One upload-gate check result (contract §7.2)."""

    name: str
    passed: bool
    detail: str = ""


class ReferenceDerivativeFacts(BaseModel):
    mime_type: str
    byte_size: int
    width: int
    height: int


class ReferenceObjectDetail(BaseModel):
    """``GET /library/reference-objects/{id}``: gate evidence for one object.

    ``failure_code`` is the stable code the browser keys its copy on when the
    gate Operation failed (``content_safety_unavailable`` means the deployment
    selected ``real`` content safety without an adapter -- the object is still
    ``quarantined``, not rejected). Object keys and signed URLs never appear
    here.
    """

    object_id: str
    state: str
    execution_mode: str
    filename: str = ""
    declared_mime: str = ""
    mime_type: str = ""
    byte_size: int = 0
    width: int = 0
    height: int = 0
    content_sha256: str = ""
    checks: list[ReferenceGateCheck] = Field(default_factory=list)
    failure_code: str | None = None
    failure_detail: str | None = None
    content_safety_unavailable: bool = False
    rights_declared: bool = False
    promoted_entry_id: str | None = None
    derivative: ReferenceDerivativeFacts | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ReferencePromotionBinding(BaseModel):
    entry_id: str
    draft_revision: int
    purpose: str
    viewpoint: str
    already_bound: bool = False


class ReferencePromotionResult(BaseModel):
    """201 response for ``POST /library/reference-objects/{id}/promote``.

    ``created`` is false when the object had already been promoted: the same
    entry and its current effective version are returned instead of a second
    publication. ``binding`` describes the product/person Draft the version
    was appended to when ``bind`` was requested.
    """

    object_id: str
    entry_id: str
    version_id: str
    version_no: int
    display_name: str
    created: bool
    binding: ReferencePromotionBinding | None = None


class Page(BaseModel):
    """Generic ``Page[T]`` envelope (spec §2): ``{ items, next_cursor }``."""

    items: list[Any]
    next_cursor: str | None = None
