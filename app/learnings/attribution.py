"""Map results to the learning unit: AssetVersion -> RunItem -> plan combination.

Chain (Issue #166 §2):

1. **AssetVersion -> producing RunItem** (``producing_run_item_id``, else its
   ``produced`` / ``reused`` binding). A regenerated item follows
   ``source_run_item_id`` back to the item that ran in the plan (<= 5 hops).
2. **RunItem -> test plan stage**: the item's ``generation_run_id`` is a
   ``test_plan_runs`` Run, whose ``selection_json.fingerprints`` maps the
   item's ``combination_fingerprint`` to the plan-local combination
   signature (``direction=dir-1|people=people-2|...``, #163).
3. **Signature -> direction + factor values**; labels come from the plan's
   recipe catalog and direction titles.

Learning identity stays inside one plan: factor value ids are plan-local and
the glossary forbids pooling by name ("维度概念键 ... 名称相同不构成身份"),
so every plan is its own round. Assets that cannot be placed are reported,
never guessed: external uploads are ``external_untagged`` until the user
confirms their tags (#168), then placed by the confirmation's plan,
direction and factor values (``external_unplaced`` when the confirmed tags
belong to no plan); other generated assets are ``not_from_test_plan``.

Evidence partitions: a BYOK asset is ``production``; ``sandbox`` stays
``sandbox``; Mock / controlled-Mock / legacy assets are ``mock``. An
external upload with no execution mode is customer material
(``production``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from app.adapters.persistence.models import (
    AdTestPlanDirectionRecord,
    AdTestPlanRecord,
    AdTestPlanRunRecord,
    AssetRecord,
    AssetVersionRecord,
    ExperimentAdBindingRecord,
    ExperimentEntryRecord,
    ExperimentVersionCandidateRecord,
    MetaAccountRecord,
    MetaAdBindingRecord,
)
from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
from app.generation.planner import DIRECTION_DIMENSION_ID
from app.platform.tenancy import TenantContext

from . import codes

MAX_SOURCE_HOPS = 5

#: An asset that no test plan describes, in the downloadable template's language.
NO_PLAN_LABELS = {
    "external_asset": {"zh-CN": "外部素材", "en": "External asset"},
    "other_asset": {"zh-CN": "其他素材", "en": "Other asset"},
}

UNATTRIBUTABLE_COPY = {
    "external_untagged": "外部素材还没有打标签，打标后才能按元素归因",
    "external_unplaced": "外部素材确认的标签不属于任何测试方案的因素，暂时无法按元素归因",
    "not_from_test_plan": "这张素材不是由测试方案生成的，暂时无法按元素归因",
}

_MODE_OF_EXECUTION = {"byok": "production", "sandbox": "sandbox", "mock": "mock", "controlled_mock": "mock"}


def evidence_mode_of_asset(origin_kind: str | None, execution_mode: str | None) -> str:
    if execution_mode:
        return _MODE_OF_EXECUTION.get(execution_mode, "mock")
    return "production" if origin_kind == "upload" else "mock"


def combine_modes(*modes: str) -> str:
    """The weakest partition wins: any mock -> mock, else any sandbox -> sandbox."""
    if "mock" in modes:
        return "mock"
    if "sandbox" in modes:
        return "sandbox"
    return "production"


def parse_signature(signature: str) -> tuple[str | None, dict[str, str]]:
    direction = None
    cells: dict[str, str] = {}
    for part in (signature or "").split("|"):
        key, _, value = part.partition("=")
        if not key or not value:
            continue
        if key == DIRECTION_DIMENSION_ID:
            direction = value
        else:
            cells[key] = value
    return direction, cells


@dataclass
class AssetInfo:
    asset_version_id: str
    asset_id: str
    code: str
    origin_kind: str
    evidence_mode: str
    plan_id: str | None = None
    direction_key: str | None = None
    cells: dict[str, str] = field(default_factory=dict)
    unattributable: str | None = None
    created_at: str = ""


@dataclass
class PlanCatalog:
    plan_id: str
    round_number: int
    goal: str
    created_at: str
    factors: list[dict]
    directions: list[AdTestPlanDirectionRecord]

    def factor(self, key: str) -> dict | None:
        return next((item for item in self.factors if item.get("key") == key), None)

    def value_label(self, key: str, value_id: str) -> str:
        factor = self.factor(key) or {}
        return next((v["label"] for v in factor.get("values") or [] if v.get("id") == value_id), value_id)

    def direction_title(self, key: str | None) -> str:
        return next((d.title for d in self.directions if d.direction_key == key), key or "")

    def describe(self, direction_key: str | None, cells: dict[str, str]) -> str:
        parts = [self.direction_title(direction_key)] if direction_key else []
        for factor in self.factors:
            if factor.get("key") in cells:
                parts.append(self.value_label(factor["key"], cells[factor["key"]]))
        return " · ".join(part for part in parts if part)


class ProductAttribution:
    """Every AssetVersion of one product with its code, partition and combination."""

    def __init__(self, session, ctx: TenantContext, product_id: str):
        self.session = session
        self.ctx = ctx
        self.product_id = product_id
        self.assets: dict[str, AssetInfo] = {}
        self.by_code: dict[str, list[str]] = {}
        self.plans: dict[str, PlanCatalog] = {}
        self._other_codes: set[str] | None = None
        self._load()

    # -- loading --------------------------------------------------------

    def _load(self) -> None:
        session, ctx = self.session, self.ctx
        plans = list(session.scalars(
            select(AdTestPlanRecord)
            .where(AdTestPlanRecord.workspace_id == ctx.workspace_id, AdTestPlanRecord.product_id == self.product_id)
            .order_by(AdTestPlanRecord.created_at, AdTestPlanRecord.id)
        ))
        for number, plan in enumerate(plans, start=1):
            directions = list(session.scalars(
                select(AdTestPlanDirectionRecord)
                .where(
                    AdTestPlanDirectionRecord.workspace_id == ctx.workspace_id,
                    AdTestPlanDirectionRecord.plan_id == plan.id,
                )
                .order_by(AdTestPlanDirectionRecord.ordinal)
            ))
            self.plans[plan.id] = PlanCatalog(
                plan_id=plan.id, round_number=number, goal=plan.goal,
                created_at=plan.created_at.isoformat() if plan.created_at else "",
                factors=list((plan.recipe_json or {}).get("factors") or []),
                directions=directions,
            )
        stage_runs: dict[str, tuple[str, dict]] = {}
        if plans:
            for row in session.scalars(
                select(AdTestPlanRunRecord).where(
                    AdTestPlanRunRecord.workspace_id == ctx.workspace_id,
                    AdTestPlanRunRecord.plan_id.in_([plan.id for plan in plans]),
                    AdTestPlanRunRecord.generation_run_id.is_not(None),
                )
            ):
                stage_runs[row.generation_run_id] = (
                    row.plan_id, dict((row.selection_json or {}).get("fingerprints") or {}),
                )

        rows = session.execute(
            select(AssetVersionRecord, AssetRecord.product_id)
            .join(
                AssetRecord,
                (AssetRecord.id == AssetVersionRecord.asset_id)
                & (AssetRecord.workspace_id == AssetVersionRecord.workspace_id),
            )
            .where(AssetVersionRecord.workspace_id == ctx.workspace_id, AssetRecord.product_id == self.product_id)
            .order_by(AssetVersionRecord.created_at, AssetVersionRecord.id)
        ).all()
        version_ids = [version.id for version, _ in rows]
        bound: dict[str, str] = {}
        if version_ids:
            for asset_version_id, run_item_id, kind in session.execute(
                select(
                    RunItemAssetVersionBindingRecord.asset_version_id,
                    RunItemAssetVersionBindingRecord.run_item_id,
                    RunItemAssetVersionBindingRecord.kind,
                ).where(
                    RunItemAssetVersionBindingRecord.workspace_id == ctx.workspace_id,
                    RunItemAssetVersionBindingRecord.asset_version_id.in_(version_ids),
                ).order_by(RunItemAssetVersionBindingRecord.created_at, RunItemAssetVersionBindingRecord.id)
            ):
                # The producing item wins over a later reuse binding.
                if kind == "produced" or asset_version_id not in bound:
                    bound[asset_version_id] = run_item_id

        from app.external_creatives.identity import latest_confirmations

        confirmations = latest_confirmations(
            session, ctx, [version.id for version, _ in rows if version.origin_kind == "upload"],
        )
        for version, _product in rows:
            info = AssetInfo(
                asset_version_id=version.id,
                asset_id=version.asset_id,
                code=codes.asset_code(ctx.workspace_id, version.id),
                origin_kind=version.origin_kind or "",
                evidence_mode=evidence_mode_of_asset(version.origin_kind, version.execution_mode),
                created_at=version.created_at.isoformat() if version.created_at else "",
            )
            run_item_id = version.producing_run_item_id or bound.get(version.id)
            located = self._locate(run_item_id, stage_runs) if run_item_id else None
            confirmation = confirmations.get(version.id) if located is None else None
            if located is not None:
                info.plan_id, signature = located
                info.direction_key, info.cells = parse_signature(signature)
            elif confirmation is not None and confirmation.plan_id in self.plans and (
                confirmation.cells_json or confirmation.direction_key
            ):
                # An external creative's confirmed tags (Issue #168) place it
                # in a round exactly like a generated combination.
                info.plan_id = confirmation.plan_id
                info.direction_key = confirmation.direction_key
                info.cells = dict(confirmation.cells_json or {})
            elif version.origin_kind == "upload":
                info.unattributable = "external_unplaced" if confirmation is not None else "external_untagged"
            else:
                info.unattributable = "not_from_test_plan"
            self.assets[version.id] = info
            self.by_code.setdefault(info.code, []).append(version.id)

    def _locate(self, run_item_id: str, stage_runs: dict[str, tuple[str, dict]]) -> tuple[str, str] | None:
        seen: set[str] = set()
        current = run_item_id
        for _ in range(MAX_SOURCE_HOPS + 1):
            if not current or current in seen:
                return None
            seen.add(current)
            item = self.session.scalars(
                select(RunItemRecord).where(
                    RunItemRecord.workspace_id == self.ctx.workspace_id, RunItemRecord.id == current,
                )
            ).one_or_none()
            if item is None:
                return None
            stage = stage_runs.get(item.generation_run_id)
            if stage is not None:
                plan_id, fingerprints = stage
                signature = fingerprints.get(item.combination_fingerprint)
                return (plan_id, signature) if signature else None
            current = item.source_run_item_id
        return None

    # -- lookups --------------------------------------------------------

    def describe(self, info: AssetInfo, language: str = "zh-CN") -> str:
        """Plan prose as written; for an asset no plan describes, a label in ``language``.

        Import rows keep the Chinese label they always stored; the browser
        words them from :meth:`description_code` instead.
        """
        if info.plan_id and info.plan_id in self.plans:
            return self.plans[info.plan_id].describe(info.direction_key, info.cells)
        return NO_PLAN_LABELS[self.description_code(info)][language]

    def description_code(self, info: AssetInfo) -> str | None:
        """What the browser words when there is no plan to describe the asset (#255)."""
        if info.plan_id and info.plan_id in self.plans:
            return None
        return "external_asset" if info.origin_kind == "upload" else "other_asset"

    def resolve_code(self, code: str) -> tuple[str | None, str | None]:
        """(asset_version_id, error code)."""
        matches = self.by_code.get(code) or []
        if len(matches) == 1:
            return matches[0], None
        return None, "ambiguous_code" if matches else "unknown_code"

    def resolve_ad(self, ad_id: str) -> tuple[str | None, str | None]:
        """A Meta ``ad_id`` bound to an asset through an experiment or a launch."""
        session, ctx = self.session, self.ctx
        candidates: set[str] = set()
        for entry_id, candidate_id in session.execute(
            select(ExperimentAdBindingRecord.experiment_entry_id, ExperimentAdBindingRecord.experiment_version_candidate_id)
            .where(ExperimentAdBindingRecord.workspace_id == ctx.workspace_id, ExperimentAdBindingRecord.ad_id == ad_id)
        ):
            if candidate_id:
                found = session.scalars(
                    select(ExperimentVersionCandidateRecord.asset_version_id).where(
                        ExperimentVersionCandidateRecord.workspace_id == ctx.workspace_id,
                        ExperimentVersionCandidateRecord.id == candidate_id,
                    )
                ).one_or_none()
                if found:
                    candidates.add(found)
            if entry_id:
                found = session.scalars(
                    select(ExperimentEntryRecord.asset_version_id).where(
                        ExperimentEntryRecord.workspace_id == ctx.workspace_id,
                        ExperimentEntryRecord.id == entry_id,
                        ExperimentEntryRecord.removed_at.is_(None),
                    )
                ).one_or_none()
                if found:
                    candidates.add(found)
        for (asset_version_id,) in session.execute(
            select(MetaAdBindingRecord.asset_version_id)
            .join(MetaAccountRecord, MetaAccountRecord.id == MetaAdBindingRecord.account_id)
            .where(MetaAccountRecord.workspace_id == ctx.workspace_id, MetaAdBindingRecord.ad_id == ad_id)
        ):
            candidates.add(asset_version_id)
        if not candidates:
            return None, "unknown_ad"
        if len(candidates) > 1:
            return None, "identifier_conflict"
        asset_version_id = candidates.pop()
        if asset_version_id not in self.assets:
            return None, "other_product"
        return asset_version_id, None

    def code_belongs_elsewhere(self, code: str) -> bool:
        """Whether a code that is not this product's names another product's asset."""
        if self._other_codes is None:
            ctx = self.ctx
            self._other_codes = {
                codes.asset_code(ctx.workspace_id, version_id)
                for version_id in self.session.scalars(
                    select(AssetVersionRecord.id).where(AssetVersionRecord.workspace_id == ctx.workspace_id)
                )
                if version_id not in self.assets
            }
        return code in self._other_codes
