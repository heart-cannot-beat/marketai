"""Experiment lifecycle interface."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import service
from ..platform.http_identity import request_tenant_context
from ..platform.tenancy import TenantContext


router = APIRouter(prefix="/api/experiments", tags=["experiments"])


class ExperimentConfigInput(BaseModel):
    primary_metric: Literal["ctr", "cpa", "roas"]
    currency: str = "USD"
    market: str = ""
    ctr_threshold: Decimal | None = None
    cpa_threshold: Decimal | None = None
    roas_threshold: Decimal | None = None
    min_days: int = Field(default=3, ge=1, le=90)
    min_impressions: int = Field(default=1000, ge=0)
    min_clicks: int = Field(default=20, ge=0)
    min_purchases: Decimal = Field(default=Decimal("3"), ge=0)
    alpha: Decimal = Field(default=Decimal("0.05"), gt=0, lt=1)
    bootstrap_samples: int = Field(default=2000, ge=200, le=100000)
    require_fresh_data: bool = True

    @model_validator(mode="after")
    def threshold_for_metric(self):
        value = getattr(self, f"{self.primary_metric}_threshold")
        if value is None or value <= 0:
            raise ValueError(f"{self.primary_metric}_threshold 必须大于 0")
        self.currency = self.currency.strip().upper()
        if len(self.currency) != 3:
            raise ValueError("currency 必须是 3 位币种代码")
        return self


class EntryInput(BaseModel):
    asset_version_id: str
    marketing_vars: dict = Field(default_factory=dict)


class CreateExperimentInput(BaseModel):
    product_id: str
    name: str = Field(min_length=1, max_length=300)
    metric_contract_hash: str = Field(min_length=64, max_length=64)
    config: ExperimentConfigInput
    entries: list[EntryInput] = Field(min_length=2, max_length=100)


class CreateExperimentV2Input(BaseModel):
    product_id: str
    name: str = Field(min_length=1, max_length=300)
    draft_context: dict


class RevisionInput(BaseModel):
    expected_revision: int = Field(ge=1)


class AddEntryV2Input(BaseModel):
    asset_version_id: str
    marketing_vars: dict = Field(default_factory=dict)
    expected_revision: int = Field(ge=1)


class UpdateEntryV2Input(BaseModel):
    asset_version_id: str | None = None
    marketing_vars: dict | None = None
    expected_revision: int = Field(ge=1)


class RemoveEntryV2Input(BaseModel):
    expected_revision: int = Field(ge=1)


class UpdateContextV2Input(BaseModel):
    draft_context: dict
    expected_revision: int = Field(ge=1)


class ConceptResolutionActionInput(BaseModel):
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


class FreezeV2Input(BaseModel):
    expected_revision: int = Field(ge=1)
    concept_resolution_plan: list[ConceptResolutionActionInput] = Field(default_factory=list)


class BindCandidateV2Input(BaseModel):
    ad_account_id: str
    ad_id: str
    source: str = "manual_read_only"
    expected_revision: int = Field(ge=1)


class StartV2Input(BaseModel):
    expected_revision: int = Field(ge=1)


class JudgeV2Input(BaseModel):
    expected_revision: int = Field(ge=1)
    as_of: date | None = None


class ConfirmProposalV2Input(BaseModel):
    expected_proposal_sequence: int = Field(ge=1)
    idempotency_key_hash: str = ""


class CloseV2Input(BaseModel):
    expected_revision: int = Field(ge=1)
    closure_kind: str
    reason: str = ""
    evidence: dict | None = None


class BindingInput(BaseModel):
    ad_account_id: str
    ad_id: str
    source: str = "manual_read_only"


class JudgeInput(BaseModel):
    as_of: date | None = None


def _translate(call):
    try:
        return call()
    except service.SchemaVersionMismatch as exc:
        raise HTTPException(409, {"code": "schema_version_mismatch", "message": str(exc)}) from exc
    except service.ExperimentNotFound as exc:
        raise HTTPException(404, {"code": "experiment_not_found", "message": str(exc)}) from exc
    except service.ExperimentConflict as exc:
        raise HTTPException(409, {"code": "experiment_conflict", "message": str(exc)}) from exc
    except service.ExperimentError as exc:
        raise HTTPException(400, {"code": "experiment_invalid", "message": str(exc)}) from exc


# ---------------------------------------------------------------------------
# Shared endpoints (schema-version agnostic)
# ---------------------------------------------------------------------------

@router.get("")
def list_experiments(
    cursor: str | None = None,
    limit: int = 20,
    state: str | None = None,
    schema_version: int | None = None,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.list_experiments(
        context, cursor=cursor, limit=limit, state=state,
        schema_version=schema_version,
    ))


# ---------------------------------------------------------------------------
# Legacy endpoints (schema-version NULL)
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
def create(
    body: CreateExperimentInput,
    context: TenantContext = Depends(request_tenant_context),
):
    experiment_id = _translate(lambda: service.create_experiment(
        context,
        product_id=body.product_id, name=body.name,
        config_snapshot=body.config.model_dump(mode="json"),
        metric_contract_hash=body.metric_contract_hash,
        entries=[row.model_dump(mode="json") for row in body.entries],
    ))
    return service.get_experiment(context, experiment_id)


@router.get("/{experiment_id}")
def get(experiment_id: str, context: TenantContext = Depends(request_tenant_context)):
    return _translate(lambda: service.get_experiment(context, experiment_id))


@router.get("/{experiment_id}/timeline")
def get_timeline(
    experiment_id: str,
    cursor: int | None = None,
    limit: int = 50,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.get_timeline(
        context, experiment_id, cursor=cursor, limit=limit,
    ))


@router.post("/{experiment_id}/freeze")
def freeze(
    experiment_id: str,
    body: RevisionInput,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.freeze_experiment(
        context, experiment_id, body.expected_revision
    ))


@router.post("/{experiment_id}/entries/{entry_id}/bind")
def bind(
    experiment_id: str,
    entry_id: str,
    body: BindingInput,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.bind_entry(
        context, experiment_id, entry_id, body.ad_account_id, body.ad_id, body.source
    ))


@router.post("/{experiment_id}/judge")
def run_judge(
    experiment_id: str,
    body: JudgeInput,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.run_judgement(context, experiment_id, body.as_of))


@router.post("/{experiment_id}/close")
def close(
    experiment_id: str,
    body: RevisionInput,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.close_experiment(
        context, experiment_id, body.expected_revision
    ))


# ---------------------------------------------------------------------------
# Schema-2 endpoints (record_schema_version = 2)
# ---------------------------------------------------------------------------

@router.post("/v2", status_code=201)
def create_v2(
    body: CreateExperimentV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.create_experiment_v2(
        context, product_id=body.product_id, name=body.name,
        draft_context=body.draft_context,
    ))


@router.post("/{experiment_id}/entries", status_code=201)
def add_entry(
    experiment_id: str,
    body: AddEntryV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.add_entry_v2(
        context, experiment_id,
        asset_version_id=body.asset_version_id,
        marketing_vars=body.marketing_vars or None,
        expected_revision=body.expected_revision,
    ))


@router.put("/{experiment_id}/entries/{entry_id}")
def update_entry(
    experiment_id: str,
    entry_id: str,
    body: UpdateEntryV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.update_entry_v2(
        context, experiment_id, entry_id,
        asset_version_id=body.asset_version_id,
        marketing_vars=body.marketing_vars,
        expected_revision=body.expected_revision,
    ))


@router.delete("/{experiment_id}/entries/{entry_id}")
def remove_entry(
    experiment_id: str,
    entry_id: str,
    body: RemoveEntryV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.remove_entry_v2(
        context, experiment_id, entry_id,
        expected_revision=body.expected_revision,
    ))


@router.put("/{experiment_id}/context")
def update_context(
    experiment_id: str,
    body: UpdateContextV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.update_context_v2(
        context, experiment_id,
        draft_context=body.draft_context,
        expected_revision=body.expected_revision,
    ))


@router.post("/{experiment_id}/freeze/v2")
def freeze_v2(
    experiment_id: str,
    body: FreezeV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.freeze_experiment_v2(
        context, experiment_id, body.expected_revision,
        concept_resolution_plan=[item.model_dump() for item in body.concept_resolution_plan],
    ))


@router.post("/{experiment_id}/candidates/{candidate_id}/bind")
def bind_candidate(
    experiment_id: str,
    candidate_id: str,
    body: BindCandidateV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.bind_candidate_v2(
        context, experiment_id, candidate_id,
        ad_account_id=body.ad_account_id,
        ad_id=body.ad_id,
        source=body.source,
        expected_revision=body.expected_revision,
    ))


@router.post("/{experiment_id}/start")
def start_experiment(
    experiment_id: str,
    body: StartV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.start_experiment_v2(
        context, experiment_id, body.expected_revision,
    ))


@router.post("/{experiment_id}/judge/v2")
def run_judge_v2(
    experiment_id: str,
    body: JudgeV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.run_judgement_v2(
        context, experiment_id, body.expected_revision,
        as_of=body.as_of,
    ))


@router.post("/{experiment_id}/proposals/{proposal_id}/confirm")
def confirm_proposal(
    experiment_id: str,
    proposal_id: str,
    body: ConfirmProposalV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.confirm_proposal_v2(
        context, experiment_id, proposal_id,
        expected_proposal_sequence=body.expected_proposal_sequence,
        idempotency_key_hash=body.idempotency_key_hash,
    ))


@router.post("/{experiment_id}/close/v2")
def close_v2(
    experiment_id: str,
    body: CloseV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.close_experiment_v2(
        context, experiment_id, body.expected_revision,
        closure_kind=body.closure_kind,
        reason=body.reason,
        evidence=body.evidence,
    ))


class NextRoundV2Input(BaseModel):
    intent_kind: str
    prompt_overrides: dict | None = None
    qc_attempt_id: str | None = None
    expected_revision: int = Field(ge=1)


class PatternCandidateV2Input(BaseModel):
    expected_revision: int = Field(ge=1)


@router.post("/{experiment_id}/decisions/{decision_id}/next-round", status_code=201)
def create_next_round(
    experiment_id: str,
    decision_id: str,
    body: NextRoundV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.create_next_round(
        context, experiment_id, decision_id,
        intent_kind=body.intent_kind,
        prompt_overrides=body.prompt_overrides,
        qc_attempt_id=body.qc_attempt_id,
        expected_revision=body.expected_revision,
    ))


@router.post("/{experiment_id}/decisions/{decision_id}/pattern-candidates", status_code=201)
def create_pattern_candidate(
    experiment_id: str,
    decision_id: str,
    body: PatternCandidateV2Input,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.create_pattern_candidate(
        context, experiment_id, decision_id,
        expected_revision=body.expected_revision,
    ))
