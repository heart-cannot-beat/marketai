"""Authenticated v1 endpoints for test projects (ADR 0019, #247).

A test's rounds are read and driven by round id through ``api.py``; these endpoints read the test itself.
"""
from fastapi import APIRouter, Depends, Response

from app.adapters.persistence.database import tenant_session
from app.platform.etag import set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from . import ad_tests
from .ad_tests_contracts import MarketingTestDetail

router = APIRouter(prefix="/marketing/tests", tags=["marketing"])


@router.get("/{test_id}", operation_id="get_marketing_test", response_model=MarketingTestDetail)
def get_test(test_id: str, response: Response, ctx: TenantContext = Depends(request_tenant_context)):
    """A test of this workspace: where its ads came from, its plan at a glance and its rounds."""
    with tenant_session(ctx) as session:
        row = ad_tests.get_test(session, ctx, test_id)
        set_etag(response, row.revision)
        return ad_tests.detail(session, ctx, row)


# A test round's full reports (#251): kept as uploaded, counted by day and Meta ad, latest confirmed first.
from fastapi import Header, Request

from app.generation.v1_router import _idempotency_key, _resource_command
from app.platform.etag import extract_if_match
from . import ad_test_reports, workflow
from .ad_test_reports_contracts import MarketingTestReport, PreviewTestReport
from .workflow_contracts import BatchCommand


def _report_command(test_id, round_id, body, request, response, ctx, key, name, action):
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        def run(s):
            _, row = ad_test_reports.test_round(s, ctx, test_id, round_id)
            # Data may still come back after a round has ended, as for any round.
            workflow.mutate(s, ctx, row, revision, allow_closed_evidence=True)
            batch = action(s, ctx, row, body)
            s.flush()
            return batch

        def replay(s, batch_id):
            return workflow.scoped(s, ctx, workflow.Batch, id=batch_id)[0]
        batch = _resource_command(session, ctx, request, key, name, body.model_dump(mode="json"), run, replay)
        _, row = ad_test_reports.test_round(session, ctx, test_id, round_id)
        set_etag(response, row.revision)
        return ad_test_reports.report(session, ctx, row, batch)


@router.post("/{test_id}/rounds/{round_id}/report/preview", operation_id="preview_marketing_test_report",
             response_model=MarketingTestReport, status_code=201, openapi_extra={"x-marketai-idempotent": True})
def preview_test_report(test_id: str, round_id: str, body: PreviewTestReport, request: Request, response: Response,
                        ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                        if_match: str | None = Header(default=None, alias="If-Match")):
    """Upload Meta's report from the test's start to today: what it would count, replace and leave out."""
    return _report_command(test_id, round_id, body, request, response, ctx, key,
                           "marketing.test_report_preview", ad_test_reports.preview)


@router.post("/{test_id}/rounds/{round_id}/report/confirm", operation_id="confirm_marketing_test_report",
             response_model=MarketingTestReport, openapi_extra={"x-marketai-idempotent": True})
def confirm_test_report(test_id: str, round_id: str, body: BatchCommand, request: Request, response: Response,
                        ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                        if_match: str | None = Header(default=None, alias="If-Match")):
    """Count an uploaded report: for each day and Meta ad it has, it replaces what an earlier report said."""
    return _report_command(test_id, round_id, body, request, response, ctx, key,
                           "marketing.test_report_confirm", ad_test_reports.confirm)
