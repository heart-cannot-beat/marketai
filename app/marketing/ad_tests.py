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


def kept_ads(session, ctx, ideas_row) -> list:
    """The ideas' kept ads, oldest first, each with the image and the recipe (or upload record) it was made from."""
    from . import ideas
    from .models import MarketingAdKeepRecord
    marks = dict(session.execute(select(MarketingAdKeepRecord.asset_version_id, MarketingAdKeepRecord.kept).where(
        MarketingAdKeepRecord.workspace_id == ctx.workspace_id, MarketingAdKeepRecord.project_id == ideas_row.id)).all())
    return [(version, recipe) for version, recipe in ideas.project_ads(session, ctx, ideas_row) if marks.get(version.id)]


def kept_ad(version, recipe):
    """A kept ad as a test takes it (``ad_test_plans.KeptAd``): the combination its image was made from, the hash
    of that intent (plan E4, E8) and its execution mode (a Controlled Mock example, or real)."""
    from . import ideas
    from .ad_test_plans import KeptAd
    intent = ideas.intent_of(recipe)
    combination = intent.get("combination") or {}
    return KeptAd(asset_version_id=version.id, combination_id=combination.get("id", ""),
                  angle_id=combination.get("angle_id", ""), expression_id=combination.get("expression_id", ""),
                  intent_hash=service.digest(intent),
                  execution_mode="mock" if version.execution_mode in ("mock", "controlled_mock") else "byok")


def product_link(session, ctx, ideas_row) -> str | None:
    """The product page link the user pasted for this product: the default landing page (design §5.2)."""
    from app.adapters.persistence.models import LibraryDraftRecord, LibraryEntryRecord, LibraryVersionRecord, ProductRecord
    product = session.scalar(select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
                                                         ProductRecord.id == ideas_row.product_id))
    entry_id = product.product_profile_entry_id if product else None
    if not entry_id:
        return None
    draft = session.scalar(select(LibraryDraftRecord.content_json).where(
        LibraryDraftRecord.workspace_id == ctx.workspace_id, LibraryDraftRecord.entry_id == entry_id))
    published = session.scalar(select(LibraryVersionRecord.content_json).join(LibraryEntryRecord,
            (LibraryEntryRecord.current_version_id == LibraryVersionRecord.id)
            & (LibraryEntryRecord.workspace_id == LibraryVersionRecord.workspace_id))
        .where(LibraryEntryRecord.workspace_id == ctx.workspace_id, LibraryEntryRecord.id == entry_id))
    for content in (draft, published):
        link = (content or {}).get("source_url")
        if isinstance(link, str) and link.strip().lower().startswith(("http://", "https://")):
            return link.strip()
    return None


def checks_quote(session, ctx, execution_mode: str, quantity: int) -> dict:
    """The "look together" check of each kept ad (plan E6): who checks in this mode, and the most it costs."""
    from decimal import Decimal
    from app.generation.selection import SelectionError, check_budget
    from .ai_qc import mode_capability
    cap = mode_capability(session, ctx, execution_mode)
    total = Decimal(cap["unit_maximum"]) * quantity
    reason = cap["reason"]
    if reason is None and execution_mode == "byok":
        try:
            check_budget(session, ctx, total)
        except SelectionError as exc:
            reason = exc.code
    return {"available": reason is None, "unavailable_reason": reason, "quantity": quantity,
            "maximum_cost": str(total), "currency": "USD", "provider": cap["provider"], "model": cap["model"]}


