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

from decimal import Decimal

from sqlalchemy import or_, select

from app.adapters.persistence.models import AuditEventRecord, ProductRecord, new_id
from app.platform.audit import append_event

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


def round_summary(session, ctx, row) -> dict:
    """A round's frozen dates and the effective ones, after a moved start or an early end (#253)."""
    from .ad_test_schedule import effective_dates
    start, end = effective_dates(session, ctx, row)
    return {"id": row.id, "ordinal": row.ordinal, "state": row.state, "start_date": row.start_date,
            "end_date": row.end_date, "effective_start_date": start, "effective_end_date": end}


def detail(session, ctx, row) -> dict:
    """``MarketingTestDetail``: the test, its source ideas, a plan summary and its rounds, oldest first."""
    from .workflow import scoped
    rounds = sorted(scoped(session, ctx, Round, project_id=row.id), key=lambda r: r.ordinal)
    current = next((r for r in reversed(rounds) if r.state != "closed"), None)
    return {"id": row.id, "revision": row.revision, "product_id": row.product_id,
            "source_project_id": row.source_project_id, "comparison": comparison(row.plan_json),
            "plan_summary": plan_summary(row.plan_json), "rounds": [round_summary(session, ctx, r) for r in rounds],
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
    return _quoted(session, ctx, ideas_row, answers)[0]


def _quoted(session, ctx, ideas_row, answers):
    """The quote, and what starting the test takes from it: the kept ads with their images and recipes, the plan
    and the round to freeze. The start command (#249) runs this same computation, so the hash it confirms
    covers exactly what it starts."""
    from datetime import timedelta
    from types import SimpleNamespace
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

    try:
        doc = Ideas.model_validate(ideas_row.ideas_json) if kept else None
    except ValueError:  # stored ideas a later contract no longer reads: their ads are not exportable either
        doc = None
    comparison = plans.comparison_of(kept) if kept else None
    # A kept ad of a claim or ad taken out of the ideas since (it needs making again) is not a compared object.
    present = doc is not None and all(ad.angle_id in {a.id for a in doc.angles}
                                      and ad.combination_id in {c.id for c in doc.combinations} for ad in kept)
    items = plans.subjects(doc, kept, comparison) if kept and present else []
    share = plans.estimate(len(items), answers.round_budget, answers.target_cost, protocol) if items else None
    plan = freeze = None
    if reason is None:
        try:
            plan = plans.build_plan(ideas_row, kept, answers, conditions, protocol)
            plans.check_plan(ideas_row, plan)
            freeze = FreezeRound(start_date=start, end_date=end, account_timezone=timezone, execution_mode=mode,
                                 protocol=protocol, attribution_window=answers.attribution_window or plans.ATTRIBUTION_WINDOW)
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
    made = SimpleNamespace(pairs=pairs, kept=kept, plan=plan, freeze=freeze)
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
            "quote_hash": digest}, made


#: Shared review values that mean a person judged the image not good enough (plan E5).
NOT_PASSED = ("rejected", "needs_changes")


