"""The provider-neutral ``CreativeRecipe``: the only input a generation adapter gets.

Front-flow contract §5.1: a plan (and a Run) describes *what* to make --
the product, the dimension values with their positive / negative English
fragments, the must-match appearance items, the reference images, the
direction and the format -- never a provider prompt. Each adapter translates
the recipe into its own call (``app.generation.prompting`` holds the text
translations; ``openai_adapter`` / ``gemini_adapter`` / ``seedream_adapter``
decide between a plain text call and a reference-image call).

Two halves:

* **Freeze** (:func:`freeze_creative_inputs`, Preview time). Everything the
  recipe needs beyond the combination itself is read once, inside the
  Workspace, and frozen into the recipe snapshot under ``creative``: the
  Product as its current ProductVersion publishes it (else the bridged
  ``products`` row), the *confirmed* must-match items, the product's bound
  ``reference_asset`` versions resolved to their committed, released
  derivatives (primary = lowest binding priority), and -- for a Run drafted
  from a test plan -- the plan's directions (title, what it tests, scene,
  copy). ``reference_policy`` (whether this Run sends the references and how
  many) is the capability decision ``execution_policy.resolve_preview`` made
  for the resolved model, so the preview's Maximum cost, the reservation and
  the Worker's call all follow the same frozen decision.
* **Build** (:func:`build_creative_recipe`, dispatch time): the frozen block
  plus the RunItem's combination identity become one immutable
  :class:`CreativeRecipe`. Recipes frozen before this module existed carry no
  ``creative`` block; they are rebuilt the way the Worker always composed
  them (the Product read live, no references, no must-match) so a resumed
  pre-#167 Run behaves exactly as before.

Tenancy: every read filters on ``ctx.workspace_id``; a reference whose
derivative is not a committed object of this Workspace is excluded at freeze
time (``reference_exclusions``) and refused again at dispatch
(:class:`ReferenceReader`), so another Workspace's blob can never reach a
provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AdTestPlanDirectionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    ProductRecord,
    ReferenceBindingRecord,
    ReferenceObjectRecord,
)
from app.generation.models import (
    AssetQCAttemptRecord,
    AssetQCCheckResultRecord,
    GenerationRecipeSnapshotRecord,
    RunItemRecord,
)
from app.platform.tenancy import TenantContext

CREATIVE_RECIPE_VERSION = "creative-recipe/v1"

#: Key of the frozen block inside a recipe snapshot's ``canonical_json``.
RECIPE_KEY = "creative"

#: At most this many reference images go to a provider (primary first). The
#: providers accept more (GPT-Image edits 16, Gemini 14, Seedream 10); four
#: keeps input cost and latency bounded and is enough for front / side /
#: detail views of one product.
MAX_REFERENCE_IMAGES = 4

#: At most this many QC corrections (Issue #169) go into one recipe: the
#: newest first, so a long failure history cannot crowd out the scene.
MAX_QC_FEEDBACK = 6

#: QC attempt states that count as a failed quality check.
QC_FAILED_STATES = ("failed_blocking", "failed_nonblocking")

#: The template dimension carrying a test-plan direction (``planner.DIRECTION_DIMENSION_ID``).
DIRECTION_DIMENSION_ID = "direction"

#: ``reference_policy.reason`` codes.
REFERENCE_USED = "reference_images_used"
REFERENCE_NONE_BOUND = "no_reference_images_bound"
REFERENCE_UNSUPPORTED = "model_does_not_support_reference_images"
REFERENCE_MODE_IGNORES = "execution_mode_ignores_reference_images"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductBrief:
    name: str
    description: str = ""
    audience: str = ""
    selling_points: tuple[str, ...] = ()
    brand_notes: str = ""
    version_id: str | None = None


@dataclass(frozen=True)
class MustMatchItem:
    """A confirmed appearance requirement (Library contract §6)."""

    id: str
    type: str
    description: str
    severity: str = "blocking"
    viewpoints: tuple[str, ...] = ()
    qc_method: str = "human_review"
    evidence_reference_version_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DimensionValue:
    """One dimension of the combination, with the value's prompt fragments."""

    dimension_key: str
    value: str
    concept_key_id: str | None = None
    positive_fragments: tuple[str, ...] = ()
    negative_fragments: tuple[str, ...] = ()

    @property
    def dimension_id(self) -> str:
        """``lib_…:场景`` -> ``场景`` (the template entry id is identity, not copy)."""
        return self.dimension_key.rsplit(":", 1)[-1] if ":" in self.dimension_key else self.dimension_key


