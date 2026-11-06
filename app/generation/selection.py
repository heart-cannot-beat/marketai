"""Workspace-scoped provider selection and frozen generation inputs.

The legacy environment-key readiness check is deliberately not used here.
Paid generation needs a Workspace binding, an entitlement lease, a spend
guardrail and an explicitly configured maximum quote for the selected model
and size. No provider-wide UI estimate is treated as a payable ceiling.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import config
from app.adapters.persistence.models import (
    LibraryEntryRecord, LibraryVersionRecord, ProductRecord, WorkspaceRecord, utcnow,
)
from app.entitlements.credentials import CredentialBindingRecord, CredentialVersionRecord
from app.entitlements.cost_models import SpendGuardrailRecord
from app.entitlements.policy import (
    EntitlementPolicy, FeatureNotEntitledError, NumericEntitlementExceededError,
    WorkspaceLifecycleError,
)
from app.entitlements.reservation_book import GuardrailExceededError, ReservationBook
from app.entitlements.types import EntitlementRequirements
from app.platform.tenancy import TenantContext
from app.providers.router import get_provider

PROVIDERS = ("gemini", "openai", "seedream", "mock")
ASPECT_RATIOS = ("1:1", "4:5", "9:16", "16:9")
IMAGE_SIZES = ("1K", "2K")
MAX_VARIANTS = 4
MAX_PROMPT_LENGTH = 4000
# Reuse the hosted Generation/llm_chat commercial gate: a current signed
# entitlement lease and Owner spend guardrails, rather than inventing an
# additional feature key absent from the published Beta plan.
REQUIREMENTS = EntitlementRequirements((), ())


class SelectionError(ValueError):
    def __init__(self, code: str, detail: str, *, params: dict | None = None):
        self.code, self.detail = code, detail
        self.params = params or {}
        super().__init__(detail)


def credential_binding(session: Session, ctx: TenantContext, provider: str, *, include_secret=False):
    if session.get_bind().dialect.name == "postgresql" and not include_secret:
        row = session.execute(text("""
            SELECT binding_id, binding_revision FROM workspace_provider_bindings
            WHERE workspace_id = :workspace AND provider = :provider
        """), {"workspace": ctx.workspace_id, "provider": provider}).first()
        return (SimpleNamespace(id=row.binding_id, revision=row.binding_revision), None) if row else None
    return session.execute(
        select(CredentialBindingRecord, CredentialVersionRecord)
        .join(CredentialVersionRecord, (
            (CredentialVersionRecord.id == CredentialBindingRecord.credential_version_id)
            & (CredentialVersionRecord.workspace_id == CredentialBindingRecord.workspace_id)
            & (CredentialVersionRecord.provider == CredentialBindingRecord.provider)
        ))
        .where(
            CredentialBindingRecord.workspace_id == ctx.workspace_id,
            CredentialBindingRecord.provider == provider,
            CredentialBindingRecord.binding_scope == "generation",
            CredentialVersionRecord.state == "active",
        )
    ).first()


def maximum_unit_cost(provider: str, model: str, size: str) -> Decimal:
    if provider == "mock":
        return Decimal("0")
    try:
        registry = json.loads(config.GENERATION_MAX_COSTS_JSON)
        amount = Decimal(str(registry[provider][model][size]))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("100"):
            raise ValueError("invalid maximum")
        return amount
    except (ValueError, TypeError, KeyError, InvalidOperation):
        raise SelectionError("price_unavailable", "该模型和分辨率尚未配置最大成本，请联系管理员。") from None


def authorize_paid(session: Session, ctx: TenantContext):
    try:
        return EntitlementPolicy().authorize(session, ctx, REQUIREMENTS, utcnow())
    except (WorkspaceLifecycleError, FeatureNotEntitledError, NumericEntitlementExceededError):
        raise SelectionError("generation_not_entitled", "当前工作区暂不能使用正式生成，请检查访问权限。") from None


def _amount(value) -> str:
    """A money param for the interface's copy: ``0.100000`` -> ``0.1``."""
    return format(Decimal(str(value)).normalize(), "f")


