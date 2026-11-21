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

from sqlalchemy import or_, select

from app.adapters.persistence.models import AuditEventRecord, ProductRecord

from . import service
from .models import MarketingProjectRecord
from .workflow_models import MarketingMetricBatchRecord as Batch, MarketingRoundRecord as Round

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


def history(session, ctx, product_id: str) -> dict:
    """``MarketingTestHistory``: the product's tests and earlier tests, each round with its confirmed reports (§5.10).

    ``product_id`` is the product's ``prod_`` id or its Library entry id; a product this Workspace does not have
    is not found, so another Workspace learns nothing about it. Reading writes nothing."""
    product = _product(session, ctx, product_id)
    tests = session.scalars(select(MarketingProjectRecord).where(
        MarketingProjectRecord.workspace_id == ctx.workspace_id, MarketingProjectRecord.product_id == product,
        MarketingProjectRecord.flow == FLOW)
        .order_by(MarketingProjectRecord.created_at.desc(), MarketingProjectRecord.id.desc())).all()
    earlier = _earlier_tests(session, ctx, product)
    rounds = _rounds(session, ctx, [row.id for row in (*tests, *earlier)])
    reports = _reports(session, ctx, [r.id for project in rounds.values() for r in project])

    def history_rounds(row):
        return [{**round_summary(r), "reports": reports.get(r.id, [])} for r in rounds.get(row.id, [])]

    return {
        "product_id": product,
        "tests": [{"id": row.id, "source_project_id": row.source_project_id, "comparison": comparison(row.plan_json),
                   "plan_summary": plan_summary(row.plan_json), "rounds": history_rounds(row),
                   "current_round_id": next((r.id for r in reversed(rounds.get(row.id, [])) if r.state != "closed"), None),
                   "created_at": row.created_at.isoformat()} for row in tests],
        "earlier_tests": [{"id": row.id, "state": row.state,
                           "product_name": (row.understanding_json or {}).get("product_name") or row.source_text[:80],
                           "rounds": history_rounds(row), "created_at": row.created_at.isoformat()} for row in earlier],
    }


def _product(session, ctx, product_id: str) -> str:
    """The Workspace product ``product_id`` names, its bridged ``prod_`` row first (as the ideas paths resolve it)."""
    found = session.scalar(select(ProductRecord.id).where(ProductRecord.workspace_id == ctx.workspace_id,
        (ProductRecord.id == product_id) | (ProductRecord.product_profile_entry_id == product_id))
        .order_by(ProductRecord.product_profile_entry_id.is_(None), ProductRecord.id).limit(1))
    if found is None:
        raise service.MarketingError("product_not_found", "找不到这个产品。", 404)
    return found


def _earlier_tests(session, ctx, product: str) -> list[MarketingProjectRecord]:
    """Plan-first tests that stand for ``product``, newest first.

    A test started from a product card stands for that card while its source text is unrevised
    (``card_lineage``), whatever anchor its confirmed understanding minted; any other test stands for the
    product its own ``product_id`` names -- the same fold the product list makes (``app.products.catalog``)."""
    from .card_lineage import card_ids, card_tests
    lineage = card_tests(session, ctx)
    cards = card_ids(session, ctx, (test.source for test in lineage.values()))
    by_card = {project for project, test in lineage.items() if cards[test.source] == product}
    rows = session.scalars(select(MarketingProjectRecord).where(
        MarketingProjectRecord.workspace_id == ctx.workspace_id, MarketingProjectRecord.flow == "plan_first",
        or_(MarketingProjectRecord.id.in_(by_card), MarketingProjectRecord.product_id == product))
        .order_by(MarketingProjectRecord.created_at.desc(), MarketingProjectRecord.id.desc())).all()
    return [row for row in rows if row.id in by_card or row.id not in lineage]


def _rounds(session, ctx, project_ids) -> dict[str, list]:
    """Each project's rounds, by ordinal."""
    rounds: dict[str, list] = {}
    if project_ids:
        for row in session.scalars(select(Round).where(Round.workspace_id == ctx.workspace_id,
                Round.project_id.in_(project_ids)).order_by(Round.project_id, Round.ordinal)).all():
            rounds.setdefault(row.project_id, []).append(row)
    return rounds


def _reports(session, ctx, round_ids) -> dict[str, list[dict]]:
    """Each round's confirmed reports, oldest first, with the person on the first audit event that names the batch.

    The upload commands record the batch id on the round's events (previewed, then confirmed), so the first one
    is the upload itself. The raw file is not read."""
    if not round_ids:
        return {}
    batches = session.execute(select(Batch.id, Batch.round_id, Batch.replaces_id, Batch.created_at, Batch.rows_json)
        .where(Batch.workspace_id == ctx.workspace_id, Batch.round_id.in_(round_ids), Batch.state == "confirmed")
        .order_by(Batch.created_at, Batch.id)).all()
    if not batches:
        return {}
    uploaders: dict[str, str | None] = {}
    for initiator, details in session.execute(select(AuditEventRecord.initiator_id, AuditEventRecord.details_json).where(
            AuditEventRecord.workspace_id == ctx.workspace_id, AuditEventRecord.resource_type == "marketing_round",
            AuditEventRecord.resource_id.in_(round_ids))
            .order_by(AuditEventRecord.created_at, AuditEventRecord.id)).all():
        batch = (details or {}).get("batch_id")
        if isinstance(batch, str):
            uploaders.setdefault(batch, initiator)
    reports: dict[str, list[dict]] = {}
    for batch_id, round_id, replaces, created_at, rows in batches:
        reports.setdefault(round_id, []).append({
            "batch_id": batch_id, "kind": "correction" if replaces else "upload", "replaces_batch_id": replaces,
            "uploaded_at": created_at.isoformat(), "uploaded_by": uploaders.get(batch_id), "row_count": len(rows or [])})
    return reports
