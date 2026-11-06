"""Cutover backfill dry-run tooling (Issue #21 Task 7).

Pre-Task-1 data (GenerationRuns without a RecipeSnapshot, Assets without a
producing RunItem, QualityReviews scoped to an Asset rather than an exact
AssetVersion) predates this slice's schema. This module is a read-only
audit: it reports what a real backfill migration would need to touch,
without writing anything, so an operator can size the work before it is
attempted. Actually rewriting historical rows is deliberately out of this
task's scope -- report-only, matching the "Persist immutable versions and
append-only evidence for historical decisions" invariant (a backfill that
mutates history would violate it).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    QualityReviewRecord,
    new_id,
    utcnow,
)
from app.generation.models import (
    GenerationRecipeSnapshotRecord,
    RunItemRecord,
    RunItemAssetVersionBindingRecord,
)


def backfill_dry_run(session: Session, workspace_id: str) -> dict:
    """Audit one Workspace's pre-Task-1 Generation data, without mutating it.

    Returns counts of the three gaps a real backfill migration would need to
    close:

    - ``runs_needing_snapshot`` -- GenerationRuns with no
      ``recipe_snapshot_id`` (created before Task 2's Atomic Submission).
    - ``assets_needing_run_item`` -- Assets belonging to those Runs, which
      therefore have no ``RunItemAssetVersionBindingRecord`` provenance back
      to a RunItem.
    - ``reviews_needing_version_scope`` -- QualityReviews recorded before
      Task 4 added the exact-version ``asset_version_id`` anchor.
    """
    runs = session.execute(
        select(GenerationRunRecord).where(
            GenerationRunRecord.workspace_id == workspace_id,
            GenerationRunRecord.recipe_snapshot_id.is_(None),
        )
    ).scalars().all()

    report = {
        "runs_needing_snapshot": len(runs),
        "assets_needing_run_item": 0,
        "reviews_needing_version_scope": 0,
        "errors": [],
    }

    for run in runs:
        assets = session.execute(
            select(AssetRecord).where(
                AssetRecord.run_id == run.id,
                AssetRecord.workspace_id == workspace_id,
            )
        ).scalars().all()
        report["assets_needing_run_item"] += len(assets)

    reviews = session.execute(
        select(QualityReviewRecord).where(
            QualityReviewRecord.workspace_id == workspace_id,
            QualityReviewRecord.asset_version_id.is_(None),
        )
    ).scalars().all()
    report["reviews_needing_version_scope"] = len(reviews)

    return report