def check_budget(session: Session, ctx: TenantContext, amount: Decimal):
    guardrail = session.scalar(select(SpendGuardrailRecord).where(
        SpendGuardrailRecord.workspace_id == ctx.workspace_id,
    ))
    workspace = session.get(WorkspaceRecord, ctx.workspace_id)
    if not guardrail:
        raise SelectionError("guardrail_required", "请先由工作区 Owner 配置生成预算上限。")
    if not workspace or workspace.budget_currency != "USD" or guardrail.budget_currency != "USD":
        raise SelectionError("currency_unsupported", "正式生成目前需要美元预算；其他币种尚未接入汇率报价。")
    platform_max = min(config.MAX_COST_PER_RUN, config.MARKETAI_PLATFORM_MAX_PER_OPERATION)
    if amount > platform_max:
        raise SelectionError(
            "budget_exceeded", "本次生成超过平台单次成本上限。",
            params={"limit": _amount(platform_max), "amount": _amount(amount), "currency": "USD"},
        )
    try:
        ReservationBook()._check_guardrails(session, guardrail, amount, utcnow())
    except GuardrailExceededError as exc:
        raise SelectionError(
            "budget_exceeded", "本次生成超过工作区预算上限或未结算成本上限。",
            params={"limit": _amount(exc.limit_value), "amount": _amount(exc.proposed_value), "currency": "USD"},
        ) from None
    return guardrail


def provider_catalog(session: Session, ctx: TenantContext) -> list[dict]:
    from .image_tools import supported_tools
    options = []
    for name in PROVIDERS:
        adapter = get_provider(name)
        binding = None if name == "mock" else credential_binding(session, ctx, name)
        reason = None
        if name != "mock":
            if binding is None:
                reason = "credential_required"
            else:
                try:
                    authorize_paid(session, ctx)
                    check_budget(session, ctx, Decimal("0"))
                except SelectionError as exc:
                    reason = exc.code
        models = []
        for model in adapter.models:
            sizes, prices = [], {}
            for size in IMAGE_SIZES:
                try:
                    price = maximum_unit_cost(name, model, size)
                    sizes.append(size)
                    prices[size] = str(price)
                except SelectionError:
                    pass
            models.append({
                "id": model, "image_sizes": sizes,
                "image_tools": supported_tools(name, model),
                "maximum_costs": prices,
                "available": reason is None and bool(sizes),
                "unavailable_reason": reason or (None if sizes else "price_unavailable"),
            })
        options.append({
            "id": name, "label": adapter.label, "default_model": adapter.default_model,
            "execution_mode": "mock" if name == "mock" else "byok",
            "credential_ready": name == "mock" or binding is not None,
            "available": any(m["available"] for m in models),
            "unavailable_reason": reason or (None if any(m["available"] for m in models) else "price_unavailable"),
            "aspect_ratios": list(ASPECT_RATIOS), "models": models,
        })
    return options


def product_options(session: Session, ctx: TenantContext) -> list[dict]:
    entries = session.scalars(select(LibraryEntryRecord).where(
        LibraryEntryRecord.workspace_id == ctx.workspace_id,
        LibraryEntryRecord.entry_type == "product",
        LibraryEntryRecord.lifecycle == "active",
        LibraryEntryRecord.current_version_id.is_not(None),
    ).order_by(LibraryEntryRecord.display_name)).all()
    options = []
    for entry in entries:
        version = session.scalar(select(LibraryVersionRecord).where(
            LibraryVersionRecord.workspace_id == ctx.workspace_id,
            LibraryVersionRecord.id == entry.current_version_id,
            LibraryVersionRecord.state == "effective",
            LibraryVersionRecord.eligibility != "blocked",
        ))
        if version:
            options.append({"id": entry.id, "name": entry.display_name, "version_id": version.id})
    # Existing products remain selectable; their complete text is frozen at
    # preview time. Published Library products use an immutable version.
    ids = {o["id"] for o in options}
    for product in session.scalars(select(ProductRecord).where(
        ProductRecord.workspace_id == ctx.workspace_id,
    ).order_by(ProductRecord.name)).all():
        if product.id not in ids and session.get(LibraryEntryRecord, product.id) is None:
            options.append({"id": product.id, "name": product.name, "version_id": None})
    return options


