"""OpenAPI-first Experiments interface used by the authenticated SaaS BFF."""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

from . import service
from .response_models import (
    DuplicateAnalysisResponse,
    ExperimentBindingMutation,
    ExperimentConfirmationResult,
    ExperimentContextMutation,
    ExperimentDetail,
    ExperimentEntryMutation,
    ExperimentJudgementResult,
    ExperimentPage,
    ExperimentTimeline,
)


router = APIRouter(prefix="/experiments", tags=["experiments"])

#: A draft command's refusals that are 409 (#340), as Problem Details.
_DRAFT_CONFLICT = (
    "Problem Details: `revision_conflict` (the draft moved past `expected_revision`, "
    "or a concurrent command took it first), `experiment_state_conflict` (no longer a draft), "
    "`schema_version_mismatch` (not a schema-2 Experiment)"
)
_ENTRY_CONFLICT = {409: {"description": (
    f"{_DRAFT_CONFLICT}, `experiment_asset_duplicate` (another live entry of this "
    "Experiment uses the AssetVersion) or `candidate_limit_exceeded`."
)}}
_CONTEXT_CONFLICT = {409: {"description": f"{_DRAFT_CONFLICT}."}}


class CreateExperimentRequest(BaseModel):
    product_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=300)
    draft_context: dict[str, Any]


class AddExperimentEntryRequest(BaseModel):
    asset_version_id: str = Field(min_length=1)
    marketing_vars: dict[str, Any] = Field(default_factory=dict)
    expected_revision: int = Field(ge=1)


class UpdateExperimentEntryRequest(BaseModel):
    asset_version_id: str | None = None
    marketing_vars: dict[str, Any] | None = None
    expected_revision: int = Field(ge=1)


class UpdateExperimentContextRequest(BaseModel):
    draft_context: dict[str, Any]
    expected_revision: int = Field(ge=1)


class ConceptResolutionActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duplicate_group_hash: str = Field(min_length=64, max_length=64)
    action: Literal[
        "allow_controlled_duplicate",
        "exclude_source_candidate",
        "remap_before_freeze",
    ]
    reason: str = Field(min_length=1, max_length=2000)
    source_candidate_fingerprints: list[str] | None = None
    source_entry_ids: list[str] | None = None
    source_candidate_fingerprint: str | None = None
    remap_concept_key_id: str | None = None


class FreezeExperimentRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    analyzed_candidate_set_hash: str = Field(min_length=64, max_length=64)
    concept_resolution_plan: list[ConceptResolutionActionRequest] = Field(default_factory=list)


class BindExperimentCandidateRequest(BaseModel):
    ad_account_id: str = Field(min_length=1)
    ad_id: str = Field(min_length=1)
    source: str = "manual_read_only"
    expected_revision: int = Field(ge=1)


class StartExperimentRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class JudgeExperimentRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    as_of: date | None = None


class ConfirmExperimentProposalRequest(BaseModel):
    expected_proposal_sequence: int = Field(ge=1)


