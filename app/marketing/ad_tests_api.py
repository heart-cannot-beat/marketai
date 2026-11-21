"""Authenticated v1 endpoints for test projects (ADR 0019, #247).

A test's rounds are read and driven by round id through ``api.py``; these endpoints read the test itself,
quote a test of the kept ads of ads-first ideas before it starts (#248), and list a product's test
history (#255).
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from app.adapters.persistence.database import tenant_session
from app.generation.v1_router import _idempotency_key, _resource_command
from app.platform.etag import extract_if_match, set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from . import ad_test_schedule, ad_tests, ideas, workflow
from .ad_tests_contracts import (EndMarketingTestRoundEarly, MarketingTestAnswers, MarketingTestDetail,
    MarketingTestHistory, MarketingTestQuote, MarketingTestResult, MarketingTestRoundSchedule, MoveMarketingTestStart)
from .service import MarketingError

router = APIRouter(prefix="/marketing/tests", tags=["marketing"])


@router.get("", operation_id="list_marketing_tests", response_model=MarketingTestHistory)
def list_tests(product_id: str = Query(min_length=1, max_length=64),
               ctx: TenantContext = Depends(request_tenant_context)):
    """A product's tests, newest first, each round with its report uploads and corrections, and the tests made
    the earlier way that stand for the product."""
    with tenant_session(ctx) as session:
        return ad_tests.history(session, ctx, product_id)


@router.get("/{test_id}", operation_id="get_marketing_test", response_model=MarketingTestDetail)
def get_test(test_id: str, response: Response, ctx: TenantContext = Depends(request_tenant_context)):
    """A test of this workspace: where its ads came from, its plan at a glance and its rounds."""
    with tenant_session(ctx) as session:
        row = ad_tests.get_test(session, ctx, test_id)
        set_etag(response, row.revision)
        return ad_tests.detail(session, ctx, row)


def _test_round(session, ctx, test_id, round_id):
    """The test and one of its rounds; a round of another test or project is not found."""
    test = ad_tests.get_test(session, ctx, test_id)
    row = workflow.get_round(session, ctx, round_id)
    if row.project_id != test.id:
        raise MarketingError("round_not_found", "找不到这轮测试。", 404)
    return test, row


def _schedule_command(test_id, round_id, body, request, response, ctx, key, name, action):
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        def run(s):
            _, row = _test_round(s, ctx, test_id, round_id)
            workflow.mutate(s, ctx, row, revision)
            action(s, ctx, row, body)
            s.flush()
            return row
        row = _resource_command(session, ctx, request, key, name, body.model_dump(mode="json"), run,
                                lambda s, resource: _test_round(s, ctx, test_id, resource)[1])
        set_etag(response, row.revision)
        return ad_test_schedule.schedule(session, ctx, ad_tests.get_test(session, ctx, test_id), row)


@router.post("/{test_id}/rounds/{round_id}/start-date", operation_id="change_marketing_test_start",
             response_model=MarketingTestRoundSchedule, openapi_extra={"x-marketai-idempotent": True})
def change_start(test_id: str, round_id: str, body: MoveMarketingTestStart, request: Request, response: Response,
                 ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                 if_match: str | None = Header(default=None, alias="If-Match")):
    """Move the round's start to the day the ads went live, before its first report is confirmed (#253)."""
    return _schedule_command(test_id, round_id, body, request, response, ctx, key, "marketing.test.move_start",
                             ad_test_schedule.move_start)


@router.post("/{test_id}/rounds/{round_id}/end", operation_id="end_marketing_test_round_early",
             response_model=MarketingTestRoundSchedule, openapi_extra={"x-marketai-idempotent": True})
def end_round_early(test_id: str, round_id: str, body: EndMarketingTestRoundEarly, request: Request, response: Response,
                    ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                    if_match: str | None = Header(default=None, alias="If-Match")):
    """Record the last day the ads ran; the result can be read once the attribution lag after it has passed (#253)."""
    return _schedule_command(test_id, round_id, body, request, response, ctx, key, "marketing.test.end_early",
                             ad_test_schedule.end_early)


@router.get("/{test_id}/rounds/{round_id}/result", operation_id="get_marketing_test_result",
            response_model=MarketingTestResult)
def get_result(test_id: str, round_id: str, response: Response, ctx: TenantContext = Depends(request_tenant_context)):
    """The round's schedule and minimal result: each compared group met the goal, missed it or cannot tell yet,
    and how many orders it is short. Reading it never evaluates; "see the result" evaluates the round first."""
    with tenant_session(ctx) as session:
        test, row = _test_round(session, ctx, test_id, round_id)
        set_etag(response, row.revision)
        return ad_test_schedule.result(session, ctx, test, row)


#: Taking the kept ads of ads-first ideas to a test starts from the ideas (#248).
ideas_router = APIRouter(prefix="/marketing/ideas", tags=["marketing"])


@ideas_router.get("/{project_id}/test-quote", operation_id="get_marketing_test_quote", response_model=MarketingTestQuote)
def get_test_quote(project_id: str, answers: Annotated[MarketingTestAnswers, Query()],
                   ctx: TenantContext = Depends(request_tenant_context)):
    """What a test of these ideas' kept ads would compare, whether the round's budget is enough for a verdict,
    when the result is due and what starting costs at most. Reads only; the start command confirms its hash."""
    with tenant_session(ctx) as session:
        return ad_tests.quote(session, ctx, ideas.get_ideas_project(session, ctx, project_id), answers)


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