def start(session, ctx, ideas_row, revision: int, body) -> MarketingProjectRecord:
    """Start a test of these ideas' kept ads (design §5.1, §5.3-§5.5, #249): one command, one transaction.

    It creates the test project with the plan the quote wrote, confirmed as revision 1; freezes its round with the
    quoted dates, mode and protocol (plan E9); puts each kept ad into the round as it is, under the combination
    its image was made from (plan E4); and admits the "look together" check of each ad, reserving its maximum in
    BYOK (plan E6). Each step writes its own audit event. The ideas and their ads are not changed.

    Refused with 409, writing nothing: the ideas changed since the page read them (``stale_marketing_revision``);
    the product has a test in progress (``test_in_progress``, plan E3); what the quote covered changed since --
    a kept ad, what it was made from, an answer, a default, the check maximum (``test_quote_changed``, plan E8);
    the quote cannot start a test (its ``unavailable_reason``, ``ads_not_exportable`` naming each ad); a kept ad
    is not passed on the Workspace's shared review chain (``ad_review_rejected``, plan E5)."""
    from app.generation.review import project_effective_review
    from . import workflow
    from .ad_test_plans import understanding
    from .plan_review import stored_plan
    service.ensure_active(session, ctx)
    # One start at a time in a Workspace (PostgreSQL), and no change of the ideas meanwhile: what is in progress,
    # what is kept and what the quote says are all read after both locks. SQLite serializes writers anyway.
    workflow.serialize_workspace(session, ctx)
    session.execute(select(MarketingProjectRecord.id).where(MarketingProjectRecord.workspace_id == ctx.workspace_id,
        MarketingProjectRecord.id == ideas_row.id).with_for_update())
    session.refresh(ideas_row)
    if ideas_row.revision != revision:
        raise service.MarketingError("stale_marketing_revision", "内容已更新，请刷新后重试。", 409)
    running = in_progress(session, ctx, ideas_row.product_id)
    if running is not None:
        raise service.MarketingError("test_in_progress", "这个产品已有一个进行中的测试：结束或取消它之后，才能开始下一个。",
                                     409, params={"test_id": running.id})
    quoted, made = _quoted(session, ctx, ideas_row, body.answers)
    if quoted["quote_hash"] != body.quote_hash or sorted(set(body.asset_version_ids)) != sorted(
            ad.asset_version_id for ad in made.kept):
        raise service.MarketingError("test_quote_changed", "留下的广告或报价有变化，请看一眼再开始。", 409)
    if not quoted["available"]:
        _refuse(quoted)
    rejected = sorted(ad.asset_version_id for ad in made.kept
                      if project_effective_review(session, ctx, ad.asset_version_id) in NOT_PASSED)
    if rejected:
        raise service.MarketingError("ad_review_rejected", "有留下的广告在别处已被判为不通过，不能拿去测试：换一张或去掉它。",
                                     409, params={"asset_version_ids": rejected})

    row = MarketingProjectRecord(id=new_id("mp"), workspace_id=ctx.workspace_id, product_id=ideas_row.product_id,
        flow=FLOW, state="plan_confirmed", revision=1, source_text=ideas_row.source_text,
        understanding_json=understanding(ideas_row), plan_json=stored_plan(made.plan, None),
        market=ideas_row.market, ad_language=ideas_row.ad_language, source_project_id=ideas_row.id)
    session.add(row)
    session.flush()
    service.checkpoint(session, ctx, row, "confirm_plan")
    frozen = workflow.freeze(session, ctx, row, row.revision, made.freeze)
    _attach_kept(session, ctx, ideas_row, frozen, made)
    calls, maximum = _admit_checks(session, ctx, frozen, quoted)
    append_event(session, event_type="marketing.test_started", resource_type="marketing_project", resource_id=row.id,
                 details={"source_project_id": ideas_row.id, "ideas_revision": ideas_row.revision,
                          "quote_hash": quoted["quote_hash"], "round_id": frozen.id,
                          "asset_version_ids": sorted(ad.asset_version_id for ad in made.kept),
                          "check_ids": [call.id for call in calls], "maximum_cost": maximum})
    session.flush()
    return row


def _refuse(quoted) -> None:
    """The quote's own reason as the start's refusal: the page shows the same quote, so it already says why."""
    reason = quoted["unavailable_reason"]
    if reason == "ads_not_exportable":
        raise service.MarketingError("ads_not_exportable", "有留下的广告还不能导出：先到广告想法页核对，需要重出的请先重出。",
                                     409, params={"ads": quoted["not_exportable"]})
    if reason == "checks_unavailable":
        raise service.MarketingError("checks_unavailable", "现在不能受理“一起看”的检查，测试没有开始。", 409,
                                     params={"reason": quoted["checks"]["unavailable_reason"]})
    if reason == "plan_invalid":
        raise service.MarketingError("plan_invalid", "按这些回答生成的方案没有通过检查，测试没有开始。", 409,
                                     params={"plan_error": quoted["plan_error"]})
    if reason == "ideas_not_ready":
        raise service.MarketingError("ideas_not_ready", "广告想法还没准备好。", 409)
    if reason == "no_kept_ads":
        raise service.MarketingError("no_kept_ads", "还没有留下的广告：先在广告想法页留下要测试的广告。", 409)
    if reason == "ads_execution_mixed":
        raise service.MarketingError("ads_execution_mixed", "留下的广告里既有示例广告又有真实广告，不能放在同一个测试里。", 409)
    if reason == "start_date_past":
        raise service.MarketingError("start_date_past", "真实投放的开始日期不能早于今天。", 409)
    if reason == "landing_page_required":
        raise service.MarketingError("landing_page_required", "请填写广告点进去的网址。", 409)
    raise RuntimeError(f"Unknown quote refusal: {reason}")