def _idempotency_key(
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> str:
    return idempotency_key


def _translate(call):
    try:
        return call()
    except service.SchemaVersionMismatch as exc:
        raise HTTPException(
            409, {"code": "schema_version_mismatch", "message": str(exc)}
        ) from exc
    except service.ExperimentNotFound as exc:
        raise HTTPException(
            404, {"code": "experiment_not_found", "message": str(exc)}
        ) from exc
    except service.ExperimentConflict as exc:
        code = "revision_conflict" if exc.code == "stale_revision" else exc.code
        raise HTTPException(409, {"code": code, "message": str(exc), "params": exc.params}) from exc
    except service.ExperimentError as exc:
        msg = str(exc)
        code = exc.code
        if code == "experiment_invalid" and any(msg.startswith(p) for p in (
            "exclude target", "remap target", "remap concept key",
            "allow_controlled_duplicate does not accept",
            "exclude_source_candidate does not accept",
            "unknown duplicate group", "invalid concept resolution",
            "concept resolution requires",
        )):
            code = "concept_resolution_invalid"
        raise HTTPException(422, {"code": code, "message": msg, "params": exc.params}) from exc


@router.get("", operation_id="list_experiments", response_model=ExperimentPage)
def list_experiments(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    state: str | None = Query(default=None),
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(
        lambda: service.list_experiments(
            context, cursor=cursor, limit=limit, state=state, schema_version=2
        )
    )


@router.post(
    "",
    operation_id="create_experiment",
    response_model=ExperimentDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_experiment(
    body: CreateExperimentRequest,
    context: TenantContext = Depends(request_tenant_context),
    _key: str = Depends(_idempotency_key),
):
    created = _translate(
        lambda: service.create_experiment_v2(
            context,
            product_id=body.product_id,
            name=body.name,
            draft_context=body.draft_context,
        )
    )
    return _translate(lambda: service.get_experiment(context, created["id"]))


@router.get(
    "/{experiment_id}",
    operation_id="get_experiment",
    response_model=ExperimentDetail,
)
def get_experiment(
    experiment_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.get_experiment(context, experiment_id))


@router.get(
    "/{experiment_id}/timeline",
    operation_id="get_experiment_timeline",
    response_model=ExperimentTimeline,
)
def get_experiment_timeline(
    experiment_id: str,
    cursor: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(
        lambda: service.get_timeline(
            context, experiment_id, cursor=cursor, limit=limit
        )
    )


@router.post(
    "/{experiment_id}/entries",
    operation_id="add_experiment_entry",
    responses=_ENTRY_CONFLICT,
    response_model=ExperimentEntryMutation,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def add_experiment_entry(
    experiment_id: str,
    body: AddExperimentEntryRequest,
    context: TenantContext = Depends(request_tenant_context),
    _key: str = Depends(_idempotency_key),
):
    return _translate(
        lambda: service.add_entry_v2(
            context,
            experiment_id,
            asset_version_id=body.asset_version_id,
            marketing_vars=body.marketing_vars,
            expected_revision=body.expected_revision,
        )
    )


@router.put(
    "/{experiment_id}/entries/{entry_id}",
    operation_id="update_experiment_entry",
    responses=_ENTRY_CONFLICT,
    response_model=ExperimentEntryMutation,
    openapi_extra={"x-marketai-idempotent": True},
)
def update_experiment_entry(
    experiment_id: str,
    entry_id: str,
    body: UpdateExperimentEntryRequest,
    context: TenantContext = Depends(request_tenant_context),
    _key: str = Depends(_idempotency_key),
):
    return _translate(
        lambda: service.update_entry_v2(
            context,
            experiment_id,
            entry_id,
            asset_version_id=body.asset_version_id,
            marketing_vars=body.marketing_vars,
            expected_revision=body.expected_revision,
        )
    )


@router.put(
    "/{experiment_id}/context",
    operation_id="update_experiment_context",
    responses=_CONTEXT_CONFLICT,
    response_model=ExperimentContextMutation,
    openapi_extra={"x-marketai-idempotent": True},
)
def update_experiment_context(
    experiment_id: str,
    body: UpdateExperimentContextRequest,
    context: TenantContext = Depends(request_tenant_context),
    _key: str = Depends(_idempotency_key),
):
    return _translate(
        lambda: service.update_context_v2(
            context,
            experiment_id,
            draft_context=body.draft_context,
            expected_revision=body.expected_revision,
        )
    )


@router.get(
    "/{experiment_id}/duplicate-analysis",
    operation_id="analyze_experiment_duplicates",
    response_model=DuplicateAnalysisResponse,
)
def analyze_experiment_duplicates(
    experiment_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(
        lambda: service.analyze_duplicates(context, experiment_id)
    )


@router.post(
    "/{experiment_id}/freeze",
    operation_id="freeze_experiment",
    response_model=ExperimentDetail,
    openapi_extra={"x-marketai-idempotent": True},
)
def freeze_experiment(
    experiment_id: str,
    body: FreezeExperimentRequest,
    context: TenantContext = Depends(request_tenant_context),
    _key: str = Depends(_idempotency_key),
):
    return _translate(
        lambda: service.freeze_experiment_v2(
            context,
            experiment_id,
            body.expected_revision,
            concept_resolution_plan=[item.model_dump() for item in body.concept_resolution_plan],
            analyzed_candidate_set_hash=body.analyzed_candidate_set_hash,
        )
    )


@router.post(
    "/{experiment_id}/candidates/{candidate_id}/bind",
    operation_id="bind_experiment_candidate",
    response_model=ExperimentBindingMutation,
    openapi_extra={"x-marketai-idempotent": True},
)
def bind_experiment_candidate(
    experiment_id: str,
    candidate_id: str,
    body: BindExperimentCandidateRequest,
    context: TenantContext = Depends(request_tenant_context),
    _key: str = Depends(_idempotency_key),
):
    return _translate(
        lambda: service.bind_candidate_v2(
            context,
            experiment_id,
            candidate_id,
            ad_account_id=body.ad_account_id,
            ad_id=body.ad_id,
            source=body.source,
            expected_revision=body.expected_revision,
        )
    )


@router.post(
    "/{experiment_id}/start",
    operation_id="start_experiment",
    response_model=ExperimentDetail,
    openapi_extra={"x-marketai-idempotent": True},
)
def start_experiment(
    experiment_id: str,
    body: StartExperimentRequest,
    context: TenantContext = Depends(request_tenant_context),
    _key: str = Depends(_idempotency_key),
):
    return _translate(
        lambda: service.start_experiment_v2(
            context, experiment_id, body.expected_revision
        )
    )


@router.post(
    "/{experiment_id}/judgements",
    operation_id="judge_experiment",
    response_model=ExperimentJudgementResult,
    openapi_extra={"x-marketai-idempotent": True},
)
def judge_experiment(
    experiment_id: str,
    body: JudgeExperimentRequest,
    context: TenantContext = Depends(request_tenant_context),
    _key: str = Depends(_idempotency_key),
):
    return _translate(
        lambda: service.run_judgement_v2(
            context,
            experiment_id,
            body.expected_revision,
            as_of=body.as_of,
        )
    )


@router.post(
    "/{experiment_id}/proposals/{proposal_id}/confirm",
    operation_id="confirm_experiment_proposal",
    response_model=ExperimentConfirmationResult,
    openapi_extra={"x-marketai-idempotent": True},
)
def confirm_experiment_proposal(
    experiment_id: str,
    proposal_id: str,
    body: ConfirmExperimentProposalRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    from app.platform.idempotency import hash_idempotency_key

    return _translate(
        lambda: service.confirm_proposal_v2(
            context,
            experiment_id,
            proposal_id,
            expected_proposal_sequence=body.expected_proposal_sequence,
            idempotency_key_hash=hash_idempotency_key(idempotency_key),
        )
    )
