"""``/api/v1/results/imports``, product learnings and plan results (Issue #166).

Thin HTTP adapters over :mod:`app.learnings.service`. Workspace and actor
come only from the verified identity; another Workspace's product, import or
plan id answers ``404`` like an unknown one. Commands require an
``Idempotency-Key``. No route writes to Meta: ``source: "meta_sync"`` only
reads the ad metrics already synced.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.platform.blob_store import CommittedBlobRef
from app.platform.blob_store_factory import build_blob_store
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

from . import service

router = APIRouter(tags=["learnings"])
_blob_store = build_blob_store()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
#: Inline bodies stay under the BFF's 1 MiB request bound for this
#: operation; a bigger sheet goes through a ``results_import`` upload.
MAX_INLINE_CSV_CHARS = 300_000
MAX_INLINE_ROWS = 2000


def _idempotency_key(
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> str:
    return idempotency_key


def _problem(exc: service.LearningsProblem) -> HTTPException:
    return HTTPException(exc.status, {
        "code": exc.code, "message": exc.message, "retryable": exc.retryable, "params": exc.params,
    })


EvidenceMode = Literal["production", "sandbox", "mock"]


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


class ManualResultRow(BaseModel):
    """One ad's results over a date range. Identify the image by its asset
    code (``MK-XXXXXX``, from the template), an ad name containing that
    code, or a Meta ``ad_id`` bound to it. ``clicks`` are link clicks."""

    model_config = {"extra": "forbid"}

    asset_code: str | None = Field(default=None, max_length=64)
    ad_name: str | None = Field(default=None, max_length=500)
    ad_id: str | None = Field(default=None, max_length=100)
    date_start: str = Field(min_length=1, max_length=20)
    date_end: str = Field(min_length=1, max_length=20)
    impressions: int | str
    clicks: int | str
    spend: str | float | int | None = None
    conversions: str | float | int | None = None
    revenue: str | float | int | None = None
    currency: str | None = Field(default=None, max_length=8)


class ResultsImportCreateRequest(BaseModel):
    """``csv``: exactly one of ``csv_text`` (a pasted table or a small file
    read by the browser, <= 300,000 characters) or ``upload_intent_id`` (a
    finalized ``results_import`` upload, <= 10 MB, <= 5,000 rows).
    ``manual``: ``rows`` (<= 2,000).
    ``meta_sync``: nothing else -- the synced Meta metrics of ads bound to
    this product's images. ``currency`` applies to rows without one."""

    model_config = {"extra": "forbid"}

    product_id: str = Field(min_length=1, max_length=64)
    source: Literal["csv", "manual", "meta_sync"]
    csv_text: str | None = Field(default=None, max_length=MAX_INLINE_CSV_CHARS)
    upload_intent_id: str | None = Field(default=None, max_length=64)
    rows: list[ManualResultRow] | None = Field(default=None, max_length=MAX_INLINE_ROWS)
    currency: str | None = Field(default=None, max_length=3)
    file_name: str | None = Field(default=None, max_length=300)


class RejectedReason(BaseModel):
    code: str
    reason: str
    count: int


class ResultsImportSummary(BaseModel):
    """Counts by row status (``ready`` / ``replaces`` / ``unattributable``
    import; ``rejected`` does not), why rows were rejected, rows per
    evidence partition, and which columns were read or ignored."""

    total_rows: int = 0
    importable_rows: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    rejected_reasons: list[RejectedReason] = Field(default_factory=list)
    evidence_modes: dict[str, int] = Field(default_factory=dict)
    impressions: int = 0
    clicks: int = 0
    attributable_assets: int = 0
    columns: dict[str, str] = Field(default_factory=dict)
    ignored_columns: list[str] = Field(default_factory=list)


class ResultsImportRow(BaseModel):
    line: int
    status: Literal["ready", "replaces", "unattributable", "rejected"]
    status_label: str
    reason_code: str | None = None
    #: Kept for older clients; the browser words ``reason_code`` (#255).
    reason: str | None = None
    #: Values for that wording, only for codes in ``parsing.REASON_PARAMS``.
    reason_params: dict[str, str] | None = None
    asset_code: str | None = None
    asset_version_id: str | None = None
    #: Plan prose for a test-plan asset; otherwise see ``description_code``.
    description: str | None = None
    description_code: Literal["external_asset", "other_asset"] | None = None
    round_number: int | None = None
    evidence_mode: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)


class ResultsImportView(BaseModel):
    """A previewed (``previewed``) or committed (``committed``) import.
    ``rows`` lists every rejected row and the first accepted ones
    (``rows_truncated``); ``duplicate`` is true when the same rows were
    already imported for this product (the existing import is returned)."""

    import_id: str
    product_id: str
    source: str
    status: Literal["previewed", "committed"]
    file_name: str = ""
    currency: str | None = None
    summary: ResultsImportSummary
    rows: list[ResultsImportRow] = Field(default_factory=list)
    rows_truncated: bool = False
    snapshot_ids: list[str] = Field(default_factory=list)
    created_at: str | None = None
    committed_at: str | None = None
    duplicate: bool = False
    replayed: bool = False