def _attach_kept(session, ctx, ideas_row, frozen, made) -> None:
    """Each kept ad goes into the round as it is (plan E4, design §5.4): a reuse record naming the ideas, the ad,
    the intent its image was made from and the verification it stands on, under that intent's combination."""
    from app.generation.paid_execution import asset_blob_ref
    from . import workflow
    from .models import MarketingIdeasExportRecord
    from .reuse import IDEAS, MAX_CHECKABLE_BYTES
    from .workflow_models import MarketingCreativeReuseRecord as Reuse
    combos = {c["id"]: c for c in frozen.snapshot_json["plan"]["combinations"]}
    verification = session.scalar(select(MarketingIdeasExportRecord.id).where(
        MarketingIdeasExportRecord.workspace_id == ctx.workspace_id, MarketingIdeasExportRecord.project_id == ideas_row.id,
        MarketingIdeasExportRecord.kind == "verification")
        .order_by(MarketingIdeasExportRecord.created_at.desc(), MarketingIdeasExportRecord.id.desc()).limit(1))
    for (version, _), ad in zip(made.pairs, made.kept):
        if not version.mime_type.startswith("image/") or version.byte_size > MAX_CHECKABLE_BYTES:
            raise service.MarketingError("asset_not_checkable", "素材不是支持检查的图片。", 409,
                                         params={"asset_version_id": version.id})
        asset_blob_ref(session, ctx, version.id)  # the exact committed file is required, as for any reuse
        source = {"kind": IDEAS, "ideas_project_id": ideas_row.id, "asset_version_id": version.id,
                  "asset_id": version.asset_id, "product_id": ideas_row.product_id, "execution_mode": version.execution_mode,
                  "content_sha256": version.content_sha256, "intent_hash": ad.intent_hash,
                  "verification_record_id": verification, "plan_hash": frozen.snapshot_json["plan_hash"],
                  "combination": combos[ad.combination_id]}
        session.add(Reuse(id=new_id("mreuse"), workspace_id=ctx.workspace_id, round_id=frozen.id,
                          asset_version_id=version.id, combination_id=ad.combination_id, source_json=source,
                          source_hash=service.digest(source), actor_id=ctx.initiator_id or ctx.executor))
        workflow.audit(session, frozen, "creative_reused", {"asset_version_id": version.id,
            "combination_id": ad.combination_id, "source_hash": service.digest(source), "kind": IDEAS,
            "ideas_project_id": ideas_row.id})
    session.flush()


def _admit_checks(session, ctx, frozen, quoted):
    """The "look together" check of every kept ad (plan E6): the round's own paired check, admitted by the API --
    one Operation, one single-attempt Job and, in BYOK, the reservation of its maximum each. It must be exactly
    what the quote put on the button; anything else refuses the start (``checks_unavailable``)."""
    from . import ai_qc, workflow
    checks, inputs, cap = ai_qc.quote_and_inputs(session, ctx, frozen, "paired")
    expected = quoted["checks"]
    if (not checks["available"] or checks["quantity"] != expected["quantity"]
            or Decimal(checks["maximum_cost"]) != Decimal(expected["maximum_cost"])):
        raise service.MarketingError("checks_unavailable", "现在不能受理“一起看”的检查，测试没有开始。", 409,
                                     params={"reason": checks["unavailable_reason"] or "checks_changed"})
    calls = [ai_qc.admit_call(session, ctx, item, cap, kind="paired", policy=ai_qc.POLICIES["paired"],
                              execution_mode=frozen.execution_mode, round_id=frozen.id) for item in inputs]
    workflow.audit(session, frozen, "ai_qc_requested", {"kind": "paired", "call_ids": [call.id for call in calls],
        "quote_hash": checks["quote_hash"], "maximum_cost": checks["maximum_cost"]})
    return calls, checks["maximum_cost"]


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
        return [{**round_summary(session, ctx, r), "reports": reports.get(r.id, [])} for r in rounds.get(row.id, [])]

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