def product_input(session: Session, ctx: TenantContext, product_id: str, *, ensure_anchor=False):
    entry = session.scalar(select(LibraryEntryRecord).where(
        LibraryEntryRecord.workspace_id == ctx.workspace_id, LibraryEntryRecord.id == product_id,
    ))
    if entry:
        version = session.scalar(select(LibraryVersionRecord).where(
            LibraryVersionRecord.workspace_id == ctx.workspace_id,
            LibraryVersionRecord.id == entry.current_version_id,
            LibraryVersionRecord.state == "effective",
            LibraryVersionRecord.eligibility != "blocked",
        ))
        if entry.entry_type != "product" or entry.lifecycle != "active" or version is None:
            raise SelectionError("product_unavailable", "请选择已发布且可用的产品。")
        content = dict(version.content_json)
        if ensure_anchor and session.scalar(select(ProductRecord).where(
            ProductRecord.workspace_id == ctx.workspace_id, ProductRecord.id == product_id,
        )) is None:
            # Compatibility identity required by the pre-existing Run/Asset
            # FKs. Execution always uses the immutable version below.
            session.add(ProductRecord(id=product_id, workspace_id=ctx.workspace_id,
                                      name=content["name"]))
            session.flush()
        return {"id": product_id, "version_id": version.id, "content_hash": version.content_hash,
                "content": content}
    product = session.scalar(select(ProductRecord).where(
        ProductRecord.workspace_id == ctx.workspace_id, ProductRecord.id == product_id,
    ))
    if not product:
        raise SelectionError("product_unavailable", "该产品不存在于当前工作区。")
    content = {k: getattr(product, k) for k in
               ("name", "description", "selling_points", "audience", "brand_notes")}
    return {"id": product.id, "revision": product.revision, "version_id": None,
            "content_hash": hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(),
            "content": content}