@dataclass(frozen=True)
class DirectionBrief:
    """The test-plan direction a combination belongs to (front-flow §3.2)."""

    direction_key: str
    title: str = ""
    what_it_tests: str = ""
    hypothesis: str = ""
    scene_description: str = ""
    headline: str = ""
    primary_text: str = ""


@dataclass(frozen=True)
class ReferenceImage:
    """A committed, released derivative of a bound ``reference_asset`` version."""

    reference_version_id: str
    reference_entry_id: str
    object_id: str
    committed_key: str
    storage_version_id: str
    sha256: str
    mime_type: str
    byte_size: int
    width: int = 0
    height: int = 0
    purpose: str = ""
    viewpoint: str = ""
    priority: int = 0
    role: str = "secondary"  # "primary" for the first one

    def lineage(self) -> dict:
        """What an AssetVersion records about this reference (no storage keys)."""
        return {
            "reference_version_id": self.reference_version_id,
            "reference_entry_id": self.reference_entry_id,
            "role": self.role,
            "purpose": self.purpose,
            "viewpoint": self.viewpoint,
            "priority": self.priority,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class OutputFormat:
    aspect_ratio: str = "1:1"
    image_size: str | None = None
    resolution: str | None = None
    quality: str | None = None

    def as_format_config(self) -> dict:
        """The ``format`` dict ``app.generation.pricing`` resolves sizes from."""
        config: dict[str, Any] = {"aspect_ratio": self.aspect_ratio}
        if self.image_size:
            config["image_size"] = self.image_size
        if self.resolution:
            config["resolution"] = self.resolution
        if self.quality:
            config["quality"] = self.quality
        return config


@dataclass(frozen=True)
class CreativeRecipe:
    """Everything one RunItem asks a provider to make, provider-neutral.

    ``positive_fragments`` / ``negative_constraints`` keep the order of the
    combination's concepts (the order the prompt has always used);
    ``dimension_values`` keeps the order of the combination's dimensions.
    ``use_reference_images`` is the frozen capability decision: an adapter
    sends ``reference_images[:reference_image_count]`` only when it is true.
    """

    product: ProductBrief
    format: OutputFormat
    dimension_values: tuple[DimensionValue, ...] = ()
    positive_fragments: tuple[str, ...] = ()
    negative_constraints: tuple[str, ...] = ()
    must_match: tuple[MustMatchItem, ...] = ()
    direction: DirectionBrief | None = None
    reference_images: tuple[ReferenceImage, ...] = ()
    use_reference_images: bool = False
    reference_image_count: int = 0
    reference_policy: Mapping[str, Any] = field(default_factory=dict)
    test_plan_id: str | None = None
    version: str = CREATIVE_RECIPE_VERSION
    #: ``False`` for a recipe rebuilt from a pre-#167 snapshot (nothing frozen).
    frozen: bool = True
    #: English corrections from earlier failed quality checks (Issue #169, v9
    #: §3.1 "QC 反馈可以进入下一次配方"): the ones a regeneration draft froze
    #: into its recipe, plus the ones this RunItem's own earlier attempts
    #: failed on (a Recovery retry). Empty for a first attempt, so the prompt
    #: of a recipe without QC history is unchanged.
    qc_feedback: tuple[str, ...] = ()

    @property
    def references_to_send(self) -> tuple[ReferenceImage, ...]:
        if not self.use_reference_images:
            return ()
        return self.reference_images[: self.reference_image_count]

    def without_references(self) -> "CreativeRecipe":
        return replace(self, use_reference_images=False, reference_image_count=0)


# ---------------------------------------------------------------------------
# Freeze (Preview)
# ---------------------------------------------------------------------------


def load_product_brief(session: Session, ctx: TenantContext, product_id: str) -> ProductBrief:
    """The Product as the Library currently publishes it, else the bridged row."""
    brief, _content = _load_product(session, ctx, product_id)
    return brief


def _load_product(
    session: Session, ctx: TenantContext, product_id: str,
) -> tuple[ProductBrief, dict | None]:
    product = session.execute(
        select(ProductRecord).where(
            ProductRecord.id == product_id,
            ProductRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    name = product.name if product is not None else product_id
    brief = ProductBrief(
        name=name,
        description=(product.description if product is not None else "") or "",
        audience=(product.audience if product is not None else "") or "",
        selling_points=tuple(
            str(point) for point in ((product.selling_points if product is not None else None) or [])
        ),
        brand_notes=(product.brand_notes if product is not None else "") or "",
    )
    entry_id = product.product_profile_entry_id if product is not None else None
    if not entry_id:
        return brief, None
    entry = session.execute(
        select(LibraryEntryRecord).where(
            LibraryEntryRecord.id == entry_id,
            LibraryEntryRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if entry is None or not entry.current_version_id:
        return brief, None
    version = session.execute(
        select(LibraryVersionRecord).where(
            LibraryVersionRecord.id == entry.current_version_id,
            LibraryVersionRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if version is None or not isinstance(version.content_json, dict):
        return brief, None
    content = version.content_json
    selling_points = content.get("selling_points")
    return ProductBrief(
        name=str(content.get("name") or name).strip() or name,
        description=str(content.get("description") or brief.description).strip(),
        audience=str(content.get("audience") or brief.audience).strip(),
        selling_points=tuple(
            str(point).strip() for point in (selling_points if isinstance(selling_points, list) else brief.selling_points)
            if str(point).strip()
        ),
        brand_notes=brief.brand_notes,
        version_id=version.id,
    ), content


def confirmed_must_match(items: Any) -> list[dict]:
    """The must-match items a recipe may carry: confirmed ones only.

    Library contract §6 / front-flow §6: an AI proposal is not a requirement
    until the user confirms it. An item counts when it says
    ``confirmed: true``, or when it was authored by a person
    (``proposed_by`` absent or ``user``) and does not say otherwise.
    """
    confirmed: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        stable_id = str(item.get("id") or "").strip()
        if not description or not stable_id:
            continue
        flag = item.get("confirmed")
        if flag is True or (flag is None and item.get("proposed_by", "user") != "ai"):
            confirmed.append({
                "id": stable_id,
                "type": str(item.get("type") or "other"),
                "description": description,
                "severity": str(item.get("severity") or "blocking"),
                "viewpoints": [str(v) for v in item.get("viewpoints") or [] if str(v).strip()],
                "qc_method": str(item.get("qc_method") or "human_review"),
                "evidence_reference_version_ids": [
                    str(v) for v in item.get("evidence_reference_version_ids") or [] if str(v).strip()
                ],
            })
    return confirmed


def committed_key_in_workspace(key: str, workspace_id: str) -> bool:
    return bool(key) and key.startswith(f"committed/{workspace_id}/")


def _frozen_references(
    session: Session, ctx: TenantContext, product_version_id: str | None,
) -> tuple[list[dict], list[dict]]:
    """The product version's bound references, primary first, and the ones left out."""
    if not product_version_id:
        return [], []
    bindings = session.execute(
        select(ReferenceBindingRecord)
        .where(
            ReferenceBindingRecord.workspace_id == ctx.workspace_id,
            ReferenceBindingRecord.owner_version_id == product_version_id,
        )
        .order_by(
            ReferenceBindingRecord.priority,
            ReferenceBindingRecord.position,
            ReferenceBindingRecord.id,
        )
    ).scalars().all()
    images: list[dict] = []
    excluded: list[dict] = []
    seen: set[str] = set()
    for binding in bindings:
        version_id = binding.reference_version_id
        if version_id in seen:
            continue
        seen.add(version_id)
        reason = None
        version = session.execute(
            select(LibraryVersionRecord).where(
                LibraryVersionRecord.id == version_id,
                LibraryVersionRecord.workspace_id == ctx.workspace_id,
            )
        ).scalar_one_or_none()
        record = None
        if version is None or version.entry_type != "reference_asset":
            reason = "reference_version_not_found"
        elif version.state == "withdrawn":
            reason = "reference_version_withdrawn"
        elif version.eligibility == "blocked":
            reason = "reference_version_blocked"
        else:
            object_id = str((version.content_json or {}).get("object_id") or "")
            record = session.execute(
                select(ReferenceObjectRecord).where(
                    ReferenceObjectRecord.id == object_id,
                    ReferenceObjectRecord.workspace_id == ctx.workspace_id,
                )
            ).scalar_one_or_none() if object_id else None
            if record is None:
                reason = "reference_object_not_found"
            elif record.state != "released" or not record.derivative_object_key:
                reason = "reference_object_not_released"
            elif not committed_key_in_workspace(record.derivative_object_key, ctx.workspace_id):
                # A dev sync-upload derivative on local disk (or anything not a
                # committed object of this Workspace) never goes to a provider.
                reason = "reference_derivative_not_committed"
        if reason is not None:
            excluded.append({"reference_version_id": version_id, "reason": reason})
            continue
        images.append({
            "reference_version_id": version_id,
            "reference_entry_id": version.entry_id,
            "object_id": record.id,
            "committed_key": record.derivative_object_key,
            "storage_version_id": record.derivative_storage_version_id or "",
            "sha256": record.derivative_sha256,
            "mime_type": record.derivative_mime or "image/jpeg",
            "byte_size": int(record.derivative_byte_size or 0),
            "width": int(record.derivative_width or 0),
            "height": int(record.derivative_height or 0),
            "purpose": binding.purpose,
            "viewpoint": binding.viewpoint,
            "priority": int(binding.priority or 0),
            "role": "primary" if not images else "secondary",
        })
    return images, excluded


def _frozen_directions(
    session: Session, ctx: TenantContext, test_plan_id: str | None,
) -> dict[str, dict]:
    if not test_plan_id:
        return {}
    rows = session.execute(
        select(AdTestPlanDirectionRecord)
        .where(
            AdTestPlanDirectionRecord.workspace_id == ctx.workspace_id,
            AdTestPlanDirectionRecord.plan_id == test_plan_id,
            AdTestPlanDirectionRecord.removed.is_(False),
        )
        .order_by(AdTestPlanDirectionRecord.ordinal)
    ).scalars().all()
    directions: dict[str, dict] = {}
    for row in rows:
        copy = row.copy_json if isinstance(row.copy_json, dict) else {}
        directions[row.direction_key] = {
            "direction_key": row.direction_key,
            "title": row.title or "",
            "what_it_tests": row.what_it_tests or "",
            "hypothesis": row.hypothesis or "",
            "scene_description": row.scene_description or "",
            "headline": str(copy.get("headline") or ""),
            "primary_text": str(copy.get("primary_text") or ""),
        }
    return directions


def plan_id_from_strategy(combination_strategy: Any) -> str | None:
    """The plan a Draft was materialised from (``app.plans.runs`` records it)."""
    if not isinstance(combination_strategy, dict):
        return None
    plan = combination_strategy.get("test_plan")
    if not isinstance(plan, dict):
        return None
    plan_id = str(plan.get("test_plan_id") or "").strip()
    return plan_id or None


def freeze_creative_inputs(
    session: Session,
    ctx: TenantContext,
    *,
    product_id: str,
    combination_strategy: Any = None,
) -> dict:
    """The ``creative`` block a Preview freezes into the recipe (sans policy)."""
    brief, content = _load_product(session, ctx, product_id)
    images, excluded = _frozen_references(session, ctx, brief.version_id)
    test_plan_id = plan_id_from_strategy(combination_strategy)
    return {
        "version": CREATIVE_RECIPE_VERSION,
        "product": {
            "name": brief.name,
            "description": brief.description,
            "audience": brief.audience,
            "selling_points": list(brief.selling_points),
            "brand_notes": brief.brand_notes,
            "version_id": brief.version_id,
        },
        "must_match": confirmed_must_match((content or {}).get("must_match")),
        "reference_images": images,
        "reference_exclusions": excluded,
        "test_plan_id": test_plan_id,
        "directions": _frozen_directions(session, ctx, test_plan_id),
    }


def product_reference_count(session: Session, ctx: TenantContext, product_id: str) -> int:
    """How many usable references the product currently has (for estimates)."""
    brief, _content = _load_product(session, ctx, product_id)
    images, _excluded = _frozen_references(session, ctx, brief.version_id)
    return len(images)


def decide_reference_use(
    adapter_info: Mapping[str, Any] | None,
    model: str,
    available: int,
    *,
    execution_mode: str,
) -> dict:
    """Whether a Run sends its references, as a frozen capability decision.

    Capability-driven: the resolved adapter's model must advertise
    ``supports_reference_images``; only a real provider call (``byok``)
    sends them (Mock and sandbox render nothing from them). The count is
    capped by the model's ``max_reference_images`` and
    :data:`MAX_REFERENCE_IMAGES`.
    """
    model_info = ((adapter_info or {}).get("models") or {}).get(model) or {}
    supported = bool(model_info.get("supports_reference_images"))
    if available <= 0:
        return {
            "use": False, "count": 0, "available": 0, "supported": supported,
            "reason": REFERENCE_NONE_BOUND,
            "detail": "The product has no bound reference images; text-only generation.",
        }
    if execution_mode != "byok":
        return {
            "use": False, "count": 0, "available": available, "supported": supported,
            "reason": REFERENCE_MODE_IGNORES,
            "detail": f"{execution_mode} mode does not send reference images to a provider.",
        }
    if not supported:
        return {
            "use": False, "count": 0, "available": available, "supported": False,
            "reason": REFERENCE_UNSUPPORTED,
            "detail": f"{model} cannot take reference images; text-only generation.",
        }
    limit = model_info.get("max_reference_images")
    cap = MAX_REFERENCE_IMAGES
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        cap = min(cap, limit)
    count = min(available, cap)
    return {
        "use": True, "count": count, "available": available, "supported": True,
        "reason": REFERENCE_USED,
        "detail": f"{count} product reference image(s) sent with each call; the product's appearance is kept.",
    }


def reference_count_of(recipe: Mapping[str, Any] | None) -> int:
    """References a frozen recipe sends per call (0 for pre-#167 recipes)."""
    creative = (recipe or {}).get(RECIPE_KEY)
    if not isinstance(creative, dict):
        return 0
    policy = creative.get("reference_policy")
    if not isinstance(policy, dict) or not policy.get("use"):
        return 0
    count = policy.get("count")
    return int(count) if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


# ---------------------------------------------------------------------------
# Build (dispatch)
# ---------------------------------------------------------------------------


def _clean(fragment: object) -> str:
    return str(fragment).strip().rstrip(".") if fragment is not None else ""


def _combination(identity: Mapping[str, Any] | None) -> tuple[
    tuple[DimensionValue, ...], tuple[str, ...], tuple[str, ...]
]:
    identity = identity or {}
    dimensions: dict[str, str] = {
        str(key): str(value) for key, value in (identity.get("dimensions") or {}).items()
    }
    positive: list[str] = []
    negative: list[str] = []
    per_dimension: dict[str, dict] = {}
    for concept in identity.get("concepts") or []:
        if not isinstance(concept, dict):
            continue
        value = concept.get("value") or {}
        if not isinstance(value, dict):
            continue
        pos = [_clean(f) for f in value.get("positive_fragments") or [] if _clean(f)]
        neg = [_clean(f) for f in value.get("negative_fragments") or [] if _clean(f)]
        positive.extend(pos)
        negative.extend(neg)
        key = str(concept.get("dimension_key") or "")
        slot = per_dimension.setdefault(key, {"concept_key_id": None, "pos": [], "neg": []})
        slot["concept_key_id"] = slot["concept_key_id"] or concept.get("concept_key_id")
        slot["pos"].extend(pos)
        slot["neg"].extend(neg)
    values = tuple(
        DimensionValue(
            dimension_key=key,
            value=value,
            concept_key_id=(per_dimension.get(key) or {}).get("concept_key_id"),
            positive_fragments=tuple((per_dimension.get(key) or {}).get("pos") or ()),
            negative_fragments=tuple((per_dimension.get(key) or {}).get("neg") or ()),
        )
        for key, value in dimensions.items()
    )
    return values, tuple(positive), tuple(negative)


def recipe_from_parts(
    *,
    creative: Mapping[str, Any] | None,
    identity: Mapping[str, Any] | None,
    format_config: Mapping[str, Any] | None,
    aspect_ratio: str | None,
    legacy_product: ProductBrief | None = None,
) -> CreativeRecipe:
    """Pure assembly of a recipe from its frozen pieces (no database)."""
    config = dict(format_config or {})
    output = OutputFormat(
        aspect_ratio=str(aspect_ratio or config.get("aspect_ratio") or "1:1"),
        image_size=(str(config["image_size"]) if config.get("image_size") else None),
        resolution=(str(config["resolution"]) if config.get("resolution") else None),
        quality=(str(config["quality"]) if config.get("quality") else None),
    )
    values, positive, negative = _combination(identity)
    if not isinstance(creative, Mapping):
        return CreativeRecipe(
            product=legacy_product or ProductBrief(name=""),
            format=output,
            dimension_values=values,
            positive_fragments=positive,
            negative_constraints=negative,
            frozen=False,
        )
    product = dict(creative.get("product") or {})
    brief = ProductBrief(
        name=str(product.get("name") or ""),
        description=str(product.get("description") or ""),
        audience=str(product.get("audience") or ""),
        selling_points=tuple(str(p) for p in product.get("selling_points") or []),
        brand_notes=str(product.get("brand_notes") or ""),
        version_id=product.get("version_id"),
    )
    must_match = tuple(
        MustMatchItem(
            id=str(item.get("id") or ""),
            type=str(item.get("type") or "other"),
            description=str(item.get("description") or ""),
            severity=str(item.get("severity") or "blocking"),
            viewpoints=tuple(str(v) for v in item.get("viewpoints") or []),
            qc_method=str(item.get("qc_method") or "human_review"),
            evidence_reference_version_ids=tuple(
                str(v) for v in item.get("evidence_reference_version_ids") or []
            ),
        )
        for item in creative.get("must_match") or []
        if isinstance(item, Mapping)
    )
    references = tuple(
        ReferenceImage(
            reference_version_id=str(ref.get("reference_version_id") or ""),
            reference_entry_id=str(ref.get("reference_entry_id") or ""),
            object_id=str(ref.get("object_id") or ""),
            committed_key=str(ref.get("committed_key") or ""),
            storage_version_id=str(ref.get("storage_version_id") or ""),
            sha256=str(ref.get("sha256") or ""),
            mime_type=str(ref.get("mime_type") or "image/jpeg"),
            byte_size=int(ref.get("byte_size") or 0),
            width=int(ref.get("width") or 0),
            height=int(ref.get("height") or 0),
            purpose=str(ref.get("purpose") or ""),
            viewpoint=str(ref.get("viewpoint") or ""),
            priority=int(ref.get("priority") or 0),
            role=str(ref.get("role") or "secondary"),
        )
        for ref in creative.get("reference_images") or []
        if isinstance(ref, Mapping)
    )
    policy = dict(creative.get("reference_policy") or {})
    count = reference_count_of({RECIPE_KEY: creative})
    direction = None
    directions = creative.get("directions") or {}
    if isinstance(directions, Mapping) and directions:
        for value in values:
            if value.dimension_id == DIRECTION_DIMENSION_ID and value.value in directions:
                raw = directions[value.value] or {}
                direction = DirectionBrief(
                    direction_key=value.value,
                    title=str(raw.get("title") or ""),
                    what_it_tests=str(raw.get("what_it_tests") or ""),
                    hypothesis=str(raw.get("hypothesis") or ""),
                    scene_description=str(raw.get("scene_description") or ""),
                    headline=str(raw.get("headline") or ""),
                    primary_text=str(raw.get("primary_text") or ""),
                )
                break
    return CreativeRecipe(
        product=brief,
        format=output,
        dimension_values=values,
        positive_fragments=positive,
        negative_constraints=negative,
        must_match=must_match,
        direction=direction,
        reference_images=references,
        use_reference_images=count > 0 and bool(references),
        reference_image_count=min(count, len(references)),
        reference_policy=policy,
        test_plan_id=creative.get("test_plan_id"),
        version=str(creative.get("version") or CREATIVE_RECIPE_VERSION),
        qc_feedback=merge_qc_feedback(feedback_hints(creative.get("qc_feedback"))),
    )


def feedback_hints(entries: Any) -> list[str]:
    """The English corrections of frozen ``qc_feedback`` entries (dicts or strings)."""
    hints: list[str] = []
    for entry in entries if isinstance(entries, (list, tuple)) else []:
        if isinstance(entry, Mapping):
            hint = _clean(entry.get("hint"))
        else:
            hint = _clean(entry)
        if hint:
            hints.append(hint)
    return hints


def merge_qc_feedback(*groups: Iterable[str]) -> tuple[str, ...]:
    """Dedupe (case-insensitively, first wins) and cap at :data:`MAX_QC_FEEDBACK`."""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for hint in group:
            key = hint.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(hint.strip())
    return tuple(merged[:MAX_QC_FEEDBACK])


def item_qc_feedback(session: Session, ctx: TenantContext, item_id: str) -> list[str]:
    """Corrections this RunItem's earlier attempts failed QC on, newest first.

    A Recovery retry of an item whose image failed the quality check is the
    same combination asked again; its failed checks (append-only evidence)
    carry the English ``fix_hint`` the VLM wrote, and they enter the retry's
    recipe instead of repeating the same mistake.
    """
    attempts = session.execute(
        select(AssetQCAttemptRecord)
        .where(
            AssetQCAttemptRecord.workspace_id == ctx.workspace_id,
            AssetQCAttemptRecord.run_item_id == item_id,
            AssetQCAttemptRecord.state.in_(QC_FAILED_STATES),
        )
        .order_by(AssetQCAttemptRecord.created_at.desc(), AssetQCAttemptRecord.id.desc())
    ).scalars().all()
    hints: list[str] = []
    for attempt in attempts:
        checks = session.execute(
            select(AssetQCCheckResultRecord)
            .where(
                AssetQCCheckResultRecord.workspace_id == ctx.workspace_id,
                AssetQCCheckResultRecord.qc_attempt_id == attempt.id,
                AssetQCCheckResultRecord.outcome == "fail",
            )
            .order_by(AssetQCCheckResultRecord.created_at, AssetQCCheckResultRecord.id)
        ).scalars().all()
        for check in checks:
            hint = _clean((check.evidence_json or {}).get("fix_hint"))
            if hint:
                hints.append(hint)
    return hints


def fragment_dimension_keys(recipe: CreativeRecipe) -> set[str]:
    """Dimensions whose value is spelled out by fragments rather than by name."""
    return {
        value.dimension_key for value in recipe.dimension_values
        if value.positive_fragments or value.negative_fragments
    }


def build_creative_recipe(
    session: Session,
    ctx: TenantContext,
    item: RunItemRecord,
    run: GenerationRunRecord,
) -> CreativeRecipe:
    """The recipe one RunItem dispatches with, from the Run's frozen snapshot."""
    snapshot = None
    if run.recipe_snapshot_id:
        snapshot = session.execute(
            select(GenerationRecipeSnapshotRecord).where(
                GenerationRecipeSnapshotRecord.id == run.recipe_snapshot_id,
                GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id,
            )
        ).scalar_one_or_none()
    canonical = dict((snapshot.canonical_json or {}) if snapshot is not None else {})
    creative = canonical.get(RECIPE_KEY)
    legacy = None
    if not isinstance(creative, dict):
        legacy = load_product_brief(session, ctx, run.product_id)
    recipe = recipe_from_parts(
        creative=creative if isinstance(creative, dict) else None,
        identity=item.combination_identity_json,
        format_config=canonical.get("format"),
        aspect_ratio=run.aspect_ratio,
        legacy_product=legacy,
    )
    history = item_qc_feedback(session, ctx, item.id) if item.id else []
    if history:
        recipe = replace(recipe, qc_feedback=merge_qc_feedback(history, recipe.qc_feedback))
    return recipe


def recipe_lineage(recipe: CreativeRecipe, sent: Iterable[ReferenceImage]) -> dict:
    """The ``reference_sources_json`` an AssetVersion records for its call."""
    sent = tuple(sent)
    policy = dict(recipe.reference_policy or {})
    return {
        "recipe_version": recipe.version,
        "decision": REFERENCE_USED if sent else (
            str(policy.get("reason") or REFERENCE_NONE_BOUND) if recipe.frozen else "recipe_predates_references"
        ),
        "used": bool(sent),
        "references": [reference.lineage() for reference in sent],
        "product_version_id": recipe.product.version_id,
        "must_match_ids": [item.id for item in recipe.must_match],
    }