def quote(session, ctx, ideas_row, answers) -> dict:
    """``MarketingTestQuote``: what taking these ideas' kept ads to a test compares, whether the money is enough,
    when the result is due and what starting costs at most (design §5.2, §5.6). Reads only.

    ``available`` is false, with the first reason, when: the ideas are not ready, nothing is kept, a kept ad
    may not be exported (each named), kept ads mix Controlled Mock and real ones, a real test would start in the
    past, there is no landing page, the plan the template writes fails a confirming check (``plan_error``), or
    the check cannot be admitted."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from pydantic import ValidationError
    from app.adapters.persistence.models import utcnow
    from . import ad_test_plans as plans, ideas_export
    from .ideas_contracts import Ideas
    from .workflow_contracts import EvaluationProtocol, FreezeRound
    service.ensure_active(session, ctx)
    protocol = EvaluationProtocol()
    ready = ideas_row.state == "ideas_ready" and bool(ideas_row.ideas_json)
    pairs = kept_ads(session, ctx, ideas_row) if ready else []
    kept = [kept_ad(version, recipe) for version, recipe in pairs]
    reason, plan_error, refused = None, None, []
    if not ready:
        reason = "ideas_not_ready"
    elif not kept:
        reason = "no_kept_ads"
    else:
        statuses = ideas_export.statuses(session, ctx, ideas_row, pairs)
        refused = [{"asset_version_id": ad.asset_version_id, **{k: statuses[ad.asset_version_id][k] for k in ("status", "reasons")}}
                   for ad in kept if statuses[ad.asset_version_id]["status"] != "exportable"]
        reason = "ads_not_exportable" if refused else None
    modes = {ad.execution_mode for ad in kept}
    if reason is None and len(modes) > 1:
        reason = "ads_execution_mixed"
    mode = next(iter(modes)) if len(modes) == 1 else None

    timezone = answers.account_timezone or plans.ACCOUNT_TIMEZONE
    today = utcnow().astimezone(ZoneInfo(timezone)).date()
    start, end, conclusion = plans.schedule(answers.start_date or today + timedelta(days=1), answers.duration_days, protocol)
    if reason is None and mode == "byok" and start < today:  # a real round's rules are set before it starts
        reason = "start_date_past"
    link = product_link(session, ctx, ideas_row)
    landing = answers.landing_page or link
    if reason is None and not landing:
        reason = "landing_page_required"
    currency = answers.currency or plans.CURRENCY
    max_rounds = answers.max_small_rounds or plans.MAX_SMALL_ROUNDS
    conditions = plans.Conditions(
        audience=answers.audience or ideas_export.confirmed_audience(session, ctx, ideas_row)
        or (ideas_row.understanding_json or {}).get("audience") or "",
        landing_page=landing or "", offer=answers.offer, placement=answers.placement, currency=currency,
        max_small_rounds=max_rounds, media_budget=answers.media_budget)

    doc = Ideas.model_validate(ideas_row.ideas_json) if kept else None
    comparison = plans.comparison_of(kept) if kept else None
    items = plans.subjects(doc, kept, comparison) if kept else []
    share = plans.estimate(len(items), answers.round_budget, answers.target_cost, protocol) if items else None
    plan = None
    if reason is None:
        try:
            plan = plans.build_plan(ideas_row, kept, answers, conditions, protocol)
            plans.check_plan(ideas_row, plan)
            FreezeRound(start_date=start, end_date=end, account_timezone=timezone, execution_mode=mode, protocol=protocol,
                        attribution_window=answers.attribution_window or plans.ATTRIBUTION_WINDOW)
        except ValidationError as exc:
            reason, plan_error, plan = "plan_invalid", exc.errors()[0]["type"], None
        except service.MarketingError as exc:
            reason, plan_error, plan = "plan_invalid", exc.code, None
    checks = checks_quote(session, ctx, mode or "byok", len(kept))
    if reason is None and not checks["available"]:
        reason = "checks_unavailable"
    window = answers.attribution_window or plans.ATTRIBUTION_WINDOW
    resolved = {**plans.as_dict(conditions), "account_timezone": timezone, "start_date": start.isoformat(),
                "attribution_window": window, "execution_mode": mode}
    digest = plans.quote_hash(ideas_row.revision, kept, answers, resolved, checks, protocol,
                              plan.model_dump(mode="json") if plan else None)
    return {"available": reason is None, "unavailable_reason": reason, "plan_error": plan_error,
            "not_exportable": refused, "ideas_revision": ideas_row.revision, "execution_mode": mode,
            "comparison": comparison,
            "question": plans.question(doc, comparison, items, answers.target_cost, currency) if items else None,
            "subjects": [{key: item[key] for key in ("kind", "angle_id", "expression_id", "title", "combination_ids",
                                                     "asset_version_ids")}
                         | {"budget": share["budget"], "expected_orders": share["expected_orders"]} for item in items],
            "target_cost": plans.money(answers.target_cost), "round_budget": plans.money(answers.round_budget),
            "currency": currency, "duration_days": answers.duration_days, "min_purchases": protocol.min_purchases,
            "tier": share["tier"] if share else None, "needed_budget": share["needed_budget"] if share else None,
            "suggested_subjects": share["suggested_subjects"] if share else None,
            "start_date": start, "end_date": end, "conclusion_date": conclusion, "account_timezone": timezone,
            "attribution_window": window, "attribution_lag_days": protocol.attribution_lag_days,
            "landing_page": landing, "landing_page_source": ("answer" if answers.landing_page else "product_link") if landing else None,
            "checks": {key: checks[key] for key in ("available", "unavailable_reason", "quantity", "maximum_cost", "currency")},
            "quote_hash": digest}