def resolve_selection(session: Session, ctx: TenantContext, draft) -> dict:
    if draft.state == "archived":
        raise SelectionError("draft_archived", "该草稿已归档。")
    if draft.provider not in PROVIDERS:
        raise SelectionError("provider_unavailable", "请选择已接入的图片供应商。")
    adapter = get_provider(draft.provider)
    if draft.model not in adapter.models:
        raise SelectionError("model_unavailable", "该供应商不支持所选模型。")
    mode = "mock" if draft.provider == "mock" else "byok"
    if draft.execution_mode != mode:
        raise SelectionError("invalid_mode", "供应商与生成模式不匹配。")
    fmt = dict(draft.format_json or {})
    ratio, size = fmt.get("aspect_ratio", "1:1"), fmt.get("image_size", "1K")
    if size == "1024x1024" and mode == "mock":
        size = "1K"  # pre-existing Mock recipes
    variants = fmt.get("variants", 1)
    if ratio not in ASPECT_RATIOS or size not in IMAGE_SIZES:
        raise SelectionError("format_unsupported", "所选画幅或分辨率不可用。")
    if type(variants) is not int or not 1 <= variants <= MAX_VARIANTS:
        raise SelectionError("variants_invalid", "每次请选择 1–4 张变体。")
    prompt = (draft.selectors_json or {}).get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_LENGTH or (mode == "byok" and not prompt.strip()):
        raise SelectionError("prompt_invalid", "请填写不超过 4000 字的创意描述。")
    product = product_input(session, ctx, draft.product_id)
    edit_source = None
    if draft.source_asset_version_id:
        from .image_inputs import source_image
        edit_source = source_image(session, ctx, draft.source_asset_version_id, draft.product_id, mode)
        if not adapter.supports_ref:
            raise SelectionError("image_edit_unsupported", "该供应商不支持原图修改。")
    # This provider-selection slice supports prompt variations, not an
    # unvalidated client-authored combination execution plan.
    strategy = draft.combination_strategy_json or {}
    if strategy and strategy != {"mode": "single"}:
        raise SelectionError("combination_unsupported", "当前页面支持单创意的多张变体，请重新预览。")
    unit_cost = maximum_unit_cost(draft.provider, draft.model, size)
    credential_evidence = {}
    if mode == "byok":
        binding = credential_binding(session, ctx, draft.provider)
        if binding is None:
            raise SelectionError("credential_required", "请先由工作区 Owner 配置该供应商的 API Key。")
        authorize_paid(session, ctx)
        check_budget(session, ctx, unit_cost * variants)
        credential_evidence = {"active": True, "provider": draft.provider,
                               "binding_id": binding[0].id, "binding_revision": binding[0].revision}
    marketing_intent = None
    marketing_feedback = None
    image_tool = None
    from app.marketing.feedback import resolve as resolve_feedback
    from app.marketing.models import MarketingGenerationLinkRecord
    from app.marketing.service import get_project, creative_intent, MarketingError
    link = session.scalar(select(MarketingGenerationLinkRecord).where(
        MarketingGenerationLinkRecord.workspace_id == ctx.workspace_id,
        MarketingGenerationLinkRecord.draft_id == draft.id))
    if link:
        try:
            project = get_project(session, ctx, link.project_id)
        except MarketingError:
            raise SelectionError("marketing_plan_unavailable", "找不到关联的测试方案。") from None
        if project.state != "plan_confirmed" or project.revision != link.plan_revision:
            raise SelectionError("marketing_plan_changed", "测试方案已变化，请从当前方案重新准备生成草稿。")
        if project.product_id != draft.product_id:
            raise SelectionError("marketing_product_changed", "这套素材的产品必须与测试方案一致。")
        marketing_intent = creative_intent(project, link.combination_id)
    try:
        feedback = resolve_feedback(session, ctx, draft)
        if feedback:
            marketing_intent, marketing_feedback = feedback["intent"], feedback["feedback"]
            image_tool = marketing_feedback.get("image_tool")
            if image_tool:
                from .image_tools import validate_tool
                validate_tool(image_tool, draft.provider, draft.model)
                if size != "2K" or variants != 1:
                    raise SelectionError("image_tool_format_changed", "图片工具固定为一次一张 2K 处理，请恢复设置或重新准备草稿。")
    except MarketingError as exc:
        raise SelectionError(exc.code, exc.detail) from exc
    except SelectionError:
        raise
    except ValueError as exc:
        raise SelectionError("image_tool_unsupported", str(exc)) from exc
    references = (draft.selectors_json or {}).get("product_references", [])
    if references:
        from app.marketing.product_references import assert_current
        assert_current(session, ctx, references)
    brand_logo = (draft.selectors_json or {}).get("brand_logo")
    if brand_logo:
        _assert_brand_logo(session, ctx, brand_logo)
    return {
        "product": product, "prompt": prompt.strip(),
        **({"source_image": edit_source} if edit_source else {}),
        **({"image_tool": image_tool} if image_tool else {}),
        **({"product_references": references, "reference_actor_id": ctx.initiator_id or ctx.executor} if references else {}),
        **({"brand_logo": brand_logo} if brand_logo else {}),
        **({"marketing_intent": marketing_intent} if marketing_intent else {}),
        **({"marketing_feedback": marketing_feedback} if marketing_feedback else {}),
        "format": {"aspect_ratio": ratio, "image_size": size, "variants": variants},
        "cost_quote": {"maximum_amount": str(unit_cost * variants), "unit_maximum": str(unit_cost),
                       "currency": "USD", "pricing_version": config.MARKETAI_PRICE_REGISTRY_VERSION,
                       "quantity": variants},
        "credential_evidence": credential_evidence,
    }


