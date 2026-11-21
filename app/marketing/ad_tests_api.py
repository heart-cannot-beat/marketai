"""Authenticated v1 endpoints for test projects (ADR 0019, #247).

A test's rounds are read and driven by round id through ``api.py``; these endpoints read the test itself, and
quote a test of the kept ads of ads-first ideas before it starts (#248).
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.adapters.persistence.database import tenant_session
from app.platform.etag import set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from . import ad_tests, ideas
from .ad_tests_contracts import MarketingTestAnswers, MarketingTestDetail, MarketingTestQuote

router = APIRouter(prefix="/marketing/tests", tags=["marketing"])


@router.get("/{test_id}", operation_id="get_marketing_test", response_model=MarketingTestDetail)
def get_test(test_id: str, response: Response, ctx: TenantContext = Depends(request_tenant_context)):
    """A test of this workspace: where its ads came from, its plan at a glance and its rounds."""
    with tenant_session(ctx) as session:
        row = ad_tests.get_test(session, ctx, test_id)
        set_etag(response, row.revision)
        return ad_tests.detail(session, ctx, row)


#: Taking the kept ads of ads-first ideas to a test starts from the ideas (#248).
ideas_router = APIRouter(prefix="/marketing/ideas", tags=["marketing"])


@ideas_router.get("/{project_id}/test-quote", operation_id="get_marketing_test_quote", response_model=MarketingTestQuote)
def get_test_quote(project_id: str, answers: Annotated[MarketingTestAnswers, Query()],
                   ctx: TenantContext = Depends(request_tenant_context)):
    """What a test of these ideas' kept ads would compare, whether the round's budget is enough for a verdict,
    when the result is due and what starting costs at most. Reads only; the start command confirms its hash."""
    with tenant_session(ctx) as session:
        return ad_tests.quote(session, ctx, ideas.get_ideas_project(session, ctx, project_id), answers)
