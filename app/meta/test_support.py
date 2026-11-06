"""Test-support route for advancing FakeMetaAdapter observation scenarios.

Present only when META_ADAPTER=fake_meta.  Called by the BFF test-support
endpoint, which resolves workspace from slug, verifies Owner membership,
and mints a delegated token before forwarding here.

Each scenario creates curated FakeMetaObjectRecord entries that the rest
of the system reads through the normal FakeMetaAdapter query path.
"""
from __future__ import annotations

from dataclasses import replace
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import new_id
from ..platform.http_identity import request_tenant_context
from ..platform.tenancy import TenantContext
from .models import (
    FakeMetaObjectRecord,
    MetaConnectionRecord,
    ScenarioVersionRecord,
)

META_ADAPTER = os.getenv("META_ADAPTER", "")

VALID_SCENARIOS = frozenset({
    "insufficient_metrics",
    "decisive_winner",
    "scale_warming",
    "scale_declining",
})

router = APIRouter(
    prefix="/api/domain/v1/workspaces/{workspace_id}/test-support",
    tags=["test-support"],
)

# The browser-facing BFF mints the normal delegated-user token and forwards to
# the protected ``/api/v1`` surface. Keep the original private domain-prefixed
# route above for the existing direct test client contract, and expose this
# middleware-covered alias for real Golden Path requests.
delegated_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/test-support",
    tags=["test-support"],
)


class AdvanceObservationRequest(BaseModel):
    namespace: str
    scenario: str
    experiment_id: str | None = None
    enrollment_id: str | None = None
    candidate_ids: list[str] | None = None


ScenarioId = Literal[
    "insufficient_metrics",
    "decisive_winner",
    "scale_warming",
    "scale_declining",
]


_SCENARIO_OBSERVATIONS: dict[str, list[dict]] = {
    "insufficient_metrics": [
        {
            "object_type": "ad_insight",
            "suffix": "insufficient",
            "state": {
                "impressions": 42,
                "clicks": 3,
                "spend": "1.50",
                "actions": [],
                "statistical_power": 0.12,
                "determination": "insufficient_data",
            },
        },
    ],
    "decisive_winner": [
        {
            "object_type": "ad_insight",
            "suffix": "decisive",
            "state": {
                "impressions": 15000,
                "clicks": 1200,
                "spend": "450.00",
                "actions": [{"action_type": "purchase", "value": "85"}],
                "statistical_power": 0.95,
                "determination": "winner_found",
            },
        },
    ],
    "scale_warming": [
        {
            "object_type": "ad_insight",
            "suffix": "warming",
            "state": {
                "impressions": 8000,
                "clicks": 640,
                "spend": "200.00",
                "cpa_trend": "decreasing",
                "roas_trend": "increasing",
                "phase": "warming",
            },
        },
    ],
    "scale_declining": [
        {
            "object_type": "ad_insight",
            "suffix": "declining",
            "state": {
                "impressions": 25000,
                "clicks": 1250,
                "spend": "800.00",
                "cpa_trend": "increasing",
                "roas_trend": "decreasing",
                "phase": "declining",
                "advice_trigger": True,
            },
        },
    ],
}


@router.post("/meta-observation/advance")
def advance_meta_observation(
    workspace_id: str,
    body: AdvanceObservationRequest,
    context: TenantContext = Depends(request_tenant_context),
):
    if META_ADAPTER != "fake_meta":
        raise HTTPException(404, "Not found")

    if context.workspace_id != workspace_id:
        raise HTTPException(
            403, {"code": "workspace_mismatch", "message": "workspace_id does not match verified claims"},
        )

    if body.scenario not in VALID_SCENARIOS:
        raise HTTPException(
            400, {"code": "invalid_scenario", "message": f"unknown scenario: {body.scenario!r}"},
        )

    effective_context = replace(context, meta_mode="customer_sandbox")
    with tenant_session(effective_context) as session:
        connection = session.scalar(
            select(MetaConnectionRecord).where(
                MetaConnectionRecord.workspace_id == workspace_id,
                MetaConnectionRecord.meta_mode == "customer_sandbox",
                MetaConnectionRecord.state == "active",
            )
        )
        if connection is None:
            raise HTTPException(
                404, {"code": "no_sandbox_connection", "message": "no active customer_sandbox connection"},
            )

        scenario_version = session.scalar(
            select(ScenarioVersionRecord).where(
                ScenarioVersionRecord.workspace_id == workspace_id,
                ScenarioVersionRecord.meta_mode == "customer_sandbox",
                ScenarioVersionRecord.meta_connection_id == connection.id,
                ScenarioVersionRecord.is_default == True,  # noqa: E712
            )
        )
        if scenario_version is None:
            raise HTTPException(
                404, {"code": "no_scenario_version", "message": "no default scenario version"},
            )

        ref_id = body.experiment_id or body.enrollment_id or body.namespace
        templates = _SCENARIO_OBSERVATIONS.get(body.scenario, [])
        observation_count = 0

        for tmpl in templates:
            remote_id = f"obs_{body.scenario}_{ref_id}_{tmpl['suffix']}"

            existing = session.scalar(
                select(func.count()).select_from(FakeMetaObjectRecord).where(
                    FakeMetaObjectRecord.workspace_id == workspace_id,
                    FakeMetaObjectRecord.meta_mode == "customer_sandbox",
                    FakeMetaObjectRecord.scenario_version_id == scenario_version.id,
                    FakeMetaObjectRecord.remote_object_id == remote_id,
                )
            )
            if existing and existing > 0:
                observation_count += 1
                continue

            state = dict(tmpl["state"])
            if body.experiment_id:
                state["experiment_id"] = body.experiment_id
            if body.enrollment_id:
                state["enrollment_id"] = body.enrollment_id
            if body.candidate_ids:
                state["candidate_ids"] = body.candidate_ids

            record = FakeMetaObjectRecord(
                id=new_id("fmo"),
                workspace_id=workspace_id,
                meta_mode="customer_sandbox",
                scenario_version_id=scenario_version.id,
                object_type=tmpl["object_type"],
                remote_object_id=remote_id,
                state=state,
            )
            session.add(record)
            observation_count += 1

        session.flush()

    return {
        "scenarioApplied": body.scenario,
        "observationCount": observation_count,
        "provenance": "customer_sandbox",
    }


delegated_router.add_api_route(
    "/meta-observation/advance",
    advance_meta_observation,
    methods=["POST"],
    name="advance_meta_observation_delegated",
    include_in_schema=False,
)