def _assert_brand_logo(session: Session, ctx: TenantContext, logo: dict):
    """The plan's frozen brand logo, separate from product-appearance references."""
    from app.marketing.brand_kits import assert_logo_current
    from app.marketing.service import MarketingError
    try:
        assert_logo_current(session, ctx, logo)
    except MarketingError as exc:
        raise SelectionError(exc.code, exc.detail) from exc


def check_frozen_selection(session: Session, ctx: TenantContext, recipe: dict):
    frozen = recipe.get("selection")
    if not frozen:
        return  # historical controlled-Mock recipes
    from app.marketing.product_references import assert_current
    assert_current(session, ctx, frozen.get("product_references", []))
    if frozen.get("brand_logo"):
        _assert_brand_logo(session, ctx, frozen["brand_logo"])
    if frozen.get("marketing_feedback") and not frozen.get("source_image"):
        raise SelectionError("edit_source_unavailable", "旧修改预览未冻结原图，请重新预览后再提交。")
    if frozen.get("source_image"):
        from .image_inputs import check_source
        check_source(session, ctx, frozen["source_image"], frozen["product"]["id"], recipe["execution_mode"])
    if frozen.get("image_tool"):
        from .image_tools import validate_tool
        try:
            validate_tool(frozen["image_tool"], recipe["provider"], recipe["model"])
        except ValueError as exc:
            raise SelectionError("image_tool_unsupported", str(exc)) from exc
        if not frozen.get("source_image") or frozen["format"]["image_size"] != "2K" or frozen["format"]["variants"] != 1:
            raise SelectionError("image_tool_format_changed", "编辑配方不完整，请重新准备草稿。")
    intent = frozen.get("marketing_intent")
    if intent:
        from app.marketing.service import get_project
        project = get_project(session, ctx, intent["project_id"])
        if project.state != "plan_confirmed" or project.revision != intent["plan_revision"]:
            raise SelectionError("marketing_plan_changed", "测试方案已变化，请重新准备素材。")
        if not intent.get("ad_language") or project.plan_json.get("language_confirmation_required"):
            raise SelectionError("ad_language_confirmation_required", "Confirm the advertising language and prepare a fresh preview before new generation.")
    feedback = frozen.get("marketing_feedback")
    if feedback:
        from app.marketing.workflow import get_round, scoped, Handoff
        row = get_round(session, ctx, feedback["round_id"])
        if row.state != "reviewing" or scoped(session, ctx, Handoff, round_id=row.id):
            raise SelectionError("feedback_round_frozen", "本轮已冻结，修改素材请准备新一轮。")
    current_product = product_input(session, ctx, frozen["product"]["id"])
    if current_product != frozen["product"]:
        raise SelectionError("preview_dependencies_changed", "产品已变化，请重新预览。")
    if recipe["execution_mode"] == "byok":
        binding = credential_binding(session, ctx, recipe["provider"])
        if binding is None:
            raise SelectionError("credential_required", "该供应商的工作区凭据已不可用。")
        authorize_paid(session, ctx)
        quote = frozen["cost_quote"]
        current_price = maximum_unit_cost(recipe["provider"], recipe["model"], frozen["format"]["image_size"])
        if str(current_price) != quote["unit_maximum"] or config.MARKETAI_PRICE_REGISTRY_VERSION != quote["pricing_version"]:
            raise SelectionError("preview_dependencies_changed", "报价已变化，请重新预览。")
        check_budget(session, ctx, Decimal(quote["maximum_amount"]))