@router.post(
    "/results/imports",
    operation_id="create_results_import",
    response_model=ResultsImportView,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_results_import(
    body: ResultsImportCreateRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Parse, validate and match result rows into a preview (nothing is
    counted until ``commit``)."""
    upload = None
    upload_bytes = None
    try:
        if body.upload_intent_id:
            if body.source != "csv":
                raise service.LearningsProblem(422, "csv_input_invalid", "只有 CSV 导入可以附带上传文件")
            upload = await run_in_threadpool(service.resolve_upload, context, body.upload_intent_id)
            ref = CommittedBlobRef(
                committed_key=upload["committed_key"],
                storage_version_id=upload["storage_version_id"],
                size_bytes=upload["size"],
                checksum_sha256_b64=upload["checksum_sha256_b64"],
                content_type_verified=upload["content_type"],
            )
            raw = bytearray()
            async for chunk in _blob_store.open_committed(ref):
                raw.extend(chunk)
                if len(raw) > MAX_UPLOAD_BYTES:
                    raise service.LearningsProblem(
                        422, "file_too_large", "文件超过 10 MB，请拆分后导入",
                        params={"limit_mb": MAX_UPLOAD_BYTES // (1024 * 1024)},
                    )
            upload_bytes = bytes(raw)
        return await run_in_threadpool(
            lambda: service.create_import(
                context,
                product_id=body.product_id,
                source=body.source,
                idempotency_key=idempotency_key,
                csv_text=body.csv_text,
                upload=upload,
                upload_bytes=upload_bytes,
                rows=[row.model_dump() for row in body.rows] if body.rows is not None else None,
                currency=body.currency,
                file_name=body.file_name,
            )
        )
    except service.LearningsProblem as exc:
        raise _problem(exc) from exc


@router.get(
    "/results/imports/{import_id}",
    operation_id="get_results_import",
    response_model=ResultsImportView,
)
def get_results_import(import_id: str, context: TenantContext = Depends(request_tenant_context)):
    """An import with its match preview."""
    try:
        return service.get_import(context, import_id)
    except service.LearningsProblem as exc:
        raise _problem(exc) from exc


@router.post(
    "/results/imports/{import_id}/commit",
    operation_id="commit_results_import",
    response_model=ResultsImportView,
    openapi_extra={"x-marketai-idempotent": True},
)
def commit_results_import(
    import_id: str,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Count the accepted rows and recompute the learnings. ``409
    preview_stale``: the evidence changed since the preview -- the import
    now holds the refreshed preview; confirm again."""
    try:
        return service.commit_import(context, import_id)
    except service.LearningsProblem as exc:
        raise _problem(exc) from exc


# ---------------------------------------------------------------------------
# Learnings
# ---------------------------------------------------------------------------


class SampleSize(BaseModel):
    impressions: int = 0
    clicks: int = 0
    spend: str = "0.00"
    conversions: str = "0"
    assets: int = 0
    ads: int = 0


class BalanceCheck(BaseModel):
    """Worst total-variation distance of another factor between the two
    compared groups (>= 0.25 ``imbalanced``, >= 0.5 ``confounded``)."""

    max_tvd: str
    factor_key: str
    factor_label: str


class MetricComparison(BaseModel):
    """One metric of one value (or direction) against the rest.

    ``estimate`` / ``interval_*``: the value's own rate (CTR: Wilson; CPA:
    seeded bootstrap). ``lift*``: ratio to the pooled other values minus
    one, with the 95% interval (Bonferroni within the factor); for CPA a
    negative lift is better. ``reasons`` explain ``insufficient_data``."""

    metric: Literal["ctr", "cpa"]
    verdict: Literal["winner", "loser", "no_difference", "insufficient_data"]
    estimate: str | None = None
    interval_low: str | None = None
    interval_high: str | None = None
    baseline: str | None = None
    lift: str | None = None
    lift_low: str | None = None
    lift_high: str | None = None
    confidence: str
    adjusted_alpha: str
    reasons: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    balance: BalanceCheck | None = None
    n: SampleSize
    baseline_n: SampleSize
    shortfall: dict[str, list[str]] = Field(default_factory=dict)


class ValueLearning(BaseModel):
    """A factor value: headline ``verdict`` on ``verdict_metric`` and the
    one-line ``sentence``; both metrics in full."""

    value_id: str
    label: str
    compared_with: str
    verdict: Literal["winner", "loser", "no_difference", "insufficient_data"]
    verdict_metric: Literal["ctr", "cpa"]
    flags: list[str] = Field(default_factory=list)
    sentence: str
    n: SampleSize
    ctr: MetricComparison
    cpa: MetricComparison


class FactorLearning(BaseModel):
    key: str
    label: str
    values: list[ValueLearning] = Field(default_factory=list)


class DirectionLearning(BaseModel):
    direction_key: str
    title: str
    compared_with: str
    verdict: Literal["winner", "loser", "no_difference", "insufficient_data"]
    verdict_metric: Literal["ctr", "cpa"]
    flags: list[str] = Field(default_factory=list)
    sentence: str
    n: SampleSize
    ctr: MetricComparison
    cpa: MetricComparison


class RoundTotals(SampleSize):
    ctr: str | None = None
    cpa: str | None = None
    cpa_assets: int = 0


class RoundLearning(BaseModel):
    """One test plan's results ("第 N 轮")."""

    round_id: str
    plan_id: str
    round_number: int
    label: str
    goal: str
    plan_created_at: str | None = None
    totals: RoundTotals
    factors: list[FactorLearning] = Field(default_factory=list)
    directions: list[DirectionLearning] = Field(default_factory=list)


class UnattributableAsset(BaseModel):
    asset_code: str
    reason_code: str
    reason: str
    impressions: int = 0


class ReviewSignalValue(BaseModel):
    value_id: str
    label: str
    approved: int = 0
    needs_changes: int = 0
    rejected: int = 0
    assets: int = 0


class ReviewSignalFactor(BaseModel):
    key: str
    label: str
    values: list[ReviewSignalValue] = Field(default_factory=list)


class ReviewSignals(BaseModel):
    """Human review counts per value of the latest round -- a weak signal
    shown only while there is no ad data."""

    round_number: int
    note: str
    factors: list[ReviewSignalFactor] = Field(default_factory=list)


class RecentImport(BaseModel):
    import_id: str
    source: str
    file_name: str = ""
    committed_at: str | None = None
    rows: int = 0


class ProductLearnings(BaseModel):
    """「这个产品我们已经学到了什么」. ``status`` ``no_data`` (show
    "尚无投放数据" and ``review_signals``) or ``ready``. ``evidence_mode``
    is the partition shown (production first); ``evidence_notice`` must be
    displayed when present (demo data). ``rounds`` newest first."""

    product_id: str
    product_name: str = ""
    status: Literal["no_data", "ready"]
    evidence_mode: EvidenceMode | None = None
    evidence_notice: str | None = None
    available_modes: list[str] = Field(default_factory=list)
    snapshot_id: str | None = None
    computed_at: str | None = None
    algorithm_version: str | None = None
    thresholds: dict[str, str] = Field(default_factory=dict)
    currency: str | None = None
    highlights: list[str] = Field(default_factory=list)
    rounds: list[RoundLearning] = Field(default_factory=list)
    unattributable: list[UnattributableAsset] = Field(default_factory=list)
    can_seed_next_round: bool = False
    review_signals: ReviewSignals | None = None
    recent_imports: list[RecentImport] = Field(default_factory=list)


class TestPlanResults(BaseModel):
    """One plan's round of the latest snapshot (directions with metrics)."""

    __test__ = False

    plan_id: str
    product_id: str
    status: Literal["no_data", "ready"]
    evidence_mode: EvidenceMode | None = None
    evidence_notice: str | None = None
    available_modes: list[str] = Field(default_factory=list)
    snapshot_id: str | None = None
    computed_at: str | None = None
    thresholds: dict[str, str] = Field(default_factory=dict)
    round: RoundLearning | None = None
    can_seed_next_round: bool = False


class TemplateColumn(BaseModel):
    key: str
    label: str


class TemplateRow(BaseModel):
    asset_code: str
    round_number: int | None = None
    direction: str
    combination: str
    attributable: bool
    evidence_mode: str


class ResultsTemplate(BaseModel):
    """The CSV template: ``columns`` in order and one row per image of the
    product (code + what it shows); the browser writes the file."""

    product_id: str
    file_name: str
    columns: list[TemplateColumn]
    currency: str | None = None
    rows: list[TemplateRow] = Field(default_factory=list)


@router.get(
    "/products/{product_id}/learnings",
    operation_id="get_product_learnings",
    response_model=ProductLearnings,
)
def get_product_learnings(
    product_id: str,
    evidence_mode: EvidenceMode | None = Query(default=None),
    context: TenantContext = Depends(request_tenant_context),
):
    """The latest learnings snapshot of the product."""
    try:
        return service.product_learnings(context, product_id, evidence_mode=evidence_mode)
    except service.LearningsProblem as exc:
        raise _problem(exc) from exc


@router.get(
    "/products/{product_id}/results/template",
    operation_id="get_results_template",
    response_model=ResultsTemplate,
)
def get_results_template(product_id: str, context: TenantContext = Depends(request_tenant_context)):
    """Rows for the downloadable results template."""
    try:
        return service.results_template(context, product_id)
    except service.LearningsProblem as exc:
        raise _problem(exc) from exc


@router.get(
    "/test-plans/{plan_id}/results",
    operation_id="get_test_plan_results",
    response_model=TestPlanResults,
)
def get_test_plan_results(
    plan_id: str,
    evidence_mode: EvidenceMode | None = Query(default=None),
    context: TenantContext = Depends(request_tenant_context),
):
    """The plan's directions and factor values with metrics."""
    try:
        return service.plan_results(context, plan_id, evidence_mode=evidence_mode)
    except service.LearningsProblem as exc:
        raise _problem(exc) from exc
