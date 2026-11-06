"""Content-addressed GenerationRecipeSnapshot reuse (Issue #21 Task 2).

A recipe snapshot is the frozen, reusable "how to generate" payload
(provider, model, combination strategy, format, exclusions, resolved
adapter/cost/scenario evidence) that a confirmed Preview carries into Atomic
Submission. Two submissions whose recipe canonicalizes identically share one
row instead of duplicating it -- ``(workspace_id, canonicalization_version,
recipe_hash)`` is unique, so a second insert with the same content is a
no-op read.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id, utcnow
from app.generation.models import GenerationRecipeSnapshotRecord
from app.platform.tenancy import TenantContext


def insert_or_reuse_snapshot(
    session: Session,
    ctx: TenantContext,
    *,
    canonical_json: dict,
    execution_mode: str,
    canonicalization_version: str = "v1",
) -> GenerationRecipeSnapshotRecord:
    recipe_hash = _canonical_hash(canonical_json)

    existing = session.execute(
        select(GenerationRecipeSnapshotRecord).where(
            GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id,
            GenerationRecipeSnapshotRecord.canonicalization_version == canonicalization_version,
            GenerationRecipeSnapshotRecord.recipe_hash == recipe_hash,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    snapshot = GenerationRecipeSnapshotRecord(
        id=new_id("rs"),
        workspace_id=ctx.workspace_id,
        canonicalization_version=canonicalization_version,
        recipe_hash=recipe_hash,
        canonical_json=canonical_json,
        execution_mode=execution_mode,
        created_at=utcnow(),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _canonical_hash(value: object) -> str:
    from ..platform.canonical import canonical_hash
    return canonical_hash(value)
