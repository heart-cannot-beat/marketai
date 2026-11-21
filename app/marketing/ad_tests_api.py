"""Authenticated v1 endpoints for test projects (ADR 0019, #247).

A test's rounds are read and driven by round id through ``api.py``; these endpoints read the test itself and a
product's test history (#255).
"""
from fastapi import APIRouter, Depends, Query, Response

from app.adapters.persistence.database import tenant_session
from app.platform.etag import set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from . import ad_tests
from .ad_tests_contracts import MarketingTestDetail, MarketingTestHistory

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
