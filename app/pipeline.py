"""组合展开 / 覆盖采样 / 批量生成任务管理。"""
from __future__ import annotations

import hashlib
import itertools
import json
from decimal import Decimal
from typing import Optional

from . import config, store
from .angle_engine import compose_prompt
from .providers.router import get_provider
from .schemas import AngleSchema, Asset, Combo, GenRequest, Plan, PlanRequest, Product, Run
from .platform.tenancy import TenantContext


# ---------- 组合展开 ----------
def build_combos(angles: AngleSchema, req: PlanRequest) -> tuple[list[Combo], int]:
    """按勾选的取值做笛卡尔积；coverage 模式下贪心采样到 cap。
    返回 (组合列表, 全组合数)。"""
    dims = [d for d in angles.dimensions if req.selected.get(d.id)]
    if not dims:
        return [], 0

    axes: list[list[tuple[str, str, str, str]]] = []  # (dim_id, dim_label, val_id, val_label)
    for d in dims:
        chosen = [v for v in d.values if v.id in set(req.selected[d.id])]
        axes.append([(d.id, d.label, v.id, v.label) for v in chosen])

    full = list(itertools.product(*axes))
    total_full = len(full)

    picked = full
    if req.mode == "coverage" and req.cap and req.cap < total_full:
        picked = _coverage_sample(full, req.cap)

    combos = []
    for i, cells in enumerate(picked, 1):
        code = "-".join(f"{chr(65 + ai)}{axis.index(cells[ai]) + 1}"
                        for ai, axis in enumerate(axes))
        combos.append(Combo(
            id=f"C{i:02d}",
            code=code,
            values={dim_id: val_id for dim_id, _, val_id, _ in cells},
            tags={dim_label: val_label for _, dim_label, _, val_label in cells},
        ))
    return combos, total_full


def _coverage_sample(full: list[tuple], cap: int) -> list[tuple]:
    """贪心采样：优先覆盖所有"单值"，其次最大化"取值两两配对"的覆盖，直到 cap。
    这是正交实验的轻量近似——保证每个维度取值都出现、且组合彼此差异尽量大。"""
    def singles(c):
        return {(cell[0], cell[2]) for cell in c}

    def pairs(c):
        ids = sorted((cell[0], cell[2]) for cell in c)
        return {(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]}

    covered_singles: set = set()
    covered_pairs: set = set()
    remaining = list(full)
    picked: list[tuple] = []

    while remaining and len(picked) < cap:
        best, best_score, best_idx = None, (-1, -1), -1
        for idx, c in enumerate(remaining):
            score = (len(singles(c) - covered_singles), len(pairs(c) - covered_pairs))
            if score > best_score:
                best, best_score, best_idx = c, score, idx
        picked.append(best)
        covered_singles |= singles(best)
        covered_pairs |= pairs(best)
        remaining.pop(best_idx)
    return picked


# ---------- 生成任务 ----------
class RunManager:
    """Compatibility interface backed by persistent runs and jobs."""

    def __init__(self) -> None:
        # Kept only so older callers importing manager continue to work.
        # No business state is held in memory.
        self.runs: dict[str, Run] = {}

    def get(self, run_id: str) -> Optional[Run]:
        del run_id
        return None

    def cancel(self, context: TenantContext, run_id: str) -> bool:
        return store.cancel_run(context, run_id)

    def start(
        self,
        context: TenantContext,
        product: Product,
        angles: AngleSchema,
        plan: Plan,
        req: GenRequest,
    ) -> Run:
        provider = get_provider(req.provider)
        model = req.model or provider.default_model

        combos = plan.combos
        if req.combo_ids:
            wanted = set(req.combo_ids)
            combos = [c for c in combos if c.id in wanted]
        if not combos:
            raise ValueError("没有可生成的组合")

        use_ref = req.use_ref and bool(product.ref_images) and provider.supports_ref
        variant_count = max(1, min(req.variants, 4))
        run = Run(
            product_id=product.id, plan_id=plan.id, provider=provider.name, model=model,
            aspect_ratio=req.aspect_ratio, image_size=req.image_size,
            variants=variant_count, use_ref=use_ref,
            status="queued", total=len(combos) * variant_count,
            currency=config.DEFAULT_CURRENCY,
        )

        # 预建 asset 记录（pending），前端立即可见完整清单
        for combo in combos:
            prompt = compose_prompt(product, combo, angles, req.aspect_ratio, use_ref)
            for v in range(1, run.variants + 1):
                fingerprint = _generation_fingerprint(
                    product=product,
                    combo=combo,
                    prompt=prompt,
                    provider=provider.name,
                    model=model,
                    aspect_ratio=req.aspect_ratio,
                    image_size=req.image_size,
                    use_ref=use_ref,
                    variant=v,
                )
                asset = Asset(
                    id=f"{run.id}_{combo.id}_v{v}",
                    run_id=run.id, combo_id=combo.id, combo_code=combo.code,
                    tags=combo.tags, filename=f"{combo.id}_{provider.name}_v{v}.png",
                    provider=provider.name, model=model, prompt=prompt, fingerprint=fingerprint,
                )
                if not req.force:
                    reusable = store.find_reusable_asset(context, fingerprint)
                    if reusable and store.materialize_reuse(
                        context, product.id, run.id, asset.filename, reusable
                    ):
                        asset.status = "ok"
                        asset.reused_from_asset_id = reusable.id
                        run.done += 1
                run.assets.append(asset)

        pending = run.total - run.done
        run.est_cost = (Decimal(pending) * config.COST_PER_IMAGE.get(provider.name, Decimal("0.05"))).quantize(
            Decimal("0.01")
        )
        if pending == 0:
            run.status = "done"
        store.create_run(context, run)
        return run


def _generation_fingerprint(
    *,
    product: Product,
    combo: Combo,
    prompt: str,
    provider: str,
    model: str,
    aspect_ratio: str,
    image_size: str,
    use_ref: bool,
    variant: int,
) -> str:
    ref_hashes: list[str] = []
    if use_ref:
        for name in product.ref_images:
            path = store.refs_dir(product.id) / name
            if path.is_file():
                ref_hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    payload = {
        "product": product.model_dump(mode="json", exclude={"created_at"}),
        "combo_values": sorted(combo.values.items()),
        "prompt": prompt,
        "provider": provider,
        "model": model,
        "aspect_ratio": aspect_ratio,
        "image_size": image_size,
        "use_ref": use_ref,
        "ref_hashes": ref_hashes,
        # Different requested variants are distinct samples, but the same run
        # retried with identical variant position remains idempotent.
        "variant": variant,
        "fingerprint_version": 1,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


manager = RunManager()
