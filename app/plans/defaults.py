"""Workspace「生成默认值」(front-flow contract §4) and plan cost quotes.

The front flow hides provider, model, execution mode, aspect ratio and the
variants per scale behind one Workspace setting. Until an Owner saves one
the *effective* defaults are computed:

* an active OpenAI generation key is bound -> ``byok`` / ``openai`` /
  :data:`LATEST_GPT_IMAGE`;
* otherwise controlled ``mock`` (the page shows a "bind a key" banner from
  ``credential_bound: false``).

Aspect ratio defaults to Feed ``1:1``; variants per combination default to
1 for a sample and 2 for a small or full test. A test-plan Run reads the
effective defaults when it is materialised and freezes them into its
generation draft (and so into the Run's recipe snapshot); changing the
defaults later never rewrites an existing Run.

Quality check (Issue #169): every image of a ``byok`` Run is checked by a
governed VLM call on the Workspace's OpenAI key, reserved next to the image
call. A plan card's maximum cost includes that check per image exactly as
the preview will (``app.generation.vlm_qc``): same model, same reference
count (the product's usable references, at most three). Mock checks cost
nothing; there is no separate toggle -- the check follows the execution
mode.

Writing the defaults is Owner-only (they decide what Members spend on);
reading them is open to every member.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import GenerationDefaultsRecord, utcnow
from app.generation.capabilities import PLATFORM_ADAPTER_REGISTRY
from app.generation.credential_binding import resolve_generation_credential
from app.generation.pricing import OPENAI_PROVIDER, UnpricedCallError, unit_price
from app.platform import audit
from app.platform.tenancy import TenantContext

LATEST_GPT_IMAGE = "gpt-image-2"
MOCK_PROVIDER = "mock"
MOCK_MODEL = "mock-v1"
DEFAULT_ASPECT_RATIO = "1:1"
DEFAULT_VARIANTS = {"sample": 1, "small": 2, "full": 2}
EXECUTION_MODES = ("mock", "byok")


class DefaultsProblem(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class GenerationDefaults:
    provider: str
    model: str
    execution_mode: str
    aspect_ratio: str
    variants: dict[str, int]
    source: str  # "workspace" (saved by an Owner) | "computed"
    revision: int  # 0 while nothing is saved
    credential_bound: bool

    def variants_for(self, scale: str) -> int:
        return int(self.variants[scale])

    def format_config(self, scale: str) -> dict:
        return {"aspect_ratio": self.aspect_ratio, "variants_per_combination": self.variants_for(scale)}

    def frozen(self) -> dict:
        """What a Run records about the defaults it ran with."""
        return {
            "provider": self.provider,
            "model": self.model,
            "execution_mode": self.execution_mode,
            "aspect_ratio": self.aspect_ratio,
            "variants": dict(self.variants),
            "source": self.source,
            "revision": self.revision,
        }

    def view(self) -> dict:
        warnings = []
        if self.execution_mode == "byok" and not self.credential_bound:
            warnings.append("byok_credential_missing")
        if self.execution_mode == "mock":
            warnings.append("mock_mode")
        return {
            **self.frozen(),
            "credential_bound": self.credential_bound,
            "warnings": warnings,
        }

    def unit_price(self, reference_images: int = 0) -> Decimal:
        """USD list price of one image under these defaults (0 for Mock)."""
        if self.execution_mode != "byok":
            return Decimal("0")
        try:
            return unit_price(
                self.provider, self.model, {"aspect_ratio": self.aspect_ratio},
                reference_images=reference_images,
            )
        except UnpricedCallError:
            return Decimal("0")

    def reference_images_per_call(self, session: Session, ctx: TenantContext, product_id: str) -> int:
        """Reference images each call would send for ``product_id`` (Issue #167).

        The same capability decision the preview freezes
        (``creative_recipe.decide_reference_use``) over the product's
        currently bound references, so the plan card's maximum cost is the
        one the preview will quote.
        """
        from app.generation.creative_recipe import decide_reference_use, product_reference_count

        if self.execution_mode != "byok":
            return 0
        available = product_reference_count(session, ctx, product_id)
        decision = decide_reference_use(
            PLATFORM_ADAPTER_REGISTRY.get(self.provider), self.model, available,
            execution_mode=self.execution_mode,
        )
        return int(decision.get("count") or 0)

    def quality_check_references(self, session: Session, ctx: TenantContext, product_id: str) -> int:
        """Reference images each image's quality check compares (Issue #169)."""
        from app.generation.creative_recipe import product_reference_count
        from app.generation.vlm_qc import MAX_QC_REFERENCES

        if self.execution_mode != "byok":
            return 0
        return min(product_reference_count(session, ctx, product_id), MAX_QC_REFERENCES)

    def quality_check_price(self, qc_references: int = 0) -> Decimal:
        """Maximum of one image's quality check under these defaults (0 for Mock)."""
        from app.generation.vlm_qc import charged_cost_per_image, resolve_qc_policy

        if self.execution_mode != "byok":
            return Decimal("0")
        policy = resolve_qc_policy(
            execution_mode=self.execution_mode,
            reference_images_available=qc_references,
            openai_key_bound=self.credential_bound,
        )
        return charged_cost_per_image(policy)

    def max_cost(self, images: int, *, reference_images: int = 0, qc_references: int = 0) -> dict:
        per_image = self.unit_price(reference_images) + self.quality_check_price(qc_references)
        amount = (per_image * images).quantize(Decimal("0.000001"))
        return {"amount": format(amount.normalize() if amount else Decimal("0"), "f"), "currency": "USD"}


def _credential_bound(session: Session, ctx: TenantContext) -> bool:
    return bool(resolve_generation_credential(session, ctx, OPENAI_PROVIDER).get("active"))


def effective_defaults(session: Session, ctx: TenantContext) -> GenerationDefaults:
    bound = _credential_bound(session, ctx)
    record = session.get(GenerationDefaultsRecord, ctx.workspace_id)
    if record is not None:
        return GenerationDefaults(
            provider=record.provider,
            model=record.model,
            execution_mode=record.execution_mode,
            aspect_ratio=record.aspect_ratio,
            variants={
                "sample": record.variants_sample,
                "small": record.variants_small,
                "full": record.variants_full,
            },
            source="workspace",
            revision=record.revision,
            credential_bound=bound,
        )
    if bound:
        provider, model, mode = OPENAI_PROVIDER, LATEST_GPT_IMAGE, "byok"
    else:
        provider, model, mode = MOCK_PROVIDER, MOCK_MODEL, "mock"
    return GenerationDefaults(
        provider=provider,
        model=model,
        execution_mode=mode,
        aspect_ratio=DEFAULT_ASPECT_RATIO,
        variants=dict(DEFAULT_VARIANTS),
        source="computed",
        revision=0,
        credential_bound=bound,
    )


def get_defaults(ctx: TenantContext) -> dict:
    if ctx.role not in ("owner", "member"):
        raise DefaultsProblem(403, "generation_defaults_forbidden", "只有工作区成员可以查看生成默认值")
    with tenant_session(ctx) as session:
        return effective_defaults(session, ctx).view()


def _validate(provider: str, model: str, mode: str, aspect_ratio: str, variants: dict[str, int]) -> None:
    if mode not in EXECUTION_MODES:
        raise DefaultsProblem(422, "execution_mode_invalid", f"execution_mode 必须是 {list(EXECUTION_MODES)} 之一")
    if mode == "mock" and provider != MOCK_PROVIDER:
        raise DefaultsProblem(422, "provider_mode_mismatch", "mock 模式只能使用 mock provider")
    if mode == "byok" and provider == MOCK_PROVIDER:
        raise DefaultsProblem(422, "provider_mode_mismatch", "byok 模式需要真实 provider")
    adapter = PLATFORM_ADAPTER_REGISTRY.get(provider)
    if not adapter or not adapter.get("advertised"):
        raise DefaultsProblem(422, "provider_unavailable", f"provider {provider!r} 不可用")
    spec = (adapter.get("models") or {}).get(model)
    if spec is None:
        raise DefaultsProblem(422, "model_unavailable", f"{provider} 没有模型 {model!r}")
    limits = spec.get("format_limits") or {}
    if aspect_ratio not in (limits.get("aspect_ratios") or []):
        raise DefaultsProblem(422, "aspect_ratio_unsupported", f"{model} 不支持画幅 {aspect_ratio}")
    ceiling = int(limits.get("max_variants_per_combination") or 1)
    for scale in ("sample", "small", "full"):
        value = variants.get(scale)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            raise DefaultsProblem(
                422, "variants_invalid", f"variants.{scale} 必须是 1 到 {ceiling} 之间的整数",
            )


def update_defaults(
    ctx: TenantContext,
    *,
    expected_revision: int,
    provider: str,
    model: str,
    execution_mode: str,
    aspect_ratio: str,
    variants: dict[str, int],
) -> dict:
    """Owner-only write; ``expected_revision`` is 0 for the first save."""
    if ctx.role != "owner":
        raise DefaultsProblem(403, "generation_defaults_owner_only", "只有 Owner 可以修改生成默认值")
    _validate(provider, model, execution_mode, aspect_ratio, variants)
    with tenant_session(ctx) as session:
        record = session.get(GenerationDefaultsRecord, ctx.workspace_id, with_for_update=True)
        current = record.revision if record is not None else 0
        if current != expected_revision:
            raise DefaultsProblem(
                409, "revision_conflict", f"生成默认值已被修改（当前 revision {current}）",
            )
        now = utcnow()
        before = None
        if record is None:
            record = GenerationDefaultsRecord(workspace_id=ctx.workspace_id, revision=1, created_at=now)
            session.add(record)
        else:
            before = {
                "provider": record.provider, "model": record.model,
                "execution_mode": record.execution_mode, "aspect_ratio": record.aspect_ratio,
                "variants": [record.variants_sample, record.variants_small, record.variants_full],
            }
            record.revision = current + 1
        record.provider = provider
        record.model = model
        record.execution_mode = execution_mode
        record.aspect_ratio = aspect_ratio
        record.variants_sample = variants["sample"]
        record.variants_small = variants["small"]
        record.variants_full = variants["full"]
        record.updated_by = ctx.initiator_id
        record.updated_at = now
        try:
            session.flush()
        except IntegrityError as exc:  # a concurrent first save won the insert
            raise DefaultsProblem(409, "revision_conflict", "生成默认值已被同时修改，请刷新后重试") from exc
        audit.append_event(
            session,
            event_type="generation_defaults.updated",
            resource_type="generation_defaults",
            resource_id=ctx.workspace_id,
            details={
                "before": before,
                "after": {
                    "provider": provider, "model": model, "execution_mode": execution_mode,
                    "aspect_ratio": aspect_ratio,
                    "variants": [variants["sample"], variants["small"], variants["full"]],
                },
                "revision": record.revision,
            },
        )
        return effective_defaults(session, ctx).view()
