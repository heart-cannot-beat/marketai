"""Test projects: kept ads of ads-first ideas taken to a test (ADR 0019, step 2 design §5.1).

A test project is a ``MarketingProjectRecord`` with ``flow = "ads_test"`` under the product of the ideas it
came from (``source_project_id``). It is plan-confirmed from the start (the "start test" command, #249,
writes its only plan), and its rounds are ordinary small rounds: the round paths of ``api.py`` act on them
by round id and read the project through ``service.get_round_project``. The plan-first project paths and
``list_marketing_projects`` never return a test project, and neither do the ideas paths.

The module is ``ad_tests``, not ``tests``, so it is never mistaken for the top-level test package or a
pytest ``test_*`` module.
"""
from __future__ import annotations

from sqlalchemy import select

from . import service
from .models import MarketingProjectRecord
from .workflow_models import MarketingRoundRecord as Round

FLOW = "ads_test"


def get_test(session, ctx, test_id: str) -> MarketingProjectRecord:
    """A test project of this Workspace; anything else -- another flow, another Workspace -- is not found."""
    row = session.scalar(select(MarketingProjectRecord).where(
        MarketingProjectRecord.workspace_id == ctx.workspace_id, MarketingProjectRecord.id == test_id,
        MarketingProjectRecord.flow == FLOW))
    if row is None:
        raise service.MarketingError("marketing_test_not_found", "找不到这次测试。", 404)
    return row


def in_progress(session, ctx, product_id: str) -> MarketingProjectRecord | None:
    """The product's test project with a round not yet closed, or ``None`` (plan E3).

    One product has at most one such test at a time (design §5.1); the start command (#249) refuses another."""
    return session.scalar(select(MarketingProjectRecord).join(Round,
            (Round.workspace_id == MarketingProjectRecord.workspace_id) & (Round.project_id == MarketingProjectRecord.id))
        .where(MarketingProjectRecord.workspace_id == ctx.workspace_id, MarketingProjectRecord.product_id == product_id,
               MarketingProjectRecord.flow == FLOW, Round.state != "closed")
        .order_by(MarketingProjectRecord.created_at.desc(), MarketingProjectRecord.id.desc()).limit(1))


def comparison(plan: dict) -> str:
    """What a test's plan compares, from the plan itself (design §5.2).

    A retest of one ad (``business_retest``) only checks whether it meets the goal; groups across two or more
    claims compare claims; groups within one claim compare its ways of telling it."""
    if plan["comparison_kind"] == "business_retest":
        return "single"
    claims = {c["id"]: c["angle_id"] for c in plan["combinations"]}
    compared = {claims[cid] for group in plan["groups"] for cid in group["combination_ids"]}
    return "claims" if len(compared) > 1 else "expressions"


def plan_summary(plan: dict) -> dict:
    """The three answers and two defaults the user sees: cost per order, this round's budget, days, currency, landing page."""
    return {"target_cost": str(plan["rules"]["threshold"]), "round_budget": str(plan["small_round_budget"]),
            "duration_days": plan["duration_days"], "currency": plan["currency"], "landing_page": plan["landing_page"]}


def round_summary(row) -> dict:
    # The effective dates are the frozen ones until a round can move its start or end early (#253).
    return {"id": row.id, "ordinal": row.ordinal, "state": row.state, "start_date": row.start_date,
            "end_date": row.end_date, "effective_start_date": row.start_date, "effective_end_date": row.end_date}


def detail(session, ctx, row) -> dict:
    """``MarketingTestDetail``: the test, its source ideas, a plan summary and its rounds, oldest first."""
    from .workflow import scoped
    rounds = sorted(scoped(session, ctx, Round, project_id=row.id), key=lambda r: r.ordinal)
    current = next((r for r in reversed(rounds) if r.state != "closed"), None)
    return {"id": row.id, "revision": row.revision, "product_id": row.product_id,
            "source_project_id": row.source_project_id, "comparison": comparison(row.plan_json),
            "plan_summary": plan_summary(row.plan_json), "rounds": [round_summary(r) for r in rounds],
            "current_round_id": current.id if current else None, "created_at": row.created_at.isoformat()}
