"""One-time importer from the v1 JSON layout into the SQL foundation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ... import config, store
from ...schemas import AngleSchema, Combo, Plan, Product, Run
from .database import tenant_session
from .models import ImportBatchRecord, WorkspaceRecord, new_id, utcnow
from ...platform.audit import append_event
from ...platform.tenancy import TenantContext


@dataclass
class ImportReport:
    source_root: str
    source_hash: str
    products: int = 0
    angle_versions: int = 0
    plans: int = 0
    runs: int = 0
    assets: int = 0
    asset_versions: int = 0
    quality_reviews: int = 0
    verdict_decisions: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_root": self.source_root,
            "source_hash": self.source_hash,
            "counts": {
                "products": self.products,
                "angle_versions": self.angle_versions,
                "plans": self.plans,
                "runs": self.runs,
                "assets": self.assets,
                "asset_versions": self.asset_versions,
                "quality_reviews": self.quality_reviews,
                "verdict_decisions": self.verdict_decisions,
                "skipped": self.skipped,
            },
            "warnings": self.warnings,
            "errors": self.errors,
            "ok": not self.errors,
        }


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象: {path}")
    return value


def source_hash(source_root: Path) -> str:
    digest = hashlib.sha256()
    products_root = source_root / "products"
    if not products_root.exists():
        digest.update(b"empty-products-root")
        return digest.hexdigest()
    for path in sorted(p for p in products_root.rglob("*") if p.is_file()):
        relative = path.relative_to(source_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _ensure_import_batch(
    context: TenantContext, report: ImportReport
) -> tuple[str, dict | None]:
    with tenant_session(context) as session:
        workspace = session.get(WorkspaceRecord, context.workspace_id)
        if workspace is None:
            session.add(
                WorkspaceRecord(
                    id=context.workspace_id,
                    name="Default Workspace",
                    default_currency=config.DEFAULT_CURRENCY,
                    authorization_epoch=context.authorization_epoch,
                )
            )
            session.flush()
        existing = session.scalar(
            select(ImportBatchRecord).where(
                ImportBatchRecord.workspace_id == context.workspace_id,
                ImportBatchRecord.source_hash == report.source_hash,
            )
        )
        if existing and existing.status == "completed":
            return existing.id, dict(existing.report_json)
        if existing:
            existing.status = "running"
            existing.report_json = report.as_dict()
            return existing.id, None
        batch = ImportBatchRecord(
            id=new_id("imp"),
            workspace_id=context.workspace_id,
            source_root=report.source_root,
            source_hash=report.source_hash,
            status="running",
            report_json=report.as_dict(),
        )
        session.add(batch)
        session.flush()
        return batch.id, None


def _finish_batch(context: TenantContext, batch_id: str, report: ImportReport) -> None:
    with tenant_session(context) as session:
        batch = session.get(ImportBatchRecord, batch_id)
        if not batch or batch.workspace_id != context.workspace_id:
            raise RuntimeError("导入批次记录丢失")
        batch.status = "completed" if not report.errors else "failed"
        batch.report_json = report.as_dict()
        batch.completed_at = utcnow()
        append_event(
            session,
            event_type="data.json_import.completed" if not report.errors else "data.json_import.failed",
            resource_type="import_batch",
            resource_id=batch_id,
            details={"source_hash": report.source_hash, "counts": report.as_dict()["counts"]},
        )


def _plan_paths(product_path: Path) -> list[Path]:
    paths = sorted((product_path / "plans").glob("*.json")) if (product_path / "plans").exists() else []
    legacy = product_path / "plan.json"
    if legacy.exists():
        paths.append(legacy)
    return paths


def _recovery_plan(product_id: str, run: Run) -> Plan:
    combos: dict[str, Combo] = {}
    for asset in run.assets:
        if asset.combo_id in combos:
            continue
        stable_values = {
            f"legacy-dim-{index}": f"legacy-value-{hashlib.sha256(f'{key}:{value}'.encode()).hexdigest()[:16]}"
            for index, (key, value) in enumerate(sorted(asset.tags.items()))
        }
        combos[asset.combo_id] = Combo(
            id=asset.combo_id,
            code=asset.combo_code,
            values=stable_values or {"legacy": asset.combo_id},
            tags=asset.tags,
        )
    return Plan(
        id=run.plan_id,
        product_id=product_id,
        mode="legacy_recovered",
        cap=len(combos),
        total_full=len(combos),
        combos=list(combos.values()),
        created_at=run.created_at,
    )


def import_json_tree(context: TenantContext, source_root: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    report = ImportReport(source_root=str(source_root), source_hash=source_hash(source_root))
    batch_id, previous = _ensure_import_batch(context, report)
    if previous is not None:
        previous = dict(previous)
        previous["idempotent_replay"] = True
        return previous

    try:
        products_root = source_root / "products"
        if not products_root.exists():
            report.warnings.append("源目录没有 products/；完成空导入")
            _finish_batch(context, batch_id, report)
            return report.as_dict()

        for product_path in sorted(path for path in products_root.iterdir() if path.is_dir()):
            product_file = product_path / "product.json"
            if not product_file.exists():
                report.skipped += 1
                report.warnings.append(f"跳过缺少 product.json 的目录: {product_path.name}")
                continue
            try:
                product = Product(**_read_json(product_file))
                store.save_product(context, product)
                report.products += 1
            except Exception as exc:
                report.errors.append(f"产品导入失败 {product_path.name}: {exc}")
                continue

            angles_file = product_path / "angles.json"
            if angles_file.exists():
                try:
                    store.save_angles(context, AngleSchema(**_read_json(angles_file)))
                    report.angle_versions += 1
                except Exception as exc:
                    report.errors.append(f"角度导入失败 {product.id}: {exc}")

            known_plan_ids: set[str] = set()
            for plan_file in _plan_paths(product_path):
                try:
                    plan = Plan(**_read_json(plan_file))
                    store.save_plan(context, plan)
                    known_plan_ids.add(plan.id)
                    report.plans += 1
                except Exception as exc:
                    report.errors.append(f"Plan 导入失败 {plan_file}: {exc}")

            run_files = sorted((product_path / "runs").glob("*/run.json")) if (product_path / "runs").exists() else []
            imported_assets: set[str] = set()
            for run_file in run_files:
                try:
                    run = Run(**_read_json(run_file))
                    if run.plan_id not in known_plan_ids:
                        recovery = _recovery_plan(product.id, run)
                        store.save_plan(context, recovery)
                        known_plan_ids.add(recovery.id)
                        report.plans += 1
                        report.warnings.append(
                            f"Run {run.id} 引用缺失 Plan {run.plan_id}；已从素材标签创建恢复快照"
                        )
                    store.save_run(context, run)
                    report.runs += 1
                    report.assets += len(run.assets)
                    imported_assets.update(asset.id for asset in run.assets)
                    for asset in run.assets:
                        if asset.status != "ok":
                            continue
                        image_path = run_file.parent / asset.filename
                        if image_path.is_file():
                            store.complete_asset(context, asset.id, image_path)
                            report.asset_versions += 1
                        else:
                            report.warnings.append(f"成功素材缺少文件: {image_path}")
                except Exception as exc:
                    report.errors.append(f"Run 导入失败 {run_file}: {exc}")

            quality_file = product_path / "quality.json"
            ratings_file = product_path / "ratings.json"
            quality_data: dict[str, str] = {}
            if quality_file.exists():
                quality_data = _read_json(quality_file)
            elif ratings_file.exists():
                rating_map = {"1": "approved", "2": "needs_changes", "-1": "rejected"}
                quality_data = {
                    asset_id: rating_map[str(value)]
                    for asset_id, value in _read_json(ratings_file).items()
                    if str(value) in rating_map
                }
            current_quality = store.get_quality(context, product.id)
            for asset_id, quality in quality_data.items():
                if asset_id not in imported_assets:
                    report.warnings.append(f"质量记录引用未知素材: {asset_id}")
                    continue
                if current_quality.get(asset_id) != quality:
                    store.set_quality(context, product.id, asset_id, quality)
                    report.quality_reviews += 1

            verdict_file = product_path / "verdicts.json"
            if verdict_file.exists():
                current_verdicts = store.get_verdicts(context, product.id)
                for asset_id, value in _read_json(verdict_file).items():
                    if asset_id not in imported_assets:
                        report.warnings.append(f"判定记录引用未知素材: {asset_id}")
                        continue
                    if not isinstance(value, dict) or not value.get("verdict"):
                        continue
                    previous_value = current_verdicts.get(asset_id) or {}
                    if (
                        previous_value.get("verdict") != value.get("verdict")
                        or previous_value.get("context", {}) != value.get("context", {})
                    ):
                        store.set_verdict(
                            context,
                            product.id,
                            asset_id,
                            value.get("verdict"),
                            value.get("context") or {},
                        )
                        report.verdict_decisions += 1
    except Exception as exc:
        report.errors.append(f"导入中止: {exc}")

    _finish_batch(context, batch_id, report)
    return report.as_dict()
